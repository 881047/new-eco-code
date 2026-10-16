"""Alpha Health / Decay —— **只许「退役」, 不许「加权」**(2026-09-11)

对应用户框架图的 `Alpha Health / Decay → 自我进化循环 → Alpha Factory`。

## 为什么「自我进化」在这一环被限制成「只退役」

框架图把健康度画成一条反馈回路: 读数 → 调整 → 回到 Alpha Factory。本项目对这条回路
做过一次完整的证伪实验 —— 方案 §10a 的 **R1 电源门控**:

> **按 die 的滚动已实现表现决定供不供电 = 追涨杀跌。** `die_flow` 在 2023 年打出
> +6.93%/月, 滚动读数因此一路为正 ⇒ 路由器一路供电 ⇒ **正好供电进 2024-26 它最差的
> 那一段**。

**这不是参数问题, 是信息问题**: 事前不存在可用读数能区分「这个 die 明年会变差」。
等读数显示它变差时, 你已经过完了它变差的那一年。所以本模块把健康度分成两类,
并且**只对第二类动作**:

| | 例子 | 本模块的处置 |
|---|---|---|
| **描述性衰减** | 收益掉、IC 掉、排名掉 | **只登记读数, 绝不据此退役** —— 那是 R1 的失败模式 |
| **结构性变化** | `hk_hold` 2024-08 覆盖度 4042→793 断崖; 与已验证 alpha 秩相关 ≈1 | **可以生成退役候选** |

判据的不对称是**有意的**: 描述性衰减将来可能变好(`die_flow` 死区 head +0.51%、
活跃区 −1.78%, 两段符号相反就是证据), 而结构性变化意味着**这个 alpha 已经不是原来
那个东西了** —— 它不会"变回来", 因为它连含义都换了。

## 硬规则(结构上强制, 不是靠自觉)

- **没有任何加权接口** —— `weights()` 直接抛异常并说明理由(与 `fusion.py` 拒绝写
  `model_score`、`pipeline.py` 不给 Critic 改状态的路径是同一套做法)
- **不自动退役** —— `retire_candidates()` 只产出候选; 退役是 `bank.retire()` 的显式动作
- **没有结构性证据的 alpha 进不了候选名单** —— `retire_worthy()` 只认 STRUCTURAL

## 边界

- 本模块**只读**: 不写任何表, 不改任何状态
- 它**不判断 alpha 好不好** —— 那是门禁(`gate.py`)与组合层两轴(`pipeline.py`)的事。
  这里只回答"它还是不是原来那个东西"。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import NoReturn

import numpy as np
import pandas as pd

from app.alpha import bank, critic, gate

log = logging.getLogger(__name__)

COVERAGE_DROP = 0.35        # 覆盖度断崖门槛(相对跌幅); hk_hold 实测跌 80%
REDUNDANCY_RHO = 0.95       # 与已有 alpha 秩相关超过它 = 同一个东西(方案 §7 硬规则)
DECAY_HEAD = -0.005         # 描述性衰减的**登记**门槛(月均 head), 与退役无关

STRUCTURAL = "structural"   # 含义变了 —— 可以退役
DESCRIPTIVE = "descriptive" # 表现掉了 —— 只登记

NO_WEIGHTS_REASON = (
    "本模块不提供按健康度加权/复活 alpha 的接口。理由是一整轮实验: R1 电源门控"
    "(按滚动已实现表现路由)已被证伪 —— 它等价于追涨杀跌, die_flow 2023 年 +6.93%/月 "
    "使路由器一路供电, 正好供电进它 2024-26 最差的那段。组合权重只由 "
    "portfolio/optimizer.py 依据**当期分数与风险**决定, 与「这个 alpha 过去赚了多少」无关。"
)


# ---------------------------------------------------------------------------
# ① 覆盖度 —— 结构性探测
# ---------------------------------------------------------------------------
def coverage_curve(wide: pd.DataFrame) -> pd.Series:
    """每月有分数的股票数。**结构性变化的第一个探针。**

    覆盖度是 alpha 的"定义域"。定义域变了, 分数就不再是同一个东西 ——
    这正是 `hk_hold` 那件事: 2024-08-19 交易所停止披露后覆盖 4042 → 793,
    此后稳定在 800~960。**含义静默改变, 而不是"表现变差"。**
    """
    return wide.notna().sum(axis=1).astype("int64")


def coverage_break(curve: pd.Series, drop_ratio: float = COVERAGE_DROP,
                   min_months: int = 6) -> dict | None:
    """覆盖度断崖: 找出"从此以后再也回不到从前"的那个月。

    判据是**前后段均值之比**, 不是逐月环比 —— 环比会把正常波动也报成断崖。
    要求断崖前后各有 `min_months` 个月, 避免在序列两端各一个点就下结论。

    返回 `{date, before, after, ratio}` 或 None(没有断崖)。
    """
    c = pd.Series(curve).dropna()
    if len(c) < min_months * 2:
        return None
    best = None
    for i in range(min_months, len(c) - min_months + 1):
        before = float(c.iloc[:i].mean())
        after = float(c.iloc[i:].mean())
        if before <= 0:
            continue
        ratio = after / before
        if ratio <= 1.0 - drop_ratio:
            if best is None or ratio < best["ratio"]:
                best = {"date": c.index[i], "before": before,
                        "after": after, "ratio": ratio}
    return best


# ---------------------------------------------------------------------------
# ② 冗余 —— 结构性探测
# ---------------------------------------------------------------------------
def redundancy(wide: pd.DataFrame, others: dict, md,
               thresh: float = REDUNDANCY_RHO) -> dict | None:
    """与已知 alpha 的秩相关 ≥ thresh ⇒ 冗余。

    这是方案 §7 写死的硬规则(「与已有 die 的秩相关 > 0.95 则退役」)。冗余是**结构性**的:
    一个与 `model_score` 秩相关 0.99 的 alpha, 无论它自己表现多好, 对组合都没有新增信息,
    而它会占掉一个本可以给真正独立信号的口粮位。
    """
    if not others:
        return None
    got = critic.corr_with_known(wide, others, md)      # 复用 Critic 的同一把尺子
    mx = got.get("_max_abs")
    if not mx or not np.isfinite(mx.get("rho", np.nan)):
        return None
    if abs(mx["rho"]) < thresh:
        return None
    return {"factor": mx["factor"], "rho": float(mx["rho"]), "thresh": float(thresh)}


# ---------------------------------------------------------------------------
# ③ 描述性衰减 —— **只登记**
# ---------------------------------------------------------------------------
def decay_readings(wide: pd.DataFrame, fwd: pd.DataFrame, md, zones) -> dict:
    """分段 + 逐年的 head 读数。**只登记, 不动作。**

    刻意复用 `gate.evaluate` / `gate._stat` —— 与门禁**同一把尺子**。换一把尺子量同一条
    alpha, 会造出"门禁说行、健康度说不行"这种无法归因的分歧。
    """
    det = gate.evaluate(wide, fwd, md, zones)
    if det.empty:
        return {}
    out = {"n_months": int(len(det)), "yearly": gate.yearly(det), "zones": {}}
    for z in ("DEAD", "ACTIVE"):
        sub = det[det["zone"] == z]
        if sub.empty:
            continue
        m, t, hit, n = gate._stat(sub["head_excess"])
        out["zones"][z] = {"head": m, "t": t, "hit": hit, "n": n}
    return out


# ---------------------------------------------------------------------------
# ④ 体检报告
# ---------------------------------------------------------------------------
@dataclass
class Reason:
    kind: str                   # STRUCTURAL / DESCRIPTIVE
    code: str                   # coverage_break / redundancy / decay / no_values
    detail: str
    evidence: dict = field(default_factory=dict)


@dataclass
class HealthReport:
    alpha_id: str
    status: str = ""
    coverage: pd.Series = field(default_factory=lambda: pd.Series(dtype="int64"))
    decay: dict = field(default_factory=dict)
    reasons: list = field(default_factory=list)

    def structural(self) -> list:
        return [r for r in self.reasons if r.kind == STRUCTURAL]

    def descriptive(self) -> list:
        return [r for r in self.reasons if r.kind == DESCRIPTIVE]

    def retire_worthy(self) -> bool:
        """**只有结构性理由能触发退役。**

        描述性衰减单独存在时永远返回 False —— 这条规则是一整轮实验(R1 电源门控)换来的,
        不是保守起见的默认值。
        """
        return bool(self.structural())

    def as_dict(self) -> dict:
        return {
            "alpha_id": self.alpha_id, "status": self.status,
            "retire_worthy": self.retire_worthy(),
            "coverage_first": None if self.coverage.empty else int(self.coverage.iloc[0]),
            "coverage_last": None if self.coverage.empty else int(self.coverage.iloc[-1]),
            "structural": [{"code": r.code, "detail": r.detail} for r in self.structural()],
            "descriptive": [{"code": r.code, "detail": r.detail} for r in self.descriptive()],
            "decay": self.decay,
        }


def audit(alpha_id: str, wide: pd.DataFrame = None, fwd: pd.DataFrame = None,
          md=None, zones=None, others: dict = None) -> HealthReport:
    """给一条 alpha 做体检。**只读** —— 不改状态、不写表。

    `wide` 缺省从 `bank.load_values(alpha_id)` 取(需要该 alpha 登记时存过打分序列)。
    `fwd` / `md` / `zones` 只影响**描述性**读数, 不给也不影响退役判定。
    """
    rec = bank.get(alpha_id) or {}
    rep = HealthReport(alpha_id=alpha_id, status=rec.get("status", ""))
    if wide is None:
        try:
            wide = bank.load_values(alpha_id)
        except Exception:
            wide = None
    if wide is None or len(wide) == 0:
        rep.reasons.append(Reason(DESCRIPTIVE, "no_values",
                                  "没有存下打分序列, 只登记不判定(不影响退役)") )
        return rep

    rep.coverage = coverage_curve(wide)
    brk = coverage_break(rep.coverage)
    if brk:
        rep.reasons.append(Reason(
            STRUCTURAL, "coverage_break",
            "覆盖度断崖: %s 起从 %.0f 只掉到 %.0f 只(×%.2f) —— 定义域变了, "
            "分数不再是同一个东西" % (pd.Timestamp(brk["date"]).date(),
                                      brk["before"], brk["after"], brk["ratio"]),
            brk))

    if md is not None and others:
        red = redundancy(wide, others, md)
        if red:
            rep.reasons.append(Reason(
                STRUCTURAL, "redundancy",
                "与 %s 的截面秩相关 %.3f ≥ %.2f —— 冗余, 对组合无新增信息"
                % (red["factor"], red["rho"], red["thresh"]), red))

    if fwd is not None and md is not None and zones is not None:
        rep.decay = decay_readings(wide, fwd, md, zones)
        act = rep.decay.get("zones", {}).get("ACTIVE", {})
        head = act.get("head")
        if head is not None and np.isfinite(head) and head < DECAY_HEAD:
            rep.reasons.append(Reason(
                DESCRIPTIVE, "decay",
                "活跃区 head %+.2f%%/月(t=%.2f) 低于登记门槛 —— **只登记, 不据此退役**"
                % (100 * head, act.get("t", np.nan)), act))
    return rep


def audit_all(md=None, zones=None, others: dict = None, fwd: pd.DataFrame = None,
              limit: int = None) -> list:
    """体检 `alpha_bank` 里所有**非 RETIRED** 的 alpha(只读)。

    RETIRED 是终态, 不再体检 —— 否则每跑一次都会把同一条已退役的 alpha 重新报成候选,
    逼人去分辨"这是新发现还是老名单"。
    """
    df = bank.list_alphas(limit=limit)
    if df is None or not len(df):
        return []
    if "status" in df.columns:
        df = df[df["status"] != "RETIRED"]
    out = []
    for aid in df["alpha_id"]:
        try:
            out.append(audit(aid, fwd=fwd, md=md, zones=zones, others=others))
        except Exception:
            log.exception("体检 %s 失败, 跳过", aid)
    return out


def retire_candidates(reports) -> list:
    """**只返回有结构性理由的**报告。描述性衰减一律不进候选名单。"""
    return [r for r in reports if r.retire_worthy()]


# ---------------------------------------------------------------------------
# ⑤ 结构性拒绝: 没有加权接口
# ---------------------------------------------------------------------------
def weights(*_args, **_kwargs) -> NoReturn:
    """**结构上拒绝** —— 本模块不提供任何按健康度给 alpha 加权/复活的接口。

    留着这个函数(而不是干脆不写)是**故意的**: 下一个想加"按近期表现调权"的人会先找到
    这里, 而不是在别处悄悄实现一个。与 `fusion.write_scores` 拒绝写 `model_score` 同理。
    """
    raise NotImplementedError(NO_WEIGHTS_REASON)
