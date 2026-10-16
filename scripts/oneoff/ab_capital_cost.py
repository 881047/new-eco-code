"""资金规模 A/B 第二段: 按资金规模校准交易成本(2026-09-09 深夜)

背景: ab_capital_diversify.py 显示 10 万资金在同口径(单边 0.3%)下反而更差 ——
      年化 4.07%/回撤 -68.6%(n6) / 3.16%/-50.7%(n12), 均不如 1 万的 5.94%/-52.1%。
      但 0.3% 单边是**为 1 万本金校准的**(券商最低佣金 5 元 / 单笔约 1600 元 ≈ 0.31%);
      10 万本金单笔约 1.7 万, 佣金 0.03% + 印花税 0.05%(卖出) + 过户费 ≈ 单边 0.1%。
      成本口径不校准, 资金对比就是被最低佣金人为压死的一方。

本段固定 capital=10万, 单边成本 0.1%(slip 0.1% 不变, 与生产一致), 扫只数:
  E 组 10万 n=6/5/4  @0.1%
  F 组 10万 n=10/8/6 @0.1%
  G 组 10万 n=12/10/8 @0.1%
对照: 生产 1万 n=6/5/4 @0.3% = 5.94%/-52.10%/0.165 (pin 20260909_fix0909)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import config as cfg_mod
from app import db
from app.logger import setup_logger
from app.backtest import engine as E

setup_logger()
db.init_db()

_orig = cfg_mod.load_config


def _patched(capital, state, cost):
    def f(*a, **k):
        cfg = _orig(*a, **k)
        if capital is not None:
            cfg["capital"] = capital
        if state is not None:
            cfg["state_stocks"] = state
        if cost is not None:
            cfg["single_side_cost"] = cost
        return cfg
    return f


ARMS = [
    ("ab2_cap10w_n654_c1", 100000, {"attack": 6, "neutral": 5, "defense": 4}, 0.001),
    ("ab2_cap10w_n1086_c1", 100000, {"attack": 10, "neutral": 8, "defense": 6}, 0.001),
    ("ab2_cap10w_n12108_c1", 100000, {"attack": 12, "neutral": 10, "defense": 8}, 0.001),
]

out = []
for tag, cap, st, cost in ARMS:
    cfg_mod.load_config = _patched(cap, st, cost)
    m = E.run_backtest(run_tag=tag)
    cfg_mod.load_config = _orig
    row = {
        "arm": tag, "capital": cap, "cost": cost, "state_stocks": st,
        "ann": m["ann_return"], "excess": m["excess_ann"], "mdd": m["max_drawdown"],
        "sharpe": m["sharpe"], "vol": m["vol_ann"], "avg_stocks": m["avg_stocks"],
        "idle": m["avg_cash_idle"], "turnover": m["avg_turnover"],
        "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
    }
    out.append(row)
    print(json.dumps(row, ensure_ascii=False), flush=True)

print()
print("=" * 110)
print(f"{'臂':>22} {'资金':>7} {'成本':>6} {'只数':>11} {'年化':>8} {'回撤':>8} {'夏普':>7} "
      f"{'波动':>7} {'实持':>6} {'闲置':>7} {'换手':>7} {'系统年化':>9} {'系统回撤':>9}")
for r in out:
    n = f"{r['state_stocks']['attack']}/{r['state_stocks']['neutral']}/{r['state_stocks']['defense']}"
    print(f"{r['arm']:>22} {r['capital']:>7.0f} {r['cost']:>6.3%} {n:>11} {r['ann']:>+8.2%} "
          f"{r['mdd']:>8.2%} {r['sharpe']:>7.3f} {r['vol']:>7.2%} {r['avg_stocks']:>6.2f} "
          f"{r['idle']:>7.1%} {r['turnover']:>7.1%} {r['sys_ann']:>+9.2%} {r['sys_mdd']:>9.2%}")
