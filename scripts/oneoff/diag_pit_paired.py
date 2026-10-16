"""PIT 三臂的**成对**检验 + 修正后的逐年覆盖率(非判据)

## 为什么必须成对

`exp_die_pit.py` 报的是两个**各自求均值**的数(基线 1.07% vs C 0.94%)。但两者是**同一个月、
同一批股票**上算出来的 —— 逐月配对做差消掉的正是主导方差的那部分(市场共同涨跌)。
未配对的均值差会被月度噪声淹没; 配对差分的 t 值才是「PIT 改变了什么」的可检验量。

三个对照, 全部**限制在同一覆盖域**:
  1. `B − A`(粒度: 111 细分行业 → 31 申万一级)
  2. `C − B`(消前视: 行业标签是否随日期变化)
  3. `C − A`(合计: 生产口径 → PIT 申万一级)

## 修正后的覆盖率

`exp_die_pit.py` 日志里那行「PIT 100.2% / 快照L1 225%」是**算错的**(分子含没有行情的
申万成员)。这里按「与当月有价股票取交」重算, 因为**若 C 臂在死区(2021-2023)的覆盖明显
低于 B 臂, 那 C 的死区读数就被训练/打分样本量拖累, 而不是前视修复的代价** ——
这个混淆必须量出来才能解释读数。

用法: ./.venv/Scripts/python.exe scripts/oneoff/diag_pit_paired.py
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
from app.factors.holder_factors import sw_industry_map_pit   # noqa: E402
from app.models import expert as ex                      # noqa: E402

LOG = r"D:/claude-home/tmp/diag_pit_paired.log"
ARMS = {"A_base": "model_score", "B_snap": "model_score_die_snap", "C_pit": "model_score_die_pit"}
PAIRS = (("B_snap", "A_base", "粒度  111细分 → 31申万一级"),
         ("C_pit", "B_snap", "消前视 快照 → 时点"),
         ("C_pit", "A_base", "合计  生产 → PIT申万一级"))


def _setup_log():
    fmt = logging.Formatter("%(asctime)s %(message)s")
    fh = logging.FileHandler(LOG, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh], force=True)


def paired(d1: pd.DataFrame, d2: pd.DataFrame, log, tag: str) -> None:
    """逐月配对差分。d1/d2 必须已在**同一覆盖域**上算过。"""
    log.info("  %s", tag)
    log.info("     %-8s%6s%12s%9s%12s%9s", "区段", "期数", "Δhead/月", "Δhead t",
             "Δtop6/月", "Δtop6 t")
    for z, (a, b) in (("DEAD", ex.DEAD), ("ACTIVE", ex.ACTIVE), ("FULL", (None, None))):
        if a is None:
            m = pd.Series(True, index=d1.index)
        else:
            m = (d1.index >= a) & (d1.index <= b)
        h = (d1.loc[m, "head_excess"] - d2.loc[m, "head_excess"]).dropna()
        t6 = (d1.loc[m, "top6_excess"] - d2.loc[m, "top6_excess"]).dropna()
        sh, s6 = ex._stat(h), ex._stat(t6)
        log.info("     %-8s%6d%+12.4f%+9.2f%+12.4f%+9.2f", z, sh["n"], sh["mean"],
                 sh["t"], s6["mean"], s6["t"])


def main():
    _setup_log()
    log = logging.getLogger("paired")
    db.init_db()

    m = fe.load_market()
    dates = pd.to_datetime(m["close"].index)
    md = fe.month_end_dates(dates, today=datetime.date.today())
    fwd = ex._forward_return(m)

    # ---- 修正后的逐年覆盖率(与当月有价股票取交) ----
    ind = sw_industry_map_pit(md, "L1")
    ind_snap = pd.DataFrame(np.tile(ind.iloc[-1].values, (len(md), 1)),
                            index=md, columns=ind.columns)
    px = m["close"].loc[md].reindex(columns=ind.columns).notna()
    n_px = px.sum(axis=1)
    log.info("有价股票 %d 只/月(中位); 因缺行业归属而**损失**的当月有价股票占比:", int(n_px.median()))
    cov = {}
    for tag, indf in (("B_snap", ind_snap), ("C_pit", ind)):
        r = 1.0 - (indf.notna() & px).sum(axis=1) / n_px.replace(0, np.nan)
        cov[tag] = r
        log.info("  %-7s 全期中位损失 %.2f%% | 逐年: %s", tag, 100 * r.median(),
                 " ".join(f"{y}:{100 * s.median():.1f}%" for y, s in r.groupby(r.index.year)))

    sc = {k: ex.load_scores(t) for k, t in ARMS.items()}
    md = md[(md >= sc["A_base"].index.min()) & (md <= sc["A_base"].index.max())]

    log.info("=" * 78)
    for hi, lo, tag in PAIRS:
        # **判据用同一覆盖域**: 两个模型都要在两者共同打分的股票上评估
        covmask = sc[hi].notna() & sc[lo].notna()
        d_hi = ex.daily_head_stats(sc[hi], fwd, md, restrict_to=sc[lo])
        d_lo = ex.daily_head_stats(sc[lo], fwd, md, restrict_to=sc[hi])
        log.info("【%s − %s】%s   (共同覆盖 %d 只/月中位)",
                 hi, lo, tag, int(covmask.sum(axis=1).median()))
        paired(d_hi, d_lo, log, f"{hi} − {lo}")


if __name__ == "__main__":
    main()
