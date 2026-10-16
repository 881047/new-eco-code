"""任务 #24: 把 die 的训练目标从月频(21 日)改成周频(5 日)

## 用户指令

「把模型训练目标从月频改到周频」(2026-09-11)。

## 先说清楚「改训练目标」到底改了什么(已 grep 核实, 不是推测)

`HORIZON` 全项目**只有一处消费者**: `trainer.py:307` 的 `forward_return(ac, horizon)`
(= 标签)。**调仓频率不由它决定** —— 月末截面来自 `fe.month_end_dates()`,
与 horizon 无关。⇒ 本实验改的是**「模型预测多远」**, 不是「多久调仓」。
**策略仍然月频**, 持有期仍是 21 个交易日。这是用户长期目标「坚持月频」下的正确理解,
也必须写在读数里: h5 臂是**用一个 5 日的预测去支撑一个 21 日的持有**。

## 为什么这个改动有理由(而不是"试试看")

1. 21 日标签里混入了大量与下月决策无关的漂移; 5 日标签的截面离散度更高、
   **信噪比更低但是更"干净"的排序信号**。
2. 项目已知的最大暴露问题是**小盘价差**(t=2.74)与**投机行情 beta** —— 长horizon 标签
   更容易学到"这只票接下来一个月会涨"的 beta 成分; 短 horizon 更难学到 beta,
   可能顺带压低那部分暴露。
3. 反方理由同样硬: 训练目标(5 日)与持有期(21 日)不一致, 是**新的目标错配**,
   而 #22 刚证明改动目标函数会显著改变排序(秩相关能掉到 −0.08)。

## ⚠️ KGD 的已知盲点(上一轮刚被 head_vt 撞到, 必须在此声明)

`expert.kgd` 五条判据**只看死区 head**。`head_vt` 就是死区 PASS 而活跃区 top6 从
+3.19% 塌到 +0.60%。故本实验**读数里必须有逐年 top6 拆解**, 不允许只报 KGD 汇总。

=============================== 预注册(写于跑之前) ===============================
**唯一变量 = 标签 horizon**。其余全部逐字不动:
  · 同一套 31 因子(与生产 `compute_all(include_new=False)` 同组) + **当前快照行业中性化**
    (PIT 已证无影响, 不引入)
  · 同 objective(regression/rmse)、同超参(50 轮 × 4 种子)、同窗口(train_months=36,
    holdout_recent=6)、同 backtest_start、同 label_mode("full")
  · 对照臂 = `model_score`(生产, horizon=21)

**两个变体(剂量-反应, 零新参数)**:
  V1 `h5`  = horizon 5  (周频, 用户点名的那个)
  V2 `h10` = horizon 10 (半个月, 用来分辨「越短越好」还是「5 日这个点特殊」)
  ⇒ 若 h5 好 h10 不好 ⇒ 非单调, 更像噪声; 两者同向 ⇒ 才有机制可讲。

**判据 = `expert.kgd(n_tests=2)`**, 五条沿用 `expert.py` 模块常量(Bonferroni 按 2 个变体校正),
**评估用 21 日**(策略持有期口径) —— 这样分数表与 `model_score` 直接可比。
`head_excess`(前 10%)与 `top6_excess`(策略真买的 6 只)**两个都要报**。

**本实验独有的额外读数(非判据, 只作机制)**: 同一批月末截面上, 同时算 **5 日前向收益**的
head/top6。它分离两件事 —— 「h5 臂有没有真的把 5 日排序学好」(看 5 日读数) 与
「学好了 5 日有没有帮到 21 日的策略」(看 21 日读数, 即判据)。
**判据永远是 21 日那条**, 5 日读数不得用来改判。

**预期与纪律**: 前 10 轮"改模型"全部失败, 共同失败方式是**同时削掉 2024-25 那唯一一段
真实收益**。若两变体都不过 ⇒ 第 11 轮, 照实记录, 并写入 `app/config.py` 已证伪登记处。
**不允许的**: 看到结果后再调 horizon、再补 h2/h15、再换 label_mode、再改 TOP_FRAC。

用法:
    ./.venv/Scripts/python.exe scripts/oneoff/exp_die_weekly.py             # 两变体全跑
    ./.venv/Scripts/python.exe scripts/oneoff/exp_die_weekly.py --arm h5
    ./.venv/Scripts/python.exe scripts/oneoff/exp_die_weekly.py --compare-only
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
    # ⚠️ 必须留住原 wrapper 的引用: `io.TextIOWrapper(sys.stdout.buffer, ...)` 之后,
    # 旧的 TextIOWrapper 一旦被 GC 就会**关掉底层 buffer**, 于是新 wrapper 与控制台
    # 上的 StreamHandler 一起失效(症状: 日志文件正常但 stderr 全是
    # `ValueError: I/O operation on closed file` 的 logging traceback)。
    # 2026-09-11 实测踩到过。_ORIG_STDOUT 不能删。
    _ORIG_STDOUT = sys.stdout
    sys.stdout = io.TextIOWrapper(_ORIG_STDOUT.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import db                                       # noqa: E402
from app.factors import factor_engine as fe              # noqa: E402
from app.models import expert as ex                      # noqa: E402
from app.models import trainer                           # noqa: E402
from scripts.oneoff.exp_die_pit import neutralize_pit, raw_factors   # noqa: E402

ARMS = {"h5": 5, "h10": 10}          # 剂量-反应: 预注册两个, 不许多加
BASE_T = "model_score"
LOG = r"D:/claude-home/tmp/die_weekly.log"


def _setup_log():
    fmt = logging.Formatter("%(asctime)s %(message)s")
    fh = logging.FileHandler(LOG, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh], force=True)


def per_year(s: pd.DataFrame, log) -> None:
    """逐年 top6 / head —— 「汇总读数在本项目里被骗过太多次」的固定解药。"""
    log.info("      %-6s%5s%12s%12s%9s", "年", "期数", "head/月", "top6/月", "命中")
    for y, sub in s.groupby(s.index.year):
        h, t = ex._stat(sub["head_excess"]), ex._stat(sub["top6_excess"])
        log.info("      %-6d%5d%+12.4f%+12.4f%9.3f", y, len(sub), h["mean"], t["mean"], h["hit"])


def zone_line(s: pd.DataFrame, log, tag: str) -> dict:
    out = {}
    for z, (a, b) in (("DEAD", ex.DEAD), ("ACTIVE", ex.ACTIVE), ("FULL", (None, None))):
        m = pd.Series(True, index=s.index) if a is None else ((s.index >= a) & (s.index <= b))
        h, t = ex._stat(s.loc[m, "head_excess"]), ex._stat(s.loc[m, "top6_excess"])
        out[z] = (h["mean"], h["t"], h["hit"], t["mean"], h["n"])
        log.info("      %-8s%6d%+12.4f%+9.2f%8.3f%+12.4f", z, h["n"], h["mean"], h["t"],
                 h["hit"], t["mean"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=sorted(ARMS), default=None)
    ap.add_argument("--compare-only", action="store_true")
    args = ap.parse_args()
    _setup_log()
    log = logging.getLogger("weekly")
    db.init_db()

    m = fe.load_market()
    dates = pd.to_datetime(m["close"].index)
    md = fe.month_end_dates(dates, today=datetime.date.today())
    md = md[md >= pd.Timestamp("2014-01-01")]
    fwd21 = ex._forward_return(m, 21)     # 判据口径 = 策略持有期
    fwd5 = ex._forward_return(m, 5)       # 机制读数口径 = h5 臂的训练目标
    log.info("行情 %s ~ %s, 月末截面 %d 个", dates[0].date(), dates[-1].date(), len(md))

    conn = db.get_conn()
    try:
        ind = fe.industry_map(conn)       # 当前快照 = 生产口径(与 #22 同)
    finally:
        conn.close()

    arms = (args.arm,) if args.arm else tuple(ARMS)
    if not args.compare_only:
        raw = raw_factors(m, md, dates)
        neu, n_orphan = {}, 0
        for k, v in raw.items():
            neu[k], n = neutralize_pit(v.loc[md], ind)
            n_orphan += n
        del raw
        log.info("因子 %d 个, 中位覆盖 %.0f 只/月; 因无行业归属置 NaN %d 条(与生产同口径)",
                 len(neu), np.median([v.notna().sum(axis=1).median() for v in neu.values()]),
                 n_orphan)
        for arm in arms:
            table = f"model_score_die_{arm}"
            ex.ensure_score_table(table)
            conn = db.get_conn()
            try:
                conn.execute(f"DELETE FROM {table}")
                conn.commit()
            finally:
                conn.close()
            t0 = time.time()
            res = ex.run_expert(
                f"die_{arm}", neu, table=table, full=True, backtest_start="2017-01-01",
                num_boost_round=trainer.ENSEMBLE_ROUNDS, early_stopping_rounds=0,
                ensemble_seeds=trainer.ENSEMBLE_SEEDS,
                train_months=36, holdout_recent=6,
                label_mode="full", horizon=ARMS[arm])
            log.info("[%s] 训练完成 %.0fs (horizon=%d): %s", arm, time.time() - t0,
                     ARMS[arm], {k: v for k, v in res.items() if k != "factor_names"})
    else:
        del ind, md

    # ---------------- 读数 ----------------
    base = ex.load_scores(BASE_T)
    md = md[(md >= base.index.min()) & (md <= base.index.max())]
    d21_base = ex.daily_head_stats(base, fwd21, md)

    log.info("=" * 88)
    log.info("【基线 model_score (horizon=21)】评估口径 = 21 日")
    r = zone_line(d21_base, log, "基线")
    log.info("   逐年:")
    per_year(d21_base, log)

    for arm in arms:
        table = f"model_score_die_{arm}"
        sc = ex.load_scores(table)
        if sc.empty:
            log.info("⚠ %s 无分数, 跳过", table)
            continue
        rec = ex.kgd(table, baseline=BASE_T, n_tests=len(ARMS), market=m, label=arm)

        log.info("=" * 88)
        log.info("【%s】horizon=%d → 表 %s (%d 期)", arm, ARMS[arm], table, len(sc))
        log.info("  与基线秩相关 %+.4f | top-6 重合 %s", rec.get("corr_with_baseline", float("nan")),
                 _overlap(base, sc, md))
        log.info("  基线**同覆盖域**对照: DEAD head %+.4f / ACTIVE head %+.4f (平均截面 %.0f 只)",
                 rec.get("baseline_DEAD_head", float("nan")),
                 rec.get("baseline_ACTIVE_head", float("nan")),
                 rec.get("baseline_avg_names", float("nan")))
        log.info("  p(Bonferroni, n=2) = %.4f | 同号 %s | 秩相关合格 %s | **KGD PASS = %s**",
                 rec.get("p_adj", float("nan")), rec.get("same_sign"),
                 rec.get("corr_ok"), rec.get("PASS"))

        log.info("  --- 判据口径: 21 日前向收益(策略持有期) ---")
        log.info("      %-8s%6s%12s%9s%8s%12s", "区段", "期数", "head/月", "head t",
                 "命中", "top6/月")
        d21 = ex.daily_head_stats(sc, fwd21, md)
        zone_line(d21, log, arm)
        log.info("   逐年(21 日):")
        per_year(d21, log)

        log.info("  --- 机制读数: 5 日前向收益(**非判据**, 只问「5 日排序有没有学好」) ---")
        log.info("      %-8s%6s%12s%9s%8s%12s", "区段", "期数", "head/月", "head t",
                 "命中", "top6/月")
        zone_line(ex.daily_head_stats(sc, fwd5, md), log, arm)
        log.info("  同口径基线(5 日) 对照:")
        log.info("      %-8s%6s%12s%9s%8s%12s", "区段", "期数", "head/月", "head t",
                 "命中", "top6/月")
        zone_line(ex.daily_head_stats(base, fwd5, md), log, arm)

    log.info("=" * 88)
    log.info("读法: 判据只看 **21 日** 那条(策略持有期)。5 日读数回答的是「训练目标达成了吗」, "
             "它达成了也不等于策略受益 —— 本实验的虚假胜利模式就是「5 日读数漂亮而 21 日变差」。")
    log.info("HORIZON 只影响标签, 不影响调仓频率: 策略仍是月频, 持有期仍 21 个交易日。")


def _overlap(a: pd.DataFrame, b: pd.DataFrame, md: pd.DatetimeIndex, k: int = 6) -> str:
    ov = []
    for d in md:
        if d not in a.index or d not in b.index:
            continue
        ta = set(a.loc[d].dropna().nlargest(k).index)
        tb = set(b.loc[d].dropna().nlargest(k).index)
        if ta and tb:
            ov.append(len(ta & tb))
    return (f"均值 {np.mean(ov):.2f}/{k}, 完全重合 {sum(1 for x in ov if x == k)}/{len(ov)} 期"
            if ov else "n/a")


if __name__ == "__main__":
    main()
