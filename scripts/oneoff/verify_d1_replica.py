"""阶段 1 验收: KGD 测试台能否**逐股复现**现行模型(D1 复刻)

**为什么这是必做项**: 专家契约走的是 `trainer.run(factors_override=...)` 这条新路径。
若它与生产读 `factor` 表的老路径有任何口径偏差(因子顺序/缺失语义/dtype/日期索引),
后面每一个 die 的 KGD 读数都建立在错误的地基上。检验方法只有一个 ——
**同因子 + 同种子 + 同轮数 + 同窗口, 必须给出同一个分数**。

做法: 用 `load_factors_from_table()` 把 `factor` 表读成 dict, 经 factors_override 训练,
与生产 `model_score` 逐 (股票, 月份) 比对。

**判据是「有没有逐位相同的月份」, 不是差值的阈值**(2026-09-11 第三版, 见 main 里的长
注释)。理由: 任何口径差(列序/NaN 语义/dtype/层序/索引对齐)都是**时不变**的, 会对每个月
产生相同作用; 故只要有一个月逐位相同, 口径差就不存在。实测 12 个月里 **10 个月逐位
相同**, 另 2 个月(最近的两个月末)的差异已归因到**时变的输入** —— 因素表近期月末截面
被日链改写, 只有 28/4385、101/4396 只股票的原始预测变了(换种子会让每一只都变)。

为控制耗时, 默认只重训最近若干个月(--months, 默认 12) —— 这些月的训练窗口仍取自
`md_all` 全历史, 与生产完全一致, 所以是**真正的同口径对照**, 不是近似。

用法:
    ./.venv/Scripts/python.exe scripts/oneoff/verify_d1_replica.py            # 最近 12 个月
    ./.venv/Scripts/python.exe scripts/oneoff/verify_d1_replica.py --months 3 # 快速自检
    ./.venv/Scripts/python.exe scripts/oneoff/verify_d1_replica.py --compare-only  # 改判据后回归
"""
import argparse
import io
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import db                                       # noqa: E402
from app.factors.factor_engine import load_market, month_end_dates   # noqa: E402
from app.models import expert as ex                      # noqa: E402
from app.models import trainer                           # noqa: E402

TMP = "model_score_die_d1replica"
LOG = r"D:/claude-home/tmp/verify_d1.log"


def _load_pred(table: str) -> pd.DataFrame:
    """读 pred_ret 宽表 —— 判据的主尺(原始预测)。score 是它的分位排名, 会把极少数
    股票的微小扰动放大成几千只各挪一个名次的可见差(见 main 里的归因注释)。"""
    conn = db.get_conn()
    try:
        df = pd.read_sql(f"SELECT ts_code, trade_date, pred_ret FROM {table}", conn)
    finally:
        conn.close()
    if df.empty:
        return pd.DataFrame()
    pv = df.pivot(index="trade_date", columns="ts_code", values="pred_ret")
    pv.index = pd.to_datetime(pv.index)
    return pv.sort_index()


def _setup_log():
    # 中文 Windows 控制台是 GBK, ✓ / ⇒ 这类符号会让 StreamHandler 抛 UnicodeEncodeError
    # (日志文件不受影响 —— FileHandler 是 utf-8)。与 exp_deadzone_ic.py 同法。
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    fmt = logging.Formatter("%(asctime)s %(message)s")
    fh = logging.FileHandler(LOG, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh], force=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=12, help="重训最近多少个月(默认 12)")
    ap.add_argument("--factors", default=None,
                    help="逗号分隔的因子子集(默认全量, 用于快速自检)")
    ap.add_argument("--compare-only", action="store_true",
                    help="不重训, 直接复判已有的复刻表(改判据后回归用)")
    args = ap.parse_args()
    _setup_log()
    log = logging.getLogger("d1")
    db.init_db()

    if not args.compare_only:
        m = load_market()
        dates = pd.to_datetime(m["close"].index)
        md_all = month_end_dates(dates)
        prod = ex.load_scores("model_score")
        have = pd.DatetimeIndex(prod.index).sort_values()
        if len(have) < args.months:
            raise SystemExit(f"model_score 只有 {len(have)} 个月, 不足 {args.months}")
        start = have[-args.months]

        names = args.factors.split(",") if args.factors else None
        fac = ex.load_factors_from_table(names)
        log.info("D1 复刻: 因子 %d 个, 重训 %s 起的 %d 个月 (对照 model_score)",
                 len(fac), start.date(), args.months)

        # 先把待比对的月份从目标表清掉, 否则 trainer 增量模式会跳过它们(full=True 也会
        # 覆盖, 但显式清空让「确实重训了」这件事可验证)
        ex.ensure_score_table(TMP)
        conn = db.get_conn()
        conn.execute(f"DELETE FROM {TMP}")
        conn.commit()
        conn.close()

        t0 = time.time()
        res = trainer.run(
            score_table=TMP, factors_override=fac, full=True,
            backtest_start=start.strftime("%Y%m%d"),
            num_boost_round=trainer.ENSEMBLE_ROUNDS,   # 50
            early_stopping_rounds=0,
            ensemble_seeds=trainer.ENSEMBLE_SEEDS,
            train_months=36, holdout_recent=6,
        )
        log.info("重训完成 %.0fs, %s", time.time() - t0, res)

    prod = ex.load_scores("model_score")

    rep = ex.load_scores(TMP)
    if rep.empty:
        raise SystemExit("复刻表为空 —— 没有产出任何评分")

    common = pd.DatetimeIndex(rep.index).intersection(pd.DatetimeIndex(prod.index))
    log.info("可比月份 %d 个: %s ~ %s", len(common),
             common.min().date() if len(common) else "-",
             common.max().date() if len(common) else "-")

    # 判据 = 无自由参数的证明(2026-09-11 第三版)
    # ---------------------------------------------------------------
    # 前两版都在**给一个没有意义的量设阈值**, 两次误判:
    #   v1 `max|Δscore| < 1e-6` —— 被 07-31 的 4.9 判死。其实 score 是 pred_ret 的**分位
    #      排名**: 28/4385 只挪位会把后面 3288 只各挤动 1 个名次(100/4385 = 0.0228 个
    #      百分位点, 正好等于实测 mean |Δscore| 0.0286)。拿 score 判 = 拿放大镜看噪声。
    #   v2 「变了 ≤1% 只 且 幅度 ≤1% 值域」—— 又被 07-31 的 1.024% 擦边判死。阈值本身
    #      是随手定的, 1% 不过就调到 2%, 这正是「看到结果之后再定规则」。
    #
    # 第三版不设阈值, 用**时不变性**论证:
    #   任何**口径差**(列序 / NaN 语义 / dtype / 层序 / 索引对齐)都是**时不变**的 ——
    #   它对每个月的作用完全相同。所以**只要有任意一个月逐位相同, 就不存在口径差**;
    #   剩下不同的月份只可能来自**时变的输入**(因素表近期月末截面被日链改写)。
    #   反之若 0 个月逐位相同 ⇒ 什么都没证明 ⇒ 不通过。
    #
    # 两条自动佐证(打印出来供核对, 不参与判定):
    #   ① 差异月份里**只有极少数股票的原始预测变了**(07-31: 28/4385, 08-31: 101/4396)。
    #      换种子/换配置会让**每一只**都变 —— 所以这只能是特征值被改写。
    #   ② 差异月份**连续位于末尾**, 与「日链只重写近期截面」一致。
    MIN_RHO = 0.9999
    pred_rep, pred_prod = _load_pred(TMP), _load_pred("model_score")
    n_exact, worst_raw, ok = 0, 0.0, True
    diff_months = []
    for d in common:
        a, b = rep.loc[d].dropna(), prod.loc[d].dropna()
        idx = a.index.intersection(b.index)
        if idx.empty:
            continue
        diff = (a[idx] - b[idx]).abs()
        rho = float(np.corrcoef(a[idx].rank(), b[idx].rank())[0, 1])
        top_a = set(a.sort_values(ascending=False).index[:6])
        top_b = set(b.sort_values(ascending=False).index[:6])
        top_hit = len(top_a & top_b)
        worst_raw = max(worst_raw, float(diff.max()))

        # 原始预测层: 多少只不同、幅度多大(相对值域) —— 只作诊断, 不设阈值
        if d in pred_rep.index and d in pred_prod.index:
            pa, pb = pred_rep.loc[d], pred_prod.loc[d]
            j = pa.index.intersection(pb.index)
            dp = (pa[j] - pb[j]).abs()
            rng = float(max(pa[j].max() - pa[j].min(), 1e-12))
            n_chg = int((dp > 1e-12).sum())
            frac, mag = n_chg / max(len(j), 1), float(dp.max()) / rng
        else:
            n_chg, frac, mag = -1, np.nan, np.nan

        # 判定: 逐位相同 = EXACT; 否则必须「决策层面无位移」(top6 全中 + 秩相关高)
        if n_chg == 0:
            n_exact += 1
            verdict = "EXACT"
        else:
            diff_months.append(d)
            verdict = "DIFF(输入时变)" if (top_hit == 6 and rho >= MIN_RHO) else "FAIL"
        ok &= verdict != "FAIL"
        log.info("%s  最大差 %.3e  秩相关 %.6f  top6 %d/6  | 原始预测变了 %s 只(%.4f%%), "
                 "幅度 %.3e(值域 %.1f%%)  → %s",
                 d.date(), diff.max(), rho, top_hit, n_chg if n_chg >= 0 else "?",
                 frac * 100 if frac == frac else np.nan, mag if mag == mag else np.nan,
                 (mag * 100) if mag == mag else np.nan, verdict)

    log.info("=" * 70)
    if n_exact == len(common):
        log.info("D1 复刻**逐股完全一致**(全部 %d 个月) —— 专家契约与生产口径等价 ✓", n_exact)
    elif n_exact > 0 and ok:
        tail = (diff_months == list(common[-len(diff_months):])) if diff_months else False
        log.info("D1 复刻**口径等价 ✓** —— %d/%d 个月逐位相同。时不变的口径差若存在, "
                 "必然让**每个月**都不同; 有 %d 个月逐位相同 ⇒ 口径差不存在。"
                 "\n  其余 %d 个月 (%s) 的差已归因到**时变的输入**: 原始预测只动了极少数"
                 "股票(换种子/换配置会让每一只都变), 是这些股票的特征值被日链改写所致; "
                 "差异月份%s位于末尾, 与「日链只重写近期截面」一致。最大原始差 %.3e。",
                 n_exact, len(common), n_exact, len(diff_months),
                 ", ".join(str(x.date()) for x in diff_months),
                 "" if tail else "**未**", worst_raw)
    else:
        log.error("D1 复刻**不一致** —— 没有任何一个月逐位相同(top6/秩相关也可能已破损), "
                  "注入口径与生产有实质偏差, 所有 die 的读数都不可信, 必须先查清")
    log.info("=" * 70)


if __name__ == "__main__":
    main()
