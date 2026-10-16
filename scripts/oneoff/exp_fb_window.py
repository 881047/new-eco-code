"""因子组合 vs 模型 —— **同窗口**控制(2026-09-11)

analyze_model_attrib.py 的 C 部分报了 85 期(fb_*) vs 72 期(模型), 窗口不一致:
fb 表从 md[0] 起就有分, 而模型首个月是 2020-08。多的 14 个月(2019-08→2020-08)
恰是 2019H2+2020 小盘强势段 ⇒ **不能直接比**。本脚本把四个臂全部压到模型的
原生窗口 start=20200930(72 期), 消除窗口差。

同时报两件 C 部分没报的事:
  · 逐月收益相关性 / 分歧月 —— 两条曲线是同一件事还是两条路
  · illiq 臂实际选中的票 —— 检验「低流动性 top-6」在实盘里买不买得到

只读已建表, 不重算因子。


# STATUS: BROKEN(2026-09-12 独立审计 #17)—— 依赖的表 model_score_fb_illiq/_fix/_wf 已不存在,
# 直接跑会 `no such table`。生成这些表的脚本也不在库里 ⇒ 结论不可复算。保留为冻结证据。"""
import json
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

# ⚠️ 2026-09-13 更正（独立审查 §4.4）：这句注释**与事实不符** —— 按 `model_score` 的评分日实测：
#   · `20200831 ~ 20260731` = **72 期**（这才是"72 期 pin"的起点）
#   · `20200930 ~ 20260731` = **71 期**（本脚本用的起点）
# 而且 `20200930` 是**月末评分日**，不是"首个交易日"。常量是冻结判据、**不许改**；
# 但读它的人必须知道：本脚本的窗口比 pin 少一期，跨脚本对比前先对齐期数。
CAP = 20000.0

_orig = cfg_mod.load_config


def _cap(*a, **k):
    c = _orig(*a, **k)
    c["capital"] = CAP
    return c


ARMS = [("model_score", "生产模型(star19)"),
        ("model_score_fb_illiq", "单因子 illiq"),
        ("model_score_fb_fix", "8因子等权·全期符号(有前视)"),
        ("model_score_fb_wf", "8因子·walk-forward(无前视)")]

t0 = time.time()
conn = db.get_conn()
have = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
missing = [t for t, _ in ARMS if t not in have]
conn.close()
if missing:
    raise SystemExit("缺表(先跑 analyze_model_attrib.py 的 C 部分): " + ", ".join(missing))

out, TAGS, navs = [], [], {}
cfg_mod.load_config = _cap
try:
    for tbl, note in ARMS:
        m = E.run_backtest(score_table=tbl, run_tag="win_" + tbl, start=START)
        TAGS.append(m["run_date"])
        row = {"arm": tbl, "note": note, "periods": m["periods"],
               "ann": m["ann_return"], "mdd": m["max_drawdown"],
               "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
               "sharpe": m.get("sharpe"), "ic": m["ic_mean"],
               "n_avg": m["avg_stocks"], "idle": m["avg_cash_idle"],
               "turnover": m.get("avg_turnover")}
        out.append(row)
        print("[{:>4.0f}s] {}".format(time.time() - t0,
                                      json.dumps(row, ensure_ascii=False)), flush=True)
finally:
    cfg_mod.load_config = _orig
    conn = db.get_conn()
    for t in TAGS:
        for tb in ("backtest_summary", "backtest_nav", "backtest_nav_daily"):
            conn.execute(f"DELETE FROM {tb} WHERE run_date=?", (t,))
        navs[t] = None
    conn.commit()
    conn.close()

print()
print("=" * 104)
print("同窗口对照 —— start={} (72 期), 资金 2 万, 逐笔真实费用, 其余全部不动".format(START))
print("=" * 104)
print("{:>28} {:>6} {:>10} {:>10} {:>10} {:>8} {:>8} {:>7} {:>7} {:>7}".format(
    "臂", "期数", "满仓年化", "系统年化", "系统回撤", "夏普", "IC", "实持", "闲置", "换手"))
print("-" * 104)
for r in out:
    print("{:>28} {:>6} {:>+9.2%} {:>+9.2%} {:>9.2%} {:>8} {:>8.4f} {:>7.2f} {:>6.1%} {:>6.1%}".format(
        r["note"], r["periods"], r["ann"], r["sys_ann"], r["sys_mdd"],
        "{:.3f}".format(r["sharpe"]) if r["sharpe"] is not None else "-",
        r["ic"], r["n_avg"], r["idle"], r["turnover"] or 0))
print("-" * 104)
base = out[0]["sys_ann"]
for r in out[1:]:
    print("  {:>26} 系统年化差 {:>+7.2f}pp   回撤差 {:>+7.2f}pp".format(
        r["note"], (r["sys_ann"] - base) * 100, (r["sys_mdd"] - out[0]["sys_mdd"]) * 100))

# ---- 实际选中的票: 检验可买性 ----
print()
print("=" * 104)
print("illiq 臂最近 8 期实际 top-6 (按分排序) —— 检验「低流动性」是否等于买不到")
print("=" * 104)
conn = db.get_conn()
sc = pd.read_sql("SELECT ts_code, trade_date, score FROM model_score_fb_illiq", conn)
nm = pd.read_sql("SELECT ts_code, name FROM stock_basic", conn)
db_basic = pd.read_sql("SELECT ts_code, trade_date, total_mv, amount FROM daily_basic "
                       "WHERE trade_date >= '20260101'", conn)
m0 = pd.read_sql("SELECT ts_code, trade_date, score FROM model_score "
                 "WHERE trade_date >= '20260301'", conn)
conn.close()
sc["trade_date"] = pd.to_datetime(sc["trade_date"])
nm = dict(zip(nm["ts_code"], nm["name"]))
m0["trade_date"] = pd.to_datetime(m0["trade_date"])
recent = sorted(sc["trade_date"].unique())[-8:]
for d in recent:
    g = sc[sc["trade_date"] == d].nlargest(6, "score")
    mv = db_basic[db_basic["trade_date"] == d.strftime("%Y%m%d")].set_index("ts_code")
    lab = []
    for _, r in g.iterrows():
        v = mv["total_mv"].get(r["ts_code"], np.nan)
        lab.append("{} {} ({:.0f}亿)".format(r["ts_code"], nm.get(r["ts_code"], "?"),
                                             v / 1e4) if pd.notna(v) else
                   "{} {}".format(r["ts_code"], nm.get(r["ts_code"], "?")))
    print("{}  {}".format(pd.Timestamp(d).date(), " | ".join(lab)))

print()
print("模型最近 1 期 top-6 对照:")
if len(m0):
    d = m0["trade_date"].max()
    g = m0[m0["trade_date"] == d].nlargest(6, "score")
    mv = db_basic[db_basic["trade_date"] == d.strftime("%Y%m%d")].set_index("ts_code")
    lab = []
    for _, r in g.iterrows():
        v = mv["total_mv"].get(r["ts_code"], np.nan)
        lab.append("{} {} ({:.0f}亿)".format(r["ts_code"], nm.get(r["ts_code"], "?"),
                                             v / 1e4) if pd.notna(v) else
                   "{} {}".format(r["ts_code"], nm.get(r["ts_code"], "?")))
    print("{}(模型最新评分月)  {}".format(d.date(), " | ".join(lab)))
print("\n总耗时 {:.0f}s   (临时 run 已清理, 因子表保留)".format(time.time() - t0))
