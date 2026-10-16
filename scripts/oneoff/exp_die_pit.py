"""缺陷修复实验: 把行业中性化从「当前快照」换成「申万 PIT 时点行业」

## 要修的是什么(不是假设, 是已确认的缺陷)

`factor_engine.neutralize()` 与 `trainer.run()` 的标签中性化都用
`industry_map()` → `stock_basic.industry`。那是 **Tushare 的当前快照**:
  · 2021 年从申万计算机划到传媒的股票, **全历史**都被拿 2026 年的行业去做减法
  · 333 只 industry 为 NULL 的股票被 `fillna("未知")` 凑成**同一个行业**互减
⇒ 2017 年以来每一次行业中性化(含**训练标签**的中性化)都含前视。

库里的替代品**早已建好但没接进生产**: `sw_member` 15,910 行全部带 `in_date`/`out_date`,
`holder_factors.sw_industry_map_pit()` 也写好并注明「调用方应把 NaN 行排除出中性化而不是
填成未知」。本脚本把它接上, 作为**独立 die** 检验 —— 不直接改生产(`factor` 表与
`model_score` 一字不动), 因为换 PIT 会改变**全部存量因子值**, 是配套全量重训与新基线的
独立动作(`factor_engine.compute_all` docstring 已如此声明)。

=============================== 预注册(写于跑之前) ===============================
**⚠️ 三臂分解(覆盖度探针在开跑前逼出来的, 不是看到 KGD 之后加的)** —— 探针实测:
旧口径 `stock_basic.industry` 有 **111 个**分类(Tushare 细分行业, 每类均 ~40 只),
`sw_industry_map_pit` 是**申万一级 31 个**(每类均 ~150 只)。⇒ 「换 PIT」这一个动作里
混了**两件不同的事**: ①消前视 ②行业粒度 111→31。只跑一臂的话, 变差也说不清是哪个。
故按同一份预注册跑三臂, 逐对隔离:

  A 生产(111 细分 + 当前快照)  = 已有的 `model_score`, **不重跑**
  B 申万一级 + 当前快照        → `A→B` 隔离**粒度/分类体系**
  C 申万一级 + PIT 时点        → `B→C` 隔离**前视**

B 臂的构造: 取 PIT 映射的**最后一行**广播到全部日期 —— 最后一行就是「今天的成员名单」,
即当前快照的申万一级。B 与 C 同源同粒度, 差别**只有**是否随日期变化。

**唯一变量 = 行业口径**。其余全部逐字不动:
  · 同一套因子: `compute_price_factors` + `compute_fina_factors` + `compute_lhb_factors`
    (与 `compute_all(include_new=False)` 同一组; 不并入资金类/股东类新因子)
  · 同一套中性化步骤: 去极值(2%) → 行业去均值 → z-score(逐行截面, 与 `fe.neutralize` 同序)
  · 同一套超参/窗口/标签: 50 轮 × 4 种子, train_months=36, holdout_recent=6,
    backtest_start=2017-01-01, bse_enabled=False
  · 只把「行业标签」从当前快照换成 `sw_industry_map_pit(md, "L1")`

**判据 = `expert.kgd(n_tests=1)`**, 五条沿用 `expert.py` 模块级常量。**这是缺陷修复,
不是调参**: 若 PIT 让读数变差, 那正说明现在的数字里有前视成分, 必须如实记录, 不得
为了让结果好看而回退到这个实验之外去调别的旋钮。

**必须同时报告的两个已知后果**(否则读数无法解释):
  1. **覆盖度会下降**: 无申万归属的股票(次新/退市/北交)在 PIT 口径下标签为 NaN、因子为
     NaN ⇒ 不进训练也不被打分。旧口径下它们被凑进「未知」行业、照常参与。
  2. 若 KGD 变差, 无法区分「PIT 更正确但信号确实弱」与「覆盖度下降本身」——
     故脚本额外报**限制在同一覆盖域上的 model_score 对照**(`kgd` 的 restrict_to 已做)。

用法:
    ./.venv/Scripts/python.exe scripts/oneoff/exp_die_pit.py --probe    # 只量覆盖度(快)
    ./.venv/Scripts/python.exe scripts/oneoff/exp_die_pit.py            # 三臂全流程
    ./.venv/Scripts/python.exe scripts/oneoff/exp_die_pit.py --arm pit  # 只跑 C 臂
    ./.venv/Scripts/python.exe scripts/oneoff/exp_die_pit.py --compare-only
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
from app.factors.holder_factors import sw_industry_map_pit   # noqa: E402
from app.models import expert as ex                      # noqa: E402
from app.models import trainer                           # noqa: E402

TABLE = "model_score_die_pit"
NAME = "die_pit"
LOG = r"D:/claude-home/tmp/die_pit.log"


def _setup_log():
    fmt = logging.Formatter("%(asctime)s %(message)s")
    fh = logging.FileHandler(LOG, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh], force=True)


def raw_factors(m: dict, md: pd.DatetimeIndex, dates: pd.DatetimeIndex) -> dict:
    """与 `compute_all(include_new=False)` 同一组**未中性化**因子(逐字复刻其顺序与索引处理)。"""
    price_f = fe.compute_price_factors(m)
    for name in price_f:
        price_f[name].index = dates
    fina_f = fe.compute_fina_factors(md.strftime("%Y%m%d").tolist(),
                                     calendar=dates.strftime("%Y%m%d"))
    for name in fina_f:
        fina_f[name].index = pd.to_datetime(fina_f[name].index)
    lhb_f = fe.compute_lhb_factors(dates)
    out = {**price_f, **fina_f, **lhb_f}
    log = logging.getLogger("pit")
    log.info("原始因子 %d 个(baseline 品种): %s", len(out), sorted(out))
    return out


def neutralize_pit(df: pd.DataFrame, ind_pit: pd.DataFrame, q: float = 0.02) -> pd.DataFrame:
    """`fe.neutralize` 的**时点行业**版: 去极值(2%) → 逐期行业去均值 → z-score。

    与 `fe.neutralize` 的差别只有一处: 行业标签按日期取(`ind_pit.loc[d]`), 而非一个
    固定的 Series。三步的顺序、分位、除零保护全部照抄 —— 否则就不是「唯一变量」了。
    逐期算即可: 中性化是纯**截面(逐行)**操作, 故只在 `md` 这些行上算与全量算再取
    这些行完全等价(本函数只被喂月末截面)。
    """
    df = df.replace([np.inf, -np.inf], np.nan)
    lo = df.quantile(q, axis=1)
    hi = df.quantile(1 - q, axis=1)
    df = df.clip(lower=lo, upper=hi, axis=0)
    out = df.copy()
    n_orphan = 0
    for d in df.index:
        if d not in ind_pit.index:
            continue
        g = ind_pit.loc[d].reindex(df.columns)
        row = df.loc[d]
        n_orphan += int((row.notna() & g.isna()).sum())
        out.loc[d] = row - row.groupby(g, sort=False).transform("mean")
    out = out.sub(out.mean(axis=1), axis=0).div(out.std(axis=1), axis=0)
    out = out.replace([np.inf, -np.inf], np.nan)
    return out, n_orphan


def report(rec: dict, log, n_orphan: int, cov: str) -> None:
    log.info("=" * 78)
    log.info("KGD 读数 —— %s (表 %s)", rec.get("name"), rec.get("table"))
    if "reason" in rec:
        log.info("  ⚠ 无读数: %s", rec["reason"])
        log.info("=" * 78)
        return
    log.info("  月份数 %s | 平均截面股票数 %.0f", rec.get("n_months"),
             rec.get("avg_names", float("nan")))
    log.info("  %-8s%10s%9s%8s%10s%7s", "区段", "head/月", "t", "命中", "top6/月", "期数")
    for z in ("DEAD", "ACTIVE"):
        log.info(f"  {z:<8}{rec.get(f'{z}_head', float('nan')):>+9.4f}"
                 f"{rec.get(f'{z}_t', float('nan')):>+9.2f}"
                 f"{rec.get(f'{z}_hit', float('nan')):>8.3f}"
                 f"{rec.get(f'{z}_top6', float('nan')):>+10.4f}"
                 f"{rec.get(f'{z}_n', 0):>7}")
    log.info(f"  {'FULL':<8}{rec.get('FULL_head', float('nan')):>+9.4f}"
             f"{rec.get('FULL_t', float('nan')):>+9.2f}")
    log.info("  与基线 model_score 秩相关 %+.4f", rec.get("corr_with_baseline", float("nan")))
    log.info("  基线**同覆盖域**对照: DEAD %+.4f / ACTIVE %+.4f (平均截面 %.0f 只)",
             rec.get("baseline_DEAD_head", float("nan")),
             rec.get("baseline_ACTIVE_head", float("nan")),
             rec.get("baseline_avg_names", float("nan")))
    log.info("  p(Bonferroni, n=1) = %.4f | 同号 %s | 秩相关合格 %s | **PASS = %s**",
             rec.get("p_adj", float("nan")), rec.get("same_sign"),
             rec.get("corr_ok"), rec.get("PASS"))
    log.info("  覆盖度: %s; 因无行业归属被置 NaN 的截面样本共 %d 条", cov, n_orphan)
    log.info("=" * 78)


def snapshot_l1(ind_pit: pd.DataFrame, md: pd.DatetimeIndex) -> pd.DataFrame:
    """把 PIT 映射的**最后一行**广播到全部日期 = 「当前快照的申万一级」。

    与 `ind_pit` 同源、同粒度, 唯一差别是**不随日期变化** —— 这正是隔离「前视」所需的对照臂。
    注意末行只含**当前仍在册**的成员, 故在册期已结束的退市股在 B/C 两臂同为 NaN,
    这个覆盖度差异对 B→C 的对照是共同的、不构成混淆(B→C 只比较时变性)。
    """
    last = ind_pit.iloc[-1]
    return pd.DataFrame(np.tile(last.values, (len(md), 1)), index=md, columns=ind_pit.columns)


def run_arm(arm: str, m: dict, md: pd.DatetimeIndex, ind: pd.DataFrame,
            log, px_med: int) -> None:
    """训练一个臂并报 KGD。`ind` 同时用于因子中性化与标签中性化(必须同口径)。"""
    table = f"model_score_die_{arm}"
    raw = raw_factors(m, md, pd.to_datetime(m["close"].index))
    neu, n_orphan = {}, 0
    for k, v in raw.items():
        neu[k], n = neutralize_pit(v.loc[md], ind)
        n_orphan += n
    del raw
    names = float(np.median([neu[k].notna().sum(axis=1).median() for k in neu]))
    log.info("[臂 %s] 因子中位覆盖 %.0f 只/月(有价股票 %d 只/月, 即 %.1f%%), "
             "因无行业归属置 NaN %d 条", arm, names, px_med, 100 * names / px_med, n_orphan)

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
        train_months=36, holdout_recent=6, label_industry=ind)
    log.info("[臂 %s] 训练完成 %.0fs: %s", arm, time.time() - t0, res)
    del neu

    rec = ex.kgd(table, baseline="model_score", n_tests=1, market=m, label=arm)
    report(rec, log, n_orphan,
           f"因子中位覆盖 {names:.0f}/{px_med} 只/月 = {100 * names / px_med:.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="只量覆盖度, 不训练")
    ap.add_argument("--arm", choices=("pit", "snap"), default=None,
                    help="只跑单臂; 缺省两臂都跑")
    ap.add_argument("--compare-only", action="store_true", help="不重训, 只复判")
    args = ap.parse_args()
    _setup_log()
    log = logging.getLogger("pit")
    db.init_db()

    m = fe.load_market()                      # 全历史: 与 compute_all 同源
    dates = pd.to_datetime(m["close"].index)
    md = fe.month_end_dates(dates, today=datetime.date.today())
    md = md[md >= pd.Timestamp("2014-01-01")]
    log.info("行情 %s ~ %s, 月末截面 %d 个", dates[0].date(), dates[-1].date(), len(md))

    ind_pit = sw_industry_map_pit(md, "L1")
    ind_snap = snapshot_l1(ind_pit, md)
    log.info("PIT 行业映射 %s; 末行行业数 %d, 样例 %s", ind_pit.shape,
             ind_pit.iloc[-1].nunique(), sorted(ind_pit.iloc[-1].dropna().unique())[:4])

    # 覆盖率必须按**当月有价的股票**归一化 —— 直接数 NaN 会把「当时还没上市/早已退市」的
    # 列也算成无归属, 得出 2014 年 2901~3880 只的吓人数字(总列 5461, 那时在市的才 ~2500)。
    # 逐年报是因为: 训练窗是 36 个月, 若早年覆盖度明显更低, 早期测试月的分数会被训练窗
    # 质量而非「前视修复」拖累 —— 这个效应对 B/C 两臂共同存在, 但必须与读数同记。
    px = m["close"].loc[md].reindex(columns=ind_pit.columns).notna()
    n_px = px.sum(axis=1)
    for tag, indf in (("PIT  ", ind_pit), ("快照L1", ind_snap)):
        # 必须与 px 取交: 申万成员表里含**没有行情**的股票(停牌/退市), 只数 indf 的
        # 非空会把它们算进分子, 得出 108%/225% 这种 >100% 的假数。
        r = (indf.notna() & px).sum(axis=1) / n_px.replace(0, np.nan)
        log.info("[%s] 有行业归属占当月有价股票: 全期中位 %.1f%% | 逐年中位 %s", tag,
                 100 * r.median(),
                 " ".join(f"{y}:{100 * s.median():.0f}%" for y, s in r.groupby(r.index.year)))
    px_med = int(n_px.median())
    log.info("有价股票 %d 只/月(中位)", px_med)

    if args.probe:
        return

    if not args.compare_only:
        arms = (args.arm,) if args.arm else ("snap", "pit")
        for a in arms:
            run_arm(a, m, md, ind_snap if a == "snap" else ind_pit, log, px_med)
    else:
        for a in (args.arm,) if args.arm else ("snap", "pit"):
            rec = ex.kgd(f"model_score_die_{a}", baseline="model_score",
                         n_tests=1, market=m, label=a)
            report(rec, log, -1, "(复用上次)")

    log.info("=" * 78)
    log.info("分解读法: A(111 细分+快照, =model_score) → B(申万一级+快照) = **粒度效应**; "
             "B → C(申万一级+PIT) = **消前视效应**。两臂若同向且量级相当, "
             "则该变化主要来自分类体系而非时点; 反之亦然。")


if __name__ == "__main__":
    main()
