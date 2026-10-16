"""分配器拒绝原因诊断(2026-09-10 深夜, 承接 diag_exposure.py)

发现: 2 万本金 + `slot = equity_cash/max_stocks` 下每槽只有 ~2910 元, 只够 1 手 ≤29 元
的股票; 买不起的**该槽预算永久闲置**(候选池只有 top-6, 不补位)。实测系统线真实暴露
只有 56.5%(名义 87.3%), 且在 2018 崩盘途中从 80% 塌到 24%。

本脚本回答三个问题:
  1. top-6 里每月有几只**买得起**? 被挡掉的是「买不起」还是「顶破行业上限」?
  2. 被挡掉的票有多贵? 隐含的价格上限是多少?
  3. 若把每个槽的预算合并重分配(或允许超出 slot), 能挽回多少暴露?

做法同 diag_exposure: monkeypatch 记录**候选池原样**(含 price/industry/score), 离线
重放分配逻辑做归因, 不碰引擎源码; 临时 run 自清。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app import config as cfg_mod
from app import db
from app.backtest import engine as E
from app.logger import setup_logger
from app.portfolio.board import lot_shares

setup_logger()
db.init_db()

TAG = "diag_rej"
CALLS = []
_orig_alloc = E.allocate_lots


def _rec(cand, budget, max_stocks, **kw):
    out = _orig_alloc(cand, budget, max_stocks, **kw)
    CALLS.append({"budget": float(budget), "n_slot": int(max_stocks),
                  "cost": float(out["cost"]), "codes": list(out["shares"].keys()),
                  "rows": cand[["close"] + (["industry"] if "industry" in cand.columns else [])]
                  .copy() if cand is not None and not cand.empty else None,
                  "ind_cap": kw.get("ind_cap", 1.0), "cost_rate": kw.get("cost_rate", 0.0),
                  "floor_stocks": kw.get("floor_stocks", 2.0)})
    return out


E.allocate_lots = _rec
t0 = time.time()
try:
    m = E.run_backtest(run_tag=TAG)
finally:
    E.allocate_lots = _orig_alloc
run_date = m["run_date"]
print("[{:.0f}s] 回测完成 sys年化={:.4f} sys回撤={:.4f}"
      .format(time.time() - t0, m["sys_ann_return"], m["sys_max_drawdown"]), flush=True)

conn = db.get_conn()
nav = pd.read_sql("SELECT trade_date FROM backtest_nav WHERE run_date=? ORDER BY trade_date",
                  conn, params=(run_date,))
conn.close()
n_m = len(nav)
sysc = CALLS[1::2]
print("期数 {}  系统线调用 {} (应相等)".format(n_m, len(sysc)))
assert len(sysc) == n_m

capital = float(cfg_mod.load_config().get("capital", 20000))

# ---------- 离线重放: 逐槽分类 ----------
rec = []
for d, c in zip(nav["trade_date"], sysc):
    if c["rows"] is None or c["rows"].empty:
        rec.append({"d": d, "n": 0, "afford": 0, "too_expensive": 0, "ind_block": 0,
                    "cap": np.nan, "cost": 0.0, "budget": c["budget"]})
        continue
    rows = c["rows"]
    equity = c["budget"]
    n_slot = c["n_slot"]
    slot = equity / n_slot
    eff_cap = max(c["ind_cap"], c["floor_stocks"] / n_slot) if c["ind_cap"] < 1.0 else 1.0
    cash, ind_w = equity, {}
    afford = too_exp = ind_block = 0
    for code, r in rows.iterrows():
        if len([1]) and False:
            pass
        price = float(r["close"])
        if price <= 0:
            continue
        budget = min(slot, cash)
        affordable = int(np.floor(budget / (price * (1 + c["cost_rate"])) + 1e-9))
        sh = lot_shares(code, affordable)
        if sh <= 0:
            too_exp += 1
            continue
        if c["ind_cap"] < 1.0 and "industry" in rows.columns:
            ind = r["industry"] if pd.notna(r["industry"]) else "未分类"
            if ind_w.get(ind, 0.0) + sh * price / equity > eff_cap + 1e-9:
                ind_block += 1
                continue
            ind_w[ind] = ind_w.get(ind, 0.0) + sh * price / equity
        afford += 1
        cash -= sh * price
    prices = rows["close"].astype(float)
    prices = prices[prices > 0]
    rec.append({"d": d, "n": int(afford), "afford": int(afford), "too_expensive": int(too_exp),
                "ind_block": int(ind_block), "cap": float(prices.min()) if len(prices) else np.nan,
                "cost": c["cost"], "budget": equity,
                "slot": slot,
                "min_price": float(prices.min()) if len(prices) else np.nan,
                "max_price": float(prices.max()) if len(prices) else np.nan})

r = pd.DataFrame(rec)
r["expo"] = r["cost"] / capital
print("\n=== 系统线槽位成败(离线重放, 应与插桩的 n_eff 一致) ===")
print("平均候选 {} 只, 成交 {:.2f} 只, 买不起 {:.2f} 只, 行业挡 {:.2f} 只"
      .format((r["afford"] + r["too_expensive"] + r["ind_block"]).mean(),
              r["afford"].mean(), r["too_expensive"].mean(), r["ind_block"].mean()))
print("含「买不起」的月份占比: {:.0%}   含「行业挡」的月份占比: {:.0%}"
      .format((r["too_expensive"] > 0).mean(), (r["ind_block"] > 0).mean()))
print("平均槽预算 {:.0f} 元 → 隐含价格上限 {:.1f} 元".format(r["slot"].mean(), r["slot"].mean()))
print("候选池价格: 最低 {:.1f} 中位 {:.1f} 最高 {:.1f}".format(
    r["min_price"].mean(), r[["min_price", "max_price"]].mean().mean(), r["max_price"].mean()))

print("\n=== 未买到手的槽 = 白扔的钱 ===")
r["lost"] = (6 - r["afford"]).clip(lower=0) * r["slot"]
r["lost_pct_of_cap"] = r["lost"] / capital
print("平均每月 {} 个槽空转, 相当于资本的 {:.1%} 被机械性闲置"
      .format((6 - r["afford"]).clip(lower=0).mean(), r["lost_pct_of_cap"].mean()))
print("按年:")
r["year"] = r["d"].str[:4]
g = r.groupby("year").agg(候选=("afford", "size"), 成交只数=("afford", "mean"),
                          买不起=("too_expensive", "mean"), 行业挡=("ind_block", "mean"),
                          暴露=("expo", "mean"), 槽预算=("slot", "mean"),
                          虚耗占资本=("lost_pct_of_cap", "mean"))
print(g.to_string(formatters={"成交只数": "{:.2f}".format, "买不起": "{:.2f}".format,
                              "行业挡": "{:.2f}".format, "暴露": "{:.1%}".format,
                              "槽预算": "{:.0f}".format, "虚耗占资本": "{:.1%}".format}))

conn = db.get_conn()
for t in ("backtest_nav", "backtest_nav_daily"):
    conn.execute("DELETE FROM {} WHERE run_date=?".format(t), (run_date,))
conn.commit()
conn.close()
print("\n已清理临时 run {}".format(run_date))
