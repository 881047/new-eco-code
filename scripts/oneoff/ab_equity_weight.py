"""权益暴露映射扫描(2026-09-09 深夜): 系统线的「年化-回撤」有效前沿

背景(diag_holding_period.py):
  · 全市场等权基准自身回撤 -40.78%, 策略 n=3/6/12 理想化回撤 -78%/-63%/-61%
    → 回撤不是选股问题, 是「暴露」问题(集中持有高波动小盘 + 几乎不降仓)。
  · 现行 config `risk_profile: aggressive` 的映射 = attack 1.0 / neutral 0.9 / defense 0.8
    → 防御态仍 80% 仓位, 系统线回撤只比满仓线好 8pp。
  · 低波动倾斜已证伪(alpha 在高波动股里), 所以「降波动」只能靠仓位, 不能靠选股。

本脚本扫 equity_weight 映射(资本用 config 现值 2 万), 给出前沿, 用户自己选点。
另加成本敏感度: 同映射下 0.3% vs 0.1% 单边。
跑完自清 run 行(不污染 backtest_summary)。
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


def _patched(wmap, cost=None):
    def f(*a, **k):
        cfg = _orig(*a, **k)
        if wmap is not None:
            cfg["regime_equity_weight"] = {"scan": wmap}
            cfg["risk_profile"] = "scan"
        if cost is not None:
            cfg["single_side_cost"] = cost
        return cfg
    return f


def _sys_sharpe(run_date: str):
    """从日频系统净值算年化夏普(rf=0)。"""
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


# (标签, 映射, 成本)  成本 None=用 config(0.003)
ARMS = [
    ("ew_flat",   {"attack": 1.0, "neutral": 1.0, "defense": 1.0}, None),   # =满仓线(自证)
    ("ew_agg",    {"attack": 1.0, "neutral": 0.9, "defense": 0.8}, None),   # 现行
    ("ew_mild",   {"attack": 1.0, "neutral": 0.8, "defense": 0.5}, None),
    ("ew_bal",    {"attack": 0.8, "neutral": 0.65, "defense": 0.5}, None),
    ("ew_strong", {"attack": 1.0, "neutral": 0.7, "defense": 0.3}, None),
    ("ew_def0",   {"attack": 1.0, "neutral": 0.6, "defense": 0.0}, None),
    ("ew_cons",   {"attack": 0.5, "neutral": 0.35, "defense": 0.2}, None),
    ("ew_agg_c1", {"attack": 1.0, "neutral": 0.9, "defense": 0.8}, 0.001),  # 成本敏感度
]

out = []
try:
    for tag, wm, cost in ARMS:
        run_tag = "ewscan_" + tag
        cfg_mod.load_config = _patched(wm, cost)
        m = E.run_backtest(run_tag=run_tag)
        cfg_mod.load_config = _orig
        run_date = "20260909_" + run_tag
        TAGS.append(run_date)
        row = {
            "arm": tag,
            "w": "{:.2f}/{:.2f}/{:.2f}".format(wm["attack"], wm["neutral"], wm["defense"]),
            "cost": 0.003 if cost is None else cost,
            "full_ann": m["ann_return"], "full_mdd": m["max_drawdown"],
            "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
            "sys_mdd_daily": m["sys_mdd_daily"], "sys_sharpe": _sys_sharpe(run_date),
            "idle": m["avg_cash_idle"],
        }
        out.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
finally:
    cfg_mod.load_config = _orig

cap = _orig().get("capital")
print()
print("=" * 104)
print("权益暴露映射扫描(capital={}, 全窗口 2017-01 → 2026-08, 月频)".format(cap))
print("=" * 104)
print("{:>10} {:>20} {:>6} {:>9} {:>9} {:>9} {:>9} {:>9} {:>8} {:>7}".format(
    "臂", "attack/neu/def", "成本", "满仓年化", "满仓回撤", "系统年化", "系统回撤",
    "日频回撤", "系统夏普", "闲置"))
for r in out:
    print("{:>10} {:>20} {:>6.1%} {:>+8.2%} {:>9.2%} {:>+8.2%} {:>9.2%} {:>9.2%} {:>8.2f} {:>7.1%}".format(
        r["arm"], r["w"], r["cost"], r["full_ann"], r["full_mdd"], r["sys_ann"],
        r["sys_mdd"], r["sys_mdd_daily"], r["sys_sharpe"], r["idle"]))
print()
print("读法: 系统线=按 state 缩放仓位的实盘口径; 满仓线=不缩放(对照, 应各臂相同)。")
print("      ew_flat 的满仓线应与 ew_agg 逐位一致(自证: 映射只影响系统线)。")
print("      成本敏感度: ew_agg(0.3%) vs ew_agg_c1(0.1%)。")

conn = db.get_conn()
for t in TAGS:
    for tbl in ("backtest_summary", "backtest_nav", "backtest_nav_daily"):
        conn.execute("DELETE FROM {} WHERE run_date=?".format(tbl), (t,))
conn.commit()
conn.close()
print("\n已清理本次扫描 run: {}".format(TAGS))
