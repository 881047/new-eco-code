"""任务 #22: 把 die 的学习目标从「全截面收益」换成「头部」

## 要检验的是什么

策略只买分数**前 6 只**, 而 trainer 的默认目标是**在全截面上**最小化 21 日收益的平方误差。
优化的量 ≠ 决策用的量, 这是标准的目标错配。本项目已有三条**独立**的旁证说明这个错配是真的:
  · IC 与实盘收益**反向**(已 3 次独立复现)
  · `head_excess`(前 10%)与 `top6_excess`(真正买的 6 只)实测**严重背离**
  · 唯一统计显著的暴露是**小盘价差**(t=2.74), 模型自己在跑的不全是"预测收益"

## ⚠️ 必须先更正一个我自己犯过的错

我曾在 2026-09-11 白天说「die 死区 head 只有 0.51% 而基线 1.07%, 是学习目标不一致造成的」。
**那是错的** —— die 与基线用**同一个目标、同一套超参**训练, 目标错配对两者完全相同, 解释不了
两者之差(真正的差别是口粮)。⇒ **本实验的理由不是「解释 die 为什么弱」, 而是上面那三条旁证。**
另注: lambdarank(排序目标)在 2026-09-05 被移除并记「完败」, 但**代码已删、无 run、无日志,
实证不留存**, 故它只是背景, 不构成本轮的证据。

=============================== 预注册(写于跑之前) ===============================
**唯一变量 = 学习目标**。其余全部逐字不动:
  · 同一套 31 因子(与生产 `compute_all(include_new=False)` 同组) **且用当前快照行业中性化
    —— 不引入 PIT**: 上一轮已证明行业口径对可测表现无影响, 引入它只会多一个变量
  · 同 horizon(21 日)、同超参(50 轮 × 4 种子)、同窗口(train_months=36, holdout_recent=6)、
    同 backtest_start。**对照臂 = `model_score`(生产模型, 同 31 因子同目标)** ——
    `verify_d1_replica.py` 已证明 `factors_override` 路径与生产读 `factor` 表口径等价
    (12 个月里 10 个逐位相同, 另 2 个仅 28/4385 与 101/4396 只股票有差、top6 6/6 一致)

**两个变体(均零新参数)**:
  V1 `head_cls` 标签 = 1{月内截面百分位 ≥ 1 − HEAD_TOP_FRAC}, binary/auc, 全样本
  V2 `head_vt`  同上, 但剔除中间 80%(只留头 10% vs 尾 10%), binary/auc
  ⇒ HEAD_TOP_FRAC = 0.10 **与 `expert.TOP_FRAC` 同值** —— 训练时定义的「头部」必须就是
    考核时度量的「头部」, 否则优化的东西和判据不是一回事。

**判据 = `expert.kgd(n_tests=2)`**, 五条沿用 `expert.py` 模块级常量(Bonferroni 按 2 个变体校正)。
`head_excess`(前 10%)与 `top6_excess`(策略真买的 6 只)**两个都要报** —— 本项目已知二者会背离,
只报一个等于挑对自己有利的那个。

**预期与纪律**: 这是个有真实理由的改造, 但**前 9 轮"改模型"全部失败**, 且共同失败方式是
「同时削掉 2024-25 那唯一一段真实收益」。⇒ 若两变体都不过, 就是第 10 轮, 照实记录;
若过了, **下一步不是接生产而是预注册 walk-forward 验证**(有效验证样本只有 24 个月)。
**不允许的**: 看到结果后再调 HEAD_TOP_FRAC、再换阈值、再补第三个变体。

用法:
    ./.venv/Scripts/python.exe scripts/oneoff/exp_die_head.py            # 两变体全跑
    ./.venv/Scripts/python.exe scripts/oneoff/exp_die_head.py --mode head_cls
    ./.venv/Scripts/python.exe scripts/oneoff/exp_die_head.py --compare-only
"""
import argparse
import datetime
import io
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import db                                       # noqa: E402
from app.factors import factor_engine as fe              # noqa: E402
from app.models import expert as ex                      # noqa: E402
from app.models import trainer                           # noqa: E402
from scripts.oneoff.exp_die_pit import neutralize_pit, raw_factors   # noqa: E402

MODES = ("head_cls", "head_vt")
LOG = r"D:/claude-home/tmp/die_head.log"


def _setup_log():
    fmt = logging.Formatter("%(asctime)s %(message)s")
    fh = logging.FileHandler(LOG, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh], force=True)


def report(rec: dict, log, mode: str) -> None:
    log.info("=" * 78)
    log.info("KGD 读数 —— %s (表 %s)", rec.get("name"), rec.get("table"))
    if "reason" in rec:
        log.info("  ⚠ 无读数: %s", rec["reason"])
        log.info("=" * 78)
        return
    log.info("  月份数 %s | 平均截面股票数 %.0f", rec.get("n_months"),
             rec.get("avg_names", float("nan")))
    log.info("  %-8s%11s%9s%8s%11s%7s", "区段", "head/月", "t", "命中", "top6/月", "期数")
    for z in ("DEAD", "ACTIVE"):
        log.info(f"  {z:<8}{rec.get(f'{z}_head', float('nan')):>+10.4f}"
                 f"{rec.get(f'{z}_t', float('nan')):>+9.2f}"
                 f"{rec.get(f'{z}_hit', float('nan')):>8.3f}"
                 f"{rec.get(f'{z}_top6', float('nan')):>+11.4f}"
                 f"{rec.get(f'{z}_n', 0):>7}")
    log.info(f"  {'FULL':<8}{rec.get('FULL_head', float('nan')):>+10.4f}"
             f"{rec.get('FULL_t', float('nan')):>+9.2f}")
    log.info("  与基线 model_score 秩相关 %+.4f",
             rec.get("corr_with_baseline", float("nan")))
    log.info("  基线**同覆盖域**对照: DEAD head %+.4f / ACTIVE head %+.4f (平均截面 %.0f 只)",
             rec.get("baseline_DEAD_head", float("nan")),
             rec.get("baseline_ACTIVE_head", float("nan")),
             rec.get("baseline_avg_names", float("nan")))
    log.info("  p(Bonferroni, n=2) = %.4f | 同号 %s | 秩相关合格 %s | **PASS = %s**",
             rec.get("p_adj", float("nan")), rec.get("same_sign"),
             rec.get("corr_ok"), rec.get("PASS"))
    log.info("=" * 78)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=MODES, default=None, help="只跑单个变体")
    ap.add_argument("--compare-only", action="store_true")
    args = ap.parse_args()
    _setup_log()
    log = logging.getLogger("head")
    db.init_db()

    m = fe.load_market()
    dates = pd.to_datetime(m["close"].index)
    md = fe.month_end_dates(dates, today=datetime.date.today())
    md = md[md >= pd.Timestamp("2014-01-01")]
    log.info("行情 %s ~ %s, 月末截面 %d 个", dates[0].date(), dates[-1].date(), len(md))

    conn = db.get_conn()
    try:
        ind_old = fe.industry_map(conn)     # 当前快照 = 生产口径(本轮**不**引入 PIT)
    finally:
        conn.close()

    modes = (args.mode,) if args.mode else MODES
    if not args.compare_only:
        raw = raw_factors(m, md, dates)
        neu, n_orphan = {}, 0
        for k, v in raw.items():
            neu[k], n = neutralize_pit(v.loc[md], ind_old)
            n_orphan += n
        del raw
        log.info("因子 %d 个, 中位覆盖 %.0f 只/月; 因无行业归属置 NaN %d 条(与生产同口径)",
                 len(neu), np.median([v.notna().sum(axis=1).median() for v in neu.values()]),
                 n_orphan)
        for mode in modes:
            table = f"model_score_die_{mode}"
            ex.ensure_score_table(table)
            conn = db.get_conn()
            try:
                conn.execute(f"DELETE FROM {table}")
                conn.commit()
            finally:
                conn.close()
            t0 = time.time()
            res = ex.run_expert(
                f"die_{mode}", neu, table=table, full=True, backtest_start="2017-01-01",
                num_boost_round=trainer.ENSEMBLE_ROUNDS, early_stopping_rounds=0,
                ensemble_seeds=trainer.ENSEMBLE_SEEDS,
                train_months=36, holdout_recent=6, label_mode=mode)
            log.info("[%s] 训练完成 %.0fs: %s", mode, time.time() - t0,
                     {k: v for k, v in res.items() if k != "factor_names"})
    else:
        del ind_old, md

    for mode in modes:
        rec = ex.kgd(f"model_score_die_{mode}", baseline="model_score",
                     n_tests=len(MODES), market=m, label=mode)
        report(rec, log, mode)

    log.info("判据解读: 与基线同覆盖域的 head 是主判据; top6 是策略真买的 6 只, "
             "**两者必须一起看** —— 本项目已知 head 与 top6 会背离, 只报一个等于挑有利的那个。")


if __name__ == "__main__":
    main()
