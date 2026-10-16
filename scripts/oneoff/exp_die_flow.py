"""阶段 2: 第一个异构 die —— 资金流家族(margin + moneyflow)

方案 §5 阶段 2: 「D2..Dk 数据 die: **阶段 0 通过的数据家族**, 各配一个 LightGBM」。
家族是否"通过"由阶段 0c 的**预注册**门禁 (`exp_deadzone_ic.py`) 决定, 本脚本只执行。

=============================== 预注册(写于跑之前) ===============================
口粮名单(家族级 —— 判据是「该家族有因子通过门禁」, 不是「逐因子挑通过者」;
名单在**看到 KGD 结果之前**写死在这里, 事后不得增删):

  margin 家族(6 个)     ← 通过: margin_net_share, DEAD head +0.64%/月, t=+4.19, p_adj=0.0005
  moneyflow 家族(6 个)  ← 通过: mf_main_sm_div,  DEAD head +0.56%/月, t=+4.31, p_adj=0.0003
  block 家族(3 个)      ← **排除**: 门禁无因子通过(block_cnt20 唯一显著读数为**负** t=-2.76;
                          block_discount / block_avg_size 截面太窄, 门禁无法评估)
  hk_*                  ← **排除**: 硬性不变量 #6(2024-08 覆盖度断崖, 研究专用)

判据 = `expert.kgd(n_tests=1)`, 五条全部沿用 `expert.py` 的模块级常量:
  ① DEAD head ≥ 0.5%/月   ② DEAD t ≥ 2.0   ③ DEAD/ACTIVE 同号
  ④ Bonferroni p_adj < 0.05   ⑤ 与 model_score 秩相关 < 0.95(否则是换皮, 无去相关价值)

**零事后自由度**: 因子集由上面两行决定, 模型超参/训练窗口/标签全部是生产默认
(50 轮 × 4 种子, train_months=36, holdout_recent=6, label_neutral=industry)。
唯一的变量是**口粮**, 这正是「异构 die」的定义。

=========================== 结果如何解释(同样写于跑之前) ===========================
预先承诺, 事后不得改判据、不得增删口粮:

  PASS            → flow die 是 KGD 合格品, 进阶段 3 与 D1 融合
  FAIL: head/t 不足 → 门禁说它的**因子**死区 head 有 +0.56~0.64%/月, 但 GBDT 学的是
                      **行业中性化后的前向收益**, 不是头部超额 —— 学目标与考核目标不一致
                      是这类失败的预设解释。此时看 fusion_diag: 融合若无增量, 数据路线
                      在本家族上判负。
  FAIL: 秩相关≥0.95 → **换皮**。单因子 |ρ| 只有 0.036 却被合成出与基线几乎同序的分数,
                      说明**标签结构主导了排序**, 口粮独立性传不到分数层。这是对
                      「数据异构 > 算法异构」论点最有价值的反例, 必须原样记录。
  FAIL: 同号不成立   → die 的信号随 regime 反号 —— 与基线同一种病, 无封装价值。

⚠️ **判据的已知盲区(2026-09-11 冒烟测试实测, 写在此处以防事后被当作"新发现")**:
`kgd` 的 head 用的是**前 10%** 分位, 而策略真正买的是 **top-6**。用 `model_score_tm36`
(≡ 生产模型) 做全链路冒烟时实测: DEAD head **+1.07%/月 (t=+5.36)** 但 DEAD top6 只有
**+0.41%/月**; ACTIVE 则是 head +0.88% / top6 **+3.19%**。⇒ 两者会严重背离, 且**方向
符合已知规律**(记忆: 全截面 IC/头部指标与 top-6 已实现盈亏反相关, 已独立确认 3 次)。

**预先承诺**: 判据本身**一个数字都不改**(改了就成"看到结果之后调阈值")。但——
**若 die 在 head 上通过而 DEAD_top6 ≤ 0, 我不得宣布它「KGD 合格」而不加限定**;
那种情况下的正确表述是「head 通过 / top6 不通过, 对 6 只组合不可用」, 并据此判定
**不得封装**。这是把判决收紧, 不是放松, 故可在跑之前定。

⚠️ 覆盖度不齐是**预期**而非缺陷: margin 只覆盖两融标的且逐年增长
(实测逐年中位覆盖 2014:667 → 2021:2024 → 2025:3439), mf 覆盖全市场
(2021:4367 → 2026:5188)。故 KGD 的**基线读数自动限制在 die 的覆盖域**上
(`kgd` 里的 `restrict_to=sc`) —— 不这么做会把「谁能打更多股票」错读成「谁更会选股」。

=============================== 与基线可比性的三条要求 ===============================
1. `load_market()` **不带 since** —— 带 since 会把训练窗静默截断, 与生产模型不同口径。
2. 中性化口径与 `factor` 表写入口径一致: `fe.neutralize`(去极值2% → 行业中性 → z-score)。
   ⚠️ 已知缺陷(方案 §8): 行业用的是 `stock_basic.industry` **当前快照、非 PIT**。
   **此处刻意不改** —— 改了就不是"唯一变量是口粮"的对照了。修 PIT 行业是独立的一次变更。
3. `backtest_start` 默认 "2017-01-01" ⇒ 评分月与 `model_score`(20170126~20260831, 116 期)
   逐月对齐。DEAD 区 40 期、ACTIVE 区 32 期因此**全覆盖**。

=============================== 数据窗口硬约束 ===============================
margin/moneyflow 原本只回填到 2020-09-01 ⇒ walk-forward 要 36 个月窗 ⇒ 打分只能从 ~2023-10
起 ⇒ DEAD 区只剩 3 期, KGD 读不出来。故先跑 `backfill_new_sources.py --start 20140101
--end 20200831 --skip-hk` 把两个表补到 2014-01(trainer 的 md_all 下界)。

用法:
    ./.venv/Scripts/python.exe scripts/oneoff/exp_die_flow.py --from 20250131   # 冒烟(快)
    ./.venv/Scripts/python.exe scripts/oneoff/exp_die_flow.py                  # 全窗
    ./.venv/Scripts/python.exe scripts/oneoff/exp_die_flow.py --compare-only   # 免重训复判
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

if hasattr(sys.stdout, "buffer"):     # 中文 Windows 控制台是 GBK
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import db                                       # noqa: E402
from app.factors import factor_engine as fe              # noqa: E402
from app.factors.flow_factors import compute_flow_factors  # noqa: E402
from app.models import expert as ex                      # noqa: E402
from app.models import trainer                           # noqa: E402

TABLE = "model_score_die_flow"
NAME = "die_flow"
LOG = r"D:/claude-home/tmp/die_flow.log"

# 预注册的口粮家族(见模块 docstring) —— 前缀即家族
FAMILIES = ("margin_", "mf_")


def _setup_log():
    fmt = logging.Formatter("%(asctime)s %(message)s")
    fh = logging.FileHandler(LOG, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh], force=True)


def build_factors(m: dict) -> dict:
    """按预注册家族名单构造 die 的口粮(已中性化)。"""
    dates_all = pd.to_datetime(m["close"].index)
    f_all = compute_flow_factors(m, dates_all, include_research=False)
    fam = {k: v for k, v in f_all.items() if k.startswith(FAMILIES)}
    dropped = sorted(set(f_all) - set(fam))
    logging.getLogger("die").info("口粮: %d 个 %s", len(fam), sorted(fam))
    logging.getLogger("die").info("按预注册排除: %s", dropped)
    if not fam:
        raise SystemExit("口粮为空 —— 检查 margin/moneyflow 表是否存在")

    conn = db.get_conn()
    try:
        ind = fe.industry_map(conn)
    finally:
        conn.close()
    return {k: fe.neutralize(v.reindex(index=dates_all), ind) for k, v in fam.items()}


def fusion_diag(partners: list, market: dict, log) -> None:
    """**非判据**诊断: die 对基线有没有**增量**?

    KGD 只回答「这个 die 单独好不好」。但一个 die 值不值得被封装进芯片, 取决于它
    在**基线之外**还带来什么 —— 两个 GBDT 在同一股票池、同一标签、同一时间窗上训练,
    哪怕口粮完全不同, 分数也会有相当高的相关性(共同的市场 beta 与标签结构)。所以
    「秩相关 < 0.95」通过 ≠ 「有增量」。

    这里预览阶段 3 的融合: 各 die 分数在**共同覆盖域**上取截面分位排名后等权平均,
    与基线在**同一覆盖域**上并列对照 —— 覆盖域不同会把「谁能打更多股票」错读成
    「谁更会选股」(expert.head_excess_at 的同一理由)。

    输出只是信息, 不参与 PASS/FAIL。判据仍以 expert.kgd 的预注册五条为准。
    """
    fwd = ex._forward_return(market)
    dates = pd.to_datetime(market["close"].index)
    md = fe.month_end_dates(dates, today=datetime.date.today())
    base = ex.load_scores("model_score")
    if base.empty:
        log.warning("fusion 诊断跳过: model_score 为空")
        return

    series, common = {}, None
    for t in ["model_score"] + list(partners):
        sc = ex.load_scores(t)
        if sc.empty:
            log.warning("fusion 诊断跳过: %s 为空", t)
            return
        common = sc.columns if common is None else common.intersection(sc.columns)
        series[t] = sc
    common = common.intersection(base.columns)

    # 只在「全部 die 与基线都有分」的股票上比较, 逐月
    panels = {}
    for t, sc in series.items():
        p = sc.reindex(columns=common).reindex(index=md)
        panels[t] = p
    mask = None
    for t, p in panels.items():
        m_ = p.notna()
        mask = m_ if mask is None else (mask & m_)

    def _zone_stats(wide):
        w = wide.where(mask)
        det = ex.daily_head_stats(w, fwd, md)
        out = {}
        for z, (a, b) in (("DEAD", ex.DEAD), ("ACTIVE", ex.ACTIVE)):
            sub = det[(det.index >= a) & (det.index <= b)]
            out[z] = (ex._stat(sub["head_excess"])["mean"],
                      ex._stat(sub["head_excess"])["t"],
                      ex._stat(sub["top6_excess"])["mean"])
        return out

    fused = sum(panels[t].where(mask).rank(axis=1, pct=True) for t in panels) / len(panels)
    log.info("---- 非判据诊断: 覆盖域对齐后的融合预览 ----")
    log.info("  共同覆盖域: %d 只, 逐月实际可比 %d 只",
             len(common), int(mask.sum(axis=1).median()))
    for label, wide in [("model_score(基线)", panels["model_score"]),
                        ("+" + "+".join(partners), fused)]:
        st = _zone_stats(wide)
        log.info("  %-22s DEAD head %+.4f (t=%+.2f, top6 %+.4f) | ACTIVE head %+.4f "
                 "(t=%+.2f, top6 %+.4f)", label,
                 st["DEAD"][0], st["DEAD"][1], st["DEAD"][2],
                 st["ACTIVE"][0], st["ACTIVE"][1], st["ACTIVE"][2])
    d = _zone_stats(fused)["DEAD"][0] - _zone_stats(panels["model_score"])["DEAD"][0]
    log.info("  ⇒ 融合 − 基线 的死区差 %+.4f/月 (**诊断, 非判据**)", d)


def style_confound_diag(table: str, market: dict, log) -> None:
    """**非判据**诊断: die 的分数是不是只是「小盘/低流动性」的代理?

    为什么必须查: margin 家族的 NaN 不是随机的 —— 「不是两融标的」本身就是小盘/次新的
    标记, LightGBM 会把缺失模式当成特征用。而对照标尺显示 size(-1.37%/月)与 illiq
    (+1.61%/月)在死区**各自就有 head**。若 die 的分数与 size/illiq 高度相关, 那它的
    死区超额可能只是把基线已有的风格暴露又买了一遍 —— 融合自然不会有增量。

    这里只报与存量因子的秩相关, 不作判据。
    """
    from app.factors.factor_engine import month_end_dates
    fwd = ex._forward_return(market)
    dates = pd.to_datetime(market["close"].index)
    md = month_end_dates(dates, today=datetime.date.today())
    sc = ex.load_scores(table)
    if sc.empty:
        return
    conn = db.get_conn()
    try:
        q = ("SELECT ts_code, trade_date, factor, value FROM factor "
             "WHERE factor IN ('size','illiq','vol_20','mom_20','rev_5','turnover_20')")
        df = pd.read_sql(q, conn)
    finally:
        conn.close()
    log.info("---- 非判据诊断: die 与存量风格因子的秩相关 ----")
    for fname, sub in df.groupby("factor"):
        pv = sub.pivot(index="trade_date", columns="ts_code", values="value")
        pv.index = pd.to_datetime(pv.index)
        log.info("  %-14s %+.4f", fname, ex.rank_corr_with(pv, sc, md))


def report(rec: dict, log) -> None:
    log.info("=" * 78)
    log.info("KGD 读数 —— %s (表 %s)", rec.get("name"), rec.get("table"))
    log.info("  月份数 %s | 平均截面股票数 %.0f", rec.get("n_months"),
             rec.get("avg_names", float("nan")))
    hdr = f"  {'区段':<8}{'head/月':>10}{'t':>9}{'命中':>8}{'top6/月':>10}{'期数':>7}"
    log.info(hdr)
    for z in ("DEAD", "ACTIVE"):
        log.info(f"  {z:<8}{rec.get(f'{z}_head', float('nan')):>+9.4f}"
                 f"{rec.get(f'{z}_t', float('nan')):>+9.2f}"
                 f"{rec.get(f'{z}_hit', float('nan')):>8.3f}"
                 f"{rec.get(f'{z}_top6', float('nan')):>+10.4f}"
                 f"{rec.get(f'{z}_n', 0):>7}")
    log.info(f"  {'FULL':<8}{rec.get('FULL_head', float('nan')):>+9.4f}"
             f"{rec.get('FULL_t', float('nan')):>+9.2f}{'':>8}{'':>10}")
    log.info("  与基线 model_score 秩相关 %+.4f   (换皮线 %.2f)",
             rec.get("corr_with_baseline", float("nan")), ex.KGD_MAX_CORR)
    log.info("  基线同集合对照(model_score 在 die 覆盖域上的读数): "
             "DEAD %+.4f / ACTIVE %+.4f (平均截面 %.0f 只)",
             rec.get("baseline_DEAD_head", float("nan")),
             rec.get("baseline_ACTIVE_head", float("nan")),
             rec.get("baseline_avg_names", float("nan")))
    log.info("  p(Bonferroni, n_tests=%s) = %.4f", rec.get("_n_tests"),
             rec.get("p_adj", float("nan")))
    # 敏感性(诚实性): 这个 die 的**口粮家族是用死区数据选出来的**(门禁在同一段数据上
    # 跑了 17 个因子)。预注册的 Bonferroni 分母只数 die 数, 故这里额外报「n_tests 要
    # 多大才会把这个 die 判死」—— 若一个很小的 n 就翻盘, 结论就不该被当成稳健的。
    p_raw = rec.get("p_adj", float("nan")) / max(1, rec.get("_n_tests", 1)) \
        if rec.get("p_adj") == rec.get("p_adj") else float("nan")
    n_kill = (int(np.ceil(ex.KGD_ALPHA / p_raw)) if p_raw == p_raw and p_raw > 0
              else float("inf"))
    log.info("  敏感性: 裸 p = %.6f ⇒ n_tests ≥ %s 时被判死(预注册值是 %s)",
             p_raw if p_raw == p_raw else float("nan"),
             n_kill if n_kill != float("inf") else "∞", rec.get("_n_tests"))
    log.info("  同号 %s | 秩相关合格 %s | **PASS = %s**",
             rec.get("same_sign"), rec.get("corr_ok"), rec.get("PASS"))
    log.info("=" * 78)


def main():
    from _guard import require_confirm
    require_confirm(__file__, "DELETE FROM {--table} 指定的打分表（表名来自命令行，可能是生产表）")
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default=None,
                    help="评分月起点(默认 20170101, 与 model_score 对齐)")
    ap.add_argument("--table", default=TABLE)
    ap.add_argument("--n-tests", type=int, default=1,
                    help="Bonferroni 分母 = 本批实际测过的 die 数(预注册: 1)")
    ap.add_argument("--compare-only", action="store_true", help="不重训, 只复判")
    ap.add_argument("--plumbing", action="store_true",
                    help="只构造口粮并报覆盖度, 不训练(验证管线与数据补齐进度)")
    args = ap.parse_args()
    _setup_log()
    log = logging.getLogger("die")
    db.init_db()

    m = fe.load_market()                      # 全历史: 带 since 会静默截断训练窗
    dates_all = pd.to_datetime(m["close"].index)
    md_all = fe.month_end_dates(dates_all, today=datetime.date.today())
    log.info("行情 %s ~ %s, 月末截面 %d 个 (trainer 下界 2014-01)",
             dates_all[0].date(), dates_all[-1].date(), len(md_all))

    if args.plumbing:
        fac = build_factors(m)
        cols = list(dates_all)
        for k in sorted(fac):
            v = fac[k]
            cov = v.notna().sum(axis=1)
            # 逐年覆盖: 补数据之前 mf_* 只从 2020-09 起, 一眼看出补齐进度
            yr = cov.groupby(cov.index.year).median().astype(int)
            log.info("%-22s 首个非空 %s | 逐年中位覆盖 %s", k,
                     v.dropna(how="all").index.min().date() if not v.dropna(how="all").empty else "-",
                     " ".join(f"{y}:{n}" for y, n in yr.items()))
        log.info("全部 %d 个口粮因子构造完毕(未训练)", len(fac))
        return

    if not args.compare_only:
        fac = build_factors(m)
        have = ex.load_scores("model_score")
        if have.empty:
            raise SystemExit("model_score 为空")
        log.info("基线 model_score %d 期 (%s ~ %s)", len(have),
                 pd.DatetimeIndex(have.index).min().date(),
                 pd.DatetimeIndex(have.index).max().date())

        ex.ensure_score_table(args.table)
        conn = db.get_conn()
        try:
            conn.execute(f"DELETE FROM {args.table}")
            conn.commit()
        finally:
            conn.close()

        t0 = time.time()
        res = ex.run_expert(NAME, fac, table=args.table, full=True,
                            backtest_start=args.start or "2017-01-01",
                            num_boost_round=trainer.ENSEMBLE_ROUNDS,
                            early_stopping_rounds=0,
                            ensemble_seeds=trainer.ENSEMBLE_SEEDS,
                            train_months=36, holdout_recent=6)
        log.info("训练完成 %.0fs: %s", time.time() - t0, res)

    rec = ex.kgd(args.table, baseline="model_score", n_tests=args.n_tests,
                 market=m, label=NAME)
    rec["_n_tests"] = args.n_tests
    report(rec, log)
    fusion_diag([args.table], m, log)
    style_confound_diag(args.table, m, log)

    # 分数分布体检: 0 分位坍缩会让 KGD 的 top-6 完全由并列决定
    sc = ex.load_scores(args.table)
    if not sc.empty:
        per = sc.reindex(columns=sc.columns)
        log.info("分数唯一值/月: 中位 %.0f, 最小 %.0f",
                 per.nunique(axis=1).median(), per.nunique(axis=1).min())
        log.info("每月被打分股票数: 中位 %.0f, 范围 %d~%d",
                 sc.notna().sum(axis=1).median(),
                 sc.notna().sum(axis=1).min(), sc.notna().sum(axis=1).max())


if __name__ == "__main__":
    main()
