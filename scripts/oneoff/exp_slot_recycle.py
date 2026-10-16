"""槽位回收实验(2026-09-10 深夜): 20.5% 的资本被整手粒度机械性闲置, 能不能救回来?

诊断(diag_exposure.py / diag_alloc_reject.py, 2 万本金恒定 0.873):
  - `slot = equity_cash/max_stocks` = 2900 元 → 只够 1 手 ≤29 元的股票;
    候选池价格中位数 33 元 → **每月平均 1.42 个槽因「买不起」空转**;
  - 系统线真实暴露只有 56.5%(名义 87.3%), 2017 年 35.3% / 2024 年 75.5%;
  - 空转的钱既不进股票也不产生 alpha, 纯现金(只有逆回购收益)。

四个臂(全部在**当前生产配置**上只改分配器/candidate_top):
  A0 现行        槽预算固定 equity/6, 买不起跳过(对照)
  A1 自适应槽位  槽预算改 equity/实际能买的只数, 迭代到不动点 —— 把空转的钱
                 按**等权**灌回已选中的票(提高暴露, 只数与上限不变)
  A2 向下补位    候选池放到 top-12(stock_candidate_top=12), 槽预算仍 equity/6 ——
                 空转的钱用来**多买几只**(提高暴露 + 提高分散)
  A3 A1+A2       自适应槽位 + top-12

A1 与 A2 的区别就是本次分析的核心问题: 闲置的钱拿去**加仓现有的票**(纯暴露效应)
还是拿去**买更多的票**(暴露+分散效应)? 若 A2 显著优于 A1 → 分散真的值钱;
若两者都差 → 暴露本身是负贡献, 现行低暴露属于「歪打正着的防守」。

窗口 full / h1 / h2 全跑(按项目纪律不选同段最优)。跑完自清本次 run。
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
from app.backtest import engine as E
from app.logger import setup_logger
from app.portfolio.board import lot_shares

setup_logger()
db.init_db()

WINDOWS = [("full", None, None), ("h1", "20170101", "20211231"),
           ("h2", "20220101", "20260831")]
_orig_alloc = E.allocate_lots
_orig_cfg = cfg_mod.load_config


def _alloc_adaptive(top, equity_cash, max_stocks, ind_cap=1.0, cost_rate=0.0,
                    floor_stocks=2.0):
    """A1 自适应槽位: 槽预算 = equity_cash / 实际能买的只数, 迭代到不动点。

    现行 `slot = equity_cash/max_stocks` 的问题: 买不起 1 手的票被跳过, 其槽预算永久
    闲置。本函数先按现行规则找「能买得起」的票, 再把预算重分成等额, 反复直到只数稳定
    (槽变大 → 更多票买得起 → 只数可能增加 → 槽再变)。**只数上限仍是 max_stocks。**
    带循环护栏: 只数单调不减且 ≤ max_stocks, 故必然收敛。
    """
    if top is None or top.empty or equity_cash <= 0 or max_stocks <= 0:
        return {"shares": {}, "weights": {}, "cost": 0.0, "n_eff": 0, "cash_left": equity_cash}
    pool = top[top["close"] > 0].copy()
    if pool.empty:
        return {"shares": {}, "weights": {}, "cost": 0.0, "n_eff": 0, "cash_left": equity_cash}
    if "ts_code" in pool.columns:
        pool = pool.set_index("ts_code")
    n_try = max_stocks
    last = None
    for _ in range(max_stocks + 2):                     # 护栏: 至多 max_stocks 轮
        last = _orig_alloc(pool, equity_cash, n_try, ind_cap=ind_cap,
                           cost_rate=cost_rate, floor_stocks=floor_stocks)
        # 下界 2: 否则极端情况会退化成「只买得起 1 只 → 全仓押 1 只」的单票组合,
        # 那不是可提案的配置(回测会给出没有意义的爆炸结果)
        got = max(2, last["n_eff"])
        if got >= n_try:
            break
        n_try = got                                       # 用「买到的只数」当分母重分
    return last


def _make_arm(adaptive: bool):
    if not adaptive:
        return _orig_alloc
    return _alloc_adaptive


ARMS = [("a0_now", False, 0), ("a1_slot", True, 0),
        ("a2_fill", False, 12), ("a3_both", True, 12)]

out = []
TAGS = []
t0 = time.time()
try:
    for tag, adaptive, cand_top in ARMS:
        E.allocate_lots = _make_arm(adaptive)

        def _cfg(*a, **k):
            c = _orig_cfg(*a, **k)
            if cand_top:
                c["stock_candidate_top"] = cand_top
            return c

        cfg_mod.load_config = _cfg
        for wname, start, end in WINDOWS:
            m = E.run_backtest(run_tag="sr_{}_{}".format(tag, wname), start=start, end=end)
            rd = m["run_date"]
            TAGS.append(rd)
            row = {"arm": tag, "win": wname, "run": rd,
                   "ann": m["ann_return"], "mdd": m["max_drawdown"],
                   "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
                   "sys_mdd_daily": m["sys_mdd_daily"], "idle": m["avg_cash_idle"],
                   "n_avg": m["avg_stocks"], "periods": m["periods"]}
            out.append(row)
            print("[{:>4.0f}s] {}".format(time.time() - t0,
                                          json.dumps(row, ensure_ascii=False)), flush=True)
finally:
    E.allocate_lots = _orig_alloc
    cfg_mod.load_config = _orig_cfg

print("\n=== 汇总(系统线) ===")
df = pd.DataFrame(out)
for w in ("full", "h1", "h2"):
    print("\n-- {} --".format(w))
    d = df[df["win"] == w]
    for r in d.itertuples():
        print("  {:9s} 满仓 {:6.2f}%/{:7.2f}%   系统 {:6.2f}%/{:7.2f}% (日频 {:7.2f}%)  闲置 {:5.1%}  只数 {:.2f}"
              .format(r.arm, r.ann * 100, r.mdd * 100, r.sys_ann * 100, r.sys_mdd * 100,
                      r.sys_mdd_daily * 100, r.idle, r.n_avg))

conn = db.get_conn()
for t in ("backtest_nav", "backtest_nav_daily"):
    conn.execute("DELETE FROM {} WHERE run_date IN ({})".format(
        t, ",".join("?" * len(TAGS))), TAGS)
conn.commit()
conn.close()
print("\n已清理 {} 个临时 run".format(len(TAGS)))
