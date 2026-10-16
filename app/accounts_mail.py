r"""全账户邮件（2026-10-05）：三模型全纸面账户的邮件覆盖层。

**为什么做在 NEW ECO 而不是三个项目各发各的**：全站唯一 SMTP 出口在
`app/notify.py`（`_send_with_retry` 重试发送 + `notify_log` 台账 + 设置页邮件配置面，
授权码住 `.env` 不进 git）。fterm（D:\\Final）与 ECO-W2（D:\\ECO-W2）两仓
**零邮件代码**（fterm 的 `notify.email_to` 是无消费者死配置，其 alerts 表只进不出；
ECO-W2 熔断只写账本 JSON）——在枢纽仓集中实现，授权码/收件人只有一份，
发送历史在 /api/notify 一页可查，`app/weekly/report.py` 复用 notify 内部发送器
已是既有先例。数据源是 `app/all_accounts.py` 的只读聚合（与 /#/accounts 看板同源），
本模块**只读**：不改任何账本、不提供写接口。

覆盖矩阵（历史背景：2026-10-05 前有 5 个账户零邮件；2026-10-05 二轮起含交易行为）：
- **全部账户**：每日 21:15「全账户日报」——按模型分节（NEW ECO / fterm / ECO-W2，
  与 /#/accounts 看板同序），每节账户表含**今日成交**列与模型小计；
  排在 fterm_daily 20:05、ECO-W2 paper_daily 20:30/20:35、NEWECO 卫星档 21:00 之后，
  保证读到的都是当日落账结果；非交易日各源数据不动，报表照发（滞后判定继续生效）。
- **全部账户**：异常按账户发「账户告警」（同 key 同日去重，失败不写标记下轮重试）：
  账本读取失败 / 数据滞后≥3 天 / fterm 权益触及熔断线 / fterm 新告警 / fterm-w2 心跳缺失 /
  ECO-W2 熔断三层 / NEW ECO 月频盘交易总闸暂停。
- **有交易行为的账户**（fterm 双轨 / ECO-W2 双臂 / NEW ECO 周频袖套）：当日 21:15 逐户发
  「账户成交」邮件（明细表；周频 09:32 执行、当晚送达）。月频盘走既有 09:31 即时邮件
  （本模块刻意排除防双发）；ECO-W2 CB 账本不记逐笔成交、无邮件可发（日报注记）。
- **盘前（08:30 今日待办）**：`morning_actions()` 供 build_daily_brief 渲染
  「其他账户」节——Final 挂单将撮合、W2 执行日/下期执行日（本仓 trade_cal 复算锚点规则）。
- NEW ECO 两账户的原有档位（08:30 今日待办 / 09:31 成交 / 18:00 信号 / 18:30 日终 /
  19:45 袖套健康）保持不动，本模块不重复它们。
"""
from __future__ import annotations

import html
import logging
import time
from email.header import Header
from email.mime.text import MIMEText

from app import config as cfg_mod
from app import db
from app.all_accounts import _norm_date, build_all
# 复用生产发送器与信封（weekly/report.py:22 同一先例，不复制发信逻辑）
from app.notify import _envelope, _send_with_retry, _table, _yuan, send_ops_alert

log = logging.getLogger(__name__)

#: 日报 tag。**不能改**：scheduler 开机补跑按 `notify_log.tag='全账户日报'`
#: 判"今天发过没有"（与 '模拟盘日终汇总' 的 tag 契约同一套纪律）。
REPORT_TAG = "全账户日报"
ALERT_TAG = "账户告警"
#: 每账户成交邮件 tag（同日同账户去重：内存 + model_meta 双层，见 _meta_marker_*）。
TRADE_TAG = "账户成交"
#: 三模型选股交集早报 tag（2026-10-08 新增；同日去重同日报契约）。
PICKS_TAG = "三模型选股交集"

#: 日报/成交邮件的模型展示顺序与口径注（前端 /#/accounts 同序）。
MODEL_ORDER = ["NEW ECO", "Final", "W2"]
MODEL_NOTE = {
    "NEW ECO": "月频盘 09:31 即时成交邮件；周频袖套健康档 19:45（边沿触发）",
    "Final": "dlv2 双轨 · t+1 开盘价撮合 · 每日 20:05 档落账",
    "W2": "B0 双臂（锚定周调仓）· 每日 20:30 档落账",
}

#: 有既有成交邮件通道的账户（避免同一笔成交双发）：月频盘 09:31 即时。
_SKIP_TRADE_MAIL = {"neweco-monthly"}

_LEVEL_CN = {"bad": "严重", "warn": "警告"}

# ---------------------------------------------------------------- 告警知识库
# 用户 2026-10-06 要求：告警不能只报现象，必须说清"意味着什么 / 影响什么 / 该怎么办"。
# 每个体检项（code）在这里登记解释条目；assess() 给每条告警挂上这四件，
# 账户告警邮件与日报告警块共用同一套文案（口径单源）。新增告警码必须同步补条目——
# 缺条目时渲染"未登记"占位（宁可显眼也不静默）。
_ALERT_KB = {
    "read_failed": {
        "title": "账本读取失败",
        "mean": "聚合层读不出该账户的账本文件——日报里它的权益/持仓是旧快照或空，"
                "账户处于「失明」状态。",
        "impact": "今晚该账户的真实成交与净值变化都无法确认；成交邮件也发不出，"
                  "直到账本恢复可读。",
        "act": "看本邮件「发生了什么」里的具体错误：文件不存在 = 该仓落账任务没跑成"
               "（fterm 20:05 / ECO-W2 20:30/20:35，查计划任务结果与 data/logs）；"
               "损坏/权限 = 用 backup/ 或 .bak 恢复，不要手改账本。",
    },
    "stale": {
        "title": "数据滞后",
        "mean": "账本最新数据日落后全站最新数据日 ≥3 天——不是数据慢，是该账户的"
                "落账任务连续几天没跑成。",
        "impact": "日报里它的权益/持仓是旧读数；当天的真实成交可能漏报"
                  "（账本里还没有那些行）。",
        "act": "查对应落账任务：fterm = 计划任务 fterm_daily（20:05，日志 "
               "data/logs/run_daily.log，exit 2 = 数据未就绪属老盘日链问题）；"
               "ECO-W2 = ECO-W2_stock_paper_daily（20:30/20:35）。连续多天红先修任务。",
    },
    "paper_paused": {
        "title": "交易总闸暂停",
        "mean": "paper.paused=true——月频盘总闸被关：不挂单、不成交，存量挂单已作废。",
        "impact": "月频盘完全停摆：数据链与记账照常，但没有任何新交易，直到恢复。",
        "act": "先确认是不是你主动停的。恢复 = config.yaml 把 paper.paused 改回 false"
               "（改完即时生效，无需重启）；不是你停的就先查配置被谁改的。",
    },
    "halt_line_breach": {
        "title": "触及熔断线",
        "mean": "账户权益已跌破总回撤熔断线（fterm total_dd_halt，从峰值回撤 12% 线）——"
                "组合级风控被触发。",
        "impact": "风控进入保护态：只拒新开、只许减仓；未人工确认重置前不会再开新仓。",
        "act": "先分清回落是行情还是故障：行情回落属机制保护，确认后再人工重置；"
               "口径见 fterm docs/02 §参数基线。熔断不会随邮件自动解除。",
    },
    "no_heartbeat": {
        "title": "运行心跳缺失",
        "mean": "fterm 的 state 库里没有今天的运行心跳——每日 20:05 落账档"
                "（计划任务 fterm_daily）今天很可能没跑。",
        "impact": "今天的信号/挂单/成交都不会有：账户停在上一交易日的状态，"
                  "明晚的撮合也会缺一日信号。",
        "act": "查计划任务 fterm_daily 上次结果与 data/logs/run_daily.log"
               "（bat 失败 / 数据未就绪都会 exit 2）；手动补跑一次 "
               "scripts/run_daily_auto.py 即可追平（幂等）。",
    },
    "fterm_alerts": {
        "title": "本地告警",
        "mean": "",   # 运行时按最新一条的告警码从 _FTERM_ALERT_CN 填充
        "impact": "单条多为执行层的正常约束留痕（封板/缺数属市场与数据现实，不是 bug）；"
                  "连续多日同类告警才值得当问题查。",
        "act": "按下文最新一条的具体内容判断；全量留痕在 fterm 对应 state 库的 alerts 表"
               "（8787 全账户页下钻也能看到）。",
    },
    "circuit_halted": {
        "title": "熔断（停交易层）",
        "mean": "ECO-W2 的 A 层硬停机被触发——单期不可成交占比超限（>5%）或数据断供"
                " ≥4 周，账本 halted=true。",
        "impact": "该账户停止交易；A 层**不可自动解除**，需人工复核后复位。",
        "act": "查 ECO-W2 账本 runs/ 与参考表（refnav）定位触发原因；确认价格供给/数据"
               "断供恢复后再人工复位。",
    },
    "circuit_derisked": {
        "title": "降险层（derisked）",
        "mean": "实现层偏离等价性检查越线——滚动 8 周相对参考表偏离 >150bp"
                "（或单期 >100bp），说明纸面与回测口径可能不再等价。",
        "impact": "停开新仓、强制人工复核；存量持仓按既有规则处理。",
        "act": "跑 scripts/recon_paper.py（§5 偏差五项）定位漂移来源（撮合/数据/费用），"
               "确认口径恢复后再解除。",
    },
    "circuit_cautious": {
        "title": "谨慎层（cautious）",
        "mean": "净值触及 C1 软线（自峰值 −20%）——进入**可自动复位**的观察态。",
        "impact": "按 C 层规则降级运行；连续 8 周无 A/B 层事件、或净值较触发日回升 ≥10% "
                  "时自动复位，无需人工介入。",
        "act": "看回落是行情还是执行偏差即可，日报会跟踪复位；若连续触发再看换手与"
               "成本是否失控。",
    },
}

#: fterm 本地告警码 → （中文名, 意味着什么）。fterm 侧 alerts 表只进不出，
#: 这里在邮件层把它翻译成人话；未识别的码走兜底（原文透传，不猜）。
_FTERM_ALERT_CN = {
    "fill_blocked": ("封板弃单",
                     "有挂单因目标股全天封死涨停/跌停而弃单——买不进/卖不出属市场约束"
                     "不是故障；订单作废，下个调仓日重排。"),
    "fill_rejected": ("撮合硬拦",
                      "有挂单被现金/持仓校验拦下（现金不足 / 无持仓卖出 / 超卖）——"
                      "账面与挂单意图不符，值得核对账本。"),
    "atr_missing": ("ATR 缺失",
                    "有候选股因 ATR 缺失/非正被按 0 手跳过——该股当天未建仓，"
                    "属数据缺失提示非故障。"),
    "watchdog": ("心跳超时",
                 "进程外探活发现 runner 心跳超过 3×阈值——服务可能停过，"
                 "看门狗会拉起并发自愈邮件。"),
    "watchdog_ok": ("探活正常",
                    "探活正常（INFO 级留痕，无需处理）。"),
}


# ---------------------------------------------------------------- 每账户体检

def _paper_paused() -> bool:
    """月频盘交易总闸（消费口径与三处既有消费点一致：.get(..., False)）。"""
    try:
        return bool(cfg_mod.load_config().get("paper", {}).get("paused", False))
    except Exception:
        return False


def assess(payload: dict = None) -> list:
    """对全部账户逐个体检，返回告警列表。

    元素: {account_id, account_name, model, level("bad"/"warn"), code, message}。
    纯函数（除 paper.paused 读本仓配置外不碰外部状态）；payload 缺省现取
    `all_accounts.build_all()`（15s 进程内缓存，调度档直调无压力）。
    """
    payload = payload if payload is not None else build_all()
    alerts = []

    def _add(a, level, code, message):
        kb = _ALERT_KB.get(code) or {}
        alerts.append({"account_id": a["id"], "account_name": a["name"],
                       "model": a["model"], "level": level, "code": code,
                       "message": message,
                       # 告警三件套（2026-10-06 内容优化：只报现象不说含义的告警
                       # 会被当成"又一个红字"忽略——每条必须自带解释与处置）：
                       "title": kb.get("title") or code,
                       "mean": kb.get("mean") or f"（体检项 {code} 尚未在 _ALERT_KB 登记解释——新增告警码时补条目）",
                       "impact": kb.get("impact") or "—",
                       "act": kb.get("act") or "—"})

    for a in payload.get("accounts") or []:
        if not a.get("ok"):
            _add(a, "bad", "read_failed", f"账本读取失败: {a.get('error') or '未知错误'}")
            continue
        if a.get("stale"):
            _add(a, "warn", "stale",
                 f"数据日 {a.get('asof') or '—'} 落后全局最新数据日 ≥3 天"
                 "（落账任务可能没跑/没成功）")
        ex = a.get("extra") or {}
        if a["id"] == "neweco-monthly" and _paper_paused():
            _add(a, "warn", "paper_paused",
                 "交易总闸 paper.paused=true：不挂单、不成交，存量挂单已作废")
        if a["id"].startswith("fterm-"):
            halt, equity = ex.get("halt_line"), a.get("equity")
            if halt is not None and equity is not None and equity <= halt:
                _add(a, "bad", "halt_line_breach",
                     f"权益 {_yuan(equity)} 已触及总回撤熔断线 {_yuan(halt)}"
                     "（total_dd_halt，需人工确认重置）")
            # 心跳只写在**第一轨（w2）**的 state 库（runner._run_dlv2 尾部 db0.heartbeat，
            # note='dlv2 asof=... tracks=N' 覆盖全部轨道）—— t3 库没有心跳是设计如此，
            # 对它体检会每天误报。要查的是"每日 20:05 档跑没跑"，w2 的心跳就是那个信号。
            if a["id"] == "fterm-w2" and not ex.get("heartbeat"):
                _add(a, "warn", "no_heartbeat", "运行心跳缺失（每日 20:05 档可能未跑）")
            hits = ex.get("alerts") or []
            if hits:
                latest = hits[0]
                latest_code = str(latest.get("code") or "")
                cn_name, cn_mean = _FTERM_ALERT_CN.get(
                    latest_code, ("", f"未识别的本地告警码「{latest_code or '?'}」，按原文理解"))
                _add(a, "warn", "fterm_alerts",
                     f"本地告警近 20 条中出现 {len(hits)} 条，最新 {latest.get('ts') or ''} "
                     f"[{latest.get('level') or ''}] {cn_name}：{latest.get('message') or ''}")
                # fterm_alerts 的"意味着什么"按**最新一条的码**具体化（KB 的通用解释是占位）
                alerts[-1]["mean"] = cn_mean
        if a["id"].startswith("ecow2-"):
            circuit = ex.get("circuit") or {}
            if circuit.get("halted"):
                _add(a, "bad", "circuit_halted", "ECO-W2 熔断 halted=true（停交易层）")
            if circuit.get("derisked"):
                _add(a, "warn", "circuit_derisked", "ECO-W2 已进入 derisked 降险层")
            if circuit.get("cautious"):
                _add(a, "warn", "circuit_cautious", "ECO-W2 已进入 cautious 谨慎层")
    return alerts


# ---------------------------------------------------------------- 每账户成交邮件

def collect_today_trades(payload: dict, today8: str = None) -> dict:
    """按账户收集**今日**成交。三处账本的 trade.date 语义都=成交执行日（已核实：
    weekly ledger 写 trade_date、fterm fills.ts=asof 日、ECO-W2 trades.date=t1），
    21:15 档过滤 date==today 即完整捕获当日成交。

    排除 `_SKIP_TRADE_MAIL`（月频有 09:31 即时邮件；CB 账本无逐笔成交）。
    返回 {account_id: [trade, ...]}，只含有成交的账户。
    """
    today8 = today8 or time.strftime("%Y%m%d")
    out = {}
    for a in payload.get("accounts") or []:
        if a.get("id") in _SKIP_TRADE_MAIL or not a.get("ok"):
            continue
        hits = [t for t in (a.get("trades") or [])
                if _norm_date(t.get("date")) == _norm_date(today8)]
        if hits:
            out[a["id"]] = hits
    return out


def _trade_rows_html(trades):
    """成交表行（列随数据源自适应：ECO-W2 有费用列，fterm 费用记在 reason）。"""
    esc = html.escape
    has_fee = any(t.get("fee") is not None for t in trades)
    rows = []
    for t in trades:
        side = str(t.get("side") or "").lower()
        side_cn = "买入" if side == "buy" else "卖出" if side == "sell" else str(t.get("side") or "--")
        amount = t.get("amount")
        if amount is None and t.get("shares") is not None and t.get("price") is not None:
            amount = t["shares"] * t["price"]
        cells = [f"<td>{esc(side_cn)}</td>",
                 f"<td>{esc(str(t.get('code') or '--'))}</td>",
                 f"<td style='text-align:right'>{t.get('shares') if t.get('shares') is not None else '--'}</td>",
                 f"<td style='text-align:right'>"
                 f"{'--' if t.get('price') is None else format(t['price'], '.3f')}</td>",
                 f"<td style='text-align:right'>{_yuan(amount) if amount is not None else '--'}</td>"]
        if has_fee:
            cells.append(f"<td style='text-align:right'>"
                         f"{_yuan(t['fee']) if t.get('fee') is not None else '--'}</td>")
        cells.append(f"<td>{esc(str(t.get('reason') or ''))}</td>")
        rows.append("".join(cells))
    headers = ["方向", "代码", "数量", "价格", "金额"] + (["费用"] if has_fee else []) + ["理由"]
    return _table(headers, rows, empty_text="无成交")


def _send_trade_email(a: dict, trades: list, today8: str, cfg: dict) -> bool:
    """单账户成交邮件（组装+发送；调用方负责去重与异常兜底）。"""
    n_buy = sum(1 for t in trades if str(t.get("side") or "").lower() == "buy")
    n_sell = len(trades) - n_buy
    verdict = f"今日成交 {len(trades)} 笔（买 {n_buy} / 卖 {n_sell}）"
    if a["id"].startswith("fterm-"):
        action = ("fterm 为 t+1 口径：信号当晚挂单、次日开盘价自动撮合 —— 本表即已撮合结果，"
                  "无需动作；下一批信号见今晚 20:05 档。")
    elif a["id"].startswith("ecow2-"):
        action = "ECO-W2 按锚定日调仓（目标调入/调出），已自动执行，无需动作。"
    else:
        action = "已自动执行，无需动作。"
    body = _envelope(f"NEW ECO · 账户成交 · {a['name']}", today8, verdict,
                     "<h3>今日成交明细</h3>" + _trade_rows_html(trades),
                     action=action, tone="ok")
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = Header(f"【NEW ECO】账户成交[{a['name']}] {today8} {len(trades)} 笔", "utf-8")
    msg["From"] = cfg["sender"]
    msg["To"] = ", ".join(cfg["recipients"])
    if not _send_with_retry(cfg, msg, TRADE_TAG):
        return False
    log.info("账户成交邮件已发送: %s %d 笔", a["name"], len(trades))
    return True


_TRADE_SENT: dict = {}


def _meta_marker_sent(key: str, day: str) -> bool:
    """读 model_meta 同日去重标记(2026-10-13)。真为"今天已发过"⇒ True。

    为什么要落库: 2026-10-13 实测 —— 服务一晚自愈重启 3 次(22:51/23:05/23:25, 见
    watchdog.log 与 S13), 每次开机补跑都把纯内存去重清零 ⇒ 当天已发过的
    账户告警/成交邮件原样重发(notify_log 三组重复), 用户侧表现为"一直收到重复邮件"。
    日报不受影响(它按 notify_log.tag 查库去重), 本函数把告警/成交拉齐到同一语义。
    查库失败按"未发过"处理(fail-open): 至多重发一封, 不允许丢信。
    """
    try:
        with db.conn_ctx() as conn:
            return db.get_meta(conn, key) == day
    except Exception:
        log.exception("邮件去重标记读取失败(按未发过处理): %s", key)
        return False


def _meta_marker_write(key: str, day: str) -> None:
    """发送成功后写 model_meta 去重标记(重启存活)。失败只影响去重, 不影响主流程
    (set_meta 自身已吞 OperationalError; 这里再兜 conn 层异常)。

    ⚠️ 2026-10-15 修复（静默回滚 bug）：`conn_ctx` 关闭连接**不提交事务** —— Python
    sqlite3 在 close 时把未提交事务**静默回滚**（无异常、无日志）⇒ 此前本函数的
    INSERT 从未真正落库 ⇒ 每次服务重启后全部告警/成交邮件原样重发（实测 21:15/21:23/
    21:24/21:40 四组重发）。修复 = 显式 `conn.commit()`；回归钉
    `tests/test_accounts_mail.py::TestMetaMarkerRoundTrip`（真库文件，非 mock）。"""
    try:
        with db.conn_ctx() as conn:
            db.set_meta(conn, key, day)
            conn.commit()          # 2026-10-15：不 commit = close 时静默回滚
    except Exception:
        log.exception("邮件去重标记写库失败(重启后该封可能重发一次): %s", key)


def send_trade_emails(payload: dict, today8: str = None) -> int:
    """有今日成交的账户逐户发成交邮件，返回发出户数。

    同日同账户双层去重（`_TRADE_SENT` 内存 + `_meta_marker_*` 落库 model_meta，
    只在发送成功后写标记——语义与 `_account_alert_once` 一致；服务重启后开机补跑
    靠落库标记判重，不再重发。DB 均不可用时退化为纯内存去重）。
    邮件未配置/未启用 → 0。
    """
    today8 = today8 or time.strftime("%Y%m%d")
    by_id = collect_today_trades(payload, today8)
    if not by_id:
        return 0
    cfg = cfg_mod.load_config().get("email", {})
    if not cfg.get("enabled") or not cfg.get("recipients") or not cfg.get("sender"):
        log.info("账户成交邮件: 邮件未配置或未启用, 跳过")
        return 0
    acc_by_id = {a["id"]: a for a in payload.get("accounts") or []}
    sent = 0
    for aid, trades in by_id.items():
        key = f"trade_{aid}_{today8}"
        if _TRADE_SENT.get(key) == today8:
            continue
        mkey = f"mail_trade:{aid}:{today8}"
        if _meta_marker_sent(mkey, today8):
            _TRADE_SENT[key] = today8   # 回填内存, 本进程后续档不再查库
            continue
        a = acc_by_id.get(aid) or {}
        try:
            ok = _send_trade_email(a, trades, today8, cfg)
        except Exception:
            log.exception("账户成交邮件发送失败(不影响主流程): %s", aid)
            ok = False
        if ok:
            _TRADE_SENT[key] = today8
            _meta_marker_write(mkey, today8)
            sent += 1
    return sent


# ---------------------------------------------------------------- 盘前行动提醒

def _ecow2_next_exec(last_anchor8: str):
    """复算 ECO-W2 下期锚定/执行日（**不跨仓 import**，用本仓 trade_cal 只读复算
    D:\\ECO-W2 w2/pipeline/cal.py 的锚点规则：每 ISO 周最大开市日=锚点，其后首个开市日=t1）。

    trade_cal 覆盖 20130101~20271231（整本日历含 is_open 标志）⇒ 无需等"周走完"。
    日历查询失败/锚点缺失 → None（调用方降级文案）。
    """
    if not last_anchor8 or not str(last_anchor8).isdigit():
        return None
    from datetime import datetime
    try:
        with db.conn_ctx() as conn:
            rows = conn.execute(
                "SELECT cal_date FROM trade_cal WHERE cal_date>? AND is_open=1 "
                "ORDER BY cal_date", (str(last_anchor8),)).fetchall()
    except Exception:
        log.exception("ECO-W2 下期执行日复算失败")
        return None
    weeks = {}
    for (d,) in rows:
        iso = datetime.strptime(d, "%Y%m%d").isocalendar()[:2]
        weeks.setdefault(iso, []).append(d)
    if not weeks:
        return None
    anchor = max(weeks[min(weeks)])
    t1 = next((d for (d,) in rows if d > anchor), None)
    return {"anchor": anchor, "t1": t1}


def morning_actions(payload: dict = None, today8: str = None) -> list:
    """盘前（08:30 今日待办）的**其他账户**行动条目（NEW ECO 两账户由既有 brief 节覆盖）。

    返回 [{account_id, account_name, model, level("ok"/"warn"/"bad"), text}]，
    只读、故障降级为条目级文案，绝不抛 —— 晨间邮件不能因为聚合层抖动发不出去。
    刻意**不含任何净值读数**（早间正文不出现净值是既有测试红线）。
    """
    today8 = today8 or time.strftime("%Y%m%d")
    try:
        payload = payload if payload is not None else build_all()
    except Exception as e:
        return [{"account_id": "-", "account_name": "全账户聚合", "model": "-",
                 "level": "bad", "text": f"聚合层读取失败: {e}"}]
    out = []
    for a in payload.get("accounts") or []:
        if a.get("id") in ("neweco-monthly", "neweco-weekly"):
            continue                     # 既有「今日待办」的挂单/袖套节已覆盖
        item = {"account_id": a.get("id"), "account_name": a.get("name"),
                "model": a.get("model"), "level": "ok", "text": ""}
        if not a.get("ok"):
            item["level"] = "bad"
            item["text"] = f"账本读取失败: {a.get('error') or '未知错误'}"
            out.append(item)
            continue
        ex = a.get("extra") or {}
        if a["id"].startswith("fterm-"):
            n = len(ex.get("orders") or [])
            if n:
                first = ((ex["orders"][0] or {}).get("reason") or "").strip()
                suffix = f"（{first[:24]}…）" if first else ""
                item["text"] = f"今晚 20:05 档将按今日开盘价撮合 {n} 笔挂单{suffix}"
                out.append(item)
        elif a["id"].startswith("ecow2-"):
            periods = ex.get("periods") or []
            last_anchor = (periods[0] or {}).get("anchor") if periods else None
            nxt = _ecow2_next_exec(last_anchor)
            if nxt and nxt["t1"] == today8:
                item["level"] = "warn"
                item["text"] = f"今日为执行日（锚定 {nxt['anchor']}），20:30/20:35 档将自动调仓"
                out.append(item)
            elif nxt:
                item["text"] = f"下期执行日 {nxt['t1']}（锚定 {nxt['anchor']}）"
                out.append(item)
            else:
                item["text"] = "下期执行日待日历数据更新"
                out.append(item)
    return out


# ---------------------------------------------------------------- 日报邮件

def _status_cell(a, alerts_by_id) -> str:
    """账户行的状态格（已产出 HTML，调用方直接拼接）。体检项用**中文名**——
    裸代码（halt_line_breach）对读邮件的人是第二次翻译负担（2026-10-06）。"""
    esc = html.escape
    if not a.get("ok"):
        return "<td style='color:#c0392b'>✗ 读取失败</td>"
    hits = alerts_by_id.get(a["id"]) or []
    if hits:
        names = "、".join(sorted({str(x.get("title") or x["code"]) for x in hits}))
        return f"<td style='color:#b45309'>⚠ {len(hits)} 项（{esc(names)}）</td>"
    if a.get("stale"):
        return "<td style='color:#b45309'>数据滞后</td>"
    return "<td style='color:#1f7a45'>✓ 正常</td>"


def build_daily_report(payload: dict = None, alerts: list = None,
                       today8: str = None) -> dict:
    """组装日报：返回 {subject, body(html), tone, alerts}（发送与组装分离，测试友好）。

    2026-10-05 内容优化：由单张 7 行大表改为**按模型分节**（NEW ECO / fterm / ECO-W2，
    与 /#/accounts 看板同序同口径），每节一张账户表（含**今日成交**列与模型小计）+ 口径注；
    告警节保持；verdict 增加当日成交计数。
    2026-10-06 内容优化：①告警表加"体检项"中文名，表下逐条给「意味着什么/影响/建议」
    （与告警邮件同源 `_ALERT_KB`）；②"今日成交"计数改全账户口径（含月频盘——
    它有 09:31 即时邮件被排除在成交邮件之外，但日报的成交列必须如实计数）。
    """
    payload = payload if payload is not None else build_all()
    alerts = alerts if alerts is not None else assess(payload)
    esc = html.escape
    t = payload.get("totals") or {}
    # 2026-10-15 E1：账户数**派生**，不再硬编码 7（账户已增至 8，硬编码即假读数）
    _n_acct = len(payload.get("accounts") or [])
    by_id = {}
    for x in alerts:
        by_id.setdefault(x["account_id"], []).append(x)
    # 日报的成交计数是**全账户口径**（含月频盘）：月频走 09:31 即时邮件被
    # collect_today_trades 排除是对的（防双发），但日报的"今日成交"列与 verdict
    # 计数若也排除，月频当天成交了会显示 0 = 假象（2026-10-06 修复）。
    # CB 账本无逐笔成交，天然为 0，口径注里说明。
    today8 = today8 or time.strftime("%Y%m%d")
    n_tr_all = {a["id"]: sum(1 for tt in (a.get("trades") or [])
                             if _norm_date(tt.get("date")) == _norm_date(today8))
                for a in (payload.get("accounts") or []) if a.get("ok")}

    sections = ""
    for model in MODEL_ORDER:
        accs = [a for a in (payload.get("accounts") or []) if a.get("model") == model]
        if not accs:
            continue
        subtotal = sum(a.get("equity") or 0.0 for a in accs if a.get("ok"))
        rows = []
        for a in accs:
            pct, dd = a.get("pct"), a.get("dd_pct")
            n_tr = n_tr_all.get(a["id"], 0)
            rows.append(
                "<tr>"
                f"<td>{esc(str(a.get('name') or ''))}</td>"
                f"<td style='text-align:right'>{_yuan(a.get('equity'))}</td>"
                f"<td style='text-align:right'>{('--' if pct is None else format(pct, '+.2f') + '%')}</td>"
                f"<td style='text-align:right'>{('--' if dd is None else format(dd, '.2f') + '%')}</td>"
                f"<td style='text-align:right'>{_yuan(a.get('cash'))}</td>"
                f"<td style='text-align:right'>{_yuan(a.get('pos_value'))}</td>"
                f"<td>{esc(str(a.get('asof') or '—'))}</td>"
                f"<td style='text-align:right'>{('--' if not a.get('ok') else str(n_tr))}</td>"
                f"{_status_cell(a, by_id)}</tr>")
        sections += (
            f"<h3>{esc(model)}（{len(accs)} 账户 · 小计 {_yuan(subtotal)}）</h3>"
            + _table(["账户", "权益", "自建仓", "回撤", "现金", "持仓市值", "数据日",
                      "今日成交", "状态"], rows)
            + f"<p style='color:#888;font-size:12px'>口径：{esc(MODEL_NOTE.get(model, ''))}</p>")

    n_bad = sum(1 for x in alerts if x["level"] == "bad")
    n_warn = sum(1 for x in alerts if x["level"] == "warn")
    n_trades = sum(n_tr_all.values())
    ok_n, total = t.get("n_ok", 0), t.get("n_total", 0)
    tr_part = f"｜ 今日成交 {n_trades} 笔" if n_trades else "｜ 今日无成交"
    if n_bad:
        verdict = f"{n_bad} 项严重异常 / {n_warn} 项警告 —— 需要人工{tr_part}"
        tone, action = "bad", ("按下面「账户告警」逐条处理——每条都写了意味着什么与建议处置；"
                               "处理前相关账户的告警每天会重提。")
    elif n_warn:
        verdict = f"全部 {ok_n}/{total} 账户可读，{n_warn} 项警告{tr_part}"
        tone, action = "warn", "警告项多数不影响交易，但每条都值得花一分钟看下面的解释。"
    else:
        verdict = f"全部 {ok_n}/{total} 账户正常，总权益 {_yuan(t.get('equity'))}{tr_part}"
        tone, action = ("ok",
                        "无需动作 —— 各账户的下一档任务见各节口径注；出现异常时会按账户"
                        "单独发信（含 意味着什么/建议处置），不会只在这里悄悄变红。")

    alert_rows = [
        f"<tr><td>{esc(str(x['account_name']))}</td>"
        f"<td style='color:{'#c0392b' if x['level'] == 'bad' else '#b45309'}'>"
        f"{esc(_LEVEL_CN.get(x['level'], x['level']))}</td>"
        f"<td>{esc(str(x.get('title') or x['code']))}</td>"
        f"<td>{esc(str(x['message']))}</td></tr>"
        for x in alerts]
    # 每条告警的"意味着什么 / 建议处置"（与逐账户告警邮件同源同文案，口径单源）。
    # 只报现象的告警块会被读成"又一个红字"——这里必须把含义和动作给足。
    alert_explain = "".join(
        f"<li><b>{esc(str(x.get('title') or x['code']))} · {esc(str(x['account_name']))}</b>"
        f" —— {esc(str(x.get('mean') or ''))}"
        f"<br>影响：{esc(str(x.get('impact') or '—'))}"
        f"<br>建议：{esc(str(x.get('act') or '—'))}</li>"
        for x in alerts)
    sections += ("<h3>账户告警（本档同步逐账户发过，同日同因不重复）</h3>"
                 + _table(["账户", "级别", "体检项", "发生了什么"], alert_rows,
                          empty_text=f"无 —— {_n_acct} 个账户全部干净")
                 + (f"<ul style='margin:8px 0 0 18px'>{alert_explain}</ul>" if alert_explain else ""))
    body = _envelope(f"NEW ECO · 全账户日报（三模型 {_n_acct} 纸面账户）",
                     time.strftime("%Y-%m-%d %H:%M"), verdict, sections,
                     action=action, tone=tone)
    subject = f"【NEW ECO】全账户日报：{verdict}"
    return {"subject": subject, "body": body, "tone": tone, "alerts": alerts}


def send_daily_report(payload: dict = None, force: bool = False,
                      alerts: list = None) -> bool:
    """发「全账户日报」。邮件未配置/未启用 → False；同日已发过且非 force → False。

    同日去重按 `notify_log.tag='全账户日报' AND ok=1`（与日终汇总的补跑契约同款），
    这样**服务崩溃重启后的开机补跑**重复调本函数也只会发出一封。
    `alerts` 可传入调用方已算好的体检结果（调度档走这条路，避免体检跑两遍）。
    """
    cfg = cfg_mod.load_config().get("email", {})
    if not cfg.get("enabled") or not cfg.get("recipients") or not cfg.get("sender"):
        log.info("全账户日报: 邮件未配置或未启用, 跳过")
        return False
    if not force:
        today = time.strftime("%Y-%m-%d")
        try:
            with db.conn_ctx() as conn:
                sent = conn.execute(
                    "SELECT COUNT(*) FROM notify_log WHERE tag=? AND ok=1 "
                    "AND substr(created_at,1,10)=?", (REPORT_TAG, today)).fetchone()[0]
            if sent:
                log.info("全账户日报: 今日已发过, 跳过重复发送")
                return False
        except Exception:
            log.exception("全账户日报: 去重查询失败(按未发过处理)")
    rep = build_daily_report(payload, alerts)
    msg = MIMEText(rep["body"], "html", "utf-8")
    msg["Subject"] = Header(rep["subject"], "utf-8")
    msg["From"] = cfg["sender"]
    msg["To"] = ", ".join(cfg["recipients"])
    if not _send_with_retry(cfg, msg, REPORT_TAG):
        return False
    log.info("全账户日报邮件已发送（告警 %d 项）", len(rep["alerts"]))
    return True


# ------------------------------------------------- 三模型选股交集早报（2026-10-08）

def build_picks_email() -> dict:
    """组装「三模型选股交集」早报：{subject, body(html), tone}（组装/发送分离，测试友好）。

    数据 = app/model_picks.build_picks(force=True)（三模型 Top100 + 交集，纯只读）。
    语气纪律：空交集是结构性正常读数（三套宇宙/频率不同），tone 恒 ok/中性，
    **不**把"交集为 0"渲染成异常；单模型榜不可用时如实点名并说明交集只按可用榜计。
    """
    from app.model_picks import build_picks
    p = build_picks(force=True)
    esc = html.escape
    models = p.get("models") or {}
    inter = p.get("intersection") or {}
    counts = inter.get("counts") or {}
    order = list(MODEL_ORDER)

    # 榜单头：各榜 asof + 口径（不可用的榜如实点名原因）
    head_rows = []
    unavailable = []
    for m in order:
        b = models.get(m) or {}
        if b.get("ok"):
            asof = b.get("asof") or "—"
            head_rows.append(f"<tr><td>{esc(m)}</td><td>{esc(str(asof))}</td>"
                             f"<td>{esc(str(b.get('n') or 0))}</td>"
                             f"<td>{esc(str(b.get('source') or ''))}</td></tr>")
        else:
            unavailable.append(f"{m}（{b.get('error') or '不可用'}）")
    sections = ("<h3>三模型榜单（各取前 100）</h3>"
                + _table(["模型", "数据日", "入榜", "口径"], head_rows,
                         empty_text="三个榜单全部不可用"))
    if unavailable:
        sections += (f"<p style='color:#b45309;font-size:13px'>榜缺席：{esc('；'.join(unavailable))}"
                     f"——交集只按可用榜计算（两榜交集仍有效）。</p>")

    # 三重交集
    triple = inter.get("triple") or []
    tr_rows = []
    for t in triple:
        r = t.get("ranks") or {}
        tds = "".join(f"<td style='text-align:right'>{esc(str(r.get(m) or '—'))}</td>"
                      for m in order)
        tr_rows.append(f"<tr><td>{esc(str(t.get('code') or ''))}</td>"
                       f"<td>{esc(str(t.get('name') or ''))}</td>{tds}</tr>")
    sections += (f"<h3>三模型同时点名（{len(triple)} 只）</h3>"
                 + _table(["代码", "名称"] + [f"{m} 名次" for m in order], tr_rows,
                          empty_text="无 —— 三套宇宙/频率不同，空交集是结构性正常读数非故障"))

    # 两两交集（2026-10-08 用户"也展示两两模型交集"：与三重交集同款表格——
    # 代码/名称/两榜各自名次，每对一张表；不再用代码串截断展示）
    pairs = inter.get("pairs") or {}
    pair_html = ""
    for pk, pv in pairs.items():
        parts = [x.strip() for x in str(pk).split("∩")]
        m1, m2 = (parts + ["", ""])[:2]
        rows = []
        for c in (pv.get("codes") or []):
            r = c.get("ranks") or {}
            rows.append(f"<tr><td>{esc(str(c.get('code') or ''))}</td>"
                        f"<td>{esc(str(c.get('name') or ''))}</td>"
                        f"<td style='text-align:right'>{esc(str(r.get(m1) or '—'))}</td>"
                        f"<td style='text-align:right'>{esc(str(r.get(m2) or '—'))}</td></tr>")
        pair_html += (f"<h3>{esc(str(pk))}（{esc(str(pv.get('n') or 0))} 只）</h3>"
                      + _table(["代码", "名称", f"{esc(m1)} 名次", f"{esc(m2)} 名次"], rows,
                               empty_text="无交集"))
    sections += ("<h3>两两交集</h3>" + (pair_html or
                 "<p style='color:#888;font-size:13px'>可用榜不足两个，两两交集无意义</p>"))

    n_triple = counts.get("triple")
    ok_models = [m for m in order if (models.get(m) or {}).get("ok")]
    asof_part = "｜".join(
        f"{m} {((models.get(m) or {}).get('asof') or '—')}" for m in ok_models)
    if n_triple is None or not ok_models:
        verdict = "三个榜单均不可读 —— 今日交集无法计算"
        tone, action = "warn", "查 model_picks 三个数据源（daily_score / fterm EMA / ECO-W2 sidecar）哪个断了。"
    elif len(ok_models) < 3:
        verdict = f"可用榜 {len(ok_models)}/3，三重交集今日不计；缺席原因见正文"
        tone, action = "warn", "缺席榜单的重建档位见正文榜缺席注；明日自动恢复则无需动作。"
    elif n_triple:
        verdict = f"三模型共同入选 {n_triple} 只（{asof_part}）"
        tone = "ok"
        action = ("共同点名 = 三套独立证据的强共识读数；个股复核点看板 /#/picks。"
                  "这是观察清单，不构成买卖指令。")
    else:
        verdict = f"今日无三模型共同入选（{asof_part}）"
        tone, action = ("ok",
                        "空交集是结构性正常读数（月频日推分/日频 EMA/周频小市值规则三套宇宙不同）；"
                        "两两交集见正文，无需动作。")

    body = _envelope("NEW ECO · 三模型选股交集（每日早报）",
                     time.strftime("%Y-%m-%d %H:%M"), verdict, sections,
                     action=action, tone=tone,
                     footer_extra="口径：三模型 Top100 与交集的完整版见看板 /#/picks；"
                                  "本邮件每交易日 08:35 发送。")
    subject = f"【NEW ECO】三模型选股交集：{verdict}"
    return {"subject": subject, "body": body, "tone": tone}


def send_picks_email(force: bool = False) -> bool:
    """发「三模型选股交集」早报。未配置/未启用 → False；同日已发过且非 force → False。

    同日去重按 `notify_log.tag='三模型选股交集' AND ok=1`（与全账户日报同款契约，
    服务崩溃重启后的开机补跑也只会发出一封）。
    """
    cfg = cfg_mod.load_config().get("email", {})
    if not cfg.get("enabled") or not cfg.get("recipients") or not cfg.get("sender"):
        log.info("三模型选股交集: 邮件未配置或未启用, 跳过")
        return False
    if not force:
        today = time.strftime("%Y-%m-%d")
        try:
            with db.conn_ctx() as conn:
                sent = conn.execute(
                    "SELECT COUNT(*) FROM notify_log WHERE tag=? AND ok=1 "
                    "AND substr(created_at,1,10)=?", (PICKS_TAG, today)).fetchone()[0]
            if sent:
                log.info("三模型选股交集: 今日已发过, 跳过重复发送")
                return False
        except Exception:
            log.exception("三模型选股交集: 去重查询失败(按未发过处理)")
    rep = build_picks_email()
    msg = MIMEText(rep["body"], "html", "utf-8")
    msg["Subject"] = Header(rep["subject"], "utf-8")
    msg["From"] = cfg["sender"]
    msg["To"] = ", ".join(cfg["recipients"])
    if not _send_with_retry(cfg, msg, PICKS_TAG):
        return False
    log.info("三模型选股交集早报已发送（tone=%s）", rep["tone"])
    return True


# ---------------------------------------------------------------- 调度档入口

#: 同 key 同日去重（与 scheduler._ops_alert_once 同一套语义；这里独立成字典是因为
#: scheduler import 本模块注册档位，反向 import 会成环）。**只在发送成功后写标记**。
_ALERTED: dict = {}


def _account_alert_once(key: str, title: str, body: str) -> bool:
    """同 key 同日只发一次。双层去重：内存 `_ALERTED`（快路径）+ model_meta 落库
    标记（2026-10-13 补；服务自愈重启会清内存但不清库，见 _meta_marker_sent 注）。
    只在发送成功后写标记；DB 不可用时退化为纯内存去重（至多重发，不丢信）。"""
    day = time.strftime("%Y-%m-%d")
    if _ALERTED.get(key) == day:
        return False
    mkey = f"mail_alert:{key}"
    if _meta_marker_sent(mkey, day):
        _ALERTED[key] = day   # 回填内存, 本进程后续档不再查库
        return False
    try:
        ok = bool(send_ops_alert(title, body, tag=ALERT_TAG))
    except Exception:
        log.exception("账户告警发送失败(不影响主流程): %s", title)
        return False
    if ok:
        _ALERTED[key] = day
        _meta_marker_write(mkey, day)
    return ok


def run_accounts_mail_slot(force: bool = False) -> dict:
    """scheduler 21:15 档 / 开机补跑入口：逐账户告警（去重）→ 逐账户成交邮件（去重）
    → 日报（同日一封）。

    返回 {report_sent, n_alerts, alerts_sent, alerts, trade_emails, trades_by_account}；
    任何一步失败都不抛 —— 邮件层的故障只允许少发一封信，不允许拖挂数据层或调度器。
    """
    out = {"report_sent": False, "n_alerts": 0, "alerts_sent": 0, "alerts": [],
           "trade_emails": 0, "trades_by_account": {}}
    try:
        payload = build_all()
        alerts = assess(payload)
        out["n_alerts"] = len(alerts)
        out["alerts"] = alerts
        # 2026-10-15 E1：账户数派生（此前硬编码 7；账户已增至 8）
        _n_acct = len(payload.get("accounts") or [])
        for x in alerts:
            title = str(x.get("title") or x["code"])
            body = (f"账户: {x['account_name']}（{x['model']} / {x['account_id']}）\n"
                    f"级别: {_LEVEL_CN.get(x['level'], x['level'])} —— {title}\n"
                    f"体检项: {x['code']}\n\n"
                    f"发生了什么：{x['message']}\n"
                    f"意味着什么：{x.get('mean') or '—'}\n"
                    f"对账户的影响：{x.get('impact') or '—'}\n"
                    f"建议处置：{x.get('act') or '—'}\n\n"
                    f"（全账户日报每晚 21:15 汇总 {_n_acct} 个账户，日报里的告警块带同款解释；"
                    "本告警同日同因只发一次，明天仍异常会再报。）")
            if _account_alert_once(f"acct_{x['account_id']}_{x['code']}",
                                   f"【账户告警】{x['account_name']} · {title}"
                                   f"({x['code']})", body):
                out["alerts_sent"] += 1
        # 每账户成交邮件（fterm/ECO-W2/周频账本 20:05-20:35 落账后, 此刻是当日最早可靠时点;
        # 月频盘已有 09:31 即时邮件, CB 无逐笔成交, 均在 collect_today_trades 排除）
        out["trade_emails"] = send_trade_emails(payload)
        out["trades_by_account"] = {k: len(v) for k, v in
                                    collect_today_trades(payload).items()}
        out["report_sent"] = bool(send_daily_report(payload, force=force,
                                                    alerts=alerts))
    except Exception:
        log.exception("全账户邮件档执行失败")
    return out
