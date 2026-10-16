"""恒定权益仓位 × 持仓只数策略(2026-09-10)

承接 0909 深夜权益暴露前沿扫描(ab_equity_const.py):
  · 现行 regime 条件映射(aggressive 1.0/0.9/0.8, 平均仓位≈0.873)被同平均仓位的
    恒定仓位完全支配(+7.30%/-50.98% vs +6.38%/-52.45%)。
  · 0909 记录里未跑的一项: **恒定仓位下 state_stocks(6/5/4) 是否仍值得保留**。

本脚本一次只动一个变量, 并按项目纪律带 walk-forward 分段(全窗口 + 前后两段):
  A. 恒定 vs 条件    —— c873_state(同平均仓位) vs agg_state(生产对照)
  B. 只数策略        —— state(6/5/4) vs 固定 6, 在恒定仓位下对照
  C. 风险端点        —— c50_state 供"回撤收回来"的一端参考

跑完自清 run 行: 用 run_backtest 返回的 metrics["run_date"] 清理, 不再手拼日期
前缀(ab_equity_const.py 的教训: 跨午夜后实际 run_date 是次日, 手拼 20260909_ 前缀
的清理全部落空, 残留 ewconst_0p873/0p900 两条)。
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


# (标签, 恒定仓位 or None=用生产条件映射, 固定只数 or None=用 state_stocks)
ARMS = [
    ("agg_state", None,   None),   # 生产对照(条件映射 + 6/5/4)
    ("c873_state", 0.873, None),   # 同平均仓位的恒定仓位
    ("c873_fix6", 0.873, 6),       # 恒定位 + 固定 6 只 → 测 state_stocks 增量
    ("c70_state", 0.70,  None),    # 候选点
    ("c70_fix6", 0.70,  6),        # 候选点 + 固定 6 只
    ("c50_state", 0.50,  None),    # 低风险端点
]

WINDOWS = [("full", None, None), ("h1", "20170101", "20211231"), ("h2", "20220101", "20260831")]

out = []
t0 = time.time()
try:
    for tag, w, nfix in ARMS:
        for wname, start, end in WINDOWS:
            run_tag = "cn_{}_{}".format(tag, wname)
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
