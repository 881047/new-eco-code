"""阶段 0c: 新数据家族「死区体检」—— 芯片式架构的硬门禁

**为什么是这个脚本**: 投入架构之前, 必须先知道新数据在**死区**里到底有没有信号。
死区 = 2020-09~2023-12 那 39 个月 —— 现行模型在此**超额为负**(-3.23%/年),
是本项目唯一还有空间的地块。一个因子若只在活跃区(2024-01~2026-08, 24 个月)有信号,
那它无法被验证: 现行模型的全部 edge 已经都在那 24 个月里了。

**⚠️ 判据不是 IC。** 本项目已三次独立复现「全截面 IC 与 top-6 实盘收益**反相关**」,
记忆 `new-money-project` 明文规定 IC 不得用于评估或选择策略。原因: 策略只买 6 只,
在意的是**分布头部**, 而全截面 IC 由中部几千只股票主导。故:
    · 主判据 = `head_excess` —— 因子排名**前十分位**的前向收益 − 全市场均值
    · 参考列 = `ic_full`(仅用于与文献对照, **不参与准入判定**)
    · 另报 `top6_excess`(策略的真实持仓规模)供读数, 但样本太小不作为判据

**预注册规则(在跑之前写死, 不得看到结果后修改)** —— 见 GATE_* 常量:
    1. 死区 `head_excess` 与活跃区**同号**(方向不能反转)
    2. 死区 `|head_excess|` ≥ GATE_MIN_HEAD(默认 0.5%/月)
    3. 死区 t ≥ GATE_MIN_T(默认 2.0)
    4. Bonferroni: 校正后 p < 0.05(alpha / 测试因子数)
    **四条全过才算通过。** 全不通过 ⇒ 数据路线证伪, 结论写入 config 的已证伪登记处。

用法:
    python scripts/oneoff/exp_deadzone_ic.py                 # 全量
    python scripts/oneoff/exp_deadzone_ic.py --no-hk         # 排除研究专用的 hk_*
    python scripts/oneoff/exp_deadzone_ic.py --include-control  # 附带现有因子作标尺
日志: D:/claude-home/tmp/deadzone_ic.log
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

from app import db                                    # noqa: E402
from app.factors import factor_engine as fe           # noqa: E402
from app.factors.flow_factors import (compute_broker_factors,   # noqa: E402
                                      compute_top10_holder_factors,
                                      compute_flow_factors,
                                      compute_report_rc_factors, analyst_ready,
                                      holder_ready)
from app.factors.holder_factors import compute_holder_factors_safe  # noqa: E402

# ==========================================================================
# 预注册门禁规则 (2026-09-11 写死于跑之前 —— 改这里等于改判据, 必须在看结果前)
# ==========================================================================
GATE_MIN_HEAD = 0.005   # 死区头部月超额下界: 0.5%/月
GATE_MIN_T = 2.0        # 死区 t 下界
GATE_ALPHA = 0.05       # 显著性水平 (Bonferroni 分母 = 实际测试因子数)
GATE_REQUIRE_SAME_SIGN = True   # 要求死区与活跃区同号

# 分区(按月末评分日)
DEAD = ("2020-09-01", "2023-12-31")
ACTIVE = ("2024-01-01", "2026-08-31")

# 标尺: 现有因子里几个有代表性的(仅 --include-control 时加载)。
# 用途不是比较优劣, 而是给出「在这个测量口径下, 一个已知的存量因子长什么样」的刻度。
CONTROL_FACTORS = ["rev_5", "mom_20", "vol_20", "illiq", "size", "turnover_20"]

log = logging.getLogger("deadzone")


def _setup_log():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(message)s")
    fh = logging.FileHandler(r"D:/claude-home/tmp/deadzone_ic.log", encoding="utf-8")
    fh.setFormatter(fmt)
    # 中文 Windows 控制台默认 GBK, 遇到 "⇒" 会抛 UnicodeEncodeError 并打一整段
    # logging 内部 traceback(2026-09-11 空跑实测) —— 包一层 utf-8/replace 的流
    if hasattr(sys.stdout, "buffer"):
        stream = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    else:
        stream = sys.stdout
    sh = logging.StreamHandler(stream)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)


def forward_return(m: dict, horizon: int = 21) -> pd.DataFrame:
    """前向 horizon 交易日收益(后复权), index=日期, columns=ts_code。
    最后 horizon 天无前向收益 → NaN(不是 0), 参与统计时自动被 dropna 排除。"""
    ac = m["close"] * m["adj"]   # 注意 load_market 的键是 "adj"(不是 "adj_factor")
    return ac.shift(-horizon) / ac - 1.0


def zone_of(dates: pd.DatetimeIndex) -> np.ndarray:
    """每个截面日属于 DEAD / ACTIVE / OTHER"""
    d = pd.Series(dates, index=dates)
    z = np.where((d >= DEAD[0]) & (d <= DEAD[1]), "DEAD",
                 np.where((d >= ACTIVE[0]) & (d <= ACTIVE[1]), "ACTIVE", "OTHER"))
    return z


def evaluate(factor: pd.DataFrame, fwd: pd.DataFrame, dates: pd.DatetimeIndex,
             zones: np.ndarray, top_frac: float = 0.10):
    """逐截面算: 头部超额 / 多空价差 / 全截面 IC / top6 超额。返回按月明细。"""
    rows = []
    for i, d in enumerate(dates):
        if zones[i] == "OTHER":
            continue
        if d not in factor.index or d not in fwd.index:
            continue
        f = factor.loc[d].dropna()
        r = fwd.loc[d].dropna()
        common = f.index.intersection(r.index)
        if len(common) < 200:          # 截面太小(早期月份)不出读数
            continue
        f, r = f[common], r[common]
        n = len(f)
        k = max(1, int(n * top_frac))
        order = f.sort_values(ascending=False).index
        mean_all = r.mean()
        top = r[order[:k]]
        head_excess = top.mean() - mean_all
        ls = top.mean() - r[order[-k:]].mean()
        ic = f.corr(r, method="spearman")
        t6 = r[order[:6]]
        rows.append({
            "date": d, "zone": zones[i], "n": n,
            "head_excess": head_excess, "ls_spread": ls, "ic_full": ic,
            "top6_excess": t6.mean() - mean_all,
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("date")


def _stat(s: pd.Series):
    """均值 / t / 命中率。t 用朴素 t = mean/(sd/sqrt(n)) —— 月度 IC 存在自相关,
    真实 t 会比此值**略小**, 故门槛已按保守方向设置(见 GATE_MIN_T)。"""
    s = s.dropna()
    if len(s) < 3:
        return np.nan, np.nan, np.nan, len(s)
    sd = s.std(ddof=1)
    t = s.mean() / (sd / np.sqrt(len(s))) if sd > 0 else np.nan
    return s.mean(), t, (s > 0).mean(), len(s)


def pair_corr(a: pd.DataFrame, b: pd.DataFrame, md: pd.DatetimeIndex) -> float:
    """平均逐截面 Spearman(输入须为**已按截面 rank 过**的矩阵)。

    这是「去相关」论点的直接测量: 芯片架构要靠多个 die 吃不同口粮来降低误差相关,
    若一个新因子与存量因子平均秩相关 0.9, 它带来的新信息就是零, 建 die 也是白建。
    """
    cs = []
    for d in md:
        if d not in a.index or d not in b.index:
            continue
        x, y = a.loc[d], b.loc[d]
        m = x.notna() & y.notna()
        if int(m.sum()) < 200:
            continue
        xv = x[m].to_numpy(dtype=float)
        yv = y[m].to_numpy(dtype=float)
        if xv.std() == 0 or yv.std() == 0:
            continue
        cs.append(float(np.corrcoef(xv, yv)[0, 1]))
    return float(np.mean(cs)) if cs else np.nan


def summarize(name: str, det: pd.DataFrame, n_tests: int) -> dict:
    """按分区汇总 + 门禁判定"""
    rec = {"factor": name}
    for zone in ("DEAD", "ACTIVE"):
        sub = det[det["zone"] == zone]
        m, t, hit, n = _stat(sub["head_excess"])
        rec[f"{zone}_head"] = m
        rec[f"{zone}_head_t"] = t
        rec[f"{zone}_hit"] = hit
        rec[f"{zone}_n"] = n
        mi, ti, _, _ = _stat(sub["ic_full"])
        rec[f"{zone}_ic"] = mi
        rec[f"{zone}_top6"] = _stat(sub["top6_excess"])[0]
        rec[f"{zone}_ls"] = _stat(sub["ls_spread"])[0]
    # Bonferroni: 双尾 p 的粗略换算(用 |t| 查正态近似), 再与 alpha/n_tests 比
    from math import erfc, sqrt
    t_d = rec.get("DEAD_head_t", np.nan)
    if t_d == t_d and t_d != 0:
        p = erfc(abs(t_d) / sqrt(2))
        rec["DEAD_p_adj"] = min(1.0, p * max(1, n_tests))
    else:
        rec["DEAD_p_adj"] = np.nan
    # 门禁
    hd, ha = rec.get("DEAD_head", np.nan), rec.get("ACTIVE_head", np.nan)
    same = (GATE_REQUIRE_SAME_SIGN and hd == hd and ha == ha and np.sign(hd) == np.sign(ha))
    rec["PASS"] = bool(
        same and hd == hd and abs(hd) >= GATE_MIN_HEAD
        and t_d == t_d and t_d >= GATE_MIN_T
        and rec["DEAD_p_adj"] == rec["DEAD_p_adj"] and rec["DEAD_p_adj"] < GATE_ALPHA
    )
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-hk", action="store_true", help="排除研究专用的 hk_* 因子")
    ap.add_argument("--include-control", action="store_true", help="附带现有因子作标尺")
    ap.add_argument("--no-corr", action="store_true", help="跳过去相关体检(省时)")
    ap.add_argument("--since", default="2019-01-01", help="起始截面(默认 2019-01, 死区前留足)")
    args = ap.parse_args()
    _setup_log()
    db.init_db()

    log.info("=" * 78)
    log.info("阶段 0c 死区体检 | 死区 %s~%s / 活跃区 %s~%s", *DEAD, *ACTIVE)
    log.info("预注册门禁: 同号=%s | 死区|head|>=%.3f | 死区t>=%.1f | Bonferroni p<%.2f",
             GATE_REQUIRE_SAME_SIGN, GATE_MIN_HEAD, GATE_MIN_T, GATE_ALPHA)
    log.info("=" * 78)

    # 只加载评估窗所需的行情: load_market() 不带 since 会把 2014 起全部日频行
    # pivot 进内存(约 1.5GB), 而本脚本只评估 --since 之后的月末截面。留 400 自然日
    # 缓冲: 新因子里最长的滚动窗是 20 交易日, 400 日远超所需, 且**不改变任何被评估
    # 截面的取值**(中性化是纯截面的, 逐日滚动窗在缓冲区内已预热完毕)。
    mkt_since = (pd.Timestamp(args.since) - pd.Timedelta(days=400)).strftime("%Y%m%d")
    m = fe.load_market(since=mkt_since)
    dates_all = pd.to_datetime(m["close"].index)
    md = fe.month_end_dates(dates_all)
    md = md[md >= pd.Timestamp(args.since)]
    log.info("月末截面: %d 个 (%s ~ %s)", len(md), md[0].date(), md[-1].date())
    zones = zone_of(md)
    log.info("分区: DEAD %d 期 / ACTIVE %d 期 / OTHER %d 期",
             (zones == "DEAD").sum(), (zones == "ACTIVE").sum(), (zones == "OTHER").sum())

    fwd = forward_return(m, 21)

    conn = db.get_conn()
    ind = fe.industry_map(conn)
    conn.close()

    # ---- 新因子(记忆里口径: 与生产一致的中性化, 结果才可迁移) ----
    new_f = {}
    new_f.update(compute_flow_factors(m, dates_all, include_research=not args.no_hk))
    new_f.update(compute_holder_factors_safe(dates_all))
    # 2026-09-13 新增第 11 族(券商月度金股, 研究专用): 与其余族走**同一套**预注册门禁,
    # 且计入 Bonferroni 的测试数。PIT 口径见 compute_broker_factors 的 docstring。
    new_f.update(compute_broker_factors(dates_all))
    # 第 12 族(分析师研报预测, 2026-09-13 起接入): **补库没完成就不许纳入判决** ——
    # 半张表跑出来的"覆盖不足 ⇒ 无效"是假结论, 会污染台账(见 report_rc_ready 的 docstring)。
    _ok, _why, _src = analyst_ready()
    if _ok:
        new_f.update(compute_report_rc_factors(m, dates_all, source=_src))
    else:
        log.warning("跳过第 12 族(分析师研报预测): %s", _why)
    # 第 13 族(十大流通股东, 持仓结构类): 同样先过完成度闸门
    _h_ok, _h_why = holder_ready()
    if _h_ok:
        new_f.update(compute_top10_holder_factors(m, dates_all))
    else:
        log.warning("跳过第 13 族(十大流通股东): %s", _h_why)

    if args.include_control:
        conn = db.get_conn()
        q = ("SELECT ts_code, trade_date, factor, value FROM factor WHERE factor IN (%s)"
             % ",".join("?" * len(CONTROL_FACTORS)))
        ctl = pd.read_sql(q, conn, params=CONTROL_FACTORS)
        conn.close()
        for fname, sub in ctl.groupby("factor"):
            pv = sub.pivot(index="trade_date", columns="ts_code", values="value")
            pv.index = pd.to_datetime(pv.index)
            new_f[f"CTL_{fname}"] = pv.reindex(index=dates_all)
        log.info("已加载对照因子 %d 个(仅标尺, 不参与门禁)", len(CONTROL_FACTORS))

    # ---- 逐因子体检 ----
    n_tests = sum(1 for k in new_f if not (args.include_control and k.startswith("CTL_")))
    recs = []
    neu_all = {}
    for name, df in sorted(new_f.items()):
        df = df.reindex(index=dates_all)
        # 2026-09-11: 中性化与生产同口径 —— 去极值(2%)→行业中性→z-score
        neu = fe.neutralize(df, ind)
        if not name.startswith("CTL_"):
            neu_all[name] = neu
        det = evaluate(neu, fwd, md, zones)
        if det.empty:
            log.warning("%-24s 无有效截面, 跳过", name)
            continue
        rec = summarize(name, det, n_tests)
        rec["coverage"] = float(neu.notna().sum(axis=1).reindex(md).mean())
        recs.append(rec)
        log.info("%-24s DEAD head %+.4f (t=%+.2f, n=%d) | ACTIVE head %+.4f | "
                 "DEAD ic %+.4f | cov %.0f | %s",
                 name, rec["DEAD_head"], rec["DEAD_head_t"], rec["DEAD_n"],
                 rec["ACTIVE_head"], rec["DEAD_ic"], rec["coverage"],
                 "PASS" if rec["PASS"] else "")

    # ---- 去相关体检: 新因子 vs 存量因子 ----
    # 「芯片的多个 die 真正独立」靠的是口粮不同。这里把「不同」量出来: 与存量 31 因子的
    # 平均/最大秩相关。若某新因子与存量因子平均 |ρ| 很高, 它就是存量因子的换皮,
    # 建 die 也不会带来去相关收益(方案 §7: 与已有 die 秩相关 > 0.95 则退役)。
    if not args.no_corr:
        lo_s, hi_s = min(md).strftime("%Y%m%d"), max(md).strftime("%Y%m%d")
        ranked_new = {k: v.reindex(index=md).rank(axis=1) for k, v in neu_all.items()}
        acc = {k: [] for k in ranked_new}
        best = {k: (np.nan, None) for k in ranked_new}
        conn = db.get_conn()
        exist = [r[0] for r in conn.execute(
            "SELECT DISTINCT factor FROM factor WHERE trade_date>=? AND trade_date<=?",
            (lo_s, hi_s)).fetchall()]
        for nm in sorted(exist):
            sub = pd.read_sql(
                "SELECT ts_code, trade_date, value FROM factor "
                "WHERE factor=? AND trade_date>=? AND trade_date<=?",
                conn, params=(nm, lo_s, hi_s))
            if sub.empty:
                continue
            pv = sub.pivot(index="trade_date", columns="ts_code", values="value")
            pv.index = pd.to_datetime(pv.index)
            rp = pv.reindex(index=md).rank(axis=1)
            for k, rk in ranked_new.items():
                c = pair_corr(rk, rp, md)
                if c == c:
                    acc[k].append(abs(c))
                    if best[k][0] != best[k][0] or abs(c) > best[k][0]:
                        best[k] = (abs(c), nm)
        conn.close()
        for rec in recs:
            k = rec["factor"]
            if k not in acc or not acc[k]:
                continue
            rec["mean_abs_corr"] = float(np.mean(acc[k]))
            rec["max_corr"] = best[k][0]
            rec["max_corr_with"] = best[k][1]
        log.info("去相关体检完成: 对照存量因子 %d 个", len(exist))

    # ---- 汇总表 ----
    res = pd.DataFrame(recs)
    if res.empty:
        log.error("没有任何因子产出有效读数 —— 检查数据是否已回填")
        return
    cols = ["factor", "DEAD_head", "DEAD_head_t", "DEAD_hit", "DEAD_n",
            "ACTIVE_head", "DEAD_ic", "ACTIVE_ic", "DEAD_top6", "ACTIVE_top6",
            "DEAD_ls", "DEAD_p_adj", "coverage",
            "mean_abs_corr", "max_corr", "max_corr_with", "PASS"]
    res = res[[c for c in cols if c in res.columns]]
    pd.set_option("display.width", 220, "display.max_columns", 50,
                  "display.float_format", lambda v: f"{v:+.4f}" if abs(v) < 100 else f"{v:.0f}")
    log.info("\n" + "=" * 78 + "\n===== 汇总(按死区头部超额排序, 仅新因子参与门禁) =====\n" + "=" * 78)
    log.info("\n%s", res.sort_values("DEAD_head", ascending=False).to_string(index=False))

    # ⚠️ 两类因子即使 PASS 也不能建 die, 必须在名单里剔掉(否则读数会被误用):
    #   CTL_* —— 对照因子是**标尺**(判断本次测量的分辨率是否正常), 明文不参与门禁。
    #            2026-09-11 空跑时 CTL_illiq 混进了通过名单 —— 若照此推进会得出
    #            「数据路线成立」的假结论(它其实是已有因子, 根本没有新信息)。
    #   hk_*  —— 方案 §3 不变量 #6: hk_hold 在 2024-08-19 覆盖度从 4042 只断崖跌到
    #            793 只, 是「含义静默改变」的特征。**只作死区研究, 不得进生产特征集**。
    from app.factors.flow_factors import RESEARCH_ONLY_PREFIX

    def _research(n: str) -> bool:
        return n.startswith(RESEARCH_ONLY_PREFIX)

    gate_res = res[~res["factor"].str.startswith("CTL_") & ~res["factor"].map(_research)]
    passed = gate_res[gate_res["PASS"]]["factor"].tolist()
    research_pass = res[res["factor"].map(_research) & res["PASS"]]["factor"].tolist()
    log.info("\n" + "=" * 78)
    if passed:
        log.info("门禁通过 %d 个: %s", len(passed), passed)
        log.info("⇒ 数据路线成立, 进入阶段 1/2 建 die")
    else:
        log.info("门禁通过 0 个 —— **数据路线证伪**")
        log.info("⇒ 按预注册规则: 结论写入 app/config.py 已证伪登记处;")
        log.info("   架构只保留「量价异构 die(阶段 2 D0/D1)」, 不建数据 die。")
    if research_pass:
        log.info("⚠️ 研究专用因子(不变量 #6, 不得建 die, 仅记录): %s", research_pass)
    log.info("对照标尺(不参与门禁): 见 CTL_* 行 —— 用于判断本次测量的分辨率是否正常")
    log.info("=" * 78)


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"\n耗时 {time.time()-t0:.0f}s")
