"""D1 复刻差异的定性诊断: **换序** 还是 **换刻度**?

背景: `verify_d1_replica.py` 报出 2026-06-30 逐位一致、2026-07-31/08-31 有 2~5 的
最大差。脚本的判据(`worst < 1e-6`)过严 —— 策略消费的是**名次**不是原始刻度, 所以
关键问题是: 那 4.9 的差是「几百只股票换了位置」还是「全体一起平移了一点」。

本脚本不重训, 只读两张已存在的表做逐月解剖:
    1. 各自的**打分集合**大小(A 独有 / B 独有) —— 集合不同会让分位排名整体错位
    2. 原始名次差 `|rank_a - rank_b|` 的最大值/中位数 —— 名次视角的真实扰动
    3. `corr(生产分数, |差|)` —— 差值若是分数的单调函数 ⇒ 纯刻度/集合效应
    4. 最大差股票的明细 —— 看是不是集中在边缘股

用法: ./.venv/Scripts/python.exe scripts/oneoff/diag_d1_diff.py
"""
import io
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# 中文 Windows 控制台是 GBK, 直接 print 中文/Unicode 符号会 UnicodeEncodeError
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import db          # noqa: E402
from app.models import expert as ex   # noqa: E402

REP = "model_score_die_d1replica"
PROD = "model_score"


def main():
    db.init_db()
    a_all = ex.load_scores(REP)
    b_all = ex.load_scores(PROD)
    if a_all.empty:
        raise SystemExit(f"{REP} 为空 —— 先跑 verify_d1_replica.py")
    common = pd.DatetimeIndex(a_all.index).intersection(pd.DatetimeIndex(b_all.index))
    print(f"复刻表 {a_all.shape}  生产表 {b_all.shape}  共同月份 {len(common)}")

    for d in common:
        a = a_all.loc[d].dropna()
        b = b_all.loc[d].dropna()
        only_a = a.index.difference(b.index)
        only_b = b.index.difference(a.index)
        idx = a.index.intersection(b.index)
        da, db_ = a[idx], b[idx]
        diff = (da - db_).abs()
        ra, rb = da.rank(), db_.rank()          # 原始名次 1..n
        rankdiff = (ra - rb).abs()

        rho = np.corrcoef(ra, rb)[0, 1]
        print("\n" + "=" * 78)
        print(f"{d.date()}   交集 n={len(idx)}   A独有 {len(only_a)}   B独有 {len(only_b)}")
        print(f"  刻度: A [{da.min():.3f}, {da.max():.3f}]   B [{db_.min():.3f}, {db_.max():.3f}]")
        print(f"  |差|: max {diff.max():.3f}  mean {diff.mean():.4f}  p99 {diff.quantile(.99):.3f}")
        print(f"  秩相关 {rho:.6f}   |名次差|: max {rankdiff.max():.0f}  中位 {rankdiff.median():.1f}")
        # 差值是否是分数的单调函数(纯刻度效应会让 |差| 在两端大、中间小)
        ecc = np.abs(db_.rank() - len(db_) / 2)
        c = float(np.corrcoef(ecc, diff)[0, 1]) if (ecc.std() > 0 and diff.std() > 0) else np.nan
        print(f"  corr(|名次-中位|, |差|) = {c:+.4f}   "
              f"(接近 +-1 表示差异集中在尾部 = 刻度/集合效应)")
        # 名次差最大的几只
        top = rankdiff.sort_values(ascending=False).head(8)
        print("  名次扰动最大的股票:")
        for code, rd in top.items():
            print(f"    {code}  A={da[code]:8.3f}(#{ra[code]:.0f})  "
                  f"B={db_[code]:8.3f}(#{rb[code]:.0f})  Δ名次 {rd:.0f}")


if __name__ == "__main__":
    main()
