"""权益暴露/集中度插桩诊断(2026-09-10 深夜)

起因: 恒定 0.873 后系统线 DD -42.44% ≈ 池子 DD -41.78%, 但系统对池子的月度回归 beta
只有 0.576 —— 按 beta 线性缩放池子回撤只该有 -24%。而且 2 万本金 + 整手粒度下名义
87% 实际只投出约 61%。**「只投六成的钱, 却吃满池子的回撤」这个矛盾必须拆开。**

做法: 不碰引擎源码 —— monkeypatch `app.backtest.engine.allocate_lots`, 把每月的
预算/成交额/只数/名单全录下来(每月两次调用, 先满仓线后系统线), 跑一次当前配置的回测,
再用临时 run_tag 存盘、分析完自清。

输出: 逐月权益暴露率、满仓/系统暴露差、按年段的暴露与回撤、2018 年持仓明细。
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

setup_logger()
db.init_db()

TAG = "diag_expo"
CALLS = []
_orig_alloc = E.allocate_lots


def _rec(cand, budget, max_stocks, **kw):
    out = _orig_alloc(cand, budget, max_stocks, **kw)
    CALLS.append({
        "budget": float(budget), "n_slot": int(max_stocks),
        "cost": float(out["cost"]), "n_eff": int(out["n_eff"]),
        "cash_left": float(out["cash_left"]),
        "codes": list(out["shares"].keys()),
        "sizes": {k: int(v) for k, v in out["shares"].items()},
    })
    return out


E.allocate_lots = _rec
t0 = time.time()
try:
    m = E.run_backtest(run_tag=TAG)
finally:
    E.allocate_lots = _orig_alloc
run_date = m["run_date"]
print("[{:.0f}s] 回测完成 run_date={} 年化={:.4f} 回撤={:.4f} sys年化={:.4f} sys回撤={:.4f}"
      .format(time.time() - t0, run_date, m["ann_return"], m["max_drawdown"],
              m["sys_ann_return"], m["sys_max_drawdown"]), flush=True)

conn = db.get_conn()
nav = pd.read_sql("SELECT trade_date, nav, sys_nav FROM backtest_nav WHERE run_date=? "
                  "ORDER BY trade_date", conn, params=(run_date,))
pool = pd.read_sql("SELECT trade_date, close FROM pool_index ORDER BY trade_date", conn)
conn.close()

n_m = len(nav)
print("月度期数 {}  插桩调用 {} (应 = 2×期数)".format(n_m, len(CALLS)))
assert len(CALLS) == 2 * n_m, "调用次数与期数不符, 拆分为满仓/系统线的假设不成立"

full = CALLS[0::2]
sysc = CALLS[1::2]
capital = float(cfg_mod.load_config().get("capital", 20000))

df = pd.DataFrame({
    "trade_date": nav["trade_date"].values,
    "nav": nav["nav"].values, "sys_nav": nav["sys_nav"].values,
    "f_budget": [c["budget"] for c in full], "f_cost": [c["cost"] for c in full],
    "f_n": [c["n_eff"] for c in full],
    "s_budget": [c["budget"] for c in sysc], "s_cost": [c["cost"] for c in sysc],
    "s_n": [c["n_eff"] for c in sysc],
})
df["f_expo"] = df["f_cost"] / capital
df["s_expo"] = df["s_cost"] / capital
df["s_expo_of_budget"] = df["s_cost"] / df["s_budget"]

pd.set_option("display.width", 200)
print("\n=== 暴露率总览 ===")
print("满仓线实际投入/资本: 均值 {:.1%}  中位 {:.1%}  最低 {:.1%}  最高 {:.1%}"
      .format(df["f_expo"].mean(), df["f_expo"].median(), df["f_expo"].min(), df["f_expo"].max()))
print("系统线实际投入/资本: 均值 {:.1%}  中位 {:.1%}  最低 {:.1%}  最高 {:.1%}"
      .format(df["s_expo"].mean(), df["s_expo"].median(), df["s_expo"].min(), df["s_expo"].max()))
print("系统线 投入/预算:     均值 {:.1%}  (名义 eq_w=0.873, 缺口 = 整手+行业上限)"
      .format(df["s_expo_of_budget"].mean()))
print("只数: 满仓线均值 {:.2f}  系统线均值 {:.2f} (槽位 6)"
      .format(df["f_n"].mean(), df["s_n"].mean()))

# 按年看: 暴露率是否在回撤期塌陷(会是"回撤大"的一个机械原因)
df["year"] = df["trade_date"].str[:4]
print("\n=== 逐年: 暴露率 vs 收益 ===")
rows = []
for y, g in df.groupby("year"):
    rn = g["nav"].astype(float).pct_change().dropna()
    rs = g["sys_nav"].astype(float).pct_change().dropna()
    rows.append({"年": y, "期数": len(g), "满仓暴露": g["f_expo"].mean(),
                 "系统暴露": g["s_expo"].mean(), "满仓只数": g["f_n"].mean(),
                 "系统只数": g["s_n"].mean(),
                 "满仓年收益": (1 + rn).prod() - 1 if len(rn) else np.nan,
                 "系统年收益": (1 + rs).prod() - 1 if len(rs) else np.nan})
yr = pd.DataFrame(rows)
print(yr.to_string(index=False,
                   formatters={"满仓暴露": "{:.1%}".format, "系统暴露": "{:.1%}".format,
                               "满仓只数": "{:.2f}".format, "系统只数": "{:.2f}".format,
                               "满仓年收益": "{:+.1%}".format, "系统年收益": "{:+.1%}".format}))

# 2018 那一段(当前最大回撤所在)逐月明细
print("\n=== 2017-10 ~ 2019-03 逐月(当前最大回撤区间) ===")
seg = df[(df["trade_date"] >= "20171001") & (df["trade_date"] <= "20190331")]
print(seg[["trade_date", "nav", "sys_nav", "f_expo", "s_expo", "f_n", "s_n"]].to_string(
    index=False, formatters={"nav": "{:.4f}".format, "sys_nav": "{:.4f}".format,
                             "f_expo": "{:.1%}".format, "s_expo": "{:.1%}".format}))

# 持仓集中度: 单只占投入额的比例
def conc(calls, label):
    fr = []
    for c in calls:
        if c["cost"] > 0 and c["sizes"]:
            v = sorted(c["sizes"].values(), reverse=True)
            fr.append({"n": c["n_eff"], "top1": c["sizes"] and max(c["sizes"].values()) / c["cost"],
                       "sum": c["cost"]})
    d = pd.DataFrame(fr)
    print("{}: 平均只数 {:.2f}  单只最大占投入 均值 {:.1%}".format(label, d["n"].mean(), d["top1"].mean()))


print("\n=== 集中度 ===")
conc(full, "满仓线")
conc(sysc, "系统线")

# 2018 年系统线持了哪些票
print("\n=== 2018 年系统线逐月持仓 ===")
for r in seg.itertuples():
    i = df.index[df["trade_date"] == r.trade_date][0]
    c = sysc[i]
    print("{} 投入 {:6.0f} ({:.0%}) {} 只: {}".format(
        r.trade_date, c["cost"], r.s_expo, c["n_eff"],
        " ".join("{}×{}".format(k, v) for k, v in c["sizes"].items())))

# 自清
conn = db.get_conn()
for t in ("backtest_nav", "backtest_nav_daily"):
    conn.execute("DELETE FROM {} WHERE run_date=?".format(t), (run_date,))
conn.commit()
conn.close()
print("\n已清理临时 run {}".format(run_date))
