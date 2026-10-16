"""恒定仓位对照(2026-09-09 深夜): regime 择时到底有没有价值?

ab_equity_weight.py 发现 1.0/0.8/0.5 比现行 1.0/0.9/0.8 用 -0.13pp 年化换来 +11.9pp 回撤。
但这可能只是「平均仓位更低」的算术结果, 与 state 条件无关。本脚本跑恒定仓位,
按平均仓位对齐, 检验 state 条件是否真的加了东西:
  · 现行 ew_agg 平均仓位 = .19*1.0+.35*0.9+.46*0.8 ≈ 0.873(2014+ 日频口径)
  · ew_mild      平均仓位 ≈ 0.70
  · ew_bal       平均仓位 ≈ 0.61
若 const 臂在同平均仓位下年化/回撤与对应条件臂持平 → regime 无增量, 应直接用恒定仓位;
若条件臂更好 → state 信号有价值, 值得保留映射结构。
跑完自清 run 行。
"""
import json
import sys
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


WS = [0.50, 0.60, 0.70, 0.80, 0.873, 0.90]

out = []
try:
    for w in WS:
        run_tag = "ewconst_{:.3f}".format(w).replace(".", "p")
        cfg_mod.load_config = _patched(w)
        m = E.run_backtest(run_tag=run_tag)
        cfg_mod.load_config = _orig
        run_date = "20260909_" + run_tag
        TAGS.append(run_date)
        row = {"w": w, "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
               "sys_mdd_daily": m["sys_mdd_daily"], "sys_sharpe": _sys_sharpe(run_date),
               "full_ann": m["ann_return"], "full_mdd": m["max_drawdown"]}
        out.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
finally:
    cfg_mod.load_config = _orig

print()
print("=" * 92)
print("恒定仓位对照(capital={}, 全窗口 2017-01 → 2026-08)".format(_orig().get("capital")))
print("=" * 92)
print("{:>8} {:>10} {:>10} {:>10} {:>10}".format("仓位", "年化", "回撤(月)", "回撤(日)", "夏普"))
for r in out:
    print("{:>8.3f} {:>+9.2%} {:>10.2%} {:>10.2%} {:>10.2f}".format(
        r["w"], r["sys_ann"], r["sys_mdd"], r["sys_mdd_daily"], r["sys_sharpe"]))
print()
print("对照(条件映射): ew_agg 0.873 → +6.38%/-52.45% | ew_mild 0.70 → +6.25%/-40.57% "
      "| ew_bal 0.61 → +5.66%/-33.94%")

conn = db.get_conn()
for t in TAGS:
    for tbl in ("backtest_summary", "backtest_nav", "backtest_nav_daily"):
        conn.execute("DELETE FROM {} WHERE run_date=?".format(tbl), (t,))
conn.commit()
conn.close()
print("\n已清理本次扫描 run: {}".format(TAGS))
