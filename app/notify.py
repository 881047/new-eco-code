"""邮件通知: 月度调仓方案推送"""
import datetime
import html
import json
import logging
import smtplib
import time
from email.header import Header
from email.mime.text import MIMEText

from app import config as cfg_mod
from app import db
from app import timeutil
from app.portfolio import fees as fees_mod
# 「真动作」集合（卖出/弱势替换/减仓/加仓/风险预警）的判据在监控侧，邮件只复用它的分类：
# 集合内 ⇒ 即时发信并单独列行；集合外（watch/buy_candidate）⇒ 只进晚间总结。
# 2026-09-18 邮件优化：原来两边各写一份，"邮件说今天有动作、正文却一行不列"迟早出现。
from app.portfolio.monitor import ACTION_NOW

log = logging.getLogger(__name__)

RETRY_ATTEMPTS = 2        # 网络抖动时重试一次, 避免一次失败即丢推送 (P2-7)
RETRY_DELAYS = [10]       # 重试前等待秒数; 最多只重试一次, 不在全局任务锁内阻塞过久


def _log_send(tag: str, ok: bool, error: str = None) -> None:
    """发送结果落库(2026-09-04 审查 P1-11): 原只写日志, 失败无落库、Web 不可见; 现 /api/notify 可见"""
    try:
        # P2-12: 原 conn.close() 在 try 里, execute/commit 抛错就漏一个连接
        with db.conn_ctx() as conn:
            conn.execute(
                "INSERT INTO notify_log (created_at, tag, ok, error) VALUES (?,?,?,?)",
                (timeutil.stamp(), tag, 1 if ok else 0,
                 (str(error)[:300] if error else None)))
            conn.commit()
    except Exception:
        log.exception("notify_log 写入失败(不影响主流程)")


def _send_with_retry(cfg: dict, msg, tag: str) -> bool:
    """SMTP 发送(带重试), 返回是否成功; 结果落库 notify_log"""
    last_err = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            with smtplib.SMTP_SSL(cfg["smtp_server"], int(cfg.get("smtp_port", 465)), timeout=10) as s:
                s.login(cfg["sender"], cfg["auth_code"])
                s.sendmail(cfg["sender"], cfg["recipients"], msg.as_string())
            _log_send(tag, True)
            return True
        except Exception as e:
            last_err = e
            if attempt < RETRY_ATTEMPTS - 1:
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                log.warning("%s 邮件发送失败(第%d次), %ds后重试: %s", tag, attempt + 1, delay, e)
                time.sleep(delay)
            else:
                log.exception("%s 邮件发送失败, 重试%d次仍失败", tag, RETRY_ATTEMPTS - 1)
    _log_send(tag, False, error=last_err)
    return False


def _html(summary) -> str:
    """月度调仓方案邮件正文（每月 1 号 18:33 档）。

    2026-09-18 邮件优化：换统一信封，并补上**口径块**。原来这封信只给"买什么、多少股"，
    没有任何东西说明"这套构型在历史上是什么表现、这个读数能不能信" ——
    而按纪律 §4 留出集已经一次性开封过，任何回测读数从此都只具方向性；
    不写这句的邮件会被当成"模型说年化 11%"来读。（口径四件套：窗口/基准/成本/样本外。）
    """
    # 所有来自数据的字段统一 html.escape (2026-09-04 审查 P1-11)
    esc = html.escape
    cfg_all = cfg_mod.load_config()
    warn = (f"<p style='color:#c0392b'><b>⚠ {esc(str(summary['warning']))}</b></p>"
            if summary.get("warning") else "")
    stocks = "".join(
        f"<tr><td>{esc(str(r['name']))}</td><td>{esc(str(r['ts_code']))}</td>"
        f"<td>{esc(str(r['reason']))}</td>"
        f"<td style='text-align:right'>{r['shares']}</td>"
        f"<td style='text-align:right'>{r['weight'] * 100:.1f}%</td></tr>"
        for r in summary["stocks"])
    etfs = "".join(
        f"<tr><td>{esc(str(r['name']))}</td><td>{esc(str(r['ts_code']))}</td>"
        f"<td>{esc(str(r['reason']))}</td></tr>"
        for r in summary["etfs"])
    otc_rows = []
    for t, items in summary["otc"].items():
        for it in items:
            otc_rows.append(
                f"<tr><td>{esc(str(t))}</td><td>{esc(str(it['code']))}</td>"
                f"<td>{esc(str(it['name']))}</td>"
                f"<td style='text-align:right'>{it.get('ret_1y')}</td></tr>")
    otc = "".join(otc_rows)
    repo = summary["repo"]
    state_cn = STATE_CN.get(str(summary.get("state")), str(summary.get("state")))
    cost_p = fees_mod.fee_params(cfg_all)
    verdict = (f"{len(summary['stocks'])} 只股票 + {len(summary['etfs'])} 只场内基金；"
               f"权益仓位 {summary['equity_weight'] * 100:.0f}%；"
               f"市场状态 {state_cn}；投入 {summary['stock_cost']:,.0f} / "
               f"{summary['capital']:,.0f} 元")
    sections = f"""{warn}
    <p>数据截至 <b>{esc(str(summary['data_date']))}</b>
       ｜ 基准指数 {esc(str(cfg_mod.benchmark_code()))}</p>
    {_style_block()}
    <h3>股票组合 (买入 {len(summary['stocks'])} 只, 合计 {summary['stock_cost']:,.0f} 元)</h3>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:14px">
    <tr style="background:#f5f5f5"><th>名称</th><th>代码</th><th>理由</th><th>建议股数</th><th>权重</th></tr>
    {stocks}</table>
    <h3>场内基金备选</h3>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:14px">
    <tr style="background:#f5f5f5"><th>名称</th><th>代码</th><th>理由</th></tr>{etfs}</table>
    <h3>场外基金推荐 (支付宝/天天基金等平台申购)</h3>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:14px">
    <tr style="background:#f5f5f5"><th>类型</th><th>代码</th><th>名称</th><th>近1年收益%</th></tr>{otc}</table>
    <h3>现金管理 (逆回购)</h3>
    <p>可用现金 <b>{repo['amount']:,.0f}</b> 元: {esc(str(repo['desc']))} (隔夜利率 {repo['rate']}%)</p>
    <h3>口径（读这份方案前先认这四件）</h3>
    <p style='color:#888;font-size:12px'>
    ① <b>窗口</b>：本方案的构型由生产回测（`app/backtest/engine.py`，pin 锁定）在同一条
    历史窗口上验证；读数请在看板「策略与方案」页对照当期 run_date 看，<b>不要引用记忆里的旧数</b>。
    ② <b>基准</b>：{esc(str(cfg_mod.benchmark_code()))}（与模拟盘/超额计算同一个源）。
    ③ <b>成本</b>：逐笔真实费用 —— 佣金 max(成交额×万{cost_p['commission_rate'] * 1e4:g},
    {cost_p['min_commission']:g} 元) + 印花税卖出 {cost_p['stamp_tax'] * 100:.2f}% +
    过户费 万{cost_p['transfer_fee'] * 1e4:g}；小仓位单会被最低佣金显著抬高单边费率。
    ④ <b>样本外</b>：留出集在判据定档时<b>已一次性开封</b> ⇒ 此后任何回测读数都只具方向性，
    真正的样本外证据在模拟盘/袖套（见 18:30 那封日终总结的累计读数）。
    </p>"""
    return _envelope("NEW ECO · 月度调仓方案", str(summary["plan_date"]), verdict, sections,
                     action=("模拟盘按此方案在下一个开盘档（09:31）自动挂单，无需动作；"
                             "实盘若要跟随，请按上表自行下单。"
                             + (" 本期有告警项，见正文顶部。" if summary.get("warning") else "")),
                     tone="warn" if summary.get("warning") else "ok")


ACTION_CN = {
    "sell": "止损卖出", "sell_weak": "弱势替换卖出", "buy_candidate": "候选买入",
    "watch": "观察", "reduce": "减仓", "add": "加仓", "warn": "风险预警",
}

#: 市场状态中文名单一真源。原来是 `_style_block` 的**局部**变量，于是动态信号邮件
#: 把英文枚举直接印进正文（`市场状态: defense`）—— 2026-09-18 邮件优化提到模块级共用。
STATE_CN = {"attack": "进攻", "neutral": "中性", "defense": "防守"}

#: 买卖方向中文（原来 `send_paper_email` 里也有一个同名局部字典，一并收上来）。
SIDE_CN = {"buy": "买入", "sell": "卖出"}

#: 看板地址。**只监听 127.0.0.1** ⇒ 手机上必然打不开，所以邮件必须自包含、
#: 链接只作"回到机器前再看明细"的辅助（守卫测试钉这条标注，别删）。
#: 2026-09-22：端口改由 `config.server_port()` 提供（`NEWECO_PORT` 优先，默认 8787）——
#: 原先写死 8787，与 `start.bat` / 看门狗探活各写一份，改端口会漏。
#: 仍保留模块级常量（`{DASHBOARD_URL}` 在下面的模板里被插值，且守卫测试按这个名字找）。
DASHBOARD_URL = f"http://127.0.0.1:{cfg_mod.server_port()}/"

_TONE = {"ok": "#1f7a45", "warn": "#b45309", "bad": "#c0392b"}


def _envelope(title: str, when: str, verdict: str, sections_html: str,
              action: str = None, tone: str = "ok", footer_extra: str = "") -> str:
    """所有邮件共用的正文骨架：结论先行 → 需要你做什么 → 自包含明细 → 统一页脚。

    **为什么要信封**：原来 8 个出口各自拼 HTML，正文是"数据快照"——读的人得自己在
    几行数字里判断"今天要不要我动手"。而邮件是在手机上读的（看板仅本机可达，点不进去），
    所以第一眼必须给结论，明细必须自包含。

    `tone` ∈ {ok, warn, bad} 决定结论条的颜色（绿/橙/红）。
    `sections_html` 由调用方拼好（**其中的数据字段必须各自 `html.escape`**，
    本函数不再转义，否则会把调用方刻意放的 `<b>`/表格吃掉）。
    `verdict`/`action` 相反 —— 它们**会被转义，所以只能放纯文本**。结论行整条本来就
    是加粗+着色的，在里面再套 `<b>` 只会变成字面标签。
    `action=None` 时给一句明确的"无需动作"——沉默不等于安全，
    读的人需要知道"没提动作"是"不需要动作"而不是"没算出来"。
    """
    esc = html.escape
    color = _TONE.get(tone, _TONE["ok"])
    act = action or "无需动作 —— 开盘档会自动执行下述步骤，人工不必介入。"
    return f"""<html><body style="font-family:'Microsoft YaHei',sans-serif;color:#222;line-height:1.6">
    <h2>{esc(str(title))} <span style="color:#888;font-size:14px">({esc(str(when))})</span></h2>
    <p style="border-left:4px solid {color};background:#fafafa;padding:7px 12px;margin:10px 0">
      <b style="color:{color}">{esc(str(verdict))}</b></p>
    <p>👉 <b>需要你做什么：</b>{esc(str(act))}</p>
    {sections_html}
    <p style="color:#999;font-size:12px">{footer_extra}
    <br>系统仅提供信号与研究读数，下单请自行判断执行；不构成投资建议。
    <br>明细可回看板 <a href="{DASHBOARD_URL}">{DASHBOARD_URL}</a>
    （<b>仅本机可达，手机上打不开</b>——本邮件正文已自包含关键读数，不必为了看懂而回机器）。
    </p></body></html>"""


def _table(headers, rows_html, empty_text=None) -> str:
    """统一小表格（邮件客户端只认最保守的 table 样式，别用 flex/grid）。

    `rows_html` 可以是**已拼接的字符串**，也可以是**行字符串的可迭代**——后者直接
    f-string 进去会渲染成 `['<tr>…']` 的 list 字面量（写邮件正文时极易踩，实测过），
    所以这里统一 join。
    `rows_html` 为空时给一行灰字而不是发一张空表——空表在手机上看像"出错了"；
    传 `empty_text=""`（空串）才会真的什么都不渲染。
    """
    if isinstance(rows_html, (list, tuple)):
        rows_html = "".join(rows_html)
    if not rows_html:
        if empty_text is None:
            return ""
        return f"<p style='color:#888;font-size:13px'>{html.escape(str(empty_text))}</p>"
    th = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    return (f"<table border='1' cellspacing='0' cellpadding='6' "
            f"style='border-collapse:collapse;font-size:14px'>"
            f"<tr style='background:#f5f5f5'>{th}</tr>{rows_html}</table>")


def _money(x) -> str:
    """紧凑金额（万元/亿元化），用于表格与聚合行。

    模拟盘账户只有 2 万级 ⇒ 小于 1 万的数**必须带千分位**，否则 `11475` 这种
    五位数字机读没问题、人读要数一遍位数。
    """
    if x is None or (isinstance(x, float) and x != x):  # None / NaN
        return "--"
    sign = "-" if x < 0 else ""
    a = abs(x)
    return sign + (f"{a / 1e8:.2f}亿" if a >= 1e8 else f"{a / 1e4:.1f}万" if a >= 1e4
                   else f"{a:,.0f}")


def _yuan(x) -> str:
    """精确到分的金额（用于账户级读数：紧凑化在这里是错的 —— 当日变动可能只有一两百元，
    `2万` 会把「今天到底动了多少」这件事完全抹掉）。"""
    if x is None or (isinstance(x, float) and x != x):
        return "--"
    return f"{x:,.2f}"


def _lhb_block(trade_date: str) -> str:
    """龙虎榜摘要块(东财口径): 持仓上榜情况 + 净买入前5; 数据缺失时返回空串
    所有字段经 html.escape, 新加内容必须转义(历史模板未转义是欠账, 不再新增)"""
    import pandas as pd

    from app import db

    conn = db.get_conn()
    try:
        plan_d = conn.execute("SELECT MAX(plan_date) FROM plan").fetchone()[0]
        top = pd.read_sql("""
            SELECT l.ts_code, l.name, l.net_buy, o.org_net_value, l.reason,
                   CASE WHEN p.ts_code IS NOT NULL THEN 1 ELSE 0 END AS in_plan
            FROM lhb l
            LEFT JOIN lhb_org o ON o.trade_date=l.trade_date AND o.ts_code=l.ts_code
            LEFT JOIN plan p ON p.ts_code=l.ts_code AND p.plan_date=? AND p.plan_type='stock'
            WHERE l.trade_date=? ORDER BY l.net_buy DESC
        """, conn, params=(plan_d, trade_date))
    finally:
        conn.close()
    if top is None or top.empty:
        return ""
    esc = html.escape
    pos_rows = top[top["in_plan"] == 1]
    pos_html = "".join(
        f"<tr><td>{esc(str(r['name']))}</td><td>{esc(r['ts_code'])}</td>"
        f"<td style='text-align:right'>{_money(r['net_buy'])}</td>"
        f"<td style='text-align:right'>{_money(r['org_net_value'])}</td></tr>"
        for _, r in pos_rows.iterrows()) or "<tr><td colspan='4'>无</td></tr>"
    top_html = "".join(
        f"<tr><td>{esc(str(r['name']))}</td><td>{esc(r['ts_code'])}</td>"
        f"<td style='text-align:right'>{_money(r['net_buy'])}</td>"
        f"<td style='text-align:right'>{_money(r['org_net_value'])}</td>"
        f"<td>{esc(str(r['reason']))}</td></tr>"
        for _, r in top.head(5).iterrows())
    return f"""
    <h3>龙虎榜 ({trade_date})</h3>
    <p><b>持仓上榜:</b></p>
    <table border="1" cellspacing="0" cellpadding="4" style="border-collapse:collapse;font-size:13px">
    <tr style="background:#f5f5f5"><th>名称</th><th>代码</th><th>龙虎榜净买入</th><th>机构净买入</th></tr>
    {pos_html}</table>
    <p><b>净买入前5:</b></p>
    <table border="1" cellspacing="0" cellpadding="4" style="border-collapse:collapse;font-size:13px">
    <tr style="background:#f5f5f5"><th>名称</th><th>代码</th><th>龙虎榜净买入</th><th>机构净买入</th><th>上榜原因</th></tr>
    {top_html}</table>"""


def _style_block() -> str:
    """风格读数块(月度方案邮件用): 池子(等权全A) vs 中证800 —— **描述性, 不是预警**。

    原计划(2026-09-10 第 3 项)是「风格预警」, 被事件研究否掉: 60日相对 ≤ -15% 这条规则
    9.5 年触发 9 次(约每年 1 次), 触发后系统线后续走向 -6.8%/-0.4%/+8.4%/-0.4%/-5.3%/+1.0%
    —— 没有预测力, 报警只会是假警报。故降级为「调仓时把此刻的风格位置摆出来」,
    不触发、不驱动任何动作。详见 app/models/style.py。

    任何异常都吞掉返回空串 —— 风格块绝不能让月度方案邮件发不出去。
    """
    try:
        from app.models import style as style_mod
        s = style_mod.load_style()
    except Exception as e:
        log.warning("风格读数不可用, 邮件跳过风格块: %s", e)
        return ""
    if not isinstance(s, dict) or not s.get("available"):   # 非 dict/None 也降级
        return ""
    esc = html.escape
    pct = lambda v: "--" if v is None else f"{v * 100:+.1f}%"     # noqa: E731
    st = lambda v: STATE_CN.get(v, str(v) if v else "--")        # noqa: E731
    diverged = ("" if not s.get("diverged") else
                f"<p style='color:#b45309'><b>⚠ 池子分支({esc(st(s['pool_state']))})与生产线"
                f"基准状态({esc(st(s['bench_state']))})不一致</b>: 生产的均线/回撤/波动算在"
                f"中证800 上, 与持仓的参照系此刻不同步。</p>")
    return f"""
    <h3>风格读数 ({esc(str(s['as_of']))})</h3>
    <p>池子(等权全A) 相对中证800: 近20日 <b>{pct(s.get('rel_20'))}</b> ｜
       近60日 <b>{pct(s.get('rel_60'))}</b> ｜ 近250日 <b>{pct(s.get('rel_250'))}</b>
       ｜ 判读 <b>{esc(str(s.get('bias')))}</b></p>
    <p>池子状态 <b>{esc(st(s.get('pool_state')))}</b> (评分 {s.get('pool_score')}) ｜
       池子自身回撤 {pct(s.get('pool_drawdown'))} ｜
       生产线基准状态 <b>{esc(st(s.get('bench_state')))}</b></p>
    {diverged}
    <p style="color:#999;font-size:12px">风格读数为<b>描述性指标, 不构成买卖信号</b> ——
       2026-09-10 事件研究显示 60日相对 ≤ -15% 触发后系统线后续走向为
       -6.8%/-0.4%/+8.4%/-0.4%/-5.3%/+1.0%(无预测力), 故它不驱动任何调仓动作。
       「判读」= 池子<b>相对基准</b>的强弱方向；「池子状态/基准状态」= 各自指数自身的
       均线/回撤档位 —— 两把独立的尺，可以一个顺风一个防守，不矛盾。</p>"""


def send_signal_email(sig: dict) -> bool:
    """动态信号邮件（18:0x 日链末尾，**只列真动作**，其余不进这封）。

    2026-09-18 邮件优化：原来 `has_action` 为真时把**整表 items** 都发出去，于是
    "1 条止损 + 22 条候选/观察"也长成一张 23 行的表 —— 收件人要自己在里面找出那 1 条。
    现在只留 `ACTION_NOW` 行，观察类改由晚间总结的「今日信号」小节承载（信息不丢）。
    龙虎榜块也搬到晚间总结：那是一天一次的事后统计，不该搭在"现在就动手"的邮件里。
    """
    cfg = cfg_mod.load_config().get("email", {})
    if not cfg.get("enabled") or not cfg.get("recipients") or not cfg.get("sender"):
        return False
    esc = html.escape  # 数据字段统一转义 (P1-11)
    items = sig.get("items") or []
    now = [i for i in items if i.get("action") in ACTION_NOW]
    rest = len(items) - len(now)
    rows = [
        f"<tr><td>{esc(str(ACTION_CN.get(i['action'], i['action'])))}</td>"
        f"<td>{esc(str(i['name']))}</td>"
        f"<td>{esc(str(i['ts_code']))}</td><td>{esc(str(i['reason']))}</td></tr>"
        for i in now]
    more = (f"<p style='color:#888;font-size:12px'>另有 {rest} 条观察/候选类信号不进这封，"
            "见晚间「今日市场与账户总结」的今日信号小节。</p>" if rest else "")
    # 结论/色调按"有没有真动作"分流（2026-10-06 复核）：调度器只在 has_action 时调本函数，
    # 但防御性兜底 —— 真动作集为空时若还套"需要处理"的橙/红结论就是狼来了。
    if now:
        verdict = f"{len(now)} 条真动作信号，需要今天/明天处理"
        tone = ("bad" if any(i.get("action") in ("sell", "sell_weak", "warn")
                             for i in now) else "warn")
        act = "模拟盘会在下一个开盘档（09:31）自动撮合相应挂单；实盘是否跟随由你判断。"
        act_html = "<h3>需要动手的动作</h3>" + _table(["动作", "名称", "代码", "理由"], rows)
    else:
        verdict = "今天没有需要动手的动作"
        tone = "ok"
        act = "无需动作 —— 模拟盘按既有挂单与晚间方案自动运行。"
        act_html = ""
    sections = (act_html + more
                + f"<p>市场状态 <b>{esc(STATE_CN.get(str(sig.get('state')), str(sig.get('state'))))}</b>"
                  "（该状态只描述池子环境，不驱动调仓 —— 见 `app/models/style.py` 的事件研究）</p>")
    body = _envelope("NEW ECO · 动态信号", str(sig.get("trade_date") or ""),
                     verdict, sections, action=act, tone=tone)
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = Header(
        f"【NEW ECO】{sig['trade_date']} 动态信号：{len(now)} 条真动作"
        + (f"（另有 {rest} 条观察）" if rest else ""), "utf-8")
    msg["From"] = cfg["sender"]
    msg["To"] = ", ".join(cfg["recipients"])
    if not _send_with_retry(cfg, msg, "动态信号"):
        return False
    log.info("动态信号邮件已发送")
    return True


def send_paper_email(res: dict) -> bool:
    """模拟盘成交邮件（09:31 开盘档即时，有成交才发）。

    2026-09-18 邮件优化：换成统一信封 —— 成交邮件是**当天第一封**，读的人要立刻知道
    "动了什么、动了多少钱、我要不要跟着做"，而不是只看到一张表。
    """
    cfg = cfg_mod.load_config().get("email", {})
    if not cfg.get("enabled") or not cfg.get("recipients") or not cfg.get("sender"):
        return False
    esc = html.escape  # 数据字段统一转义 (P1-11)
    trades = res.get("trades") or []
    buy_amt = sum((t.get("shares") or 0) * (t.get("price") or 0)
                  for t in trades if t.get("side") == "buy")
    sell_amt = sum((t.get("shares") or 0) * (t.get("price") or 0)
                   for t in trades if t.get("side") != "buy")
    rows = [
        f"<tr><td>{esc(SIDE_CN.get(t['side'], str(t['side'])))}</td>"
        f"<td>{esc(str(t['name']))}</td><td>{esc(str(t['ts_code']))}</td>"
        f"<td style='text-align:right'>{t['shares']}</td>"
        f"<td style='text-align:right'>{t['price']}</td>"
        f"<td style='text-align:right'>{_money((t['shares'] or 0) * (t['price'] or 0))}</td>"
        f"<td>{esc(str(t['reason']))}</td></tr>"
        for t in trades]
    nb = len([t for t in trades if t.get("side") == "buy"])
    verdict = (f"开盘档成交 {len(trades)} 笔："
               f"买 {nb} 笔 {_money(buy_amt)} ／ 卖 {len(trades) - nb} 笔 {_money(sell_amt)}")
    # 2026-10-06 内容优化：成交邮件原只有逐笔表——收件人看完不知道"这一批到底动了多少钱、
    # 动了哪些标的"。补一行聚合（费用在 18:30 日终汇总逐笔列出，这里不算两套账）。
    codes_n = len({t.get("ts_code") for t in trades})
    agg = (f"<p style='color:#888;font-size:12px'>买入合计 {_money(buy_amt)} ｜ "
           f"卖出合计 {_money(sell_amt)} ｜ 净额 {_money(buy_amt - sell_amt)}"
           f"（正 = 净买入） ｜ 涉及标的 {codes_n} 只 ｜ 逐笔费用见 18:30 日终汇总。</p>")
    body = _envelope("NEW ECO · 模拟盘成交", str(res.get("trade_date") or ""), verdict,
                     "<h3>逐笔成交</h3>" + _table(["方向", "名称", "代码", "股数", "成交价",
                                                    "金额", "理由"], rows) + agg,
                     action="模拟盘已按今开价自动撮合，无需动作。"
                            "是否跟随下单由你自行判断 —— 本邮件只是记录，不是指令。")
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = Header(f"【NEW ECO】{res['trade_date']} 模拟盘成交 {len(trades)} 笔", "utf-8")
    msg["From"] = cfg["sender"]
    msg["To"] = ", ".join(cfg["recipients"])
    if not _send_with_retry(cfg, msg, "模拟盘"):
        return False
    log.info("模拟盘成交邮件已发送")
    return True


def _ops_section(day8: str) -> str:
    """「今日运维」小节（2026-09-18 邮件优化）。

    为什么必须有：自愈类告警改成"同日只发首封"之后，**被合并掉的那几封必须在这里可见** ——
    否则"合并"就等同于"隐瞒"，那是本项目最反对的一类静默。
    """
    d = f"{day8[:4]}-{day8[4:6]}-{day8[6:]}"
    try:
        conn = db.get_conn()
        try:
            rows = _rows(conn, "SELECT tag, ok, error, created_at FROM notify_log "
                               "WHERE substr(created_at,1,10)=? ORDER BY created_at", (d,))
        finally:
            conn.close()
    except Exception as e:
        log.info("日终总结: 运维小节取数失败(跳过): %s", e)
        return ""
    fails = []
    if not rows:
        out = ("<h3>今日运维</h3><p style='color:#888;font-size:13px'>今天没有任何邮件收发记录"
               "（含本封之前的全部）—— 若预期有告警，说明告警链路没跑，值得看一眼 logs。</p>")
    else:
        fails = [r for r in rows if not r["ok"]]
        by_tag: dict = {}
        for r in rows:
            by_tag[r["tag"]] = by_tag.get(r["tag"], 0) + 1
        esc = html.escape
        stat = " ｜ ".join(f"{esc(str(t))} <b>{c}</b>" for t, c in sorted(by_tag.items()))
        out = f"<h3>今日运维</h3><p>今天共 <b>{len(rows)}</b> 封：{stat}。</p>"
    # 自愈台账（`logs/service_heal.json`，由进程外看门狗写）：成功类告警改成"同日只发首封"
    # 之后，**被合并掉的次数必须在这里数得出来** —— 否则"合并"就等同于"隐瞒"。
    try:
        heal = json.loads((cfg_mod.LOG_DIR / "service_heal.json").read_text(encoding="utf-8"))
        d = (heal.get("days") or {}).get(day8)
        if d:
            mailed = int(d.get("mailed") or 0)
            merged = max(0, int(d.get("ok") or 0) - mailed)
            out += (f"<p>今天自愈拉起 <b>{d.get('ok', 0)}</b> 次"
                    + (f"（其中 <b>{merged}</b> 次被同日合并，没单独发信 —— 就是这一行让它们可见）"
                       if merged else "")
                    + (f"，拉起失败 <b>{d.get('fail')}</b> 次" if d.get("fail") else "") + "。</p>")
            if int(d.get("ok") or 0) >= 3:
                out += ("<p style='color:#b45309'>一天死三次以上 ⇒ 这不是邮件问题，"
                        "是服务本身不稳。根因要查 logs/server.log 与 logs/watchdog.log，"
                        "本轮邮件改造不解决它。</p>")
    except Exception as e:
        log.info("日终总结: 自愈台账读不到(跳过该行): %s", e)
    if fails:
        frows = "".join(
            f"<tr><td>{esc(str(r['created_at']))}</td><td>{esc(str(r['tag']))}</td>"
            f"<td>{esc(str(r['error'] or ''))}</td></tr>" for r in fails[:10])
        out += ("<p style='color:#c0392b'><b>⚠ 其中 " + str(len(fails))
                + " 封发送失败：</b></p>"
                + _table(["时间", "类型", "错误"], frows))
    return out


def _signal_section(day8: str) -> str:
    """当日信号小节（观察/候选类不再单独发信，改在这里出现）。"""
    try:
        conn = db.get_conn()
        try:
            rows = _rows(conn, "SELECT action, ts_code, name, reason FROM signal "
                               "WHERE signal_date=? ORDER BY action, ts_code", (day8,))
        finally:
            conn.close()
    except Exception as e:
        log.info("日终总结: 信号小节取数失败(跳过): %s", e)
        return ""
    if not rows:
        return ""
    esc = html.escape
    quiet = [r for r in rows if r["action"] not in ACTION_NOW]
    now = [r for r in rows if r["action"] in ACTION_NOW]
    # 2026-10-06 内容优化：观察/候选类**不再逐行列**——它们是每日轮换的候选清单
    #（"新进前20"一族），信息量低却占了大半封邮件，把真动作与账户读数往下挤。
    # 改成分类计数 + 一句口径；逐行明细看板「打分与因子」页全天有。
    by_action: dict = {}
    for r in quiet:
        by_action[r["action"]] = by_action.get(r["action"], 0) + 1
    quiet_summary = "、".join(
        f"{esc(ACTION_CN.get(a, str(a)))} {n} 条" for a, n in sorted(by_action.items()))
    now_rows = "".join(
        f"<tr><td>{esc(ACTION_CN.get(r['action'], str(r['action'])))}</td>"
        f"<td>{esc(str(r['name']))}</td><td>{esc(str(r['ts_code']))}</td>"
        f"<td>{esc(str(r['reason'] or ''))}</td></tr>" for r in now)
    # 分类计数必须写出来：读的人要能确认"没收到即时信号邮件"= 今天没有真动作，
    # 而不是信号链断了。
    head = (f"<p>今日 {len(rows)} 条信号：次日真动作 <b>{len(now)}</b> 条"
            + ("（已即时发信，下表）" if now else "")
            + (f" ｜ 观察/候选 <b>{len(quiet)}</b> 条（{quiet_summary}）——"
               "每日轮换的候选清单不逐条列，全部读数在看板「打分与因子」页。</p>"
               if quiet else "</p>"))
    return ("<h3>今日信号</h3>" + head
            + (_table(["动作", "名称", "代码", "理由"], now_rows) if now else ""))


def _sleeve_section(day8: str) -> str:
    """周频袖套小节 —— 承接被取消的独立 KPI 邮件（2026-09-18）。

    取消那封的前提是**信息不能丢**：袖套的净值/持仓/成交/风控状态改在这里出现，
    所以这里**每个交易日都渲染**，不做"只在周五"的门控。
    （计划稿写的是"周五附袖套小节"，前提是"袖套周五执行"；实测账本 98 笔成交全在
    `20260914`（周一），执行判据是 `pipeline.is_first_session_after_anchor`
    = 本周第一个开市日 ⇒ 门控到周五会让周一的调仓晚 4 天才被人看到。以实测为准。）

    账本还没建、`weekly.enabled=false`、或读失败 ⇒ 返回空串（不能让日终总结因袖套读不到而发不出）。
    `kpi_lines` 是 CLI 与邮件共用格式（`run_weekly.py` 也打印它），所以这里不改它、只包裹。
    """
    try:
        cfg = cfg_mod.load_config()
        if not (cfg.get("weekly") or {}).get("enabled"):
            return ""
        from app.weekly import plan as wk_plan
        from app.weekly.ledger import Ledger
        from app.weekly.report import kpi_lines
        led = Ledger.load(float(wk_plan.sleeve_capital(cfg)))
        snap = led.snapshot(benchmark=False)
        lines = kpi_lines(snap)
        trades = led.state.get("trades") or []
    except Exception as e:
        log.info("日终总结: 跳过周频袖套小节(%s)", e)
        return ""
    esc = html.escape
    # 上一次执行的成交：独立 KPI 邮件原来带这张"本次成交"表，取消后必须有个地方能看到
    last_day = max((str(t.get("date")) for t in trades), default=None)
    exec_html = ""
    if last_day:
        batch = [t for t in trades if str(t.get("date")) == last_day]
        nb = len([t for t in batch if t.get("side") == "buy"])
        exec_html = (f"<p>上次执行 <b>{esc(last_day)}</b>：{len(batch)} 笔"
                     f"（买 {nb} ／ 卖 {len(batch) - nb}）。</p>")
        if last_day == str(day8):
            rows = [
                f"<tr><td>{esc(SIDE_CN.get(t.get('side'), str(t.get('side'))))}</td>"
                f"<td>{esc(str(t.get('ts_code')))}</td>"
                f"<td style='text-align:right'>{(t.get('shares') or 0):,.0f}</td>"
                f"<td style='text-align:right'>{t.get('price')}</td>"
                f"<td style='text-align:right'>{_yuan(t.get('fee'))}</td></tr>"
                for t in batch[:10]]
            exec_html += (_table(["方向", "代码", "股数", "成交价", "费用"], rows)
                          + (f"<p style='color:#888;font-size:12px'>另有 {len(batch) - 10} 笔未列出。</p>"
                             if len(batch) > 10 else ""))
        else:
            exec_html += ("<p style='color:#888;font-size:12px'>今天没有袖套调仓"
                          "（袖套每周只在<b>本周第一个开市日</b>执行一次）。</p>")
    return ("<h3>周频袖套</h3><ul>"
            + "".join(f"<li>{esc(str(x))}</li>" for x in lines) + "</ul>" + exec_html
            + "<p style='color:#888;font-size:12px'>口径：袖套独立资金，与上方月频模拟盘互不影响；"
            "累计计息单列（研究侧回测不含利息，混进收益会把记账项当成 alpha）。"
            "原来那封独立的袖套 KPI 邮件（执行日 09:32 后）已并入本封（2026-09-18 邮件优化）。</p>")


def send_daily_summary_email(s: dict) -> bool:
    """日终「市场与账户总结」（每交易日 18:30 档）。

    2026-09-18 邮件优化：原来是纯数据 dump（裸净值 + 两张全量表），读的人要自己算
    "今天到底是赚是亏、跑赢基准没有"。现在结论先行 + Δ昨日 + 持仓截断 + 口径四件套，
    并吸收运维/信号/袖套三个小节 —— 邮件在手机上读，正文必须自包含。
    记账缺失（18:00 链没跑完）由调用方 `daily_summary_slot` 挡掉，这里不重复判。
    """
    cfg_all = cfg_mod.load_config()
    cfg = cfg_all.get("email", {})
    if not cfg.get("enabled") or not cfg.get("recipients") or not cfg.get("sender"):
        return False
    esc = html.escape
    day8 = str(s.get("trade_date") or "")
    nav, prev, initial, bench0 = s.get("nav"), s.get("prev"), s.get("initial"), s.get("bench0")

    # ---- 结论：当日 / 累计 / 超额 ----
    day_pnl = (nav - prev["nav"]) if (nav is not None and prev) else None
    cum_pct = (nav / initial - 1) if (nav is not None and initial) else None
    bench_pct = ((s.get("bench_nav") / bench0 - 1)
                 if (s.get("bench_nav") is not None and bench0) else None)
    parts = []
    if day_pnl is not None:
        # 正号是刻意的：手机上第一眼要能不看百分号就判出方向
        signed = ("-" if day_pnl < 0 else "+") + _yuan(abs(day_pnl))
        parts.append(f"当日 {signed}（{day_pnl / prev['nav'] * 100:+.2f}%）")
    if cum_pct is not None:
        parts.append(f"累计 {cum_pct * 100:+.2f}%")
    if bench_pct is not None:
        parts.append(f"基准 {bench_pct * 100:+.2f}%，超额 {(cum_pct - bench_pct) * 100:+.2f}pp"
                     if cum_pct is not None else f"基准 {bench_pct * 100:+.2f}%")
    verdict = "｜".join(parts) if parts else f"总资产 {_yuan(nav)}（缺参照点，算不出涨跌幅）"
    tone = "warn" if (day_pnl is not None and day_pnl < 0) else "ok"

    # ---- 账户快照（结论之下的绝对值；原邮件把这几个数散在一行里，没有归类） ----
    # `_yuan`/`_money` 的输出只含数字与 , . - ⇒ 不需要 esc，别为了"看着安全"包一层
    snap = (f"<p>总资产 <b>{_yuan(nav)}</b>（初始 {_yuan(initial)}）"
            f" ｜ 现金 {_yuan(s.get('cash'))} ｜ 持仓市值 {_yuan(s.get('market_value'))}"
            + (f" ｜ 累计计息 {_yuan(s.get('interest'))}" if s.get("interest") else "")
            + (f" ｜ 上一交易日 {esc(str(prev['trade_date']))} {_yuan(prev['nav'])}"
               if prev else "") + "</p>")

    # ---- 持仓：按 |浮盈| 排序取前 10，其余折成一行 ----
    pos = list(s.get("positions") or [])
    pos.sort(key=lambda p: abs(p.get("pnl") or 0), reverse=True)
    shown, rest = pos[:10], pos[10:]

    def _num(v, fmt="{:,.2f}"):
        return "--" if v is None else fmt.format(v)

    pos_rows = [
        f"<tr><td>{esc(str(p['name']))}</td><td>{esc(str(p['ts_code']))}</td>"
        f"<td style='text-align:right'>{p['shares']}</td>"
        f"<td style='text-align:right'>{_num(p['avg_cost'], '{:,.3f}')}</td>"
        f"<td style='text-align:right'>{_num(p['close'], '{:,.2f}')}</td>"
        f"<td style='text-align:right'>{_money(p['pnl'])}</td>"
        f"<td style='text-align:right'>{_num(p['pnl_pct'], '{:+.2f}%')}</td></tr>"
        for p in shown]
    if rest:
        rs = sum((p.get("pnl") or 0) for p in rest)
        pos_rows.append(f"<tr><td colspan='5' style='color:#888'>其余 {len(rest)} 只合计</td>"
                        f"<td style='text-align:right'><b>{_money(rs)}</b></td>"
                        f"<td style='color:#888'>见看板</td></tr>")
    pos_html = _table(["名称", "代码", "股数", "成本", "现价", "浮盈", "浮盈%"], pos_rows,
                      empty_text="空仓")

    tr_rows, buy_amt, sell_amt, fee_amt = [], 0.0, 0.0, 0.0
    for t in s.get("trades") or []:
        amt = (t.get("shares") or 0) * (t.get("price") or 0)
        fee_amt += float(t.get("cost") or 0)
        if t.get("side") == "buy":
            buy_amt += amt
        else:
            sell_amt += amt
        tr_rows.append(
            f"<tr><td>{esc(SIDE_CN.get(t.get('side'), str(t.get('side'))))}</td>"
            f"<td>{esc(str(t.get('name')))}</td><td>{esc(str(t.get('ts_code')))}</td>"
            f"<td style='text-align:right'>{t.get('shares')}</td>"
            f"<td style='text-align:right'>{_num(t.get('price'), '{:,.3f}')}</td>"
            f"<td style='text-align:right'>{_money(amt)}</td>"
            f"<td style='text-align:right'>{_num(t.get('cost'), '{:,.2f}')}</td>"
            f"<td>{esc(str(t.get('reason') or ''))}</td></tr>")
    trades_html = _table(["方向", "名称", "代码", "股数", "成交价", "金额", "费用", "理由"],
                         tr_rows, empty_text="当日无成交")
    if tr_rows:
        trades_html += (f"<p style='color:#888;font-size:12px'>买入合计 {_money(buy_amt)}"
                        f" ｜ 卖出合计 {_money(sell_amt)} ｜ 净额 {_money(sell_amt - buy_amt)}"
                        f" ｜ 当日费用 <b>{_yuan(fee_amt)}</b> 元</p>")

    cost_p = fees_mod.fee_params(cfg_all)
    cost = ("<h3>口径</h3><p style='color:#888;font-size:12px'>"
            f"基准指数 <b>{esc(str(cfg_mod.benchmark_code()))}</b>"
            "，起点=模拟盘首期净值日（累计% 与超额同起点，可比）"
            " ｜ 成本：成交按<b>逐笔真实费用</b>计费"
            f"（佣金 max(成交额×万{cost_p['commission_rate'] * 1e4:g}, "
            f"{cost_p['min_commission']:g} 元)"
            f" + 印花税卖出 {cost_p['stamp_tax'] * 100:.2f}%"
            f" + 过户费 万{cost_p['transfer_fee'] * 1e4:g}），"
            "上表「费用」列即实扣额 ｜ 样本外：<b>模拟盘按实盘时序逐日成交，属样本外</b>，"
            "但仓位规模与回测构型不同，不能拿它的收益率直接对比回测年化"
            " ｜ 现金计息单列，不计入策略收益。"
            "</p>")

    sections = ("<h3>账户</h3>" + snap
                + "<h3>持仓（按浮盈绝对值前 10）</h3>" + pos_html
                + "<h3>当日成交</h3>" + trades_html
                + _signal_section(day8) + _lhb_block(day8)
                + _sleeve_section(day8) + _ops_section(day8) + cost)
    body = _envelope("NEW ECO · 今日市场与账户总结", day8, verdict, sections,
                     action=("无需动作 —— 这是收盘快照，账户由程序记账。"
                             "需要人工处理的事项会在早上 08:30 那封「今日待办」里点名。"
                             + (" 今天账户是亏的，属正常波动，不必干预。"
                                if (day_pnl is not None and day_pnl < 0) else "")),
                     tone=tone)
    msg = MIMEText(body, "html", "utf-8")
    # verdict 是纯文本（信封会转义它，所以里面不能放 `<b>`；整行本来就已加粗着色）
    msg["Subject"] = Header(f"【NEW ECO】{day8} 日终总结：{verdict}", "utf-8")
    msg["From"] = cfg["sender"]
    msg["To"] = ", ".join(cfg["recipients"])
    # tag 不能改：`scheduler.py:1114` 的开机补跑按 `notify_log.tag='模拟盘日终汇总'`
    # 判"今天发过没有"，换了 tag 就会补发第二封。
    if not _send_with_retry(cfg, msg, "模拟盘日终汇总"):
        return False
    log.info("模拟盘日终汇总邮件已发送")
    return True


def send_plan_email(summary: dict) -> bool:
    cfg = cfg_mod.load_config().get("email", {})
    if not cfg.get("enabled") or not cfg.get("recipients") or not cfg.get("sender"):
        log.info("邮件未配置或未启用, 跳过推送")
        return False
    msg = MIMEText(_html(summary), "html", "utf-8")
    msg["Subject"] = Header(f"【NEW ECO】{summary['plan_date']} 月度调仓方案", "utf-8")
    msg["From"] = cfg["sender"]
    msg["To"] = ", ".join(cfg["recipients"])
    if not _send_with_retry(cfg, msg, "调仓方案"):
        return False
    log.info("调仓方案邮件已发送")
    return True


def send_ops_alert(title: str, body: str, tag: str = "运维告警") -> bool:
    """运维告警出口（2026-09-13）：服务自愈成功/失败时通知人，不留"静默死掉"的可能。

    为什么单独一个函数而不是复用方案/成交邮件：那些函数的**主题与正文都是策略内容**，
    运维事件塞进去会被当成交易信息读。这里只做一件事 —— 把一段纯文本按同一套 SMTP 口径发出去
    （仍走 `_send_with_retry`，仍落 `notify_log`，所以「什么时候发过什么」可查）。

    调用方是**进程外**的看门狗（`scripts/alert_service.py` 子进程），因此：
    - 邮件未配置/未启用 ⇒ 返回 False，绝不抛异常；
    - 任何异常都被吞掉并记日志 —— 告警发不出去，不该把看门狗本身弄挂。
    """
    try:
        cfg = cfg_mod.load_config().get("email", {})
        if not cfg.get("enabled") or not cfg.get("recipients") or not cfg.get("sender"):
            log.info("运维告警: 邮件未配置或未启用, 跳过")
            return False
        esc = html.escape
        rows = "".join(f"<p>{esc(str(ln))}</p>" for ln in str(body).splitlines() if str(ln).strip())
        # 2026-09-18 审查 Q-15：页脚原来断言"本条由进程外看门狗自动发出…同类告警 6 小时内
        # 只发一条" —— 两句对**调度器直发**的告警（备份校验失败、周频成交核对异常）都不成立：
        # 它们不是看门狗发的，而 6h 去重只在 `scripts/service_watchdog.py:54`。
        # 承诺失实的后果很实际：运维把"第二天又收到同标题"当成重复发送 bug 或漏收。
        # 现在按来源把两条语义都写明白（调度器侧已统一走 `_ops_alert_once` 的按天键去重）。
        # ⚠️ 这段注释必须在 f""" 之外 —— 放进去会被当 HTML 文本渲染进邮件正文。
        html_body = f"""
    <html><body style="font-family:'Microsoft YaHei',sans-serif;color:#222;line-height:1.6">
    <h2 style="color:#c0392b">⚠ {esc(str(title))}</h2>
    {rows}
    <p style="color:#999;font-size:12px">本条由 NEW ECO 自动发出（运维告警，不经看板）。
    去重按来源不同：进程外看门狗 = 每 10 分钟探活、同类 6 小时内只发一条；
    调度器直发 = 同一告警 key 当天只发一次。终端本体不依赖邮件。</p>
    </body></html>"""
        msg = MIMEText(html_body, "html", "utf-8")
        msg["Subject"] = Header(f"【NEW ECO·运维】{title}", "utf-8")
        msg["From"] = cfg["sender"]
        msg["To"] = ", ".join(cfg["recipients"])
        if not _send_with_retry(cfg, msg, tag):
            return False
        log.info("运维告警邮件已发送: %s", title)
        return True
    except Exception:
        log.exception("运维告警发送异常(不影响主流程)")
        return False


# ---------------------------------------------------------------------------
# 开盘前「今日待办」（2026-09-18 邮件优化新增）
# ---------------------------------------------------------------------------
def _rows(conn, sql, params=()) -> list:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def build_daily_brief(today: str = None) -> dict:
    """组装晨间待办所需读数（**全程只读**）。

    为什么单独一个 builder 而不直接塞进 `send_*`：与 `paper.engine.build_daily_summary`
    同构 —— 数据组装与呈现分开，测试只喂一个 dict 就能断言渲染，不必碰生产库。

    可选来源（`trading_paused` / 袖套账本）单独 try 掉不是洁癖：账本文件在首次执行前
    不存在、`weekly.enabled=false` 时也没有 —— 那些情况邮件**照样要发得出去**，
    把"读不到"渲染成"未知"，而不是让晨间邮件整个不开。
    """
    from app import timeutil
    today = today or timeutil.date8()
    out = {"today": today, "is_open": False, "pending": [], "signal": None,
           "daily_last": None, "open_days_late": None, "mail_fail_24h": 0,
           "ops_7d": 0, "paused": False, "halt": {}, "halt_unknown": False}
    conn = db.get_conn()
    try:
        r = conn.execute("SELECT is_open FROM trade_cal WHERE cal_date=?", (today,)).fetchone()
        out["is_open"] = bool(r and r[0])
        out["pending"] = _rows(
            conn, "SELECT order_date, ts_code, name, side, shares, reason "
                  "FROM paper_order WHERE status='pending' ORDER BY side, ts_code")
        # 挂单「约金额」按最新日线收盘价估（挂单实际按次日开盘价成交 ⇒ 这是量级参考，
        # 不是精确值；没有它，读的人面对 9 行股数不知道这次调仓动多少钱——2026-10-06）。
        if out["pending"]:
            codes = sorted({p["ts_code"] for p in out["pending"] if p.get("ts_code")})
            if codes:
                ph = ",".join("?" * len(codes))
                close_rows = conn.execute(
                    f"SELECT ts_code, close FROM daily WHERE trade_date="
                    f"(SELECT MAX(trade_date) FROM daily) AND ts_code IN ({ph})",
                    codes).fetchall()
                close_by = {r[0]: r[1] for r in close_rows}
                for p in out["pending"]:
                    c = close_by.get(p.get("ts_code"))
                    p["est_amt"] = round((p.get("shares") or 0) * c, 0) if c else None
        out["daily_last"] = (conn.execute(
            "SELECT last_date FROM data_status WHERE name='daily'").fetchone() or [None])[0]
        out["open_days_late"] = db.open_days_since(conn, "daily", today)
        d = _rows(conn, "SELECT MAX(signal_date) AS d FROM signal")
        sd = d[0]["d"] if d else None
        if sd:
            items = _rows(conn, "SELECT action, ts_code, name, reason FROM signal "
                                "WHERE signal_date=? ORDER BY action, ts_code", (sd,))
            out["signal"] = {"date": sd, "items": items,
                             "now": [i for i in items if i["action"] in ACTION_NOW]}
        cutoff24 = (timeutil.now() - datetime.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        cutoff7d = (timeutil.now() - datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        out["mail_fail_24h"] = int(conn.execute(
            "SELECT COUNT(*) FROM notify_log WHERE ok=0 AND created_at>=?",
            (cutoff24,)).fetchone()[0])
        out["ops_7d"] = int(conn.execute(
            "SELECT COUNT(*) FROM notify_log WHERE tag='运维告警' AND created_at>=?",
            (cutoff7d,)).fetchone()[0])
        # 袖套执行日预览：袖套每周只在**本周第一个开市日**调一次仓（09:32 档），
        # 判据直接复用 `pipeline.is_first_session_after_anchor`，不在邮件里另算 weekday。
        # 方案文件还没出来时这里会说"未就绪" —— 那意味着开盘档会现算或跳过，属于要看的信号。
        try:
            from app.weekly import pipeline as wk_pipe
            from app.weekly import plan as wk_plan
            prev = conn.execute("SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date<? "
                                "ORDER BY cal_date DESC LIMIT 1", (today,)).fetchone()
            if prev and wk_pipe.is_first_session_after_anchor(prev[0], today):
                pf = wk_plan.PLAN_DIR / f"plan_{prev[0]}.json"
                n = None
                if pf.exists():
                    try:
                        n = len((json.loads(pf.read_text(encoding="utf-8")).get("stocks")) or [])
                    except Exception:
                        n = None
                out["sleeve_exec"] = {"anchor": prev[0], "ready": pf.exists(), "n_stocks": n}
            elif prev:
                # 不是执行日也要给个日期：否则"下周哪天动"只能自己去翻日历
                fwd = [r["d"] for r in _rows(
                    conn, "SELECT cal_date AS d FROM trade_cal WHERE is_open=1 AND cal_date>? "
                          "ORDER BY cal_date LIMIT 12", (today,))]
                p2 = today if out["is_open"] else prev[0]
                for d in fwd:
                    if wk_pipe.is_first_session_after_anchor(p2, d):
                        out["sleeve_next_exec"] = d
                        break
                    p2 = d
        except Exception as e:
            out["sleeve_exec_unknown"] = True
            log.info("晨间待办: 袖套执行日判据取不到(跳过该行): %s", e)
    finally:
        conn.close()
    try:
        from app.paper.engine import trading_paused
        out["paused"] = bool(trading_paused())
    except Exception as e:
        log.warning("晨间待办: 取不到 paper.paused（按未暂停渲染）: %s", e)
    try:
        # 只读 `halt`：袖套的净值/持仓/收益**不在早上这封里**（用户 2026-09-18 定：
        # 读数只晚间账户总结报一次）。风控却是开盘前就得出声的事实 —— 停买中意味着
        # 09:32 那档只卖不买（恢复 healthy 后 19:45 档自动解除——auto_resume，2026-09-28），
        # 等收盘再说就晚了一整个交易日。
        # 读 JSON 而不是 `Ledger.load()`：后者签名要 `capital`（`ledger.py:90`），
        # 旁观场景硬凑一个资本参数既别扭又可能污染账本状态。路径仍从 ledger 取。
        from app.weekly.ledger import DEFAULT_PATH as LEDGER_PATH
        if LEDGER_PATH.exists():
            out["halt"] = json.loads(LEDGER_PATH.read_text(encoding="utf-8")).get("halt") or {}
        else:
            out["halt_unknown"] = True        # 袖套还没建账本 ⇒ "未知"，不是"没停买"
    except Exception as e:
        log.warning("晨间待办: 袖套账本读不到（按未知渲染）: %s", e)
        out["halt_unknown"] = True
    # 其他账户（Final / W2）的今日行动（2026-10-05）：挂单将撮合 / ECO-W2 执行日。
    # accounts_mail 全程只读；独立 try —— 聚合层抖动按"缺键"降级（渲染为空），不拖垮晨间邮件。
    # 刻意放在 08:30：fterm/ECO-W2 的动作都在当晚档自动执行，早上知道、白天就不用盯。
    try:
        from app.accounts_mail import morning_actions
        out["accounts_actions"] = morning_actions()
    except Exception as e:
        log.warning("晨间待办: 其他账户行动取不到（按缺失渲染）: %s", e)
        out["accounts_actions"] = None
    return out


def send_daily_brief_email(brief: dict = None) -> bool:
    """开盘前待办邮件（每交易日 08:30）。返回是否真的发出。

    主题与正文都按"能不能直接决定我今天要不要动手"来排：先结论、再动作、再明细。
    非交易日由调用方（调度档）挡掉，这里不重复判。
    """
    cfg = cfg_mod.load_config().get("email", {})
    if not cfg.get("enabled") or not cfg.get("recipients") or not cfg.get("sender"):
        log.info("邮件未配置或未启用, 跳过晨间待办")
        return False
    b = brief or build_daily_brief()
    esc = html.escape
    today = str(b.get("today") or "")

    # ---- 结论与动作：先收集今日**全部**命中项（2026-10-06 内容优化：原 elif 链
    #      多问题并存时只报第一个，"总闸暂停 + 数据落后"会互相遮蔽——全部列出，不藏）----
    late = b.get("open_days_late")
    halt = b.get("halt") or {}
    pend = b.get("pending") or []
    se = b.get("sleeve_exec")
    issues = []      # (严重度序, 文本, 处置)——0=需人工(bad) 1/2/3=警告(warn)
    if b.get("paused"):
        issues.append((0, "⛔ 交易总闸暂停（paper.paused=true）—— 今天不会成交任何挂单",
                       "需人工处理：改 config.yaml 的 paper.paused=false（或看板设置页）才会恢复成交。"))
    if halt.get("active"):
        # 2026-09-28 auto_resume: 动作文案按开关分流 —— 开启时恢复 healthy 后 19:45 档
        # 自动解除（不必人工），人工 resume 永远是立即生效的备选；关闭时维持旧口径。
        _auto = bool(((cfg_mod.load_config().get("weekly") or {}).get("risk_halt") or {})
                     .get("auto_resume"))
        issues.append((1, f"⛔ 周频袖套风控停买中（自 {halt.get('since')}）—— 只卖不买",
                       (("恢复 healthy 后 19:45 档自动解除（auto_resume）；要立即恢复就人工 "
                         "`python scripts/run_weekly.py resume --reason \"...\"`" if _auto else
                         "需人工处理：风控不会自动解除，确认后才 resume")
                        + "（原因：" + str(halt.get("reason")) + "）。")))
    if late is not None and late >= 2:
        issues.append((2, f"⚠ 日线已落后 {late} 个开市日（截至 {b.get('daily_last')}）",
                       "建议先看数据：看板「立即更新数据」或查 logs/app.log —— 数据不新，今天的信号与挂单都不可信。"))
    if se and not se.get("ready"):
        issues.append((2, f"⚠ 今天是袖套执行日，但锚点 {se.get('anchor')} 的方案还没就绪",
                       "开盘档（09:32）会试着现算一次，或直接跳过本周调仓 —— 不想跳过就先跑 "
                       "`python scripts/run_weekly.py doctor`（只读体检）看它到底会不会动。"))
    if b.get("mail_fail_24h"):
        issues.append((3, f"⚠ 近 24 小时有 {b['mail_fail_24h']} 封邮件发送失败",
                       "告警链路本身可能不通：查看板「邮件记录」页。"))
    issues.sort(key=lambda x: x[0])
    if issues:
        # 色调与排序分离：0/1 都是 bad（总闸/停买改变今天的成交行为），其余 warn——
        # 排序只决定 verdict 取哪条，不改变严重度语义（2026-10-06 复核修正）。
        tone = "bad" if issues[0][0] <= 1 else "warn"
        verdict, action = issues[0][1], issues[0][2]
    elif pend:
        tone = "ok"
        verdict = f"今日 {len(pend)} 笔挂单待开盘档（09:31）自动成交"
        action = f"无需动作 —— 开盘档会自动按今开价撮合下述 {len(pend)} 笔。"
    else:
        tone, verdict, action = "ok", "今日无待执行挂单，链路正常", "无需动作。"

    # ---- 明细 ----
    prow = "".join(
        f"<tr><td>{esc(SIDE_CN.get(p['side'], str(p['side'])))}</td><td>{esc(str(p['name']))}</td>"
        f"<td>{esc(str(p['ts_code']))}</td><td style='text-align:right'>{p['shares']}</td>"
        f"<td style='text-align:right'>"
        f"{('约 ' + format(p['est_amt'], ',.0f')) if p.get('est_amt') else '--'}</td>"
        f"<td>{esc(str(p['reason'] or ''))}</td><td>{esc(str(p['order_date']))}</td></tr>"
        for p in pend)
    orders = ("<h3>待执行挂单</h3>" + _table(
        ["方向", "名称", "代码", "股数", "约金额*", "理由", "挂单日"], prow,
        empty_text="今日无挂单（开盘档不会有成交）")
        + "<p style='color:#888;font-size:12px'>* 约金额按最新日线收盘价估算"
          "（挂单实际按次日开盘价成交），用于量级参考——一眼看出这次调仓动多少钱。</p>")

    # 多问题并存：verdict 只放最严重的一条，其余全部列在这里（不互相遮蔽）
    if len(issues) > 1:
        extra = "".join(
            f"<li><b style='color:{'#c0392b' if rk <= 1 else '#b45309'}'>{esc(t)}</b>"
            f" —— {esc(a)}</li>" for rk, t, a in issues[1:])
        issues_html = ("<h3>今日其他注意项（多个问题并存，全部列出）</h3>"
                       "<ul style='margin:6px 0 0 18px'>" + extra + "</ul>")
    else:
        issues_html = ""

    sig = b.get("signal")
    if sig:
        stale = "" if sig["date"] == today else f"（{esc(str(sig['date']))} 起未再产生新信号）"
        now_rows = "".join(
            f"<tr><td>{esc(STATE_CN.get(i['action'], ACTION_CN.get(i['action'], i['action'])))}</td>"
            f"<td>{esc(str(i['name']))}</td><td>{esc(str(i['ts_code']))}</td>"
            f"<td>{esc(str(i['reason'] or ''))}</td></tr>" for i in sig["now"])
        sig_html = (f"<h3>最近信号 {stale}</h3>"
                    + _table(["动作", "名称", "代码", "理由"], now_rows,
                             empty_text=f"最近一批（{sig['date']}）共 {len(sig['items'])} 条，"
                                        f"无卖出/减仓类真动作")
                    + ("<p style='color:#888;font-size:12px'>观察/候选买入类 "
                       f"{len(sig['items']) - len(sig['now'])} 条不进即时邮件，见晚间总结。</p>"
                       if len(sig["items"]) > len(sig["now"]) else ""))
    else:
        sig_html = "<h3>最近信号</h3><p style='color:#888;font-size:13px'>库里还没有信号记录</p>"

    late_txt = ("未知（交易日历缺失）" if late is None else f"落后 {late} 个开市日")
    fresh = (f"<h3>数据新鲜度</h3><p>日线最新 <b>{esc(str(b.get('daily_last') or '--'))}</b>，"
             f"{esc(late_txt)}。袖套风控：{esc(str(halt.get('reason')) if halt.get('active') else ('未知（账本还没建）' if b.get('halt_unknown') else '未停买'))}。"
             f"近 7 天运维告警 <b>{b.get('ops_7d', 0)}</b> 封（服务自愈/备份校验/链路告警等，"
             f"逐封见看板「邮件记录」页），近 24 小时发送失败 <b>{b.get('mail_fail_24h', 0)}</b> 封。</p>")

    # 其他账户（Final / W2）行动条目（2026-10-05）：信息性，不进结论阶梯 ——
    # 这些账户的动作都在当晚档自动执行，早上的价值是"知道今晚会发生什么"。
    # 同袖套红线：本节**不出现净值读数**，只有挂单数与执行日。
    oa = b.get("accounts_actions") or []
    oa_rows = []
    for x in oa:
        lvl = str(x.get("level") or "ok")
        color = "#c0392b" if lvl == "bad" else "#b45309" if lvl == "warn" else "#1f7a45"
        oa_rows.append(f"<tr><td>{esc(str(x.get('account_name') or ''))}</td>"
                       f"<td style='color:{color}'>{esc(str(x.get('text') or ''))}</td></tr>")
    other_html = (("<h3>其他账户（Final / W2）</h3>"
                   + _table(["账户", "行动"], oa_rows)) if oa_rows else "")

    # 袖套：早上只回答"今天动不动、动之前方案在不在"。
    # 净值/持仓/收益这类**读数不在这里**（用户 2026-09-18 定：晚间账户总结报一次就够）；
    # 风控停买是例外 —— 它改变今天 09:32 那档的行为，等收盘再说就晚了一整个交易日。
    sp = []
    if se:
        if se.get("ready"):
            n_txt = (f"，方案共 <b>{se.get('n_stocks')}</b> 只"
                     if se.get("n_stocks") is not None else "")
            sp.append(f"<p><b>今天是袖套执行日</b>（09:32 档）：按锚点 "
                      f"{esc(str(se.get('anchor')))} 的方案调仓{n_txt}。</p>")
        else:
            sp.append(f"<p><b>今天是袖套执行日</b>，但锚点 {esc(str(se.get('anchor')))} "
                      "的方案文件还没就绪（处置见上方结论行）。</p>")
    elif b.get("sleeve_next_exec"):
        sp.append(f"<p>今天不调仓；下次 <b>{esc(str(b['sleeve_next_exec']))}</b>"
                  "（袖套只在每周第一个开市日动一次）。</p>")
    elif not b.get("sleeve_exec_unknown"):
        # 判据取不到时不写这句 —— "今天不是执行日"是断言, 没依据就不能说
        sp.append("<p>今天不是袖套执行日（它只在每周第一个开市日调仓）。</p>")
    if sp:
        sp.append("<p style='color:#888;font-size:12px'>袖套的净值、持仓与累计收益"
                  "在晚间「今日市场与账户总结」里报，不在这里重复。</p>")
    sleeve_html = ("<h3>周频袖套</h3>" + "".join(sp)) if sp else ""

    body = _envelope("NEW ECO · 今日待办", today, verdict,
                     orders + issues_html + other_html + sleeve_html + sig_html + fresh,
                     action=action, tone=tone)
    msg = MIMEText(body, "html", "utf-8")
    # 主题在收件箱列表里是**唯一能被扫到的一行** ⇒ 把"有没有活要干"放进主题，
    # 而不是只写"今日待办"让人点开才知道。
    # warn/bad 一律把结论本身写进主题：只有 ok 才谈"几笔挂单"——否则"袖套方案没就绪"
    # 这种当天唯一要管的事会被"无挂单"三个字盖掉（实测周一就是这个情形）。
    if tone == "ok":
        subj = (f"【NEW ECO】{today} 今日待办：{len(pend)} 笔挂单待开盘成交" if pend
                else f"【NEW ECO】{today} 今日待办：无挂单，链路正常")
    else:
        mark = "⛔ 需人工处理" if tone == "bad" else "⚠ 建议先看一眼"
        reason = verdict.lstrip("⛔⚠ ").split("（")[0].strip()
        subj = f"【NEW ECO】{today} {mark}：{reason}"
    msg["Subject"] = Header(subj, "utf-8")
    msg["From"] = cfg["sender"]
    msg["To"] = ", ".join(cfg["recipients"])
    if not _send_with_retry(cfg, msg, "今日待办"):
        return False
    log.info("今日待办邮件已发送: %s", today)
    return True
