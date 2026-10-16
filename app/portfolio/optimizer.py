"""Portfolio Optimizer —— Risk / Liquidity / Exposure 约束下的组合构造(2026-09-11)

对应用户框架图的 `Pure Alpha Score → Portfolio Optimizer(Risk/Liquidity/Exposure)
→ Top 20~30 → Execution`。

## 本项目的第一条约束不是风险模型, 是**费用算术**

框架图写的是 `Top 20~30`。在本项目的账户规模下那是**算术上不可行**的 ——
用本项目自己的费用模型(`app/portfolio/fees.py`)算 2 万账户、约 1.2 万实投、等分:

| 持仓数 | 每只金额 | 往返费率 |
|---|---|---|
| **5 只(现状)** | 2 400 元 | **0.469%** |
| 10 只 | 1 200 元 | 0.885% |
| 20 只 | 600 元 | 1.719% |
| **30 只** | 400 元 | **2.552%** |

原因是用户的**永久约束**(2026-09-11:「券商没有免五的」): `max(额×万2.5, 5元)` 在
400 元一笔时把有效佣金率从 0.025% 顶到 **1.25%(50 倍)**。现状摩擦总成本 3.16pp/年,
Top 30 会把它推到 ~17pp/年, 超过系统线全部收益。

**⇒ 所以本模块的第一个公开方法是 `feasible_n()`, 不是 `optimize()`。**
它把"该持几只"从拍脑袋变成一个由费用模型解出的数。§10b 的结论是
「Top 20~30 不是错的, 是被账户规模卡死的」—— 资金规模到了就解锁。

## 第二条约束: 可交易粒度是**整手**, 不是浮点

A 股买入必须是 100 股的整数倍(科创板 200 股起且可 1 股递增, 本模块按 100 保守处理)。
故权重解出来之后必须落成**整手**, 落的过程中会剩下现金 —— 这部分现金**不是 bug,
是承重的**: 本项目 0910 那轮实验证明"机械性闲置的现金是承重的"(三轮提高暴露的实验
全部被现行支配)。

## 第三条约束: 流动性 —— **它现在不 binding, 这是测出来的, 不是假设的**

框架图的三个子盒 `Risk / Liquidity / Exposure` 里, **Liquidity 一度只出现在本文档的标题里**
(2026-09-11 审计发现的缺口: `cons` 实际只有 `[预算, 单票上限, 行业上限]`)。现在补上真实的
**参与率上限**(`w_i × 资金 ≤ adv_i × max_participation`, 机构惯例量级 10% of ADV)。

但补之前先**量了它有多大**: 用 20260831 方案的实际权重 与 库里的 20 日均成交额
(`daily.amount`, 千元)算参与率(账户 2 万, 上限 10%):

| 持仓 | 权重 | 20 日均额 | 计划金额 | 参与率 |
|---|---|---|---|---|
| 000571.SZ | 0.2071 | 1.27 亿 | 4 142 元 | 0.00326% |
| **002513.SZ** | 0.2320 | 4 349 万 | 4 640 元 | **0.01067%** |
| 300125.SZ | 0.1631 | 8 762 万 | 3 262 元 | 0.00372% |
| 300227.SZ | 0.2281 | 3.00 亿 | 4 562 元 | 0.00152% |
| 300787.SZ | 0.1698 | 4 989 万 | 3 396 元 | 0.00681% |

**最大参与率 0.0107%, 比 10% 的上限低 937 倍。** 换句话说: **账户要放大到约 1 875 万,
这条约束才会第一次绑住。** 在 2 万的账户上它是**恒真的空约束**。
(该数字由 `tests/test_optimizer.py::TestLiquidityPinsTheUnlockScale` 钉住 —— 账户规模或
持仓数一变, 那条测试会红, 逼人重算这张表。)

⇒ 所以它的实现方式与别的约束不同: **不是"加上去保护", 而是"加上去并报告它没有生效"** ——
`diag["part_max"]` **无条件**返回(与 `framework._stage_health` 的 `auto_retired: 0` 同一手法:
让"什么都没发生"成为一个**被观测到的读数**, 而不是一句靠读代码相信的断言)。

⇒ 仍**不做**"剔除小票"那条: `builder.py:67-74` 的 R11-3 硬过滤代码还在, 但被实验证伪后
**默认关闭**(年化 −6.9pp 且回撤未降, 小盘是模型核心 alpha)。把一条已证伪的过滤换个地方
重写一遍, 不是弥补缺口, 是绕过结论。

⚠️ **单位陷阱**: `adv` 的单位是**千元**(与 `daily.amount` 一致), 内部 ×1000 转成元再算参与率。
本项目在 `funds.py:56` 栽过同一个坑。

## SOTA 结构

| 部件 | 做法 | 为什么不是更简单的做法 |
|---|---|---|
| 协方差估计 | **Ledoit-Wolf 收缩**(`sklearn.covariance`) | 样本协方差在 N≫T 时不可逆/特征值爆炸; 收缩估计是它的标准解 |
| 权重求解 | **凸优化**(cvxpy QP): `max s'w − λ·w'Σw`, 约束 = 预算/单票上限/行业暴露 | 裸 top-N 忽略相关性与暴露, 会在同一个风格上押满 |
| 基数约束 | 连续松弛 + top-n 截断(**不重归一**) | 基数约束非凸; 混合整数在 N=5000 上不可解, 松弛是工业界标准做法。**截断后不放大**: 放大就可能顶破单票/行业上限 |
| 稳健对照 | **HRP**(层次风险平价, López de Prado) | 不需求逆, 对估计误差远稳健; 用来检验 QP 的解是不是被协方差噪声带跑了 |
| 费用 | 逐笔 `fees.trade_cost`, 含 5 元地板与印花税 | 用扁平费率会**系统性低估**小仓位成本(本项目实测 0.13~0.19%/笔) |

## 边界(不假装)

- 本模块**不接生产**(方案 §3 不变量 #7)。它只产出权重, 由 `builder.py` 决定要不要用。
- 行业暴露用的是**当前快照**行业(任务 #23 实测 PIT 行业的代价 ≈ 0, 故不为此单独建表)。
- ⚠️ **QP 与 HRP 的尺度口径不同**(2026-09-12 审查 P2-07 的实测副产品): `hrp` 截断后
  **重归一**再 `×budget` ⇒ 和恒为 `budget`; `qp` **不重归一** ⇒ 和 ≤ `budget`
  (差多少看 `diag["budget_gap"]`)。两者本来就是"稳健对照", 但对照的应当是**构成**
  (谁拿了钱、集中在哪), 不是和 —— 直接比 `sum(w)` 是把口径差当成结论差。
  是否要让 QP 也统一到"满预算"是**口径变更**(会改所有用到它的实验读数), 需预注册 A/B,
  不在这里顺手改。
- HRP 与 QP 的对比**不是判据** —— 判据仍是组合层两轴。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.portfolio import fees

log = logging.getLogger(__name__)

LOT = 100                      # A 股一手 = 100 股
DEFAULT_MAX_WEIGHT = 0.35      # 单票上限(相对可投资金)
DEFAULT_IND_CAP = 0.40         # 单行业上限
DEFAULT_ROUND_TRIP_TARGET = 0.005   # 可接受的往返费率上限
DEFAULT_MAX_PART = 0.10        # 单票参与率上限(占 20 日均成交额), 机构惯例量级
ADV_UNIT = 1000.0              # `adv` 的单位是千元(与 `daily.amount` 一致) ⇒ ×1000 转元


def _cvxpy():
    try:
        import cvxpy as cp
        return cp
    except Exception:                                  # pragma: no cover
        return None


# ---------------------------------------------------------------------------
# ① 费用算术: 该持几只
# ---------------------------------------------------------------------------
def round_trip_rate(amount: float, date=None, params: dict = None) -> float:
    """一笔完整往返(买+卖)的费用率 = (买入费 + 卖出费) / 成交额。"""
    amt = float(amount or 0)
    if amt <= 0:
        return 0.0
    return (fees.trade_cost(amt, "buy", date, params)
            + fees.trade_cost(amt, "sell", date, params)) / amt


def min_amount_for_rate(target_rate: float, date=None, params: dict = None) -> float:
    """要让往返费率 ≤ `target_rate`, 单笔**至少**要投多少钱。

    闭式解(费用模型是分段的, 所以有解):
        费率(a) = 2·max(a·c, m)/a + 2·t + s
      · a ≥ m/c 时 = 2c + 2t + s(常数地板, 再大也不降)
      · a < m/c 时 = 2m/a + 2t + s  ⇒  a = 2m / (target − 2t − s)

    关键读数: 5 元地板把"费率地板"钉在 **≈0.102%** 往返, 无论投多少钱都下不去。
    """
    p = params or fees._DEFAULTS
    m = p["min_commission"]
    # 注: 闭式解只需 m/t/s —— `c`(佣金费率) 不出现在 a < m/c 的分支解里, 故不读它。
    # 2026-09-13 删掉了 `c = p["commission_rate"]` 这个死赋值（ruff F841）。
    t = p["transfer_fee"]
    s = fees.stamp_tax_rate(date, p)
    base = 2 * t + s
    if target_rate <= base:
        return float("inf")            # 目标比地板还低 ⇒ 任何金额都做不到
    return float(2 * m / (target_rate - base))


def feasible_n(investable: float, target_rate: float = DEFAULT_ROUND_TRIP_TARGET,
               date=None, params: dict = None, cap: int = 40) -> dict:
    """**该持几只** —— 由费用算术解出, 不是拍脑袋。

    investable: 实际可投金额(元)。等分假设下 n ≤ investable / min_amount。
    返回 {n, per_position, round_trip, min_amount}。
    """
    need = min_amount_for_rate(target_rate, date, params)
    if not np.isfinite(need):
        return {"n": 0, "per_position": 0.0, "round_trip": None,
                "min_amount": need,
                "why": "目标费率低于费用地板(2c+2t+s), 任何金额都不可能达到"}
    raw = int(max(1, np.floor(investable / need)))
    n = int(min(cap, raw))
    per = investable / n
    # 2026-09-13（独立审查 #8）：原来不区分"按费率解出的 n"与"被 cap 钳住的 n" ⇒
    # 账户足够大时它会说"等分 X 元 ⇒ 往返 Y% ≤ 目标"，却隐瞒了"其实还能持更多只"。
    capped = raw > cap
    return {"n": n, "per_position": float(per),
            "round_trip": round_trip_rate(per, date, params),
            "min_amount": float(need),
            "n_by_rate": raw, "capped_by_policy": bool(capped),
            "why": "等分 %.0f 元 ⇒ 往返 %.3f%% ≤ 目标 %.3f%%"
                   % (per, 100 * round_trip_rate(per, date, params), 100 * target_rate)
                   + ("（n 已被 cap=%d 钳住：按费率还能持 %d 只；这是**策略上限**不是费用约束）"
                      % (cap, raw) if capped else "")}


# ---------------------------------------------------------------------------
# ①b 流动性: 参与率(占 20 日均成交额)
# ---------------------------------------------------------------------------
def participation_of(weights: pd.Series, equity: float, adv: pd.Series) -> pd.Series:
    """每股参与率 = 计划金额 / 20 日均成交额。

    ⚠️ `adv` 的单位是**千元**(与 `daily.amount` 一致), 内部 ×`ADV_UNIT` 转成元 ——
    单位混用会让参与率差 1000 倍, 而 1000 倍正好把"永不 binding"变成"永远 binding"。
    """
    amt = weights.astype("float64") * float(equity)
    a = pd.to_numeric(adv.reindex(weights.index), errors="coerce") * ADV_UNIT
    with np.errstate(divide="ignore", invalid="ignore"):
        return amt / a


def _part_diag(p: pd.Series, cap: float) -> dict:
    """把参与率序列压成可落库的读数。**没有分数时也返回一个说明**, 不静默省略 ——
    省略会让"没算"和"算了但为零"长得一模一样。"""
    p = p.replace([np.inf, -np.inf], np.nan).dropna()
    if not len(p):
        return {"part_max": None, "part_note": "无有效成交额数据, 参与率未计算"}
    top = p.sort_values(ascending=False)
    mx = float(top.iloc[0])
    return {
        "part_max": mx,
        "part_argmax": str(top.index[0]),
        "part_cap": float(cap),
        "part_headroom": (float(cap) / mx) if mx > 0 else float("inf"),
        # "离 binding 最近"的几只(≥ 上限一半)。**不是违规名单** —— 命名要诚实
        "part_nearest": [str(x) for x in top[top >= float(cap) / 2].index[:5]],
        "part_note": ("上限 %.1f%% | 实际最大 %.4f%% (%s) | 余量 %.0f 倍"
                      % (100 * float(cap), 100 * mx, top.index[0],
                         (float(cap) / mx) if mx > 0 else float("inf"))),
    }


def _maybe_part(w: pd.Series, equity, adv, cap: float) -> dict:
    """有资金与成交额才算参与率。缺任一个 ⇒ 返回空(而不是编一个数出来)。"""
    if equity is None or adv is None or not len(adv) or float(equity or 0) <= 0 or not len(w):
        return {}
    return _part_diag(participation_of(w, equity, adv), cap)


# ---------------------------------------------------------------------------
# ② 协方差: Ledoit-Wolf 收缩
# ---------------------------------------------------------------------------
def shrunk_cov(returns: pd.DataFrame, fallback_diag: float = 1e-4) -> np.ndarray:
    """Ledoit-Wolf 收缩协方差(没有 sklearn 就退化为"样本协方差 + 对角加载")。

    返回 (N, N) 的**相关阵**或协方差阵, 单位无所谓 —— 它只进 `λ·w'Σw` 的风险项。
    """
    X = returns.to_numpy(dtype="float64")
    X = X[:, ~np.isnan(X).all(axis=0)]
    if X.shape[1] == 0 or X.shape[0] < 3:
        return np.eye(max(1, returns.shape[1])) * fallback_diag
    try:
        from sklearn.covariance import LedoitWolf
        cov = LedoitWolf().fit(X).covariance_
    except Exception:                                  # pragma: no cover
        cov = np.cov(X, rowvar=False)
        cov = cov + np.eye(cov.shape[0]) * fallback_diag
    return np.asarray(cov, dtype="float64")


# ---------------------------------------------------------------------------
# ③ HRP: 层次风险平价(不需求逆的稳健对照)
# ---------------------------------------------------------------------------
def hrp_weights(corr: pd.DataFrame) -> pd.Series:
    """López de Prado 的 HRP: 距离 → 层次聚类 → 递归二分分配。

    与 QP 的关系是**对照**, 不是替代: QP 在协方差估得准时更优, HRP 在估不准时更稳。
    哪个更好**由组合层两轴判**, 不由本函数判。
    """
    from scipy.cluster.hierarchy import linkage
    from scipy.spatial.distance import squareform

    # ⚠️ `to_numpy()` 在 pandas 3.x 的写时复制下返回**只读视图**, `fill_diagonal` 会
    # `ValueError: underlying array is read-only` ⇒ 必须显式 copy。
    c = corr.fillna(0.0).to_numpy(dtype="float64", copy=True)
    np.fill_diagonal(c, 1.0)
    d = np.sqrt(np.clip((1.0 - c) / 2.0, 0.0, 1.0))       # 相关 → 距离
    np.fill_diagonal(d, 0.0)
    d = (d + d.T) / 2.0
    n = d.shape[0]
    if n == 1:
        return pd.Series([1.0], index=corr.index)
    link = linkage(squareform(d, checks=False), method="single")
    order = _quasi_diag(link, n)
    w = pd.Series(1.0, index=range(n))
    clusters = [order]
    while clusters:
        nxt = []
        for cl in clusters:
            if len(cl) > 1:
                half = len(cl) // 2
                nxt += [cl[:half], cl[half:]]
        for i in range(0, len(nxt), 2):
            a, b = nxt[i], nxt[i + 1]
            var_a = _cluster_var(c, a)
            var_b = _cluster_var(c, b)
            alpha = 1.0 - var_a / (var_a + var_b) if (var_a + var_b) > 0 else 0.5
            w[a] *= alpha
            w[b] *= (1.0 - alpha)
        clusters = nxt
    out = pd.Series(w.to_numpy(), index=corr.index)
    return out / out.sum()


def _quasi_diag(link, n) -> list:
    """把 linkage 矩阵还原成叶子的准对角顺序(López de Prado 的递归展开)。"""
    link = link.astype(int)
    sort_ix = pd.Series([link[-1, 0], link[-1, 1]])
    # 循环条件恒以 `n`(原始标的数)为界。原版 López de Prado 里那个 `numItems` 变量
    # 就是它 —— 曾经在这里写成 `num_items = sort_ix.max()`, 会让循环提前退出,
    # 且 **n≤2 时完全看不出来**(那时循环本来就不执行), 只在 n≥3 时静默给出错权重。
    while sort_ix.max() >= n:
        sort_ix.index = range(0, sort_ix.shape[0] * 2, 2)
        df0 = sort_ix[sort_ix >= n]
        i, j = df0.index, df0.values - n
        sort_ix[i] = link[j, 0]
        df0 = pd.Series(link[j, 1], index=i + 1)
        sort_ix = pd.concat([sort_ix, df0]).sort_index()
        sort_ix.index = range(sort_ix.shape[0])
    return sort_ix.tolist()


def _cluster_var(corr: np.ndarray, idx) -> float:
    sub = corr[np.ix_(idx, idx)]
    ivp = 1.0 / np.diag(sub)
    ivp = ivp / ivp.sum()
    return float(ivp @ sub @ ivp)


# ---------------------------------------------------------------------------
# ④ 凸优化: max s'w − λ·w'Σw, 约束 = 预算 / 单票 / 行业
# ---------------------------------------------------------------------------
@dataclass
class Allocation:
    weights: pd.Series                 # 目标权重(和 ≤ 1, 未满仓即留现金)
    lots: pd.Series = field(default_factory=lambda: pd.Series(dtype="int64"))
    cash: float = 0.0
    fees_est: float = 0.0
    n: int = 0
    method: str = ""
    diag: dict = field(default_factory=dict)

    @property
    def shares(self) -> pd.Series:
        """**股数** = 手数经**板块规则**换算（`board.lot_shares`，唯一真源）。

        `lots` 存的是**手数**, 不是股数 —— 这个区分是必要的: 下单接口要股数, 而人看方案
        时想的是"几手"。留一个含糊的字段名, 迟早有人把 2 手当成 2 股下单。

        2026-09-13（独立审查 #8）：原来硬编码 `lots × 100` —— 那是**沪深主板/创业板**的规则；
        **科创板（688/689）最低 200 股**，`board.lot_shares` 才是判定真源（生产 builder/账本
        也走它）。这里改成逐只过 `lot_shares`，拿不到规则时才退回 ×100（并**在 diag 里留痕**）。
        """
        base = (self.lots * LOT).astype("int64")
        try:
            from app.portfolio import board as board_mod
            out = {c: int(board_mod.lot_shares(c, int(v))) for c, v in base.items()}
            return pd.Series(out, dtype="int64")
        except Exception:                              # pragma: no cover - 规则不可用时保守退回
            log.warning("lot_shares 不可用 ⇒ shares 退回 lots×%d（科创板最低 200 股规则未生效）",
                        LOT)
            return base

    def as_dict(self) -> dict:
        return {"n": self.n, "cash": self.cash, "fees_est": self.fees_est,
                "method": self.method, **self.diag}


def optimize(scores: pd.Series, cov: pd.DataFrame = None, industry: pd.Series = None,
             n: int = 6, budget: float = 0.95, max_weight: float = DEFAULT_MAX_WEIGHT,
             industry_cap: float = DEFAULT_IND_CAP, risk_aversion: float = 1.0,
             method: str = "qp", equity: float = None, adv: pd.Series = None,
             max_participation: float = DEFAULT_MAX_PART) -> Allocation:
    """给截面分数 + 协方差 + 行业, 产出目标权重。

    `scores`: index=ts_code(截面内)。**NaN 视为不可投**(与引擎"NaN = 不买"一致)。
    `n`:      目标持仓数 —— 应当来自 `feasible_n()`, 不是框架图的 20~30。
    `budget`: 目标仓位上限(1.0 = 满仓)。
    `equity`: 可投资金(元) —— **参与率约束与读数都需要它**(权重×资金 = 金额)。
    `adv`:    20 日均成交额, **单位千元**(与 `daily.amount` 一致), index=ts_code。
              给了它 ⇒ 参与率**无条件**进 `diag`; 配合 `equity` ⇒ 约束真的加进 QP。
              **NaN/非正 ⇒ 该票被剔除**(与 PIT `engine.py:185` 的 NaN 保守剔除同口径)。
    `max_participation`: 单票上限(占 ADV), 默认 10%(机构惯例量级)。

    `method`:
      · `qp`  凸优化(默认)
      · `hrp` 层次风险平价(**只用协方差, 不看分数**, 作为稳健对照)
      · `topn` 裸 top-N 等权(现行做法, 保留为对照)

    ⚠️ 参与率约束在 2 万账户上是**恒真的空约束**(实测余量 937 倍, 见模块 docstring)。
    所以 `diag["part_max"]` 的价值不是"保护生效了", 而是**它证明了保护没生效** ——
    账户放大到约 1 875 万之前, 这个数字应该一直是 ~1e-4 量级。
    """
    sc = scores.dropna().astype("float64")
    if sc.empty:
        return Allocation(pd.Series(dtype="float64"), method=method)
    sc = sc.sort_values(ascending=False)

    # --- 流动性: 无有效成交额的票按不可投剔除(保守, 与 PIT 的 NaN 口径一致) ---
    part_extra = {}
    if adv is not None and len(adv):
        a = pd.to_numeric(adv.reindex(sc.index), errors="coerce")
        bad = ~(a > 0)
        if bad.any():
            log.warning("流动性: %d/%d 只无有效 20 日均额 ⇒ 剔除(与 PIT NaN 保守口径一致)",
                        int(bad.sum()), len(sc))
            part_extra["part_excluded"] = int(bad.sum())
            sc, a = sc[~bad], a[~bad]
        adv = a
        if sc.empty:
            return Allocation(pd.Series(dtype="float64"), method=method, diag=part_extra)

    if method == "topn" or n >= len(sc):
        picked = sc.index[:n]
        w = pd.Series(budget / len(picked), index=picked)
        return Allocation(w, n=len(picked), method="topn",
                          diag={"why": "裸 top-N 等权(现行做法, 对照用)", **part_extra,
                                **_maybe_part(w, equity, adv, max_participation)})

    if method == "hrp":
        a = _hrp_alloc(sc, cov, n, budget, max_weight)
        a.diag.update(part_extra)
        a.diag.update(_maybe_part(a.weights, equity, adv, max_participation))
        return a

    cp = _cvxpy()
    if cp is None:                                     # pragma: no cover
        log.warning("cvxpy 不可用, 回退 topn")
        # 2026-09-13（独立审查 #8）：回退是**允许**的，但必须**留痕** —— 回退到 topn 等于
        # 丢掉了协方差风险项、单票上限与行业上限。原来返回值里的 `why` 只说"裸 top-N 等权
        # (对照用)"，读的人无从知道"这是引擎缺依赖导致的降级"，会把降级结果当成 QP 的解。
        a = optimize(sc, cov, industry, n, budget, max_weight,
                     industry_cap, risk_aversion, method="topn",
                     equity=equity, adv=adv, max_participation=max_participation)
        a.diag.update({"fallback": "cvxpy_unavailable",
                       "constraints_dropped": ["risk_term(cov)", "max_weight", "industry_cap"]})
        return a

    # 只对候选池做优化(top-n 的 4 倍), 否则 5000 维 QP 没有意义也无必要
    cand = sc.index[:min(len(sc), max(n * 4, n + 5))]
    mu = sc.reindex(cand).to_numpy(dtype="float64")
    # 分数是序数(分位/预测值), 先归一到均值 0 方差 1, 避免 λ 的量纲随分数缩放漂移
    mu = (mu - mu.mean()) / (mu.std() + 1e-12)
    N = len(cand)

    if cov is not None and len(cov):
        S = cov.reindex(index=cand, columns=cand).to_numpy(dtype="float64")
        S = np.nan_to_num(S, nan=0.0)
    else:
        S = np.eye(N)
    S = S + np.eye(N) * 1e-6                           # 数值兜底, 保证 PSD

    w = cp.Variable(N, nonneg=True)
    obj = mu @ w - risk_aversion * cp.quad_form(w, cp.psd_wrap(S))
    cons = [cp.sum(w) <= budget, w <= max_weight]
    if industry is not None and len(industry):
        ind = industry.reindex(cand).fillna("__na__")
        for g in ind.unique():
            m = (ind == g).to_numpy(dtype="float64")
            if m.sum() > 0:
                cons.append(m @ w <= industry_cap)
    # 流动性: 参与率上限。**必须用绝对金额**(权重×资金), 用权重写这条是错的 ——
    # 同一个 5% 权重在 2 万和 2000 万账户上对市场的冲击差 1000 倍。
    if (adv is not None and len(adv) and equity is not None
            and float(equity) > 0 and float(max_participation or 0) > 0):
        adv_c = (pd.to_numeric(adv.reindex(cand), errors="coerce")
                 .to_numpy(dtype="float64") * ADV_UNIT)
        cons.append(cp.multiply(w, float(equity)) <= adv_c * float(max_participation))
        log.info("流动性约束已入 QP: 单票 ≤ %.1f%% × 20日均额 | 可投资金 %.0f 元",
                 100 * float(max_participation), float(equity))
    prob = cp.Problem(cp.Maximize(obj), cons)
    try:
        prob.solve()
    except Exception:
        log.exception("QP 求解失败, 回退 topn")
        return optimize(sc, cov, industry, n, budget, max_weight,
                        industry_cap, risk_aversion, method="topn",
                        equity=equity, adv=adv, max_participation=max_participation)
    if w.value is None:
        log.warning("QP 无解(status=%s), 回退 topn", prob.status)
        return optimize(sc, cov, industry, n, budget, max_weight,
                        industry_cap, risk_aversion, method="topn",
                        equity=equity, adv=adv, max_participation=max_participation)

    wv = pd.Series(np.clip(w.value, 0, None), index=cand)
    wv = wv[wv > 1e-6]
    if len(wv) > n:                                    # 基数: 只截断, **不重归一**(见下)
        wv = wv.sort_values(ascending=False).iloc[:n]
    if wv.sum() <= 0:
        return optimize(sc, cov, industry, n, budget, max_weight,
                        industry_cap, risk_aversion, method="topn",
                        equity=equity, adv=adv, max_participation=max_participation)
    # 2026-09-12 审查 P2-07: 这里原是一行 `wv = wv / wv.sum() * min(budget, wv.sum())` ——
    # 它**恒等于 wv**(约束 `sum(w) <= budget` 保证 min 恒取 wv.sum()), 是一行看起来在"按
    # 预算重归一"、实则什么都没做的死代码。审查者据此判「budget 完全失效」——**那半个判断
    # 实测被否证**(tests/test_optimizer.py::TestQpBudgetActuallyBinds):
    #   · 上限不绑时 QP 解本身就花满预算(sum==budget, 不是"远低于目标仓位");
    #   · sum<budget 只有两种成因: ①单票/行业上限绑住(此时**不能**放大 —— 放大就越界);
    #     ②目标函数的内点解(λ 大 ⇒ 边际收益=边际风险, 解本身就要留现金)。
    # ②在本项目里是**承重行为**(0910 那轮: 三轮提高暴露的实验全被现行支配), 不是 bug。
    # 所以这里不改成 `* budget`(那会在①里把单票权重顶破 max_weight, 20 倍), 而是把
    # 「差多少、为什么差」变成**被观测到的读数** —— 与本模块 `part_max` / `auto_retired: 0`
    # 同一手法。
    budget_gap = float(budget) - float(wv.sum())
    return Allocation(wv, n=len(wv), method="qp",
                      diag={"status": prob.status,
                            "lambda": risk_aversion,
                            "n_candidates": int(N),
                            # 2026-09-13（独立审查 #8）：风险项到底用了外部（通常是 Ledoit-Wolf
                            # 收缩）协方差，还是退化成单位阵？两者产出的"QP 解"看起来一样，
                            # 必须让读的人知道。调用方若用了 shrunk_cov，请自行在 diag 里标注来源。
                            "cov_provided": bool(cov is not None and len(cov)),
                            "sum_w": float(wv.sum()),
                            # >0 = 有预算没用掉; 结合 max_w_hit / 行业上限读成因(见上)
                            "budget_gap": budget_gap,
                            "max_w_hit": bool(float(wv.max()) >= float(max_weight) - 1e-9)
                            if len(wv) else False,
                            **part_extra,
                            **_maybe_part(wv, equity, adv, max_participation)})


def _hrp_alloc(sc, cov, n, budget, max_weight) -> Allocation:
    cand = sc.index[:min(len(sc), max(n * 4, n + 5))]
    if cov is not None and len(cov):
        sub = cov.reindex(index=cand, columns=cand).astype("float64")
        d = np.sqrt(np.diag(sub.to_numpy()))
        d[d == 0] = 1.0
        corr = sub / np.outer(d, d)
    else:
        corr = pd.DataFrame(np.eye(len(cand)), index=cand, columns=cand)
    w = hrp_weights(corr).clip(upper=max_weight)
    w = w / w.sum()
    if len(w) > n:
        w = w.sort_values(ascending=False).iloc[:n]
        w = w / w.sum()
    return Allocation(w * budget, n=len(w), method="hrp",
                      diag={"why": "HRP 只用协方差、不看分数 —— 用于检验 QP 是否被噪声带跑"})


# ---------------------------------------------------------------------------
# ⑤ 落整手(费用感知)
# ---------------------------------------------------------------------------
def to_lots(weights: pd.Series, prices: pd.Series, equity: float,
            cash: float = 0.0, date=None, params: dict = None,
            min_lot_value: float = 0.0) -> Allocation:
    """权重 → **整手股数**。费用与整手粒度都在这里显式处理。

    - 买入按 `fees.trade_cost(amount, 'buy')` 从现金里实扣(不是按比率估算)
    - 不足一手的、或买入后现金为负的, **降一手直到放得下**; 降不动就放弃该标的
    - `min_lot_value` > 0 时, 单笔金额低于它的标的直接不买(避免 5 元地板把费率顶穿)

    **剩下的现金是结果, 不是缺陷** —— 本项目 0910 那轮已证明机械性闲置的现金是承重的。
    """
    avail = float(equity) + float(cash)
    px = prices.reindex(weights.index).astype("float64")
    lots, spent, fee_total = {}, 0.0, 0.0
    for code in weights.sort_values(ascending=False).index:
        p = px.get(code)
        if not np.isfinite(p) or p <= 0:
            continue
        want = float(weights[code]) * float(equity)
        if want < min_lot_value:
            continue
        n_lot = int(want // (p * LOT))
        while n_lot > 0:
            amt = n_lot * LOT * p
            fee = fees.trade_cost(amt, "buy", date, params)
            if spent + amt + fee <= avail:
                lots[code] = n_lot
                spent += amt
                fee_total += fee
                break
            n_lot -= 1
    out_w = pd.Series({c: (v * LOT * px[c]) / float(equity) for c, v in lots.items()})
    return Allocation(out_w, lots=pd.Series(lots, dtype="int64"),
                      cash=avail - spent - fee_total, fees_est=fee_total,
                      n=len(lots), method="lots",
                      diag={"invested": spent, "equity": float(equity)})
