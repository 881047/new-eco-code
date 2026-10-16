"""账户级回撤治理(决策函数) —— 三只模拟盘共用的「C 层」回撤闸

## 它是什么(以及不是什么)

把 ECO-W2 三层熔断(`w2/live/circuit.py`)的 **C 层哲学**移植回老盘的两只盘:
**治理的对象是"继续失血", 不是"波动"** —— 触发后**停开新仓**(方案买单/候选替换买入
一律不挂), **退出照旧**(止损/调出/弱势/减仓卖出全部放行), 敞口只降不升; 回升到解除带
后**自动恢复**。与 `weekly.risk_halt`(信号健康 IC 退化 ⇒ 停买且**不自动解除**, 须人工
resume)是两层不同的决策: 那一层回答"信号还活着吗", 这一层回答"账户还在继续失血吗"。

## 判定 = 滞回带(hysteresis), 纯函数、可复算、零持久状态

    触发:   净值自峰值回撤 dd ≤ dd_trip   (默认 -20%)
    解除:   触发后 dd 回升 ≥ dd_reset     (默认 -10%, 即回到峰值 90%)
    再触发: 解除后再跌破 dd_trip 再次触发(解除带与触发带之间不动作 = 滞回防抖)

因为判据是**全历史净值序列的纯函数**(逐点扫描), 任何时候都能从 `paper_nav` /
`ledger.nav_history` 逐位重算, 不需要另一份可丢失/可漂移的状态文件 —— 这是与
W2 `Circuit`(随账本落盘)的**有意差异**: 老盘两只盘的净值历史本身就是唯一事实源。
滞回语义靠"扫描整个历史"而非"记住上一态"实现: 当前态 = 序列从头走到尾的结果。

## 各盘参数与 2026-09-25 A/B 判决(scripts/ab_governor_overlay.py, 证据入 experiments/)

| 盘 | 配置段 | A/B 判决 | 机制 |
|---|---|---|---|
| 2万月盘 `paper.governor` | **停用**(enabled=false) | **判否**: 系统线 -12.76pp/年 且 MDD 反而恶化 -1.53pp; 时限版(gov3) MDD 仍恶化 | 月换手 89.5% ⇒ 「停买+退出照旧」≈ 1~2 个月清仓成现金 ⇒ 回撤冻结在触发位、解除线不可达(20240531 触发后 28 期未解除) —— **清仓陷阱** |
| 100万周盘 `weekly.governor` | **启用**(enabled=true) | **采纳**: 年化 -3.14pp/年(保费), MDD -38.44%→-24.69%(+13.75pp), 日频 +20.95pp, 收益/回撤比 0.53→0.69 | band=8 留存持仓 ⇒ 谨慎期组合不散架、回撤能自然恢复、解除线可达 |
| 2万周盘(ECO-W2) | (内建) | 预注册冻结, 本模块不覆盖 | `w2/live/circuit.py` C1/C2; 低换手结构与本盘袖套同类, A/B 结论不否及它 |

**同一决策函数, 两盘结论相反 —— 差异就在换手率**: 谨慎态"退出照旧"对高换手组合
等价于强制清仓, 对低换手组合只是缓慢失血。把回撤治理装到哪只盘上, 先看它的换手。

⚠️ 这是**风控叠加层**, 不是被回测验证过的收益增强: V 型反转里它会少买回一部分(代价),
持续下跌里它不再向亏损方向加仓(收益)。`band()` 的代码级兜底(enabled 缺省 True)只服务
配置段缺失/损坏的防御场景; 生产缺省以 DEFAULT_CONFIG 为准(月盘 false / 袖套 true)。

## 前向契约

- 只在**下单决策点**闸(apply_plan / apply_signals / 袖套 execute_plan 的 allow_buys),
  **不碰撮合**: `_exec_price` / `execute_pending` 的成交语义一寸不动(袖套复用它, 动了
  就是换两条 track record 的共同口径);
- 已挂出的 pending 买单**不追溯撤销**(它们是触发前口径下的决定, 5 日过期时钟自会收口)。
"""
from __future__ import annotations

DEFAULT_TRIP = -0.20
DEFAULT_RESET = -0.10

# 滞回判定要求的最少净值点数: 两点才谈得上"自峰值的回撤"。
MIN_POINTS = 2

# 阈值比较的容差(W2 `circuit.py` 同款教训): `80/100 − 1 = −0.19999999999999996` ⇒
# 朴素比较会让"恰好 −20%"**不触发**。风险规则不许靠浮点运气决定触发与否 ⇒
# 在阈值侧给 1e-12 容差(触发从宽、解除从宽, 各自朝"动作方向"偏)。
_LE = 1e-12


def band(section: dict) -> tuple:
    """→ (enabled, trip, reset)。

    ⚠️ enabled **缺省 True**(fail-safe, 与 `weekly.risk_halt.enabled` 同取向):
    换机/手删配置段时治理器仍在, 而不是静默失效。
    阈值配置错(正数 trip / reset ≤ trip)直接抛 —— 风控参数坏了必须响, 不许带病降级。
    """
    section = section or {}
    enabled = bool(section.get("enabled", True))
    trip = float(section.get("dd_trip", DEFAULT_TRIP))
    reset = float(section.get("dd_reset", DEFAULT_RESET))
    if not (trip < 0):
        raise ValueError(f"dd_trip 必须为负(自峰值的回撤阈值), 实际 {trip}")
    if not (reset > trip):
        raise ValueError(f"dd_reset({reset}) 必须大于 dd_trip({trip}) —— 解除带在触发带上方")
    return enabled, trip, reset


def hysteresis(navs, dates=None, trip: float = DEFAULT_TRIP,
               reset: float = DEFAULT_RESET) -> dict:
    """纯函数: 净值序列(旧→新) → 滞回判定。

    `dates` 可选, 与 navs 等长, 只用于报 `since`(本次触发 episode 的起点日期)。
    NaN / 非正净值剔除(净值 ≤ 0 是数据病, 不该由风控层解释); 少于 `MIN_POINTS`
    个有效点 ⇒ 不判定、不闸(`reason` 写明), 让"新账户头几天"自然豁免。
    """
    pts = (list(zip(dates, navs)) if dates is not None
           else list(zip([None] * len(navs), navs)))
    pts = [(d, float(v)) for d, v in pts
           if v is not None and v == v and float(v) > 0]   # NaN/非正/缺值剔除(数据病不由风控解释)
    if len(pts) < MIN_POINTS:
        return {"cautious": False, "dd": 0.0, "peak": (pts[-1][1] if pts else None),
                "since": None, "n_points": len(pts),
                "reason": f"有效净值点 {len(pts)} < {MIN_POINTS}, 治理器不判定(不闸)"}
    peak = pts[0][1]
    tripped, since = False, None
    for d, v in pts:
        if v > peak:
            peak = v
        dd = v / peak - 1.0
        if tripped:
            if dd >= reset - _LE:                # 回升过解除带 ⇒ 自动恢复
                tripped, since = False, None
        elif dd <= trip + _LE:                   # 跌破触发带 ⇒ 谨慎态
            tripped, since = True, d
    dd_now = pts[-1][1] / peak - 1.0
    return {"cautious": tripped,
            "dd": round(dd_now, 6), "peak": round(peak, 2),
            "since": str(since) if since is not None else None,
            "n_points": len(pts),
            "reason": (f"自峰值回撤 {dd_now:.2%} ≤ 触发线 {trip:.0%}(自 {since})"
                       if tripped else f"自峰值回撤 {dd_now:.2%}")}


def decide(points, section: dict = None) -> dict:
    """适配器: `points` = [(date, nav)] 旧→新, `section` = 配置段
    (`paper.governor` / `weekly.governor`)。enabled=false ⇒ 显式不闸(带 reason)。
    """
    enabled, trip, reset = band(section)
    if not enabled:
        return {"cautious": False, "dd": None, "peak": None, "since": None,
                "n_points": 0, "reason": "governor.disabled"}
    return hysteresis([v for _, v in points], [d for d, _ in points],
                      trip=trip, reset=reset)
