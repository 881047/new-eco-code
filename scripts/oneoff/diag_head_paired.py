"""头部学习目标两臂的诊断(非判据): 逐年拆 top6 + 同覆盖域成对检验

`exp_die_head.py` 报的是汇总读数: head_vt 死区好、活跃区差; head_cls 死区为负、活跃区 top6 最好。
**汇总读数在本项目里被骗过太多次**(die_flow 死区 top6 +2.11% 拆开是 −0.59/−0.80/+1.09/**+6.93**)。
故在把「head_vt 用死区换活跃区」写成结论之前必须查两件事:

  1. **逐年拆 top6** —— 活跃区的塌陷是分散在 2024/2025/2026, 还是某一年单独贡献?
  2. **成对检验** —— 同覆盖域逐月做差, 消掉市场共同涨跌。各自求均值的差会被月度噪声淹没。

**本脚本不产生判决** —— 判据在 `exp_die_head.py` 已预注册并跑完(`head_vt` PASS / `head_cls` FAIL)。

用法: ./.venv/Scripts/python.exe scripts/oneoff/diag_head_paired.py
"""
import datetime
import io
import logging
import sys
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

LOG = r"D:/claude-home/tmp/diag_head_paired.log"
ARMS = {"V1_head_cls": "model_score_die_head_cls", "V2_head_vt": "model_score_die_head_vt"}
BASE_T = "model_score"


def _setup_log():
    fmt = logging.Formatter("%(asctime)s %(message)s")
    fh = logging.FileHandler(LOG, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh], force=True)


def per_year(det: pd.DataFrame, log, tag: str) -> None:
    log.info("  %s", tag)
    log.info("     %-6s%5s%12s%12s%9s", "年", "期数", "head/月", "top6/月", "命中")
    for y, sub in det.groupby(det.index.year):
        log.info("     %-6d%5d%+12.4f%+12.4f%9.3f", y, len(sub),
                 ex._stat(sub["head_excess"])["mean"],
                 ex._stat(sub["top6_excess"])["mean"],
                 ex._stat(sub["head_excess"])["hit"])


def paired(d1: pd.DataFrame, d2: pd.DataFrame, log, tag: str) -> None:
    log.info("  %s", tag)
    log.info("     %-8s%6s%12s%9s%12s%9s", "区段", "期数", "Δhead/月", "Δhead t",
             "Δtop6/月", "Δtop6 t")
    for z, (a, b) in (("DEAD", ex.DEAD), ("ACTIVE", ex.ACTIVE), ("FULL", (None, None))):
        m = pd.Series(True, index=d1.index) if a is None \
            else ((d1.index >= a) & (d1.index <= b))
        sh = ex._stat((d1.loc[m, "head_excess"] - d2.loc[m, "head_excess"]).dropna())
        s6 = ex._stat((d1.loc[m, "top6_excess"] - d2.loc[m, "top6_excess"]).dropna())
        log.info("     %-8s%6d%+12.4f%+9.2f%+12.4f%+9.2f", z, sh["n"], sh["mean"],
                 sh["t"], s6["mean"], s6["t"])


def main():
    _setup_log()
    log = logging.getLogger("head")
    db.init_db()

    m = fe.load_market()
    dates = pd.to_datetime(m["close"].index)
    md = fe.month_end_dates(dates, today=datetime.date.today())
    fwd = ex._forward_return(m)                    # 21 日: 策略的持有期, 与 KGD 同口径
    base = ex.load_scores(BASE_T)
    md = md[(md >= base.index.min()) & (md <= base.index.max())]
    log.info("基线 %d 期 / 月末截面 %d 个", len(base), len(md))

    sc = {k: ex.load_scores(t) for k, t in ARMS.items()}
    log.info("=" * 78)
    log.info("V1 vs V2 秩相关 %+.4f  ← 两个头部目标互相之间",
             ex.rank_corr_with(sc["V1_head_cls"], sc["V2_head_vt"], md))
    for k, s in sc.items():
        log.info("%s vs 基线 秩相关 %+.4f | top-6 重合: %s", k,
                 ex.rank_corr_with(base, s, md), _overlap(base, s, md))

    for k, t in ARMS.items():
        log.info("=" * 78)
        log.info("【%s】本臂 %d 只/月, 与基线逐月可比 %d 只", k,
                 int(sc[k].notna().sum(axis=1).median()),
                 int((base.notna() & sc[k].notna()).sum(axis=1).median()))
        log.info("--- 基线 model_score **限制在本臂覆盖域** ---")
        per_year(ex.daily_head_stats(base, fwd, md, restrict_to=sc[k]), log, "基线(同域)")
        log.info("--- 本臂 ---")
        per_year(ex.daily_head_stats(sc[k], fwd, md), log, k)
        log.info("--- 成对: 本臂 − 基线 (同覆盖域) ---")
        paired(ex.daily_head_stats(sc[k], fwd, md, restrict_to=base),
               ex.daily_head_stats(base, fwd, md, restrict_to=sc[k]), log, f"{k} − 基线")

    log.info("=" * 78)
    log.info("读法: 若活跃区塌陷集中在单一年份 ⇒ 与 PIT/ die_flow 一样是年度噪声, 不是机制; "
             "若分散在 2024/2025/2026 三年 ⇒ 是稳定的 trade-off, 才是真结论。")


def _overlap(a: pd.DataFrame, b: pd.DataFrame, md: pd.DatetimeIndex, k: int = 6) -> str:
    ov = []
    for d in md:
        if d not in a.index or d not in b.index:
            continue
        ta = set(a.loc[d].dropna().nlargest(k).index)
        tb = set(b.loc[d].dropna().nlargest(k).index)
        if ta and tb:
            ov.append(len(ta & tb))
    return f"均值 {np.mean(ov):.2f}/{k}, 完全重合 {sum(1 for x in ov if x == k)}/{len(ov)} 期" \
        if ov else "n/a"


if __name__ == "__main__":
    main()
