"""恒定权益仓位前沿扫描(2026-09-10 晚): 去掉 regime 择时后, 仓位定多少?

背景: 风格防守 A/B(scripts/oneoff/ab_style_guard.py)结论 —— ①池子分支换眼睛**证伪**
(即使完全改用等权全A 分支, 系统回撤仍 -51.63%); ②恒定仓位 + 固定 6 只在 full/h1/h2
三窗两轴全胜。0910 凌晨的权益暴露前沿扫描做于**旧扁平成本口径**(2 万/单边 0.3%),
本脚本在**现行逐笔真实费用**(佣金 max(额×万2.5, 最低5元)+印花税+过户费)下重测 ——
最低佣金对小仓位的非线性惩罚会改变结论: 仓位越低 → 单笔越小 → 费率越重。

固定 top_n=6(与 v4 同口径), 只扫 full 窗口(仓位是终身风险偏好, 不是择时)。不选
"同段最优" —— 输出的是前沿曲线, 由用户按回撤承受度定档。
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
WEIGHTS = [0.50, 0.60, 0.70, 0.80, 0.873]


def _patched_const(w):
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


out, TAGS = [], []
t0 = time.time()
try:
    for w in WEIGHTS:
        cfg_mod.load_config = _patched_const(w)
        m = E.run_backtest(top_n=6, run_tag="sw_{:.3f}".format(w))
        rd = m["run_date"]
        TAGS.append(rd)
        row = {"w": w, "ann": m["ann_return"], "mdd": m["max_drawdown"],
               "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
               "sys_mdd_daily": m["sys_mdd_daily"], "sharpe": _sys_sharpe(rd),
               "idle": m["avg_cash_idle"], "n_avg": m["avg_stocks"],
               "periods": m["periods"]}
        out.append(row)
        print("[{:>4.0f}s] {}".format(time.time() - t0, json.dumps(row, ensure_ascii=False)),
              flush=True)
finally:
    cfg_mod.load_config = _orig

print()
print("=" * 88)
print("恒定仓位前沿 (逐笔真实费用, 2 万本金, 固定 6 只, full 窗口)")
print("=" * 88)
print("{:>7} {:>10} {:>10} {:>10} {:>10} {:>9} {:>8} {:>7} {:>6}".format(
    "权益", "满仓年化", "满仓回撤", "系统年化", "系统回撤", "日频回撤", "夏普", "闲置", "只数"))
for r in out:
    print("{:>7.3f} {:>+9.2%} {:>10.2%} {:>+9.2%} {:>10.2%} {:>9.2%} {:>8.2f} {:>7.1%} {:>6.2f}".format(
        r["w"], r["ann"], r["mdd"], r["sys_ann"], r["sys_mdd"],
        r["sys_mdd_daily"], r["sharpe"], r["idle"], r["n_avg"]))
print()
print("对照 pin 基线 20260910_fees0910 (现行 aggressive 择时 1.0/0.9/0.8): "
      "满仓 +11.80%/-58.75%  系统 +7.86%/-51.81% (日频 -53.84%)")

conn = db.get_conn()
for t in TAGS:
    for tbl in ("backtest_summary", "backtest_nav", "backtest_nav_daily"):
        conn.execute("DELETE FROM {} WHERE run_date=?".format(tbl), (t,))
conn.commit()
conn.close()
print("已清理本次 run ({} 条)".format(len(TAGS)))
