"""恒定权益仓位 × 持仓只数 —— 逐笔真实费用口径复测(2026-09-10)

背景: ab_const_n.py 的 18 次对照跑在**旧扁平成本(单边 0.3%)**下(进程早于费用模型
改造启动), 而当天下午成本口径已换成逐笔真实费用(config `fees`: 佣金 max(额×万2.5,
最低 5 元)+ 卖出印花税 + 过户费)。2 万/4~6 只每笔约 3000~4000 元 → 真实单边约
0.13~0.19%, **系统性低于 0.3%**; 且只数越多单笔越小、费率越高 —— 即费用口径变化
对"固定 6 只"臂的惩罚重于对"6/5/4"臂。故排序必须在新口径下复测, 不能沿用旧结论。

臂(与 ab_const_n.py 同名同义, 只改用真实费用引擎):
  agg_state  生产对照(regime 条件映射 1.0/0.9/0.8 + state_stocks 6/5/4)
  c873_fix6  恒定位 0.873 + 固定 6 只  ← 旧口径下两轴最优
  c70_fix6   恒定位 0.70  + 固定 6 只  ← 旧口径下低回撤端点
窗口: full / h1(2017-2021) / h2(2022-2026) 三段, 按项目纪律不选同段最优。

跑完自清本次 run(用 run_backtest 返回的 run_date, 不用手拼日期前缀)。生产基线
`20260910_fees0910` 是同口径同 config 的 full 窗口单跑, 已 pin 在看板, 不在本脚本内。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app import config as cfg_mod
from app import db
from app.logger import setup_logger
from app.backtest import engine as E

setup_logger()
db.init_db()

_orig = cfg_mod.load_config
TAGS = []


def _patched(w):
    def f(*a, **k):
        cfg = _orig(*a, **k)
        cfg["regime_equity_weight"] = {"scan": {"attack": w, "neutral": w, "defense": w}}
        cfg["risk_profile"] = "scan"
        return cfg
    return f


def _sys_sharpe(run_date: str):
    conn = db.get_conn()
    df = pd.read_sql("SELECT trade_date, sys_nav FROM backtest_nav_daily "
                     "WHERE run_date=? ORDER BY trade_date", conn, params=(run_date,))
    conn.close()
    if len(df) < 30:
        return float("nan")
    r = df["sys_nav"].astype(float).pct_change().dropna()
    if r.std() == 0:
        return float("nan")
    return float(r.mean() / r.std(ddof=1) * np.sqrt(252))


ARMS = [
    ("agg_state", None, None),
    ("c873_fix6", 0.873, 6),
    ("c70_fix6", 0.70, 6),
]

WINDOWS = [("full", None, None), ("h1", "20170101", "20211231"),
           ("h2", "20220101", "20260831")]

out = []
t0 = time.time()
try:
    for tag, w, nfix in ARMS:
        for wname, start, end in WINDOWS:
            run_tag = "cnf_{}_{}".format(tag, wname)
            cfg_mod.load_config = _orig if w is None else _patched(w)
            m = E.run_backtest(top_n=nfix, run_tag=run_tag, start=start, end=end)
            cfg_mod.load_config = _orig
            rd = m["run_date"]
            TAGS.append(rd)
            row = {
                "arm": tag, "win": wname, "run_date": rd,
                "capital": m.get("capital"),
                "full_ann": m["ann_return"], "full_mdd": m["max_drawdown"],
                "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
                "sys_mdd_daily": m["sys_mdd_daily"], "sys_sharpe": _sys_sharpe(rd),
                "idle": m["avg_cash_idle"], "n_avg": m["avg_stocks"],
                "periods": m["periods"],
            }
            out.append(row)
            print("[{:>4.0f}s] {}".format(time.time() - t0, json.dumps(row, ensure_ascii=False)),
                  flush=True)
finally:
    cfg_mod.load_config = _orig

print()
for wname, _s, _e in WINDOWS:
    rows = [r for r in out if r["win"] == wname]
    if not rows:
        continue
    print("=" * 100)
    print("窗口 {} (capital={}, {} 期)".format(wname, rows[0]["capital"], rows[0]["periods"]))
    print("=" * 100)
    print("{:>11} {:>9} {:>9} {:>9} {:>9} {:>8} {:>7} {:>6}".format(
        "臂", "满仓年化", "满仓回撤", "系统年化", "系统回撤", "日频回撤", "夏普", "闲置"))
    for r in rows:
        print("{:>11} {:>+8.2%} {:>9.2%} {:>+8.2%} {:>9.2%} {:>8.2%} {:>7.2f} {:>6.1%}".format(
            r["arm"], r["full_ann"], r["full_mdd"], r["sys_ann"],
            r["sys_mdd"], r["sys_mdd_daily"], r["sys_sharpe"], r["idle"]))
    print()

conn = db.get_conn()
for t in TAGS:
    for tbl in ("backtest_summary", "backtest_nav", "backtest_nav_daily"):
        conn.execute("DELETE FROM {} WHERE run_date=?".format(tbl), (t,))
conn.commit()
conn.close()
print("已清理本次 run ({} 条): {}".format(len(TAGS), TAGS))
