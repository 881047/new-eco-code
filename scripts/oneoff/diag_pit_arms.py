"""PIT 三臂的**诊断**(非判据): 逐年 top6、B/C 互相的秩相关、同覆盖域对照

`exp_die_pit.py` 跑完给出的是三段汇总读数。汇总读数在本项目里已经被骗过一次
(die_flow 死区 top6 +2.11% 看着好, 逐年拆开是 −0.59/−0.80/+1.09/**+6.93**, 剔掉 2023
后 ≈0)。所以在把任何 top6 差异写成结论之前, 必须先做三件事:

  1. **逐年拆 top6** —— 看改善/恶化是分散在多数年份, 还是某一年单独贡献。
  2. **B vs C 的秩相关** —— 若高达 0.95+, 则 ACTIVE top6 的背离是 top-6 噪声而非模型差异;
     若明显更低, 两臂确实选了不同的票。
  3. **同覆盖域对照** —— 基线的 top6 必须限制在**每个臂自己的覆盖域**上算(kgd 只报 head
     的同域对照, 不报 top6)。否则把「能打多少只」混进「选得准不准」。

**本脚本不产生判决, 只产生读数** —— 判据在 `exp_die_pit.py` 里已经预注册并跑完了。

用法: ./.venv/Scripts/python.exe scripts/oneoff/diag_pit_arms.py
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

LOG = r"D:/claude-home/tmp/diag_pit_arms.log"
ARMS = (("B_snap", "model_score_die_snap"), ("C_pit", "model_score_die_pit"))


def _setup_log():
    fmt = logging.Formatter("%(asctime)s %(message)s")
    fh = logging.FileHandler(LOG, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh], force=True)


def per_year(det: pd.DataFrame, log, tag: str) -> None:
    """逐年的 head / top6 / 命中率 —— top6 的年度集中性是本项目最会骗人的东西。"""
    g = det.groupby(det.index.year)
    log.info("  %s", tag)
    log.info("     %-6s%5s%11s%11s%9s", "年", "期数", "head/月", "top6/月", "命中")
    for y, sub in g:
        h = ex._stat(sub["head_excess"])
        t6 = ex._stat(sub["top6_excess"])
        log.info("     %-6d%5d%+11.4f%+11.4f%9.3f", y, len(sub), h["mean"], t6["mean"], h["hit"])
    zone = pd.Series("FULL", index=det.index)
    zone[(det.index >= ex.DEAD[0]) & (det.index <= ex.DEAD[1])] = "DEAD"
    zone[(det.index >= ex.ACTIVE[0]) & (det.index <= ex.ACTIVE[1])] = "ACTIVE"
    for z in ("DEAD", "ACTIVE"):
        sub = det[zone.values == z]
        log.info("     [%s] top6 %+.4f (n=%d)", z, ex._stat(sub["top6_excess"])["mean"], len(sub))


def main():
    _setup_log()
    log = logging.getLogger("diag")
    db.init_db()

    m = fe.load_market()
    dates = pd.to_datetime(m["close"].index)
    md = fe.month_end_dates(dates, today=datetime.date.today())
    fwd = ex._forward_return(m)
    base = ex.load_scores("model_score")
    md = md[(md >= base.index.min()) & (md <= base.index.max())]
    log.info("基线 %d 期 / 月末截面 %d 个 (%s ~ %s)",
             len(base), len(md), md[0].date(), md[-1].date())

    sc = {tag: ex.load_scores(t) for tag, t in ARMS}
    log.info("=" * 78)
    log.info("**B vs C 秩相关 %+.4f** —— 两臂只差行业标签是否随日期变化; 若 >0.95 则"
             "下面的 ACTIVE top6 背离是 top-6 噪声",
             ex.rank_corr_with(sc["B_snap"], sc["C_pit"], md))
    for tag, _t in ARMS:
        log.info("%s vs 基线 秩相关 %+.4f | top-6 重合: %s", tag,
                 ex.rank_corr_with(base, sc[tag], md), _overlap(base, sc[tag], md))
    log.info("B vs C top-6 重合: %s", _overlap(sc["B_snap"], sc["C_pit"], md))

    for tag, t in ARMS:
        log.info("=" * 78)
        log.info("【%s】覆盖域: 本臂 %d 只/月(中位), 基线与本臂逐月可比 %d 只",
                 tag, int(sc[tag].notna().sum(axis=1).median()),
                 int((base.notna() & sc[tag].notna()).sum(axis=1).median()))
        log.info("--- 基线 model_score, **限制在本臂覆盖域** (判据用这一份) ---")
        per_year(ex.daily_head_stats(base, fwd, md, restrict_to=sc[tag]), log, "基线(同域)")
        log.info("--- 本臂 ---")
        per_year(ex.daily_head_stats(sc[tag], fwd, md), log, tag)

    log.info("=" * 78)
    log.info("--- 基线 model_score, 全覆盖(仅参考, 与上面同域版不可直接比) ---")
    per_year(ex.daily_head_stats(base, fwd, md), log, "基线(全)")


def _overlap(a: pd.DataFrame, b: pd.DataFrame, md: pd.DatetimeIndex, k: int = 6) -> str:
    """两模型逐月 top-6 的重合只数 —— 与秩相关相比, 这才是「买的是不是同一批票」。"""
    ov = []
    for d in md:
        if d not in a.index or d not in b.index:
            continue
        ta = set(a.loc[d].dropna().nlargest(k).index)
        tb = set(b.loc[d].dropna().nlargest(k).index)
        if ta and tb:
            ov.append(len(ta & tb))
    if not ov:
        return "n/a"
    return f"均值 {np.mean(ov):.2f}/{k}, 完全重合 {sum(1 for x in ov if x == k)}/{len(ov)} 期"


if __name__ == "__main__":
    main()
