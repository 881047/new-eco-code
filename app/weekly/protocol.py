r"""纸面 → 小资金 的**放行协议**（2026-09-13 预注册，只读）

## 为什么要把判据现在就写死

§6.4 的阶梯写着「纸面 8~12 周 → 小资金 5%~20%」，但**没写"凭什么放行"**。
等到那时手上已经有数据，人一定会在有数据的情绪里定标准 —— 所以判据必须在**数据出现之前**冻结。

## 最关键的一条：纸面阶段能证明什么、不能证明什么

**12 周纸面只能验证"运行与撮合"，不能验证 alpha。** 量级算一下：周频超额的年化标准差 ≈
`σ_week × √52`，本项目周超额 σ ≈ 5~6% ⇒ 年化噪声 ≈ 36~43pp ⇒ **12 周样本上"年化超额"的
标准误约 ±20~25pp**。也就是说，12 周跑出 +30pp 或 −10pp 都在噪声范围内。
任何"alpha 已被实盘确认"的说法，在**少于约 2 年**的样本上都站不住。

因此本协议**不设 alpha 门槛**（设了就是自欺），只设**可测量的运行门槛**：

| 闸 | 判据 | 为什么是这条 |
|---|---|---|
| G1 连续运行 | 净值点 ≥ 12 且最后一点在 14 天内 | 无人值守链路真的每周都跑通了（缺一期就说明有洞） |
| G2 撮合口径 | 成交价对当日开盘的 \|**残差**均值\| ≤ 20bps<br>（残差 = 偏离 − 该方向本应有的滑点） | 回测假设滑点 10bps；实测长期偏离大于它 ⇒ 回测口径偏乐观（F30 的成本结论要打折） |
| G3 资金投放 | 平均投入率 ≥ 0.70 | 回测 0.769；低于它就说明整手粒度/可负担性把敞口吃掉了一块（F24 的现金拖累） |
| G4 风控状态 | 未处于停买（`halt.active == False`） | 🟠/🔴 触发过就说明模型在实盘上退化，先查清楚再谈加钱 |
| G5 记账完整 | 资金恒等式成立 且 归因能算出四轴 | 账本/归因是后面所有判断的依据，坏一个就全不可信 |

> **G2 判据变更记录（2026-09-19，2026-09-13 原式冻结在后）**：原写作
> `|成交价对开盘的均值偏离| ≤ 20bps`，但买单偏离本应 ≈ +10bps、卖单 ≈ −10bps，
> 混在一个序列里取均值 ⇒ 混合账本上必然对消成 ≈0（实测 425 笔回放簿：均值 −0.0，
> 而单笔最差偏离 11.1bps）—— 那是一条**永远不会红**的闸，放行结论里 G2 那一票是白送的。
> 现改判**残差均值**（先按方向扣掉该笔本应有的滑点，再取均值），**阈值 20bps 一字未改**、
> "无成交不判"不动 ⇒ 属"写错了改对"（可失败性变高），不属放宽判据。
> 判据冻结与判读见 `plans/g2-residual-criterion-20260919.md`；结论登记见知识库 14 号三补二十六。

**全部通过 ⇒ 允许进入小资金档（5%~20%），但仍不等于 alpha 被证明**；任何一条不过 ⇒
写清楚缺哪条、怎么补，不允许"四舍五入"。

与 §2.28 的关系：C3 样本外判据（≥+10pp）在 **F28** 上已**不通过** ⇒ 即便 G1~G5 全过，
也只是"运行可信"，不是"超额可信"。本协议刻意不把 C3 塞进放行门槛里 —— 混在一起就会变成
"运行没问题所以超额也没问题"这种最危险的推理。

CLI：`run_weekly.py protocol`（只读）。
"""
from __future__ import annotations

import logging

import pandas as pd

from app.weekly import reconcile as rc
from app.weekly.ledger import Ledger, week_anchors

log = logging.getLogger(__name__)

MIN_WEEKS = 12
MAX_STALE_DAYS = 14
MAX_ABS_SLIP_BPS = 20.0   # 2026-09-19 起 G2 判的是**残差均值**(见下方 G2 注释)，阈值未变；
                          # 名字保留是为了不让台账/脚本里的引用悬空
MIN_INVESTED = 0.70
#: B1（流动性/规模）暴露偏离**可交易域中位数**多少个 σ 才标注（D 案 ③-3，2026-09-22）。
#: 1σ 是"看得见但不吵"的量级：F67 实测剔微盘段会让留出集转负，规模暴露是超额的本体，
#: 但**要不要动作属于换 pin 级决策** ⇒ 本项只标注，绝不自动改任何东西。
B1_SIGMA_ALERT = 1.0


def size_exposure_note(size_z, threshold: float = B1_SIGMA_ALERT) -> dict:
    """B1 规模暴露守卫（纯函数，便于钉住）：`size_z` = 组合对 size 的加权暴露，
    单位是可交易域截面的**标准差**（0 = 与池子中位数一致，+1 = 偏大盘 1σ）。

    返回 `{"available", "size_z", "threshold", "flagged", "note"}`；
    `size_z` 取不到（无归因缓存 / 非有限值）时 `available=False`，**不**当成"正常"。
    """
    if size_z is None or pd.isna(size_z) or not isinstance(size_z, (int, float)):
        return {"available": False, "size_z": None, "threshold": threshold,
                "flagged": False, "note": "读不到规模暴露（归因尚未出数）⇒ 本项无读数，不按正常处理"}
    z = float(size_z)
    if not pd.isna(z) and (z == float("inf") or z == float("-inf")):
        return {"available": False, "size_z": None, "threshold": threshold,
                "flagged": False, "note": f"规模暴露非有限值({size_z}) ⇒ 本项无读数"}
    flagged = abs(z) > threshold
    return {"available": True, "size_z": round(z, 4), "threshold": threshold,
            "flagged": bool(flagged),
            "note": (f"规模暴露 {z:+.2f}σ ⇒ **偏离可交易域中位数 >{threshold:g}σ**（"
                     f"{'偏大盘' if z > 0 else '偏小盘'}）；仅标注，**不动作** —— "
                     f"动作属于换 pin 级决策，需另立预注册")
                    if flagged else
                    f"规模暴露 {z:+.2f}σ，在 ±{threshold:g}σ 内（正常）"}


def _nav_series(led) -> pd.DataFrame:
    h = led.state.get("nav_history") or []
    if not h:
        return pd.DataFrame(columns=["date", "nav", "invested"])
    df = pd.DataFrame(h)
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
    return df.dropna(subset=["date"])


def status(ledger_path=None) -> dict:
    """逐闸给出当前状态（只读；任何异常都降级成该闸"取不到"）。"""
    led = Ledger.load(0.0, ledger_path)
    nav = _nav_series(led)
    inv = (float((nav["invested"] / nav["nav"]).mean())
           if len(nav) and (nav["nav"] > 0).all() else 0.0)
    # 2026-09-28 收编 timeutil(东八区): stale 天数用本地钟换机漂一天
    from app import timeutil
    stale = ((timeutil.now() - nav["date"].max()).days if len(nav) else None)
    price = rc.price_deviation(led.state.get("trades") or [])
    ident = rc.cash_identity(led)
    halt = (led.state.get("halt") or {}).get("active", False)
    # 2026-09-21 修正: G1 原来是 `len(nav) >= 12` —— `len(nav)` 是**日点**数(每天盯市一个),
    # 而这条闸的本意是"连续运行 **12 周**"。约 12 个日点(≈2.5 周)就会点亮放行,
    # 于是 09-29 前后会打印出误导性的"可进入小资金档"。改用真·自然周(唯一口径)。
    n_anchor = week_anchors(nav["date"]) if len(nav) else 0
    gates = {
        "G1_连续运行": {
            "ok": bool(n_anchor >= MIN_WEEKS and stale is not None and stale <= MAX_STALE_DAYS),
            "detail": (f"覆盖 {n_anchor}/{MIN_WEEKS} 个自然周（净值点 {len(nav)} 个，"
                       f"日点≈周数×5，不可混用），最后一点距今天 {stale} 天")},
        "G2_撮合口径": {
            # ⚠️ 不能用 `x or 99`: 偏离正好是 0.0 时它是 falsy, 会被兜底成 99 ⇒
            # **完美撮合反而判 ✗**（2026-09-13 用真实回放账本做集成演练时踩到）。
            # 2026-09-19 审查 B3: 被测量的量从 `mean_bps` 换成 `resid_mean_bps` ——
            # 均值把买单(+10bps)与卖单(−10bps)混在一起, 混合账本上必然对消成 ≈0,
            # 于是这条放行闸**永远不可能红**（回放簿实测 mean=−0.0 / 单笔最差 11.1bps）。
            # 残差 = 先按方向扣掉该笔本应有的滑点再取均值 ⇒ 执行劣化不再被方向抵消。
            # 阈值与"无成交不判"一字未动（只收紧不放宽）；判据冻结见
            # `plans/g2-residual-criterion-20260919.md`。缺 `resid_mean_bps`（旧版核对结果）
            # 时**按不通过处理**并在 detail 里说明 —— 闸宁可假红, 不可假绿。
            "ok": bool(price.get("n") and price.get("resid_mean_bps") is not None
                       and abs(float(price["resid_mean_bps"])) <= MAX_ABS_SLIP_BPS),
            "detail": (f"成交 {price.get('n', 0)} 笔，执行残差均值 "
                       f"{price.get('resid_mean_bps')} bps（阈值 {MAX_ABS_SLIP_BPS:.0f}）"
                       f"｜单笔最差偏离 {price.get('max_abs_bps')} bps"
                       if price.get("n") else price.get("note"))},
        "G3_资金投放": {
            "ok": bool(inv >= MIN_INVESTED),
            "detail": f"平均投入率 {inv:.3f}（阈值 {MIN_INVESTED}）"},
        "G4_风控状态": {
            "ok": not halt,
            "detail": ("停买中：" + str((led.state.get("halt") or {}).get("reason"))) if halt
                      else "未停买"},
        "G5_记账完整": {
            "ok": bool(ident.get("ok") and len(nav) >= 2),
            # 同理: cash_identity 成功时没有 `n` 键 ⇒ 按 `n` 判断会打印"无净值点"(实测踩到)
            "detail": (f"恒等式差 {ident.get('gap')}" if "gap" in ident
                       else ident.get("note", "无净值点"))},
    }
    passed = [k for k, v in gates.items() if v["ok"]]
    # B1 规模暴露（D 案 ③-3）：**不是第六道闸** —— 放行结论只由 G1~G5 决定，
    # 这一项只是把"规模敞口跑到哪了"并排显示出来（F67：剔微盘段会让留出集转负）。
    try:
        from app.weekly import attrib as wk_attrib
        b1 = size_exposure_note((wk_attrib.cached() or {}).get("size_z_mean"))
    except Exception as e:                          # 标注项不许把放行协议带崩
        log.warning("B1 规模暴露读数失败: %s", e)
        b1 = {"available": False, "size_z": None, "threshold": B1_SIGMA_ALERT,
              "flagged": False, "note": f"读不到规模暴露: {e}"}
    return {"gates": gates, "n_pass": len(passed), "n_gates": len(gates),
            "b1_size_exposure": b1,
            "weeks": int(n_anchor),            # 真·自然周(见 G1 的修正说明)
            "n_day_points": int(len(nav)),     # 日点数(每天盯市一个), 与 weeks 不可混用
            "alpha_disclaimer": ("12 周样本的年化超额标准误约 ±20~25pp ⇒ 本协议不设 alpha 门槛；"
                                 "C3 样本外判据在 F28 上已不通过，运行可信 ≠ 超额可信"),
            "verdict": ("可进入小资金档（5%~20%）—— 仅代表运行可信，不代表 alpha 已确认"
                        if len(passed) == len(gates)
                        else "未达放行：还差 " + "、".join(k for k in gates if k not in passed))}


def lines(res: dict) -> list:
    out = [f"纸面放行协议：{res['n_pass']}/{res['n_gates']} 通过 | 已运行 {res['weeks']} 周"]
    for k, v in res["gates"].items():
        out.append(f"  {'✓' if v['ok'] else '✗'} {k}: {v['detail']}")
    b1 = res.get("b1_size_exposure") or {}
    if b1:      # 标注项：有读数就并排显示（放行与否与它无关，别让人误读成第六道闸）
        out.append(f"  · B1 规模暴露（标注项，不计入放行）: {b1.get('note')}")
    out.append(f"  ⇒ {res['verdict']}")
    out.append(f"  注: {res['alpha_disclaimer']}")
    return out
