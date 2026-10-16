"""资金规模 × 持仓只数 A/B(2026-09-09 深夜, 头部校准诊断 D5 的引擎层验证)

诊断结论(diag_head_calibration.py): 模型十分位单调(d1 +0.52%/月 → d10 -1.67%/月,
多空 t=4.99), 但**头部次序噪声大**(top3 t=0.49/月胜率 47.8%), 且等权持有只数越多
实现路径越好(top3 +0.42%/年/-75% → top6 +7.29%/-46.6% → top12 +9.54%/-24.0%)。
瓶颈是 1 万本金 + 整手约束逼出的集中度(avg 3.26 只 / 闲置 43%)。

本脚本用**生产引擎**(含整手/行业上限/成本/滑点/结转/择时)验证同一件事:
  A 组 cap=1万  n=6/5/4    ← 生产口径, 应与 pin 20260909_fix0909 逐位一致(自证)
  B 组 cap=10万 n=6/5/4    ← 只放大资金, 只数不变
  C 组 cap=10万 n=12/10/8  ← 资金+分散一起上
  D 组 cap=10万 n=10/8/6
只改 cfg 里的 capital 与 state_stocks(monkeypatch load_config), 不落 config.yaml。
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


def _patched(capital, state):
    def f(*a, **k):
        cfg = _orig(*a, **k)
        if capital is not None:
            cfg["capital"] = capital
        if state is not None:
            cfg["state_stocks"] = state
        return cfg
    return f


ARMS = [
    ("ab_cap1w_n654", 10000, {"attack": 6, "neutral": 5, "defense": 4}),
    ("ab_cap10w_n654", 100000, {"attack": 6, "neutral": 5, "defense": 4}),
    ("ab_cap10w_n1086", 100000, {"attack": 10, "neutral": 8, "defense": 6}),
    ("ab_cap10w_n12108", 100000, {"attack": 12, "neutral": 10, "defense": 8}),
]

out = []
for tag, cap, st in ARMS:
    cfg_mod.load_config = _patched(cap, st)
    m = E.run_backtest(run_tag=tag)
    cfg_mod.load_config = _orig
    row = {
        "arm": tag, "capital": cap, "state_stocks": st,
        "ann": m["ann_return"], "excess": m["excess_ann"], "mdd": m["max_drawdown"],
        "sharpe": m["sharpe"], "vol": m["vol_ann"], "avg_stocks": m["avg_stocks"],
        "idle": m["avg_cash_idle"], "turnover": m["avg_turnover"],
        "ic": m["ic_mean"], "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
        "mdd_daily": m["mdd_daily"], "sys_mdd_daily": m["sys_mdd_daily"],
    }
    out.append(row)
    print(json.dumps(row, ensure_ascii=False), flush=True)

print()
print("=" * 118)
print(f"{'臂':>16} {'资金':>7} {'只数':>12} {'年化':>8} {'超额':>8} {'回撤':>8} {'夏普':>7} "
      f"{'波动':>7} {'实持':>6} {'闲置':>7} {'换手':>7} {'系统年化':>9} {'系统回撤':>9}")
for r in out:
    n = f"{r['state_stocks']['attack']}/{r['state_stocks']['neutral']}/{r['state_stocks']['defense']}"
    print(f"{r['arm']:>16} {r['capital']:>7.0f} {n:>12} {r['ann']:>+8.2%} {r['excess']:>+8.2%} "
          f"{r['mdd']:>8.2%} {r['sharpe']:>7.3f} {r['vol']:>7.2%} {r['avg_stocks']:>6.2f} "
          f"{r['idle']:>7.1%} {r['turnover']:>7.1%} {r['sys_ann']:>+9.2%} {r['sys_mdd']:>9.2%}")
