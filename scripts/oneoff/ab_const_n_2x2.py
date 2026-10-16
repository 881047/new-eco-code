"""权益仓位 × 持仓只数 2x2 解耦(2026-09-10, 仅全窗口, 逐笔真实费用)

ab_const_n_fees.py 只跑了 3 个组合臂(agg_state / c873_fix6 / c70_fix6), 无法回答
"若只动一个旋钮够不够"。本脚本补齐 2x2 缺的两格 + 低风险端点:

        只数=state(6/5/4)   只数=固定6
  agg     agg_state ✓已有     agg_fix6 ← 本脚本
  c873    c873_state ←本脚本   c873_fix6 ✓已有
  c50(仅固定6)                c50_fix6 ← 低风险端点(「回撤再收回来」的一端)

跑完自清(按 run_backtest 返回的 run_date)。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

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


ARMS = [("agg_fix6", None, 6), ("c873_state", 0.873, None), ("c50_fix6", 0.50, 6)]

out = []
t0 = time.time()
try:
    for tag, w, nfix in ARMS:
        cfg_mod.load_config = _orig if w is None else _patched(w)
        m = E.run_backtest(top_n=nfix, run_tag="cnf2_{}".format(tag))
        cfg_mod.load_config = _orig
        rd = m["run_date"]
        TAGS.append(rd)
        row = {"arm": tag, "run_date": rd, "full_ann": m["ann_return"],
               "full_mdd": m["max_drawdown"], "sys_ann": m["sys_ann_return"],
               "sys_mdd": m["sys_max_drawdown"], "sys_mdd_daily": m["sys_mdd_daily"],
               "idle": m["avg_cash_idle"], "n_avg": m["avg_stocks"]}
        out.append(row)
        print("[{:>4.0f}s] {}".format(time.time() - t0, json.dumps(row, ensure_ascii=False)),
              flush=True)
finally:
    cfg_mod.load_config = _orig

print()
print("{:>11} {:>9} {:>9} {:>9} {:>9} {:>8} {:>6}".format(
    "臂", "满仓年化", "满仓回撤", "系统年化", "系统回撤", "日频回撤", "闲置"))
print("{:>11} {:>+8.2%} {:>9.2%} {:>+8.2%} {:>9.2%} {:>8.2%} {:>6.1%}".format(
    "agg_state", 0.118, -0.5875, 0.0786, -0.5181, -0.5384, 0.2642))  # 已有, 供同表对照
for r in out:
    print("{:>11} {:>+8.2%} {:>9.2%} {:>+8.2%} {:>9.2%} {:>8.2%} {:>6.1%}".format(
        r["arm"], r["full_ann"], r["full_mdd"], r["sys_ann"], r["sys_mdd"],
        r["sys_mdd_daily"], r["idle"]))
print("{:>11} {:>+8.2%} {:>9.2%} {:>+8.2%} {:>9.2%} {:>8.2%} {:>6.1%}".format(
    "c873_fix6", 0.1256, -0.51, 0.1105, -0.4190, -0.4244, 0.3228))
print("{:>11} {:>+8.2%} {:>9.2%} {:>+8.2%} {:>9.2%} {:>8.2%} {:>6.1%}".format(
    "c70_fix6", 0.1256, -0.51, 0.0583, -0.3310, -0.3369, 0.3228))

conn = db.get_conn()
for t in TAGS:
    for tbl in ("backtest_summary", "backtest_nav", "backtest_nav_daily"):
        conn.execute("DELETE FROM {} WHERE run_date=?".format(tbl), (t,))
conn.commit()
conn.close()
print("已清理本次 run ({} 条): {}".format(len(TAGS), TAGS))
