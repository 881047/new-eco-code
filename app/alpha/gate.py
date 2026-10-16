"""Alpha Factory 的**预注册门禁**(2026-09-11 写死于任何候选被评估之前)

## 与既有门禁的关系(不是替代, 是加了更严的一条)

`scripts/oneoff/exp_deadzone_ic.py` 的阶段 0c 门禁是**冻结**的(它的判据在跑之前写死,
结果也已记录在案)。本模块**不改它**, 而是为 Alpha Factory 立一份**新**预注册:

| | 阶段 0c 门禁(冻结) | 本门禁(工厂) |
|---|---|---|
| 死区 head ≥ 0.5%/月 | ✅ | ✅ 同 |
| 死区 t ≥ 2.0 | ✅ | ✅ 同 |
| 死区/活跃区同号 | ✅ | ✅ 同 |
| Bonferroni p_adj < 0.05 | ✅ | ✅ 同 |
| top6 同号 | 仅读数 | ⭐ **升为判据** |

**为什么 top6 要升为判据 —— 三条已有证据(全部早于本文件)**:
`die_flow`(head +0.51% t=2.97, 但 top6 几乎全是 2023 一年)、`die_head_vt`(head +1.59%
t=4.85, 组合层两窗两轴全否)、`die_h10`(head +1.35% t=3.72, top6 三区段全负 −0.62%/
−0.87%/−0.38%)。**"前 10% 平均好"与"前 6 名好"可以符号相反**, 而策略买的是前 6 名。

⚠️ **这是把门禁改严, 不是改松。** 判据数从 4 条变 5 条, 通过率只会下降。
(反之 —— 若本文件写的是"放宽"，那就是看到结果后换判据, 不允许。)

## 但门禁通过 ≠ 可用(这一条最容易被忘)

`kgd`/门禁是**入场券(噪声筛)**, 不是**判决(值不值封装)**。判决只能是**组合层两轴**
(系统年化 + 系统回撤都不劣于同窗基线), 见 `scripts/oneoff/exp_die_stage_b.py`。
本门禁的通过名单只能作为「有资格进组合层回测」的输入, **不得据以改实盘**。

## 口径(与阶段 0c 逐字一致, 保证读数可比)

因子先 `fe.neutralize`(去极值 2% → 行业中性 → z-score)再评估 —— 与生产因子入库口径同;
前向收益用后复权价 `close × adj` 的 21 交易日前向; 头部 = 分数前 10%; 超额 = 相对**同截面
被打分股票**的均值(不拿全市场均值, 那会把覆盖域差异读成选股能力)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# ==========================================================================
# 预注册判据(2026-09-11 写死于任何候选被评估之前 —— 改这里等于改判据)
# ==========================================================================
GATE_MIN_HEAD = 0.005        # 死区头部月超额下界 0.5%/月
GATE_MIN_T = 2.0             # 死区 t 下界
GATE_ALPHA = 0.05            # 显著性水平(Bonferroni 分母 = 实际测过的候选数)
GATE_REQUIRE_SAME_SIGN = True
GATE_REQUIRE_TOP6_NONNEG = True   # ⭐ 新增(更严): 死区 top6 不得为负

TOP_FRAC = 0.10              # 头部定义
HORIZON = 21                 # 策略持有期(交易日)
MIN_NAMES = 200              # 截面股票数下限

DEAD = ("2020-09-01", "2023-12-31")
ACTIVE = ("2024-01-01", "2026-08-31")

# --- S5 时效轴常量（2026-09-27 预注册 plans/s5-half-life-prereg-20260927.md，跑前冻结）---
S5_HL_BAR = 2.0                  # 半衰期下界（月）= 2× 月频调仓周期
S5_LAGS = (0, 1, 2, 3, 4, 5)     # 执行延迟 lag（锚点步数 = 月频周期数）
S5_MIN_ANCHORS = 24              # DEAD 区可用锚点下限，低于此记"未测"不否决（沿 S4 惯例）


def forward_return(m: dict, horizon: int = HORIZON) -> pd.DataFrame:
    """前向 horizon 交易日收益(后复权)。末尾 horizon 天无前向收益 → NaN(不是 0)。

    ⚠️ **与生产标签有意不同**（2026-09-13 独立审查 §2.3/#5 登记）：
    这里是"朴素标签" `shift(-h)/cur − 1`；生产标签是
    `app.models.trainer.forward_return(ac, horizon, delist_last_price=True)` —— 它在股票于窗口内
    **退市/长期停牌**（daily 表直接缺行）时回退用窗口内最后有效价，因而那批尾部样本**不是 NaN**
    （2026-09-09 修复，实测影响 327 个样本）。
    ⇒ 门禁（KGD）与生产优化的是**略有差异的两个目标**。要统一必须走 A/B（会改候选判决），
    不要顺手改；统一前，任何"门禁通过/不通过"的结论都应把这条差异记在心里。
    """
    ac = m["close"] * m["adj"]
    return ac.shift(-horizon) / ac - 1.0


def zone_of(dates) -> np.ndarray:
    d = pd.Series(pd.to_datetime(dates), index=pd.to_datetime(dates))
    return np.where((d >= DEAD[0]) & (d <= DEAD[1]), "DEAD",
                    np.where((d >= ACTIVE[0]) & (d <= ACTIVE[1]), "ACTIVE", "OTHER"))


def evaluate(factor: pd.DataFrame, fwd: pd.DataFrame, dates: pd.DatetimeIndex,
             zones: np.ndarray, top_frac: float = TOP_FRAC) -> pd.DataFrame:
    """逐截面: 头部超额 / 多空价差 / 全截面 IC(**仅参考, 不参与判定**) / top6 超额。"""
    rows = []
    for i, d in enumerate(dates):
        if zones[i] == "OTHER":
            continue
        if d not in factor.index or d not in fwd.index:
            continue
        f = factor.loc[d].dropna()
        r = fwd.loc[d].dropna()
        common = f.index.intersection(r.index)
        if len(common) < MIN_NAMES:
            continue
        f, r = f[common], r[common]
        # 2026-09-26 GP 首跑(alpha_factory_gp1)踩出的潜在崩溃: 因子面板与 fwd 同源自行情
        # pivot ⇒ columns.name 都是 "ts_code" ⇒ f.index.name == "ts_code", 而 S5 tie-break
        # (2026-09-22)的 `_tie["ts_code"] = _tie.index` 会造出「索引层与列同名」歧义,
        # sort_values 当场 ValueError。intersection 只在两边同名时保留名字 ⇒ 此前
        # kg/flow 轮(一边未命名)侥幸不炸。清掉索引名不影响 tie-break 语义
        # ((score 降序, 代码升序) 不变)。
        f.index.name = None
        n = len(f)
        k = max(1, int(n * top_frac))
        # S5：确定性 tie-break（2026-09-22 用户拍板 B 实施；预注册判据 S5-1，
        # plans/s5-gate-stable-tiebreak-prereg-20260921.md）：
        # (分数降序, 代码升序) 稳定排序 ⇒ 选面与输入列序无关、可复现。
        # 此前 `f.sort_values(ascending=False)` 默认 quicksort 非稳定，并列时
        # "谁进 top-k"取决于输入顺序（因子级翻档 0.4663%；8 条 kg 计数候选的
        # 门禁读数随之移动 5~44bp/月 —— experiments/s5_tiebreak_factor_level.json
        # 与 experiments/s5_tiebreak_return_impact.json）。
        _tie = f.rename("score").to_frame()
        _tie["ts_code"] = _tie.index
        order = _tie.sort_values(by=["score", "ts_code"],
                                 ascending=[False, True], kind="stable").index
        mean_all = r.mean()
        top = r[order[:k]]
        t6 = r[order[:6]]
        rows.append({"date": d, "zone": zones[i], "n": n,
                     "head_excess": float(top.mean() - mean_all),
                     "ls_spread": float(top.mean() - r[order[-k:]].mean()),
                     "ic_full": float(f.corr(r, method="spearman")),
                     "top6_excess": float(t6.mean() - mean_all)})
    return pd.DataFrame(rows).set_index("date") if rows else pd.DataFrame()


def ic_by_lag(factor: pd.DataFrame, tradable: pd.DataFrame, fwd: pd.DataFrame,
              dates: pd.DatetimeIndex, zones: np.ndarray,
              lags: tuple = S5_LAGS, zone: str = "DEAD",
              min_names: int = MIN_NAMES) -> dict:
    """执行延迟衰减曲线 IC_L（S5 时效轴的数据机械; 预注册 plans/s5-half-life-prereg-20260927.md）。

    IC_L(t_i) = Spearman(factor[t_{i−L}], fwd21[t_i]) —— "信号晚 L 个调仓周期执行还剩
    多少边缘"。lag 以**锚点步数**计（月频 = 1 步 1 个月），不是交易日；全部 lag 共用
    同一个 fwd 面板 ⇒ 各 lag 可用锚点集一致，曲线逐点可比；L=0 与 evaluate() 的
    ic_full 口径逐位一致（同交集、同 Spearman），可作交叉校验锚。

    交集域 = tradable[t_i]（执行时点可交易）∧ 因子行 t_{i−L} 非 NaN ∧ fwd 行 t_i 非 NaN，
    ≥ min_names 只；不足则该锚点该 lag 跳过。⚠️ factor 必须传**未打码**的原始面板 ——
    打码在本函数内部按 t_i 做；若传入预打码面板，lag 行会带上 t_{i−L} 的可交易域（错口径）。
    返回 {L: Series(锚点日 → IC)}。
    """
    f_all = factor.reindex(index=dates)
    tr_all = tradable.reindex(index=dates, columns=f_all.columns).fillna(False)
    out = {L: {} for L in lags}
    for i, d in enumerate(dates):
        if zones[i] != zone or d not in fwd.index:
            continue
        r = fwd.loc[d].dropna()
        tr = tr_all.loc[d]
        for L in lags:
            j = i - L
            if j < 0:
                continue
            frow = f_all.iloc[j].dropna()
            common = frow.index.intersection(r.index)
            common = [c for c in common if bool(tr.get(c, False))]
            if len(common) < min_names:
                continue
            out[L][d] = float(frow[common].corr(r[common], method="spearman"))
    return {L: pd.Series(v).sort_index() for L, v in out.items()}


def half_life_from_curve(ic_dead: dict, lags: tuple = S5_LAGS,
                         min_anchors: int = S5_MIN_ANCHORS,
                         hl_bar: float = S5_HL_BAR) -> dict:
    """从 lag 衰减曲线到机械半衰期 + S5 判定（预注册 §一 规则，跑前冻结）。

    ic_dead: {L: (该 lag 的 DEAD 区 IC 均值, 可用锚点数)}。
      HL = 最小的 L>=1 使 |IC_L| < 0.5×|IC_0|（单位 = 月 = 锚点步数）;
      全曲线（L<=5）未过线 ⇒ HL=6.0 且 censored=True（"≥6"，通过）;
      IC_0 为 NaN 或 0 ⇒ HL=0.0（fail-closed，同 cost_per_month 缺失判否先例）;
      其余 lag 均值 NaN ⇒ HL=None（不可测）;
      n_min = 各 lag 可用锚点的最小值 < min_anchors ⇒ s5=None（"未测"不否决，如实列报，
      沿 S4 覆盖缺口惯例）——HL 读数照报，只是不参与判定。
    """
    ic0 = ic_dead.get(0, (np.nan, 0))[0]
    n_min = int(min((n for _, n in ic_dead.values()), default=0))
    if ic0 != ic0 or abs(float(ic0)) == 0.0:
        hl, censored = 0.0, False
    elif any(v != v for L, (v, _) in ic_dead.items() if L != 0):
        hl, censored = None, False
    else:
        hl, censored = 6.0, True
        for L in sorted(lags):
            if L == 0:
                continue
            if L not in ic_dead:
                continue
            if abs(ic_dead[L][0]) < 0.5 * abs(float(ic0)):
                hl, censored = float(L), False
                break
    s5 = None if (hl is None or n_min < min_anchors) else bool(hl >= hl_bar)
    return {"half_life_months": hl, "censored": censored,
            "n_anchors_min": n_min, "s5_half_life": s5, "hl_bar": float(hl_bar)}


def verdict_single(dead_head: float, dead_t: float, active_head: float,
                   shadow_abs_rho: float = None,
                   head_bar: float = 0.003, t_bar: float = 2.0,
                   rho_bar: float = 0.7,
                   half_life_months: float = None,
                   hl_bar: float = S5_HL_BAR) -> dict:
    """单因子档判据（2026-09-26 用户拍板新增; 预注册 plans/single-factor-gate-prereg-20260926.md）。

    与六判据(生产候选准入, 集成级 bar)并行、互不替代 —— 本档测"池准入/库健康度":
      S1 |DEAD head| >= 0.3%/月(单因子可达 bar; 六判据的 0.5% 是集成级, 232 候选 p95=0.41%);
      S2 |DEAD t| >= 2.0(p_adj 取消 —— 池准入测增量, 独立发现的多重比较已由六判据承担);
      S3 死区/活跃区同号(稳健性, 保留);
      S4 去相关: 与现役池 |rho|max <= 0.7(AlphaAgent 机制; gp_000 rho=0.951 影子教训)。
    shadow_abs_rho=None(未测)不否决 —— 如实列报。top6 判据取消: 校准实证池成员 top6 也为负,
    单因子以"进模型混合"贡献, 不独立挑 top6。

    S5 时效轴（2026-09-27 新增; 预注册 plans/s5-half-life-prereg-20260927.md, 跑前冻结）:
      执行延迟 IC 半衰期 >= 2× 调仓周期（月频 = HL >= 2.0 月）。封杀"存在性显著但
      衰减快于执行"的假阳性（W2 M1 铁证: 配对差 t=+2.73 但 α 只在首期、lag>=1 转负 ——
      当时若有时效判据可在门禁层拦住, 不必烧 70 分钟机时 + 组合层回测）。
    half_life_months=None(未测)不否决 —— 向后兼容: 不传该参数的既有调用行为逐位不变
    （S1-S4 判决、返回键序照旧, 仅新增 s5_half_life 键）。
    """
    s1 = abs(float(dead_head)) >= head_bar
    s2 = abs(float(dead_t)) >= t_bar
    s3 = float(dead_head) * float(active_head) > 0
    s4 = None if shadow_abs_rho is None else bool(abs(float(shadow_abs_rho)) <= rho_bar)
    s5 = None if half_life_months is None else bool(float(half_life_months) >= hl_bar)
    passed = s1 and s2 and s3 and (s4 is not False) and (s5 is not False)
    return {"s1_head": bool(s1), "s2_t": bool(s2), "s3_same_sign": bool(s3),
            "s4_decor": s4, "s5_half_life": s5, "PASS_single": bool(passed)}


def verdict_portfolio(deltas: list, ann_bar_prod: float = 0.020,
                      ann_bar_full: float = 0.010, mdd_bar_prod: float = -0.020,
                      mdd_bar_full: float = -0.030, turnover_bar: float = 0.050) -> dict:
    """组合层档判据（2026-09-27 新增; 预注册 plans/portfolio-admission-gate-prereg-20260927.md）。

    门禁第三层：单因子档/打分层 head 过闸 ≠ 组合层可用（nf 全量重训线第 6 例）。
    输入 = 成对评分表（候选−控制）的生产路径回测读数 per 种子集，**≥2 套种子集**
    （双种子集铁律）。deltas 元素字段：sys_ann_prod / sys_ann_full / sys_mdd_prod /
    sys_mdd_full / turnover（差值，候选−控制；turnover 可缺省=未测不否决，沿 S4 惯例）。

      P1 收益轴: 所有种子集 sys_ann_prod ≥ +2.0pp 且所有 sys_ann_full ≥ +1.0pp
        （阈值依据: 种子噪声实测 ±3.5pp/年 —— 单种子集的 Δ 不可作数，双种子集一致
        才是两次独立方向确认；chase6 先例 +2.07pp 同量级）;
      P2 回撤轴: sys_mdd 均值 prod ≥ −2pp 且 full ≥ −3pp;
      P3 换手轴: 所有种子集 turnover ≤ +5pp。

    判决词（机械；人只做最终拍板）: 收益轴双种子集异号 ⇒ "INDETERMINATE"（读数
    不可辨，补样本再测）；P1∧P2∧P3 ⇒ "PASS（进拍板）"；其余 ⇒ "REJECT（线结清）"。
    阈值参数均可显式覆写（覆写须在预注册/台账留痕）。
    """
    if len(deltas) < 2:
        raise ValueError("组合层档要求 ≥2 套种子集的读数（双种子集铁律）")
    fields = ("sys_ann_prod", "sys_ann_full", "sys_mdd_prod", "sys_mdd_full", "turnover")
    for d in deltas:
        missing = [f for f in fields[:4] if f not in d]
        if missing:
            raise ValueError(f"读数缺字段: {missing}")

    prods = [float(d["sys_ann_prod"]) for d in deltas]
    fulls = [float(d["sys_ann_full"]) for d in deltas]
    p1 = all(v >= ann_bar_prod for v in prods) and all(v >= ann_bar_full for v in fulls)
    mdd_prod = sum(float(d["sys_mdd_prod"]) for d in deltas) / len(deltas)
    mdd_full = sum(float(d["sys_mdd_full"]) for d in deltas) / len(deltas)
    p2 = mdd_prod >= mdd_bar_prod and mdd_full >= mdd_bar_full
    tos = [float(d["turnover"]) for d in deltas if d.get("turnover") is not None]
    p3 = None if not tos else all(v <= turnover_bar for v in tos)

    same_pos = all(v > 0 for v in prods)
    same_neg = all(v < 0 for v in prods)
    if not (same_pos or same_neg):
        verdict = "INDETERMINATE"
    elif p1 and p2 and p3 is not False:
        verdict = "PASS"
    else:
        verdict = "REJECT"
    return {"p1_ann": bool(p1), "p2_mdd": bool(p2), "p3_turnover": p3,
            "n_seed_sets": len(deltas), "PASS_portfolio": verdict == "PASS",
            "verdict": verdict}


def _stat(s) -> tuple:
    """均值 / 朴素 t / 命中率 / n。t 未做自相关修正 ⇒ 真 t 略小, 门槛已按保守方向设。"""
    s = pd.Series(s).dropna()
    if len(s) < 3:
        return np.nan, np.nan, np.nan, len(s)
    sd = s.std(ddof=1)
    t = float(s.mean() / (sd / np.sqrt(len(s)))) if sd and sd > 0 else np.nan
    return float(s.mean()), t, float((s > 0).mean()), int(len(s))


def _p_two_sided(t: float) -> float:
    from math import erfc, sqrt
    if t != t:
        return np.nan
    return float(erfc(abs(t) / sqrt(2)))


def bonferroni(p: float, n_tests: int) -> float:
    if p != p:
        return np.nan
    return float(min(1.0, p * max(1, int(n_tests))))


def yearly(det: pd.DataFrame) -> dict:
    """逐年 head / top6 —— 衰减曲线。**用来发现"全由单年贡献"**:
    `die_flow` 就是这样被抓出来的(剔掉 2023 后 ≈ 0)。"""
    out = {}
    for y, sub in det.groupby(det.index.year):
        out[int(y)] = {"head": _stat(sub["head_excess"])[0],
                       "top6": _stat(sub["top6_excess"])[0], "n": int(len(sub))}
    return out


def summarize(name: str, det: pd.DataFrame, n_tests: int, extra: dict = None,
              cost_per_month: float = None, n_tests_family: int = None,
              n_tests_prior: int = None) -> dict:
    """按分区汇总 + 预注册门禁判定。

    2026-09-14 门禁 v2（判据见 KB 14 号 三补十四，**只会更严**）：

    - `cost_per_month`（①经济口径）：该因子"自换手 × 往返真实费率"的月度成本，
      来自 `critic.churn_cost()`。**缺失 ⇒ `c_head_size` 判否**（fail-closed）——
      与 2026-09-12 修的"基线读不到反而放行"是同一类坑：不许因为少了一个输入就静默放宽。
    - `n_tests_family` / `n_tests_prior`（②累计分母）：本批冻结的分母与**跑之前**登记簿里的
      历史试验数；真正用于 Bonferroni 的 `n_tests` = `max(本批, 历史 + 本批)`（由调用方算，
      见 `factory.run`）。三个数都留在 rec 里，事后能复算。
    """
    rec = {"alpha_id": name}
    for zone in ("DEAD", "ACTIVE"):
        sub = det[det["zone"] == zone]
        m, t, hit, n = _stat(sub["head_excess"])
        rec[f"{zone}_head"], rec[f"{zone}_head_t"] = m, t
        rec[f"{zone}_hit"], rec[f"{zone}_n"] = hit, n
        rec[f"{zone}_top6"] = _stat(sub["top6_excess"])[0]
        rec[f"{zone}_ic"] = _stat(sub["ic_full"])[0]      # 仅参考
        rec[f"{zone}_ls"] = _stat(sub["ls_spread"])[0]
    mh, th, _, nh = _stat(det["head_excess"])
    rec["FULL_head"], rec["FULL_head_t"], rec["FULL_n"] = mh, th, nh
    rec["FULL_top6"] = _stat(det["top6_excess"])[0]
    rec["p_adj"] = bonferroni(_p_two_sided(rec.get("DEAD_head_t")), n_tests)
    rec["n_tests"] = int(n_tests)
    rec["n_tests_family"] = int(n_tests) if n_tests_family is None else int(n_tests_family)
    rec["n_tests_prior"] = 0 if n_tests_prior is None else int(n_tests_prior)
    # ⚠️ 显式存 None（而不是缺键）: 事后能区分"没算成本"与"成本为 0"
    rec["cost_per_month"] = None if cost_per_month is None else float(cost_per_month)
    rec["yearly"] = yearly(det)
    if extra:
        rec.update(extra)
    rec.update(verdict(rec))
    return rec


def verdict(rec: dict) -> dict:
    """预注册判据（5 条 + 门禁 v2 的成本项，全部只看死区与活跃区，不看全区）。

    `c_head_size` 的门槛自 2026-09-14 起是 `GATE_MIN_HEAD + cost_per_month`
    （`cost_per_month` 缺失 ⇒ 直接判否，不是退回旧门槛）。
    `head_bar` 会写回 `rec`，便于事后看"这条线当时定在哪"。
    """
    hd, ha = rec.get("DEAD_head", np.nan), rec.get("ACTIVE_head", np.nan)
    t6 = rec.get("DEAD_top6", np.nan)
    same = (hd == hd and ha == ha and np.sign(hd) == np.sign(ha))
    top6_ok = (not GATE_REQUIRE_TOP6_NONNEG) or (t6 == t6 and t6 >= 0)
    cost = rec.get("cost_per_month")
    cost_ok = bool(cost is not None and cost == cost and float(cost) >= 0)
    head_bar = GATE_MIN_HEAD + (float(cost) if cost_ok else 0.0)
    rec["head_bar"] = float(head_bar)
    c = {
        "c_same_sign": bool(same),
        "c_head_size": bool(cost_ok and hd == hd and abs(hd) >= head_bar),
        "c_t": bool(rec.get("DEAD_head_t", np.nan) == rec.get("DEAD_head_t", np.nan)
                    and rec["DEAD_head_t"] >= GATE_MIN_T),
        "c_p_adj": bool(rec.get("p_adj", np.nan) == rec.get("p_adj", np.nan)
                        and rec["p_adj"] < GATE_ALPHA),
        "c_top6": bool(top6_ok),
        "c_cost": bool(cost_ok),
    }
    c["PASS"] = bool(all(c[k] for k in c))
    return c


__all__ = ["forward_return", "zone_of", "evaluate", "summarize", "verdict",
           "yearly", "bonferroni", "DEAD", "ACTIVE", "TOP_FRAC", "HORIZON",
           "GATE_MIN_HEAD", "GATE_MIN_T", "GATE_ALPHA",
           "ic_by_lag", "half_life_from_curve", "S5_HL_BAR", "S5_LAGS",
           "S5_MIN_ANCHORS"]
