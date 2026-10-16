"""周频袖套的**风控自动动作**（2026-09-13）—— 规划 §6.1-4「风控阈值触发时自动降仓或停止」

## 为什么要有它

健康监控（`app/weekly/health.py`）只**报警**；而 §6.1-4 要求阈值触发时**自动降仓或停止**。
两者并不冲突：方案不变量禁止的是「自动改**模型参数/权重/约束**」，不是「自动停止开新仓」。
一个无人值守的终端，报警邮件没人看的时候必须自己有反应。

## 规则（写死在代码里，不许在别处悄悄调）

- **触发**（任一）：健康度 ∈ {`degrading`, `dead`}，或连续为负锚点数 ≥ 6；
- **动作**：**停止开新仓**（不再买入），已有持仓照常按方案卖出 ⇒ 敞口只降不升；
- **解除（2026-09-28 改，用户拍板）**：`weekly.risk_halt.auto_resume` 开启时，健康档读到
  **healthy** 即自动解除（`maybe_auto_resume`；滞回：触发要 `degrading`、解除要严格更好的
  `healthy`，`warning` 不解除 —— 与 governor 的 trip/reset 双带同构）；关闭时维持旧口径
  —— 只人工 `scripts/run_weekly.py resume --reason "..."`。`apply()` 本身仍只置位不解除；
- **开关**：`weekly.risk_halt.enabled`（默认 true；关掉只报警不动作）+ `auto_resume`
  （DEFAULT_CONFIG 缺省 false = 旧「只人工」口径，测试/新装机不变；生产 config.yaml 已开）；
- 触发与解除都写进**账本** `state["halt"]`（带日期/理由/auto 标记）⇒ 事后可审计、可复现。
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def decide(health: dict, cfg: dict) -> dict:
    """纯函数：健康读数 → 是否触发停买。"""
    r = (cfg.get("weekly") or {}).get("risk_halt") or {}
    if not r.get("enabled", True):
        return {"halt": False, "reason": "risk_halt.disabled"}
    level = health.get("level")
    neg = int(health.get("neg_streak") or 0)
    if level == "dead":
        return {"halt": True, "reason": f"健康度 dead(连续 {neg} 个锚点滚动 IC<0 ⇒ 退役候选)"}
    if level == "degrading":
        return {"halt": True, "reason": f"健康度 degrading(连续 {neg} 个锚点滚动 IC<0 或换手 >3×)"}
    return {"halt": False, "reason": f"健康度 {level}"}


def is_halted(led) -> dict | None:
    """账本里的停买状态（未触发返回 None）。"""
    h = led.state.get("halt") or {}
    return h if h.get("active") else None


def governor(led, cfg: dict) -> dict:
    """**回撤治理**（C 层闸，2026-09-25 新增；纯函数在 `app/portfolio/governor.py`）：
    账本净值历史 → 滞回判定（自峰值回撤 ≤ dd_trip 触发，回升 ≥ dd_reset 自动解除）。

    与本模块 `decide` 的分工：`decide`/`halt` 回答"**信号还活着吗**"（IC 退化 ⇒ 停买，
    恢复 healthy 后由 `maybe_auto_resume` 自动解除或人工 resume）；`governor` 回答
    "**账户还在继续失血吗**"（深回撤 ⇒ 停开新仓、退出照旧，回升后**自动解除**）。
    执行档的 allow_buys = 两者都不在。

    判定**零持久状态**（nav_history 是唯一事实源，任何时候可逐位重算）；这里只把
    最新判定副本写进 `state["governor"]` 留痕（随 execute_plan / 健康档的 save() 落盘，
    看板与对账可见），不参与下次判定。
    """
    from app.portfolio import governor as ac_gov
    pts = [(p.get("date"), p.get("nav")) for p in (led.state.get("nav_history") or [])]
    gov = ac_gov.decide(pts, (cfg.get("weekly") or {}).get("governor"))
    led.state["governor"] = {k: gov.get(k) for k in ("cautious", "dd", "since", "reason")}
    if gov.get("cautious"):
        log.warning("回撤治理触发(%s): 停开新仓(退出照旧, 回升过解除带自动解除)",
                    gov.get("reason"))
    return gov


def apply(led, decision: dict, today: str) -> dict:
    """把决定落到账本（幂等）。**只允许置位，不允许自动解除**。"""
    cur = led.state.get("halt") or {}
    if decision.get("halt") and not cur.get("active"):
        led.state["halt"] = {"active": True, "since": str(today), "reason": decision.get("reason")}
        # 2026-09-15（外部独立审查 F4）：save() 的 False = 这次**没写盘**（锁被占/账本被
        # 别的进程改过）。停买是安全动作 —— 写不下去就必须响亮地失败（运维告警 + 台账 error），
        # 而不是返回"已停买"骗自己：内存里 active、磁盘上没有，重启后停买就消失了。
        saved = bool(led.save())
        if not saved:
            log.error("风控停买**未能落账**（账本被其他进程修改或锁被占用）—— 本次调整仅存在于内存")
        else:
            log.warning("周频袖套触发风控停买: %s (自 %s)", decision.get("reason"), today)
        return {"changed": True, "saved": saved, "halt": led.state["halt"]}
    if not decision.get("halt") and cur.get("active"):
        return {"changed": False, "halt": cur,
                "note": "本函数只置位不解除; 解除走健康档 auto_resume(若开) 或人工 "
                        "`run_weekly.py resume --reason ...`"}
    return {"changed": False, "halt": cur or None}


def clear(led, today: str, reason: str = "", auto: bool = False) -> dict:
    """解除停买（CLI `resume` / 健康档 `auto_resume` 共用的落账路径）。未停买时**短路不落账**。

    # 2026-09-28 审查修复: 原实现无条件 save() —— 在账本文件不存在的场景(新装机/演练
    # 时先敲了 resume, Ledger.load(0.0) 凭空构造 0 资金账本)会写出 capital=0 的**毒账本**,
    # 此后执行档因文件已存在永远按 0 资金运行 ⇒ 袖套永久空仓(执行层另有毒账本守卫兜底)。
    `auto=True`（健康档自动解除）在记录里带 `auto: True`，与人工 resume 区分（事后审计）。
    """
    if is_halted(led) is None:
        return {"active": False, "changed": False,
                "note": "未处于停买状态, 无需解除(不落账)"}
    led.state["halt"] = {"active": False, "cleared": str(today),
                         "reason": reason or ("自动解除" if auto else "人工解除")}
    if auto:
        led.state["halt"]["auto"] = True
    if not led.save():
        # 写不进去必须**响亮失败**，否则调用方会打印"已解除"而磁盘上仍是停买
        # （CLI 路径向上抛；健康档路径由 maybe_auto_resume 捕获转 saved=False 发告警）。
        raise RuntimeError("解除停买未能落账（账本被其他进程修改或锁被占用）—— 请重试")
    log.warning("周频袖套%s解除停买: %s", "自动" if auto else "人工", led.state["halt"]["reason"])
    return led.state["halt"]


def maybe_auto_resume(led, health: dict, cfg: dict, today: str) -> dict:
    """健康档专用：停买中且健康度恢复 **healthy** ⇒ 自动解除（2026-09-28 用户拍板）。

    滞回设计：触发（`decide`→`apply`）要 `degrading`/`dead`；解除要**严格更好**的
    `healthy` —— `warning` 不解除（IC 未回到基线带，宁可多停一档也不带病开仓）。
    开关 `weekly.risk_halt.auto_resume`（DEFAULT 缺省 False = 旧「只人工」口径）。
    落账走 `clear()` 同一条路径（auto=True 留痕）；save 被拒**不抛**（调度档后面还有
    看门狗/快照要跑），返回 `saved=False` 由调用方发运维告警 —— 与 risk_halt_not_saved
    同一纪律：解除是恢复性动作，必须让人看见。
    """
    if not (((cfg or {}).get("weekly") or {}).get("risk_halt") or {}).get("auto_resume", False):
        return {"changed": False, "note": "auto_resume.disabled"}
    if is_halted(led) is None:
        return {"changed": False, "note": "not_halted"}
    level = (health or {}).get("level")
    if level != "healthy":
        return {"changed": False, "note": f"level={level}(滞回: 只认 healthy)"}
    try:
        out = dict(clear(led, today, "自动解除: 健康度恢复 healthy", auto=True))
        out.update(changed=True, saved=True)
        return out
    except RuntimeError as e:                     # 落账被拒: 磁盘仍是停买态, 下档照常重试
        return {"changed": True, "saved": False, "note": str(e)}
