"""周频袖套的报表出口(CLI 文本 + 告警邮件)

发信复用 `app.notify._send_with_retry`（不复制发信逻辑）；邮件正文与主题在本模块内构造。
唯一的例外是**净值/成交读数**：`kpi_lines` 是共用的呈现层，既给本模块的 CLI，
也给 `app/notify.py::_sleeve_section`（日终总结的袖套小节）—— 一处格式，两处消费。

发信开关: `config.yaml` 的 `weekly.email`(缺省 false) → 未显式打开时只打印不发送。

2026-09-18 邮件优化：**删掉了 `send_kpi_email`（袖套净值/成交的独立邮件）**。
理由不是"少发一封"，而是那封的内容与 18:30 的「今日市场与账户总结」重叠、且它
自己也缺口径四件套 ⇒ 现在由 `notify._sleeve_section` 作为日终总结的一个小节承载
（含上次执行的成交）。`kpi_lines` **保留**：CLI(`run_weekly.py kpi`) 与那个小节共用它。
"""
from __future__ import annotations

import html
import logging
from email.header import Header
from email.mime.text import MIMEText

from app import config as cfg_mod
from app.notify import _send_with_retry          # 复用生产重试发送器(不复制发信逻辑)

log = logging.getLogger(__name__)


def kpi_lines(snap: dict) -> list:
    """把 snapshot 渲染成便于人读的若干行(CLI 与邮件共用)。"""
    out = [f"袖套资金 {snap.get('capital', 0):,.0f} | 起始 {snap.get('inception')} "
           f"| 最后盯市 {snap.get('last_mark')}",
           f"净值 {snap.get('nav', 0):,.0f} | 现金 {snap.get('cash', 0):,.0f} "
           f"| 持仓 {snap.get('n_positions', 0)} 只 | 累计成交 {snap.get('n_trades', 0)} 笔 "
           # 闲置现金计息单列: 它是"白捡的"、与研究侧回测口径不同(回测不含), 混进收益里
           # 会让人把 0.37pp 量级的记账项当成 alpha
           f"| 累计计息 ¥{snap.get('interest', 0):,.2f}"]
    if "total_return" in snap:
        out.append(f"累计收益 {100 * snap['total_return']:+.2f}%")
    if "ann_return" in snap:
        out.append(f"年化 {100 * snap['ann_return']:+.2f}% | 回撤 {100 * snap['mdd']:.2f}% "
                   f"| 基准年化 {100 * snap.get('bench_ann', 0):+.2f}% "
                   # 2026-09-21: 周数与日点**并排**显示 —— 单看"N 周"看不出它是自然周
                   # 还是日点(此前二者被混用, 9 个日点就能顶穿"≥8 周"的守卫)
                   f"| 超额 {snap.get('excess_pp', 0):+.2f}pp | 已运行 "
                   f"{snap.get('weeks')} 个自然周"
                   + (f"(净值点 {snap['n_day_points']} 个)" if snap.get("n_day_points") else "")
                   + ")")
    if snap.get("note"):
        out.append(f"注: {snap['note']}")
    h = snap.get("halt") or {}
    if h.get("active"):
        out.append(f"⛔ 风控停买中(自 {h.get('since')}): {h.get('reason')} —— "
                   f"只卖不买, 健康度恢复 healthy 后自动解除(auto_resume)或人工 resume")
    return out


def block_ic_lines(res: dict) -> list:
    """块级 IC 读数若干行（CLI / 运维告警邮件共用；D 案 ③-1）。

    块读数与整体健康**分开看**的意义：整体 IC 在正常带时某一块可能已经先塌（会被平均掉）
    ⇒ 块级读数用来**定位是哪一块、哪个成员**在掉，块间跨块 |ρ|≤0.4 ⇒ 不是把总量切碎重说。

    ⚠️ **定位 ≠ 提前预警**（2026-09-22 回放后修正）：本文件早先写"某一块可能先塌"被当成
    "块级更早"来读；2021 年月频回放判 **NULL** —— 块级报警**没有**比全模型读数更早
    （台账 §5.16）⇒ 这里只承诺定位，不承诺提前量。
    """
    if not res.get("available"):
        return [f"块级 IC 不可用: {res.get('note') or res.get('error') or '无读数'}"]
    out = [f"块级 IC asof {res.get('asof')} | 前瞻 {res.get('horizon')} 交易日 | "
           f"窗 {res.get('window')} 期 / 基线 {res.get('baseline_anchors')} 期"
           f"（基线区间 {res.get('baseline_range')}）"]
    for b, v in (res.get("blocks") or {}).items():
        mark = "⚠" if v.get("alarm") else "·"
        out.append(f"  {mark} {b} {v.get('role', '')}（{v.get('priority')}）"
                   f" IC {v.get('ic')} vs 基线 {v.get('baseline')} = {v.get('ratio')}×"
                   f" {v.get('note') or ''}")
    if res.get("p0_alarms"):
        out.append("  上报(P0): " + ", ".join(res["p0_alarms"])
                   + "；P1/P2 块按 D 案只记录不上报")
        out.append("  ⚠️ 块级是**定位器**不是**提前预警器**：2021 回放判 NULL"
                   "（没比全模型更早，§5.16）⇒ 别当提前量；但有**独立信息**"
                   "（v2 未见区间 2022+2023 判 PASS，§5.17）⇒ 该看，不证明处置有效")
    return out


#: 等级中文（**不带图标** —— 图标由 `send_health_email` 的 `icon` 统一给，两处都放会出现
#: "🟠 告警: 🟠 判据退化" 这种重复）
_LEVEL_CN = {"warning": "轻度预警", "degrading": "判据退化", "dead": "退役候选",
             "healthy": "健康", "unknown": "读数不足"}


def health_threshold_rows(res: dict) -> list:
    """健康邮件的"读数 vs 阈值"三列表（**纯函数**，便于测试）。

    2026-09-24 从 `send_health_email` 里抽出来：换手那一行现在要**并排显示样本量**，
    而并排的前提（只有 1 个卖出日时 `turnover_est` 不代表长期水平，见 07 号 #63）
    是一条**必须被测试钉住的呈现纪律** —— 埋在发信函数里就测不了。
    """
    from app.weekly import health as wk_health
    frozen = res.get("frozen_ic") or wk_health.FROZEN_IC
    bt = wk_health.BACKTEST_TURNOVER
    tn = res.get("turnover_n")
    tmin = res.get("turnover_min_days") or getattr(wk_health, "TURNOVER_MIN_DAYS", None)
    if tn is None:
        note = ""
    elif tmin and tn < tmin:
        note = f"（样本 n={tn}，<{tmin} ⇒ 不判停买）"
    else:
        note = f"（样本 n={tn}）"
    return [(f"滚动 IC（近 {res.get('ic_n') or 12} 期）", res.get("rolling_ic"),
             f"≥ 0.5×冻结基线 {frozen} ⇒ ≥ {0.5 * frozen:.4f}"),
            ("连续为负锚点", res.get("neg_streak"), "≥ 6 判退化；≥ 12 判退役候选"),
            (f"换手（账本估计）{note}", res.get("turnover_est") or res.get("turnover"),
             f"≤ 2×回测基线 {bt} ⇒ ≤ {2 * bt:.3f}；2~3× 预警，>3× 退化"),
            ("投入率", res.get("deployed_ratio"), "≥ 0.60 健康；0.50~0.60 预警")]


def send_health_email(res: dict, kind: str = "alert") -> bool:
    """健康告警邮件（**由 `health.decide_alert` 决定什么时候发**，2026-09-18 改边沿触发）。

    用途: 终端自己发现"实盘行为与验证过的行为不一致"时通知人 —— 这是 L8 闭环里
    **唯一被允许的自动动作: 报警**; 按方案不变量, 不得自动改模型/权重/约束。

    `kind` ∈ {alert, repeat, recovered}：
      - `repeat` = 同一档还没处理、满 7 天的重复提醒（不是"又坏了一次"）；
      - `recovered` = 回到 healthy 的结案信（收件人需要知道那件事结了，否则悬念比告警更扰人）。
    阈值列**从 `health.py` 的常量渲染**，不在邮件里手抄数字：手抄的那份会在下次调阈值时
    变成一句假话（本项目反复踩过"文案与判据分家"）。
    """
    w = (cfg_mod.load_config().get("weekly") or {})
    em = cfg_mod.load_config().get("email", {})
    if not w.get("email", False) or not em.get("enabled") \
            or not em.get("recipients") or not em.get("sender"):
        log.info("weekly.email/email 未启用, 跳过健康告警邮件")
        return False
    from app.weekly import health as wk_health
    esc = html.escape
    level = res.get("level")
    icon = {"warning": "🟡", "degrading": "🟠", "dead": "🔴"}.get(level, "🟢" if level == "healthy" else "⚪")
    rows = "".join(f"<li>{esc(str(x))}</li>" for x in res.get("reasons", [])) or "<li>(无)</li>"
    th = health_threshold_rows(res)
    trows = "".join(
        f"<tr><td>{esc(str(n))}</td><td style='text-align:right'>{esc(str(v))}</td>"
        f"<td>{esc(s)}</td></tr>" for n, v, s in th)
    title = {"recovered": "周频袖套健康已恢复",
             "repeat": "周频袖套健康告警（同一档重复提醒）"}.get(kind, "周频袖套健康告警")
    # 恢复信必须交代停买的去向（2026-09-28 auto_resume 起恢复档自动解除有三种可能）：
    # 调度档在本邮件**之前**已跑 maybe_auto_resume 并把结果挂进 res —— 已解除就说已解除
    # （不许再说"不会自动解除"的假话）；没带该键（CLI --email 路径）或未解除则给人工路径。
    _ar = res.get("auto_resume") or {}
    if kind == "recovered":
        if _ar.get("changed") and _ar.get("saved") is not False:
            tail = ("读数回到 healthy，风控停买已随本档自动解除（auto_resume），"
                    "下个执行档恢复开新仓；如需立即人工干预仍可用 `run_weekly.py resume`。")
        else:
            tail = ("上一封告警可以结案了：读数回到 healthy。若风控仍在停买中，人工 "
                    "`run_weekly.py resume` 可立即恢复开新仓（auto_resume 开启时恢复档"
                    "也会自动解除）。")
    else:
        tail = {"repeat": f"这一档已持续超过 {wk_health.REPEAT_DAYS} 天没处理"
                          "（不是新发生的退化）。仍未处理时袖套处于只卖不买状态。"}.get(kind, "")
    heading = (f"{icon} {esc(title)}" if kind == "recovered"
               else f"{icon} {esc(title)}: {esc(str(_LEVEL_CN.get(level, level)))}")
    body = f"""<html><body style="font-family:'Microsoft YaHei',sans-serif;color:#222;line-height:1.6">
    <h2 style="{'color:#1f7a45' if kind == 'recovered' else 'color:#c0392b'}">{heading}</h2>
    {f"<p style='color:#b45309'>{esc(tail)}</p>" if tail else ""}
    <ul>{rows}</ul>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:14px">
      <tr style="background:#f5f5f5"><th>读数</th><th>当前值</th><th>判据 / 阈值</th></tr>
      {trows}
    </table>
    <p style="color:#999;font-size:12px">自动告警只提示异常, 不会自动改模型/权重/约束;
    如需处置请人工确认。本邮件为<b>边沿触发</b>（等级变化 / 退化满 7 天 / 恢复）⇒
    <b>没有新邮件不代表没在退化</b>，持续状态看早上 08:30 的「今日待办」。</p></body></html>"""
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = Header(f"【NEW ECO】{title} {esc(str(level))}", "utf-8")
    msg["From"] = em["sender"]
    msg["To"] = ", ".join(em["recipients"])
    ok = _send_with_retry(em, msg, "袖套健康")
    log.info("袖套健康告警邮件%s", "已发送" if ok else "发送失败")
    return ok


def send_watchdog_email(item: dict) -> bool:
    """**执行档看门狗**告警：执行日账本里没有当日净值点 ⇒ 执行档没跑或中途失败。

    与健康告警共用发信开关(`weekly.email` + `email.enabled`)。为什么单独一封:
    "策略退化"和"今天根本没交易"是两类事故, 处置动作完全不同, 主题行必须一眼分得清。
    """
    w = (cfg_mod.load_config().get("weekly") or {})
    em = cfg_mod.load_config().get("email", {})
    if not w.get("email", False) or not em.get("enabled") \
            or not em.get("recipients") or not em.get("sender"):
        log.info("weekly.email/email 未启用, 跳过看门狗邮件")
        return False
    esc = html.escape
    # 邮件正文是 HTML，markdown 的 `**加粗**` 不会渲染，只会以字面星号出现在标题里
    # （2026-09-18 邮件优化实测发现；守卫见 tests/test_weekly_report.py）。
    body = f"""<html><body style="font-family:'Microsoft YaHei',sans-serif;color:#222;line-height:1.6">
    <h2 style="color:#c0392b">⛔ 周频袖套执行档没跑</h2>
    <p style="color:#888;font-size:13px">检查项: {esc(str(item.get('name') or 'watchdog'))}</p>
    <p>{esc(str(item.get('detail')))}</p>
    <p style="background:#fff7ed;border:1px solid #fed7aa;padding:8px 12px;border-radius:8px">
      处置: {esc(str(item.get('fix')))}</p>
    <p style="color:#999;font-size:12px">这是"根本没交易"类事故(不是策略退化)。晚间兜底档 18:05
    还会再试一次; 若两档都没落账, 说明调度或行情链路断了, 请人工介入。</p></body></html>"""
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = Header("【NEW ECO】⛔ 周频袖套执行档没跑", "utf-8")
    msg["From"] = em["sender"]
    msg["To"] = ", ".join(em["recipients"])
    ok = _send_with_retry(em, msg, "袖套看门狗")
    log.info("袖套看门狗邮件%s", "已发送" if ok else "发送失败")
    return ok
