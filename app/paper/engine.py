"""模拟盘引擎: 全自动纸面交易

规则:
- 初始资金取 config.capital, 创建后独立于后续配置修改
- 月度方案/动态信号 → 生成订单 → 次一交易日开盘价成交
- 整手(100股)、单边成本 single_side_cost、涨停买不进/跌停卖不出/停牌顺延(最长5个交易日)
- 卖出信号优先于同股反向挂单(自动撤单); 候选买入仅在当日有卖出/add 信号时启用
- 方案期内被动态层卖出(止损/弱势/减仓)的股票, 方案层不再按 delta 补买(2026-09-30)
- 动态层买入的仓位(候选替换/加仓)方案层不调出, 由动态层风控管理至月末重建(2026-09-30)
- 估值口径(总收益, P0-3): 持仓按 股数×开仓成本×(当日adj/开仓adj) 计值, 分红/送转体现在净值中,
  卖出时把累计总收益一次性计入现金, 除权除息不再产生假浮亏/假止损
"""
import datetime
import logging
import sqlite3

import pandas as pd

from app import config as cfg_mod
from app import db
from app import timeutil
from app.portfolio import fees as fees_mod
from app.portfolio import governor as gov_mod
from app.portfolio.board import board_of, limit_price, limit_ratio, lot_shares, no_limit_trading_days

log = logging.getLogger(__name__)

MAX_EXEC_DAYS = 5

#: 动态层买入(候选替换/加仓)成交的 reason 后缀 —— 写入(apply_signals)与识别
#(apply_plan 的调出豁免)共用这一个常量, 不许两边各写一份字符串
SWAP_BUY_REASON_SUFFIX = " (自动替换买入)"


def _merge_entry_adj(old_sh: int, old_ea: float, new_sh: int, new_ea: float) -> float:
    """加仓后合并 entry_adj: 估值 shares×price×(adj/ea) 的可加性要求 S/ea = Σ s_i/ea_i,
    即 ea 取股数加权调和平均(而非成本加权)。"""
    return (old_sh + new_sh) / (old_sh / old_ea + new_sh / new_ea)


def _max_buy_shares(cash: float, price: float, ts_code: str, trade_date, fees_p) -> int:
    """「成交额 + 逐笔真实费用 ≤ 现金」约束下的最大整手股数(2026-09-10 费用模型)。
    原预算只按 single_side_cost(0.3%) 预留 —— 该预留对小额单**不再保守**: 最低 5 元
    佣金可能超过 0.3% 预留(成交额 < ~1670 元时), 成交后现金会转负。逐手递减至满足
    约束(通常 0~2 次迭代: 乐观上界 int(cash/price) 先试, 不满足才回退)。
    按实际扣款口径比较(费用与总额都 round 到分, 与下方 cash -= 同式), 免得边界上
    "够买"却因 1e-5 的浮点残差被挡。"""
    n = int(cash / price + 1e-9)
    while n > 0:
        m = lot_shares(ts_code, n)
        if m <= 0:
            return 0
        cost = round(fees_mod.trade_cost(m * price, "buy", trade_date, fees_p), 2)
        if round(m * price + cost, 2) <= cash + 1e-9:
            return m
        n = m - 1
    return 0


def ensure_account():
    cfg = cfg_mod.load_config()
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT id FROM paper_account WHERE id=1").fetchone()
        if not row:
            cap = float(cfg.get("capital", 10000))
            conn.execute(
                "INSERT INTO paper_account (id, cash, created_at, last_applied_plan, initial_capital) "
                "VALUES (1,?,?,NULL,?)",
                # 2026-09-15: 裸 `date.today()` → timeutil（东八区）。账户创建日是账本起点，
                # 与 run_date/pipeline_run 必须同源 —— 机器时区 ≠ 东八区时会差一天。
                (cap, timeutil.date8(), cap))
            conn.commit()
            log.info("模拟盘账户已创建: 初始资金 %.2f 元", cap)
    finally:
        conn.close()


def _slip():
    return float(cfg_mod.load_config().get("paper", {}).get("slip", 0.001))


def _governor(conn) -> dict:
    """账户级回撤治理(决策函数, `app/portfolio/governor.py`): paper_nav 净值历史 →
    滞回判定。纯函数可复算(无持久状态, 净值历史本身是唯一事实源); `enabled` 缺省
    True(fail-safe, 与 weekly.risk_halt 同取向)。

    口径注: 调用点(apply_plan/apply_signals)都在 18:00 链的 `update_nav` **之前**,
    判定吃的是**截至上一盯市日**的净值 —— 与链上其它决策(方案/信号)同一天的输入时点,
    至多一日滞后, 不引入额外前视。
    """
    g = (cfg_mod.load_config().get("paper") or {}).get("governor") or {}
    rows = conn.execute("SELECT trade_date, nav FROM paper_nav ORDER BY trade_date").fetchall()
    return gov_mod.decide([(r[0], r[1]) for r in rows], g)


def _regime_state(conn, trade_date) -> str | None:
    """决策日时点的市场状态(与 builder 同口径的 regime 表查询)。
    表不可用(测试夹具/极老库)⇒ None 并记日志 —— defense 候选闸是"文档化意图对齐",
    不因它的数据缺失而阻断主流程; 治理器(governor)不经过这里, 不受影响。"""
    try:
        row = conn.execute(
            "SELECT state FROM regime WHERE trade_date<=? ORDER BY trade_date DESC LIMIT 1",
            (trade_date,)).fetchone()
    except sqlite3.OperationalError:
        log.warning("regime 表不可用, defense 候选闸本次跳过(%s)", trade_date)
        return None
    return row[0] if row else None


def trading_paused() -> bool:
    """交易总闸(2026-09-09 用户拍板「暂停实盘, 先修」): paper.paused=true 时
    不下单(方案/信号)、不成交(晚间链/开盘档), 存量挂单作废; 数据链、训练、回测、
    净值记账(update_nav) 照常 —— 暂停的只是「交易」, 不是「观测」。
    恢复: config.yaml 里改回 false, 次一交易日 18:00 链按最新方案自动重建挂单。"""
    return bool(cfg_mod.load_config().get("paper", {}).get("paused", False))


def _expire_pending_on_pause() -> int:
    """暂停期作废存量挂单: 挂单是暂停前(旧口径)生成的意图, 恢复后按恢复日开盘价补成交
    会与最新方案脱节; 且暂停期不推进 first_attempt, 5 日过期时钟停摆 → 陈旧挂单永不失效。
    幂等: 作废后无 pending 行, 重复调用返回 0。恢复后 apply_plan 按最新方案重建。"""
    conn = db.get_conn()
    try:
        n = conn.execute("SELECT COUNT(*) FROM paper_order WHERE status='pending'").fetchone()[0]
        if n:
            conn.execute(
                "UPDATE paper_order SET status='expired', "
                "reason = COALESCE(reason,'') || '（暂停交易作废）' WHERE status='pending'")
            conn.commit()
        return int(n)
    finally:
        conn.close()


def _no_limit_day(conn, code, d):
    """上市初期无涨跌幅限制日判定(2026-09-07 全市场扩展): 主板(注册制)/创业/科创
    上市前 5 个交易日、北交所仅首日不设限幅 —— 按交易日历计数(上市日是交易日)"""
    nd = no_limit_trading_days(code)
    if nd <= 0:
        return False
    if not db.table_exists(conn, "stock_basic"):
        return False  # 测试临时库/新装机: 无上市日信息, 按有限幅处理(保守方向)
    row = conn.execute("SELECT list_date FROM stock_basic WHERE ts_code=?",
                       (code,)).fetchone()
    if not row or not row[0] or row[0] > d:
        return False
    n = conn.execute(
        "SELECT COUNT(*) FROM trade_cal WHERE is_open=1 AND cal_date>=? AND cal_date<=?",
        (row[0], d)).fetchone()[0]
    return 0 < n <= nd


def _exec_price(conn, code, d, side, slip=None, name=None, name_fn=None):
    """次日开盘价成交模拟(符合A股规则):
    - 集合竞价按开盘价成交, 市价单附加滑点(默认0.1%)
    - 一字涨停(开盘即封板)买不进 / 一字跌停卖不出 → 顺延次日
    - 停牌(vol=0) → 顺延
    - 涨跌停限幅按股票状态动态取(2026-09-04 审查 P2-8): ST/退市整理 5%, 普通 10%。
      2026-09-06 第十轮 N10-4: name_fn 提供时按成交日时点名称 PIT 判定(与 trainer/回测
      同口径), 否则退回订单冻结名(旧口径, 建单次日成交名称几乎不变的场景)
    """
    slip = slip if slip is not None else _slip()
    row = conn.execute("SELECT open, pre_close, vol FROM daily WHERE ts_code=? AND trade_date=?",
                       (code, d)).fetchone()
    if not row or row[0] is None or row[2] is None or row[2] <= 0:
        return None
    o, pre, _ = row
    nm = name_fn(code, d) if name_fn else name
    # 2026-09-07 全市场扩展: 限幅按板块(创业/科创 20%、北交 30%), ST 仅主板收窄 5%;
    # "退"(退市整理期)按普通限幅; 上市初期(主板/创业/科创前5交易日, 北交首日)无限制
    is_st = bool(nm and "ST" in str(nm))
    no_limit = _no_limit_day(conn, code, d)
    if pre and not no_limit:
        # 2026-09-12: 传成交日 + 当日名称 —— 创业板 2020-08-24 前 ±10%; 主板 ST
        # 2026-07-06 起由 ±5% 放宽到 ±10%; S股 ±5%; 退市整理期走板块常态档(创业板
        # 存量股新老划断需回查改革日名称, 故 name_fn 兼作 name_of)。
        # 实盘 d 恒为今天, 故这几条对生产逐位无影响, 对**历史回放/补建**才是修正。
        ratio = limit_ratio(code, is_st, d, nm, name_fn)
        # 2026-09-08 审查: 涨跌停价改交易所四舍五入(原 round 银行家舍入差 1 分)
        # 2026-10-05: 北交所取整接线(上轨 floor/下轨 ceil, 见 board.limit_price)
        limit_up = limit_price(pre, ratio, board_of(code))
        limit_dn = limit_price(pre, -ratio, board_of(code))
    else:
        limit_up = limit_dn = None
        if not pre and not no_limit:
            # 2026-09-14：**缺 pre_close ⇒ 涨跌停闸门整段跳过**（方向偏乐观：一字板也放行）。
            # 实库里有 193 行这样的数据（全部是每只票在库内的首行，绝大多数是上市前的北交/新三板）。
            # 这里**不改行为** —— 改它会与回测引擎（`backtest/engine.py` 同一约定）分叉、进而动 pin；
            # 但必须让它**看得见**：否则"闸门被跳过、一字板买得进"只会静默发生。
            log.warning("成交价: %s %s 缺 pre_close ⇒ 无法判定涨跌停, 本次按无闸门放行(%s)",
                        code, d, side)
    if side == "buy":
        if limit_up is not None and o >= limit_up - 0.001:
            return None  # 一字涨停, 竞价买不进
        return round(o * (1 + slip), 3)
    if limit_dn is not None and o <= limit_dn + 0.001:
        return None  # 一字跌停, 卖不出
    return round(o * (1 - slip), 3)


def _price(conn, code, d):
    row = conn.execute("SELECT close FROM daily WHERE ts_code=? AND trade_date=?", (code, d)).fetchone()
    return row[0] if row else None


def _adj(conn, code, d):
    """当日复权因子(无记录返回 None, 调用方回退未复权价, 避免错误收缩/放大小)"""
    row = conn.execute("SELECT adj_factor FROM adj_factor WHERE ts_code=? AND trade_date=?",
                       (code, d)).fetchone()
    if row and row[0]:
        return float(row[0])
    return None


def _create_order(conn, order_date, code, name, side, shares, reason):
    if shares <= 0:
        # 2026-09-08 审查 P2: 目标股数为 0(已足额持有/无差额)时撤销同批同向 pending
        # 旧单 —— 防陈旧买单继续成交造成超买, 隔日再靠减仓自愈白付双边成本
        conn.execute("DELETE FROM paper_order WHERE ts_code=? AND status='pending' "
                     "AND side=? AND order_date=?", (code, side, order_date))
        return 0
    # 新单优先: 撤销同股同批(同 order_date)反向挂单 —— 2026-09-05 审查修复: 原无日期
    # 过滤, 新买单会撤掉此前信号产生的待执行止损卖单(且冷却期内监控不会重发)
    opp = "buy" if side == "sell" else "sell"
    conn.execute("DELETE FROM paper_order WHERE ts_code=? AND status='pending' AND side=? "
                 "AND order_date=?", (code, opp, order_date))
    # 幂等(2026-09-04 审查 P0-3): 主键改为自增 id 后, 同日同向重复挂单需显式去重,
    # 否则重跑 apply_signals 会生成重复订单
    row = conn.execute(
        "SELECT id FROM paper_order WHERE order_date=? AND ts_code=? AND side=? AND status='pending'",
        (order_date, code, side)).fetchone()
    if row:
        # 更新股数/理由不刷新 created_at: 执行闸以首次挂单时刻为准
        conn.execute("UPDATE paper_order SET shares=?, reason=?, name=? WHERE id=?",
                     (int(shares), reason, name, row[0]))
    else:
        conn.execute(
            "INSERT INTO paper_order (order_date, ts_code, name, side, shares, reason, status, "
            "created_at) VALUES (?,?,?,?,?,?,'pending',?)",
            # `created_at` 是执行闸的口径（"首次挂单时刻"）—— 走 timeutil，与
            # paper_order 其它时间列/台账同源（2026-09-15 收编裸 datetime.now）
            (order_date, code, name, side, int(shares), reason, timeutil.stamp()))
    conn.commit()
    return 1


def execute_pending(trade_date, price_fn=None) -> list:
    """执行所有已到期的挂单(次日开盘价成交, 模拟真实A股规则)

    price_fn(conn, code, d, side, name=, name_fn=) -> float|None:
    默认 _exec_price(日线表开盘价, 晚间链口径); 开盘档传入实时今开价实现(2026-09-07),
    其余闸门(created_at/首试过期/先卖后买/资金校验)完全共用。

    2026-09-04 修复:
    - 两阶段执行: 先全部卖单(筹资)再全部买单(验现金) —— 原先单遍按行序逐行验现金,
      同日换仓的买单会因卖单入账晚于自身而错误截断/过期, 结果取决于代码字母序
    - 过期时钟从首次尝试日(first_attempt)起算 —— 原先从 order_date 起算, 停机数日后的
      首次补跑会把整月未尝试过的订单一次性静默作废
    """
    if trading_paused():   # 2026-09-09 交易总闸: 暂停期任何路径都不成交
        log.info("模拟盘已暂停(paper.paused=true), 跳过挂单执行 (%s)", trade_date)
        return []
    conn = db.get_conn()
    # 交易日闸(2026-09-05, 第五轮复查 P5-4 修正注释): 校验的是"成交日 trade_date"本身
    # 是否为交易日。默认路径 trade_date=MAX(daily)=最近数据日, 数据日必为交易日, 闸恒放行
    # —— 停机补跑按最近数据日回放成交是预期语义(该日市场已真实存在); "今天是否交易日"
    # 由链级早退(daily_update/monthly_pipeline)把关, 同日建单由 created_at 闸保护。
    # 本闸的真实作用: 防御显式传入非交易日/日历缺行的调用, 不拦截合法补跑
    try:
        row = conn.execute("SELECT is_open FROM trade_cal WHERE cal_date=?",
                           (trade_date,)).fetchone()
        if not row or not row[0]:
            log.info("execute_pending: %s 非交易日(或无日历记录), 跳过执行", trade_date)
            return []
        cost_rate = float(cfg_mod.load_config().get("single_side_cost", 0.003))
        # 2026-09-10 逐笔费用: 成交时按真实费率计费(佣金 max(成交额×率, 最低5元) +
        # 印花税卖出 + 过户费, 见 fees.py); cost_rate 仅用于可买股数/预算预留(保守口径)
        fees_p = fees_mod.fee_params(cfg_mod.load_config())
        # 第十轮 N10-4: 限幅按成交日时点名称 PIT(每次执行期构造一次, 名称日更不跨天缓存)
        from app.models.names import NameLookup
        nl = NameLookup()
        orders = pd.read_sql(
            "SELECT * FROM paper_order WHERE status='pending' ORDER BY order_date, ts_code", conn)
        trades = []
        # 先卖后买: 换仓批次内卖单资金先入账, 买单按最新现金逐行校验
        seq = pd.concat([orders[orders["side"] == "sell"], orders[orders["side"] == "buy"]],
                        ignore_index=True)
        for _, o in seq.iterrows():
            # 创建时间闸(2026-09-05): 只执行成交日严格晚于创建日的订单 —— 当日(或之后)建单
            # 顺延次一交易日。原 order_date 闸无法防"补跑/手动触发把次日挂单按当日开盘价成交"
            # (order_date 是方案锚定日, 早于成交日即放行); 旧订单无 created_at 时回退按挂单日
            created = o.get("created_at")
            if not created or pd.isna(created):
                created = f"{o['order_date']} 00:00:00"
            if str(created)[:10].replace("-", "") >= trade_date:
                continue
            if o["first_attempt"]:
                # 过期 = 首次尝试后顺延满 MAX_EXEC_DAYS 个交易日(一字板/停牌/资金不足均算尝试)
                n_days = conn.execute(
                    "SELECT COUNT(*) FROM trade_cal WHERE is_open=1 AND cal_date>? AND cal_date<=?",
                    (o["first_attempt"], trade_date)).fetchone()[0]
                if n_days >= MAX_EXEC_DAYS:
                    conn.execute("UPDATE paper_order SET status='expired' WHERE id=?", (o["id"],))
                    continue
            else:
                conn.execute("UPDATE paper_order SET first_attempt=? WHERE id=?",
                             (trade_date, o["id"]))
                n_days = 0
            _pf = price_fn or _exec_price
            price = _pf(conn, o["ts_code"], trade_date, o["side"], name=o["name"],
                        name_fn=nl.name_at)
            if not price or price <= 0:
                continue  # 一字板/停牌顺延
            if o["side"] == "buy":
                cash = conn.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()[0]
                # 2026-09-08 审查 P2: 按板块整手规则(科创 200 起 1 股递增/北交 100 起
                # 1 股递增), 原硬编码 100 股整数倍 —— 板块开关重开时不截错手数
                units = int(cash / (price * (1 + cost_rate)) + 1e-9)
                # 两条约束取小: ①预算预留(0.3% 保守) ②逐笔真实费用不突破现金(2026-09-10,
                # 最低 5 元佣金在小额单上比 0.3% 预留更贵, 单靠 ① 会透支)
                affordable = min(lot_shares(o["ts_code"], units),
                                _max_buy_shares(cash, price, o["ts_code"], trade_date, fees_p))
                if affordable <= 0:
                    # 2026-09-05 审查修复: 资金不足按"尝试"顺延(先卖后买的换仓场景里卖单
                    # 顺延成交时买单资金才到位), 原首日即 expired 让信号买单永久丢失;
                    # 顺延满 MAX_EXEC_DAYS 后由上方过期逻辑作废
                    continue
                # 第十轮 N10-5 语义注: 0<affordable<挂单 时部分成交、整单结清, 剩余股数
                # 当日不再追补 —— 次日 apply_plan 按 delta(目标-已持)自动补挂差额, 语义
                # 自愈; 信号买单的差额由后续信号再生成
                shares = min(int(o["shares"]), affordable)
                cost = round(fees_mod.trade_cost(shares * price, "buy", trade_date, fees_p), 2)
                net_amount = round(shares * price, 2)
                conn.execute("UPDATE paper_account SET cash = cash - ? WHERE id=1",
                             (round(shares * price + cost, 2),))
                a_now = _adj(conn, o["ts_code"], trade_date)
                old = conn.execute("SELECT shares, avg_cost, entry_adj FROM paper_position WHERE ts_code=?",
                                   (o["ts_code"],)).fetchone()
                if old:
                    tot = old[0] + shares
                    avg = (old[0] * old[1] + shares * price) / tot
                    # 估值按 股数×价×(adj/ea) 可加 ⇒ 合并后 ea 取股数加权调和平均: S/ea = Σ s_i/ea_i
                    old_ea = old[2] if old[2] else None
                    if old_ea is None:
                        # 旧仓 entry_adj 缺失: 从最早买入成交日的复权因子回填(2026-09-04 修复,
                        # 原先用 1.0 伪造基准会让整仓按上市以来累计复权因子缩放, 卖出时记幻影盈亏)
                        row = conn.execute(
                            "SELECT MIN(exec_date) FROM paper_trade WHERE ts_code=? AND side='buy'",
                            (o["ts_code"],)).fetchone()
                        if row and row[0]:
                            old_ea = _adj(conn, o["ts_code"], row[0])
                        if old_ea is None:
                            old_ea = a_now  # 仍缺失: 用今日因子近似, 合并当日口径连续
                    if old_ea and a_now:
                        ea = _merge_entry_adj(old[0], old_ea, shares, a_now)
                    elif old_ea:
                        # 2026-09-08 审查 P2: 开盘档加仓当日因子缺失时保留旧 ea(除权
                        # 尺度不变) —— 原置 NULL 后晚间被"当日因子"覆盖, 旧仓累计
                        # 分红尺度被重置, 估值与卖出结算都错
                        ea = old_ea
                    else:
                        ea = None  # 双缺失: 保持未复权口径, 不强行缩放
                    conn.execute("UPDATE paper_position SET shares=?, avg_cost=?, entry_adj=? WHERE ts_code=?",
                                 (tot, avg, ea, o["ts_code"]))
                else:
                    conn.execute(
                        "INSERT INTO paper_position (ts_code, name, shares, avg_cost, entry_adj) VALUES (?,?,?,?,?)",
                        (o["ts_code"], o["name"], shares, price, a_now))
            else:
                pos = conn.execute("SELECT shares, avg_cost, entry_adj FROM paper_position WHERE ts_code=?",
                                   (o["ts_code"],)).fetchone()
                if not pos or pos[0] <= 0:
                    conn.execute("UPDATE paper_order SET status='expired' WHERE id=?", (o["id"],))
                    continue
                shares = min(int(o["shares"]), pos[0])
                cost = round(fees_mod.trade_cost(shares * price, "sell", trade_date, fees_p), 2)
                # 总收益口径变现: 当前市价×复权比, 持有期间累计分红/送转一并计入现金 (P0-3)
                # 复权因子缺失时回退未复权价, 不强行缩放
                ea = pos[2] if pos[2] else None
                adj_t = _adj(conn, o["ts_code"], trade_date)
                if ea and adj_t is None:
                    # 2026-09-08 审查 P0: 当日因子未入库(晚间链同步失败等)时改用最近
                    # 可得因子 —— 原回退未复权价会把除权除息日的分红从现金账永久丢失,
                    # 与买入侧"晚间回填 entry_adj"的补救不对称。非除权日两口径恒等;
                    # 除权日开盘档已在 execute_open 顺延晚间链(精确口径)
                    row = conn.execute(
                        "SELECT adj_factor FROM adj_factor WHERE ts_code=? AND trade_date<=? "
                        "ORDER BY trade_date DESC LIMIT 1",
                        (o["ts_code"], trade_date)).fetchone()
                    adj_t = float(row[0]) if row and row[0] else None
                if ea and adj_t:
                    proceeds = shares * price * (adj_t / ea)
                else:
                    proceeds = shares * price
                conn.execute("UPDATE paper_account SET cash = cash + ? WHERE id=1",
                             (round(proceeds - cost, 2),))
                net_amount = round(proceeds - cost, 2)
                rem = pos[0] - shares
                if rem > 0:
                    # 部分卖出: 成本与 entry_adj 按比例保留(entry_adj 不变, 摊薄成本不变)
                    conn.execute("UPDATE paper_position SET shares=? WHERE ts_code=?", (rem, o["ts_code"]))
                else:
                    conn.execute("DELETE FROM paper_position WHERE ts_code=?", (o["ts_code"],))
            conn.execute("UPDATE paper_order SET status='done' WHERE id=?", (o["id"],))
            # 成交表自增 id 主键(2026-09-04 审查 P0-3): 同日同股同向多笔成交各占一行, 不再互相覆盖
            conn.execute(
                "INSERT INTO paper_trade (exec_date, ts_code, name, side, shares, price, cost, amount, reason) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (trade_date, o["ts_code"], o["name"], o["side"], shares, price, cost,
                 net_amount, o["reason"]))
            trades.append({"ts_code": o["ts_code"], "name": o["name"], "side": o["side"],
                           "shares": shares, "price": price, "reason": o["reason"]})
        conn.commit()
    finally:
        # 第十轮 N10-6: 异常路径连接必须释放(原靠 GC 回收, 网页触发与定时重叠时长持有)
        conn.close()
    if trades:
        log.info("模拟盘成交 %d 笔 (%s)", len(trades), trade_date)
    return trades


def _exec_price_open(quotes, code, d, side, slip=None, name=None, name_fn=None):
    """开盘档成交价(2026-09-07): 实时行情今开价(集合竞价定盘价)±滑点。
    `slip` 缺省取 `_slip()`（config）；显式传入时**必须被采纳** —— 与 `_exec_price` 同语义
    （2026-09-14 修：原先接了参数却写死 `_slip()`，零滑点臂会被静默忽略）。
    - 无行情/无今开/今开相对昨收越界(按板块限幅防脏数据) → None, 顺延晚间链按日线开盘价
    - 一字涨停(今开≥涨停价)买不进 / 一字跌停卖不出 → None, 与 _exec_price 同规则
    - 限幅按板块(2026-09-07 全市场扩展): 主板 10%/ST 5%、创业/科创 20%、北交 30%;
      复牌首日等无限制日的开盘越限会顺延晚间链(晚间链同样顺延到恢复限幅日, 保守近似)
    """
    q = quotes.get(code)
    if not q or not q.get("open") or not q.get("prev_close"):
        return None
    slip = slip if slip is not None else _slip()
    o, pre = q["open"], q["prev_close"]
    nm = name_fn(code, d) if name_fn else name
    is_st = bool(nm and "ST" in str(nm))
    # 2026-09-12: 传当日+当日名称(创业板改革日 / 主板 ST 新规 20260706 / "S" 股);
    # 退市整理期走板块常态档, name_fn 兼作 name_of 供创业板新老划断回查
    ratio = limit_ratio(code, is_st, d, nm, name_fn)
    # 脏数据防护: 开盘价超出本板块涨跌停范围视为行情异常(北交所 ±30% 正常波动不再误判)
    if not (pre * (1 - ratio) * 0.999 <= o <= pre * (1 + ratio) * 1.001):
        return None
    # 2026-09-08 审查: 涨跌停价改交易所四舍五入(原 round 银行家舍入差 1 分)
    if side == "buy":
        if o >= limit_price(pre, ratio, board_of(code)) - 0.001:
            return None  # 一字涨停, 竞价买不进
        return round(o * (1 + slip), 3)
    if o <= limit_price(pre, -ratio, board_of(code)) + 0.001:
        return None  # 一字跌停, 卖不出
    return round(o * (1 - slip), 3)


def execute_open(trade_date=None) -> list:
    """开盘档(2026-09-07): 集合竞价定盘后立即执行昨日盘后挂单, 盘中即可看到真实持仓。
    失败/无实时行情/停牌/一字板全部静默顺延 —— 晚间链 18:00 用日线开盘价兜底重试,
    成交价口径一致(同为开盘价), 本档纯粹把"看到成交"提前几小时, 不改变账务。
    """
    if trading_paused():   # 2026-09-09 交易总闸
        log.info("模拟盘已暂停(paper.paused=true), 开盘档跳过")
        return []
    conn = db.get_conn()
    try:
        if trade_date is None:
            trade_date = timeutil.date8()          # 2026-09-15: 同源东八区（原裸 datetime.now）
        row = conn.execute("SELECT is_open FROM trade_cal WHERE cal_date=?",
                           (trade_date,)).fetchone()
        if not row or not row[0]:
            return []
        codes = [r[0] for r in conn.execute(
            "SELECT DISTINCT ts_code FROM paper_order WHERE status='pending'").fetchall()]
    finally:
        conn.close()
    if not codes:
        return []
    from app.datasource.quote_client import get_quotes
    quotes = get_quotes(codes)
    if not quotes:
        log.info("开盘档: 实时行情不可用, 挂单顺延至晚间链按日线开盘价执行")
        return []
    # 2026-09-08 审查 P0: 开盘档卖出若持仓有复权基准(ea)而当日复权因子尚未入库,
    # 未复权价结算会把除权日的分红从现金账永久丢失 —— 剔除这些代码让卖单顺延
    # 晚间链(18:00 同步后因子精确, 账务等价); 买入不受影响(entry_adj 晚间自愈)
    c2 = db.get_conn()
    try:
        for (code,) in c2.execute(
                "SELECT ts_code FROM paper_order WHERE status='pending' AND side='sell' "
                "AND ts_code IN (SELECT ts_code FROM paper_position "
                "WHERE entry_adj IS NOT NULL)"):
            if _adj(c2, code, trade_date) is None:
                quotes.pop(code, None)
    finally:
        c2.close()
    # 单只缺今开(停牌/瞬时失败)由价格函数逐单顺延, 不影响其余成交
    def _pf(conn, code, d, side, slip=None, name=None, name_fn=None):
        return _exec_price_open(quotes, code, d, side, slip, name, name_fn)

    trades = execute_pending(trade_date, price_fn=_pf)
    if trades:
        log.info("开盘档成交 %d 笔 (%s, 来源=腾讯实时今开价)", len(trades), trade_date)
    return trades


def apply_plan(plan_date) -> int:
    """按最新月度方案生成调仓订单。可重复调用(幂等):
    - 只撤销"已不在新方案中"的本期挂单; 仍在方案内的挂单保留 —— created_at/first_attempt
      不被刷新, 执行闸与 5 日过期时钟不重置(2026-09-05 审查修复: 原整批 DELETE+重建
      每天把过期时钟清零, 停牌/一字板订单永不过期, 且门控失败时陈旧方案被反复重挂)
    - 上期方案遗留的未成交挂单(月度方案买入/卖出/减仓)一并撤销
    - _create_order 对同日同向挂单去重, 重复调用不产生重复订单
    - **本月无股票方案(plan_type='stock' 零行) ⇒ 一单不下**: 空目标集 != "决定清仓",
      它同样出现在 plan 表被清空/只写出 etf+cash 行时, 见下方 A7 守卫注释
    - 买入量按 delta(目标股数-已持)计算: 部分成交次日自动补挂差额(第十轮 N10-5 语义)
    - **动态层卖出锁(2026-09-30)**: 本方案期内被动态信号卖出(止损/弱势替换/减仓)的股票
      不补买 —— delta 补挂只服务"方案买单未成交/部分成交", 不覆盖动态层后续的风险卖出
      (2026-09 光韵达止损卖出次日被方案买回事故); 在途方案买单一并撤销
    - **动态层买入豁免(2026-09-30)**: 本期内经动态层买入(候选替换/加仓)的仓位不挂
      「月度方案调出」—— 替换买入不许被方案层次日清掉; 月度方案重建时自然收敛
    """
    if trading_paused():   # 2026-09-09 交易总闸: 暂停期不按方案挂单
        log.info("模拟盘已暂停(paper.paused=true), 跳过方案挂单 (%s)", plan_date)
        return 0
    conn = db.get_conn()
    try:
        targets = pd.read_sql(
            "SELECT ts_code, name, shares FROM plan WHERE plan_type='stock' AND plan_date=?",
            conn, params=(plan_date,))
        if targets.empty:
            # 2026-09-19 审查 A7: 目标集为空时, 下面那段"持仓不在目标集 ⇒ 挂全仓卖单"会把
            # **每一笔持仓**都清掉(理由"月度方案调出")。而空目标集并不等于"本月决定空仓":
            # `run_daily` 取的是 `MAX(plan_date) FROM plan`(**不按 plan_type 过滤**), 于是
            # plan 表被清空/手工改库, 或本月只写出 etf/cash/meta 行(builder.py:152 那条
            # "无上月股票组合可沿用"的兜底)时都会落到这里。builder 那侧的注释明确写着目的
            # 是"避免模拟盘误清仓" —— 下游必须有对应的守卫, 否则那条意图只到函数边界为止。
            # 真要主动清仓请走显式路径(风控停买/手工), 别靠"方案里没有股票"隐式表达。
            log.error("apply_plan(%s): 本月**无股票方案**(plan_type='stock' 零行), "
                      "不生成任何调仓订单 —— 空目标集会被判成「全部调出」而误清仓", plan_date)
            return 0
        tset = set(targets["ts_code"])
        q = ",".join("?" * len(tset))
        conn.execute(
            f"DELETE FROM paper_order WHERE order_date=? AND status='pending' "
            f"AND reason LIKE '月度方案%' AND ts_code NOT IN ({q})",
            (plan_date, *sorted(tset)))
        # 上期方案遗留挂单清理(信号单不受影响: reason 不带"月度方案"前缀)
        conn.execute(
            "DELETE FROM paper_order WHERE status='pending' AND order_date<? AND reason LIKE '月度方案%'",
            (plan_date,))
        held = pd.read_sql("SELECT ts_code, name FROM paper_position", conn)
        # 回撤治理(决策函数): 谨慎态下**停开新仓** —— 买单(补仓)不挂, 卖出/减仓照常挂,
        # 敞口只降不升; 解除(回撤回升过 dd_reset)后下一期自动恢复补仓。已挂 pending
        # 买单不追溯撤销(触发前口径下的决定, 5 日过期时钟自会收口)。
        gov = _governor(conn)
        gov_block = bool(gov.get("cautious"))
        if gov_block:
            log.warning("回撤治理触发(%s): 方案 %s **停开新仓**(买单不挂, 卖出照常)",
                        gov.get("reason"), plan_date)
        n = 0
        skipped_buys = 0
        target_codes = set(targets["ts_code"])
        # 动态层卖出锁(2026-09-30): 本方案期内被动态信号卖出过(止损/弱势替换/减仓)的
        # 股票不再按 delta 补买 —— 动态层卖出是方案日之后更晚的风险决策, delta 补挂会把
        # 刚止损清掉的仓位次日原样买回(2026-09 光韵达 300227.SZ: 09-29 止损卖出,
        # 09-30 方案买单又成交), 架空止损且白付双边成本; 口径与回测 R13-1 对齐(月内
        # 止损释放的资金不再入场, 只计息)。边界: signal_date >= plan_date —— 上期止损、
        # 本期方案重新纳入是新的月度决策, 不受影响; 月内再入场走动态层自己的候选路径
        #(信号可见), 不走方案层静默补挂。信号行除同日重跑外持久保留, 抑制在方案期内稳定。
        sold_by_signal = {r[0] for r in conn.execute(
            "SELECT DISTINCT ts_code FROM signal WHERE signal_date>=? "
            "AND action IN ('sell','sell_weak','reduce')", (plan_date,))}
        if sold_by_signal:
            log.info("方案补买抑制(%s): 动态层本期内卖出过 %s, 不按 delta 补买",
                     plan_date, sorted(sold_by_signal))
        # 动态层买入豁免(2026-09-30): 本方案期内经动态层买入(候选替换/加仓)的仓位,
        # 持仓循环不挂「月度方案调出」—— 这些股票几乎必然不在目标集(方案=top6,
        # 候选=top20 减持仓), 不豁免则替换买入成交次日就被方案层清掉, 替换机制形同
        # 虚设(churn 的反向变体)。豁免后由动态层自己的风控(止损/弱势)管理, 下次
        # 月度方案重建时不在新目标集 ⇒ 正常调出(月度收敛)。
        dynamic_bought = {r[0] for r in conn.execute(
            "SELECT DISTINCT ts_code FROM paper_trade WHERE side='buy' AND exec_date>=? "
            "AND reason LIKE ?", (plan_date, "%" + SWAP_BUY_REASON_SUFFIX))}
        for _, h in held.iterrows():
            if h["ts_code"] in dynamic_bought:
                continue
            if h["ts_code"] not in target_codes:
                n += _create_order(conn, plan_date, h["ts_code"], h["name"], "sell",
                                   conn.execute("SELECT shares FROM paper_position WHERE ts_code=?",
                                                (h["ts_code"],)).fetchone()[0], "月度方案调出")
        for _, t in targets.iterrows():
            if t["ts_code"] in sold_by_signal:
                # 在途的本期方案买单(如部分成交补挂)一并撤掉, 否则次日"信号卖单+方案
                # 买单"先后成交; 持仓仍在的(信号卖单在途)留给那张卖单处理 —— 该股仍在
                # target_codes, 上面的持仓循环不会给它叠"月度方案调出"。
                conn.execute("DELETE FROM paper_order WHERE order_date=? AND status='pending' "
                             "AND side='buy' AND reason LIKE '月度方案%' AND ts_code=?",
                             (plan_date, t["ts_code"]))
                continue
            cur = conn.execute("SELECT shares FROM paper_position WHERE ts_code=?",
                               (t["ts_code"],)).fetchone()
            cur = cur[0] if cur else 0
            delta = int(t["shares"]) - cur
            if delta > 0:
                if gov_block:
                    skipped_buys += 1
                    continue
                n += _create_order(conn, plan_date, t["ts_code"], t["name"], "buy", delta, "月度方案买入")
            elif delta < 0:
                n += _create_order(conn, plan_date, t["ts_code"], t["name"], "sell", -delta, "月度方案减仓")
        if skipped_buys:
            log.warning("回撤治理: 方案买单 %d 只被拦下(补仓顺延到解除后), 卖出/减仓不受影响",
                        skipped_buys)
        conn.commit()
    finally:
        # 第十轮 N10-6: 异常路径连接必须释放
        conn.close()
    if n:
        log.info("模拟盘方案订单 %d 笔 (%s)", n, plan_date)
    return n


def apply_signals(signal_date) -> int:
    """按当日动态信号生成订单"""
    if trading_paused():   # 2026-09-09 交易总闸: 暂停期不按信号挂单
        log.info("模拟盘已暂停(paper.paused=true), 跳过信号挂单 (%s)", signal_date)
        return 0
    conn = db.get_conn()
    try:
        sig = pd.read_sql("SELECT * FROM signal WHERE signal_date=?", conn, params=(signal_date,))
        if sig.empty:
            return 0
        cost_rate = float(cfg_mod.load_config().get("single_side_cost", 0.003))
        n = 0
        sells = sig[sig["action"].isin(["sell", "sell_weak", "reduce"])]
        for _, s in sells.iterrows():
            pos = conn.execute("SELECT shares FROM paper_position WHERE ts_code=?",
                               (s["ts_code"],)).fetchone()
            if pos and pos[0] > 0:
                n += _create_order(conn, signal_date, s["ts_code"], s["name"], "sell", pos[0], s["reason"])
        adds = sig[sig["action"] == "add"]
        cands = sig[sig["action"] == "buy_candidate"]
        # 双闸(决策函数, 卖出路径一概不经过):
        # ① 回撤治理谨慎态 ⇒ add/候选替换买入全停(敞口只降不升);
        # ② defense 状态 ⇒ 候选**只看不動** —— config.yaml dynamic 段登记过的已知项
        #    ("候选名单不看市场状态")在执行侧的落法: 邮件照推, 替换买入不做。
        gov_block = bool(_governor(conn).get("cautious"))
        regime_now = _regime_state(conn, signal_date)
        if gov_block:
            log.warning("回撤治理触发: 动态层买入(add/候选替换)全停, 卖出照常 (%s)", signal_date)
        elif regime_now == "defense":
            log.info("defense 状态: 候选替换买入停(只看不動), add/卖出不受影响 (%s)", signal_date)
        if not sells.empty or not adds.empty:
            cash = conn.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()[0]
            # 预算冻结(2026-09-04 审查 P2-10): 原按全额现金均分, 未考虑已挂起的方案买单,
            # 成交时靠 affordable 截断 → 实际成交偏离方案。现扣除所有 pending 买单价款
            frozen = 0.0
            for code, shares in conn.execute(
                    "SELECT ts_code, shares FROM paper_order WHERE status='pending' AND side='buy'"):
                pr = _price(conn, code, signal_date)
                if pr:
                    frozen += shares * pr * (1 + cost_rate)
            cash_avail = max(0.0, cash - frozen)
            picks = pd.concat([adds, cands]).head(5)
            # 2026-09-06 第十三轮 R13-5: 先滤出实际可买清单(未持有且有行情), 预算按其
            # 数量均摊 —— 原按候选总数均分, 跳过已持/无价标的后单只预算偏小、现金闲置
            buyable = []
            for _, p in picks.iterrows():
                if gov_block and p["action"] in ("add", "buy_candidate"):
                    continue                      # 治理器: 停开新仓
                if p["action"] == "buy_candidate" and regime_now == "defense":
                    continue                      # defense: 候选只看不動
                held = conn.execute("SELECT shares FROM paper_position WHERE ts_code=?",
                                    (p["ts_code"],)).fetchone()
                if held and held[0] > 0:
                    continue
                price = _price(conn, p["ts_code"], signal_date)
                if not price:
                    continue
                buyable.append((p, price))
            for p, price in buyable:
                budget = cash_avail / max(1, len(buyable))
                # 2026-09-08 审查 P2: 按板块整手规则(与 execute_pending 同口径)
                units = int(budget / (price * (1 + cost_rate)) + 1e-9)
                shares = lot_shares(p["ts_code"], units)
                if shares > 0:
                    n += _create_order(conn, signal_date, p["ts_code"], p["name"], "buy", shares,
                                       p["reason"] + SWAP_BUY_REASON_SUFFIX)
        conn.commit()
    finally:
        # 第十轮 N10-6: 异常路径连接必须释放
        conn.close()
    return n


def _accrued_interest(conn, trade_date) -> float:
    """现金余额按逆回购隔夜日利率计的累计利息(至 trade_date 含)。
    2026-09-04 审查 P0-6: 兑现 README「现金余额计回购收益」承诺, 与回测口径对齐。
    计法: 上一 nav 日现金 × 区间日历日(含周末/节假日) × 上一 nav 日最近隔夜利率(年化, 无数据用1.5%);
    按日 upsert 到 paper_interest, 同日重复调用幂等; 现金本列不动, 利息只体现在净值里。
    """
    prev = conn.execute("SELECT MAX(trade_date) FROM paper_nav WHERE trade_date<?",
                        (trade_date,)).fetchone()[0]
    if not prev:
        return 0.0
    row = conn.execute("SELECT cash FROM paper_nav WHERE trade_date=?", (prev,)).fetchone()
    if not row or row[0] is None:
        return 0.0
    rate_row = conn.execute(
        "SELECT overnight FROM repo_rate WHERE trade_date<=? ORDER BY trade_date DESC LIMIT 1",
        (prev,)).fetchone()
    rate = float(rate_row[0]) if rate_row and rate_row[0] else 1.5
    days = (datetime.datetime.strptime(trade_date, "%Y%m%d")
            - datetime.datetime.strptime(prev, "%Y%m%d")).days
    amount = round(float(row[0]) * (rate / 100) * days / 365, 2)
    conn.execute("INSERT OR REPLACE INTO paper_interest (trade_date, amount) VALUES (?,?)",
                 (trade_date, amount))
    total = conn.execute("SELECT SUM(amount) FROM paper_interest WHERE trade_date<=?",
                         (trade_date,)).fetchone()[0]
    return float(total or 0.0)


def update_nav(trade_date):
    conn = db.get_conn()
    try:
        cash = conn.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()[0]
        # 2026-09-07 回填: 开盘档成交时当日复权因子尚未发布(entry_adj 存 NULL) ——
        # 晚间同步后按当日官方因子回填, 总收益口径自愈。只回填当日有买入成交的持仓
        # (避免动历史旧账); adj_factor 缺失时 UPDATE 置 NULL 为无害空操作
        conn.execute(
            "UPDATE paper_position SET entry_adj = ("
            "  SELECT a.adj_factor FROM adj_factor a "
            "  WHERE a.ts_code = paper_position.ts_code AND a.trade_date = ?) "
            "WHERE entry_adj IS NULL AND ts_code IN "
            "  (SELECT ts_code FROM paper_trade WHERE exec_date = ? AND side = 'buy')",
            (trade_date, trade_date))
        pos = pd.read_sql("SELECT * FROM paper_position", conn)
        mv = 0.0
        for _, p in pos.iterrows():
            pr = _price(conn, p["ts_code"], trade_date)
            if not pr:
                # 2026-09-05 审查修复: 停牌/退市当日无行情 → 沿用最近收盘价, 原直接跳过
                # 会让整仓市值在停牌期凭空缺席(净值幻影回撤, 复牌日跳回)
                row = conn.execute(
                    "SELECT close FROM daily WHERE ts_code=? AND trade_date<=? "
                    "ORDER BY trade_date DESC LIMIT 1", (p["ts_code"], trade_date)).fetchone()
                pr = row[0] if row and row[0] else None
            if pr:
                # 总收益口径: 股数×当日收盘×(当日adj/开仓adj), 除权除息不产生假浮亏 (P0-3)
                # adj 或 entry_adj 缺失时回退未复权市值。
                # 2026-09-16 资金线审查 FUND-02: adj 当日缺失时改用**最近可得因子**(与卖出
                # 侧 2026-09-08 P0 修复同语义) —— 原回退未复权价会在除权日制造一日幻影回撤,
                # 次日因子到位又跳回; 非除权日两口径恒等, 当前库恒等式核查偏差 0.0000 不变。
                ea = p["entry_adj"] if pd.notna(p["entry_adj"]) and p["entry_adj"] else None
                if ea:
                    adj_t = _adj(conn, p["ts_code"], trade_date)
                    if adj_t is None:
                        row = conn.execute(
                            "SELECT adj_factor FROM adj_factor WHERE ts_code=? AND trade_date<=? "
                            "ORDER BY trade_date DESC LIMIT 1",
                            (p["ts_code"], trade_date)).fetchone()
                        adj_t = float(row[0]) if row and row[0] else None
                    if adj_t:
                        mv += p["shares"] * pr * (adj_t / ea)
                        continue
                mv += p["shares"] * pr
        # 2026-09-07 一致性修复(R18-2 遗漏): 基准统一从 config.benchmark_code() 取,
        # 原硬编码 '000906.SH' 在改 config.benchmark 时模拟盘基准列不跟随(仅影响基准对比列)
        bench = conn.execute("SELECT close FROM index_daily WHERE ts_code=? AND trade_date=?",
                             (cfg_mod.benchmark_code(), trade_date)).fetchone()
        interest = _accrued_interest(conn, trade_date)
        conn.execute(
            "INSERT OR REPLACE INTO paper_nav (trade_date, nav, cash, market_value, bench_nav) VALUES (?,?,?,?,?)",
            (trade_date, round(cash + mv + interest, 2), cash, round(mv, 2),
             bench[0] if bench else None))
        conn.commit()
    finally:
        # 第十轮 N10-6: 异常路径连接必须释放
        conn.close()


def run_daily(trade_date: str = None) -> dict:
    ensure_account()
    conn = db.get_conn()
    try:
        if trade_date is None:
            trade_date = conn.execute("SELECT MAX(trade_date) FROM daily").fetchone()[0]
        plan_date = conn.execute("SELECT MAX(plan_date) FROM plan").fetchone()[0]
    finally:
        conn.close()
    if trade_date is None:
        # 2026-09-05 审查修复: daily 空表时(新装机)跳过执行与净值, 防 paper_nav 主键 NULL 行
        log.warning("daily 表为空, 跳过模拟盘执行与净值(等待行情初始化)")
        return {"trade_date": None, "trades": [], "plan_orders": 0, "signal_orders": 0}
    if trading_paused():
        # 2026-09-09 用户拍板「暂停实盘, 先修」: 交易闸 —— 不成交/不挂单, 存量挂单作废;
        # 净值记账继续(持仓照市, 只是没有新成交), 数据/训练/回测/监控链路不受影响
        n = _expire_pending_on_pause()
        update_nav(trade_date)
        log.warning("模拟盘已暂停(paper.paused=true): 作废存量挂单 %d 笔, 仅记净值 %s",
                    n, trade_date)
        return {"trade_date": trade_date, "trades": [], "plan_orders": 0,
                "signal_orders": 0, "paused": True, "expired_orders": n}
    trades = execute_pending(trade_date)
    if plan_date is None:
        # 2026-09-08 审查 P1: plan 表为空时 apply_plan(None) 会把全部持仓判为"调出"
        # 而逐只挂清仓卖单 —— 跳过方案应用, 只执行与记账, 防误清仓
        log.warning("plan 表为空, 跳过方案应用(防误清仓), 仅执行挂单与记账")
        n_plan = 0
    else:
        n_plan = apply_plan(plan_date)
    n_sig = apply_signals(trade_date)
    update_nav(trade_date)
    # 治理态随日报出(18:30 日终汇总/日志可见): 触发时给出 reason, 未触发为 None
    conn = db.get_conn()
    try:
        gov = _governor(conn)
    finally:
        conn.close()
    return {"trade_date": trade_date, "trades": trades,
            "plan_orders": n_plan, "signal_orders": n_sig,
            "governor": gov.get("reason") if gov.get("cautious") else None}


def build_daily_summary(trade_date: str = None) -> dict:
    """日终汇总(2026-09-07 18:30 邮件档): 当日净值/现金/市值/累计计息/持仓盈亏/当日成交。
    持仓估值与面板 /api/paper 同口径(总收益口径 tr=close×adj/entry_adj, P0-3);
    当日尚未记账(18:00 链未跑完/失败)时返回空 dict, 调用方跳过本次发送。

    2026-09-18 邮件优化补三个参照点(`prev`/`initial`/`bench0`): 邮件要直接给结论
    (当日盈亏、累计%、对基准超额)，而这三个数都是"相对"读数，缺参照点时**邮件退化成
    只报绝对值**，不猜数。
    """
    conn = db.get_conn()
    try:
        if trade_date is None:
            trade_date = conn.execute("SELECT MAX(trade_date) FROM daily").fetchone()[0]
        if not trade_date:
            return {}
        nav = conn.execute(
            "SELECT nav, cash, market_value, bench_nav FROM paper_nav WHERE trade_date=?",
            (trade_date,)).fetchone()
        if not nav:
            return {}
        pos = pd.read_sql("""
            SELECT p.ts_code, p.name, p.shares, p.avg_cost, p.entry_adj,
                   d.close, a.adj_factor
            FROM paper_position p
            LEFT JOIN daily d ON d.ts_code=p.ts_code AND d.trade_date=?
            LEFT JOIN adj_factor a ON a.ts_code=p.ts_code AND a.trade_date=?
        """, conn, params=(trade_date, trade_date))
        # `cost` 是**逐笔真实费用**(见 `app/portfolio/fees.py`), 邮件要能报出今天花了多少手续费。
        # 不取 `amount`: 买入行存成交额、卖出行存扣费后净回收, 两处语义不同, 邮件里统一用
        # shares×price 自己算成交额。
        trades = pd.read_sql(
            "SELECT ts_code, name, side, shares, price, cost, reason FROM paper_trade "
            "WHERE exec_date=? ORDER BY id", conn, params=(trade_date,))
        interest = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM paper_interest WHERE trade_date<=?",
            (trade_date,)).fetchone()[0]
        # 2026-09-18 邮件优化：日终总结要能说"今天涨了多少、累计多少、跑赢基准没有"，
        # 这三个数各需要一个参照点 —— 全部从库里取，不在邮件里另算一套口径。
        #   prev    = 上一交易日净值行（当日盈亏 = nav - prev.nav）
        #   initial = 建仓时的初始资金（`paper_account.initial_capital`，与看板 /api/paper
        #             同一个源；它创建后独立于后续 config 改动，所以中途改本金不会篡改历史）
        #   bench0  = 最早一期的基准点位（基准同期涨幅）
        prev = conn.execute(
            "SELECT trade_date, nav, cash, market_value, bench_nav FROM paper_nav "
            "WHERE trade_date<? ORDER BY trade_date DESC LIMIT 1", (trade_date,)).fetchone()
        initial = conn.execute(
            "SELECT initial_capital FROM paper_account LIMIT 1").fetchone()
        bench0 = conn.execute(
            "SELECT bench_nav FROM paper_nav WHERE bench_nav IS NOT NULL "
            "ORDER BY trade_date ASC LIMIT 1").fetchone()
    finally:
        conn.close()
    positions = []
    for _, r in pos.iterrows():
        close = r["close"] if pd.notna(r["close"]) else None
        tr = None
        if close and pd.notna(r["avg_cost"]):
            ea = r["entry_adj"] if pd.notna(r["entry_adj"]) and r["entry_adj"] else None
            if ea and pd.notna(r["adj_factor"]) and r["adj_factor"]:
                tr = close * (r["adj_factor"] / ea)
            else:
                tr = close
        positions.append({
            "ts_code": r["ts_code"], "name": r["name"], "shares": int(r["shares"]),
            "avg_cost": round(float(r["avg_cost"]), 3) if pd.notna(r["avg_cost"]) else None,
            "close": close,
            "pnl": round(r["shares"] * (tr - r["avg_cost"]), 2) if tr else None,
            "pnl_pct": round((tr / r["avg_cost"] - 1) * 100, 2) if tr and r["avg_cost"] else None,
        })
    return {
        "trade_date": trade_date,
        "nav": nav[0], "cash": nav[1], "market_value": nav[2],
        "bench_nav": nav[3],
        "interest": round(float(interest or 0), 2),
        "positions": positions,
        "trades": trades.to_dict("records") if not trades.empty else [],
        # 邮件算"当日/累计/超额"用的三个参照点（缺任何一个 ⇒ 邮件退化成只报绝对值，
        # 不猜数）
        "prev": ({"trade_date": prev[0], "nav": prev[1], "cash": prev[2],
                  "market_value": prev[3], "bench_nav": prev[4]} if prev else None),
        "initial": (float(initial[0]) if initial and pd.notna(initial[0]) else None),
        "bench0": (float(bench0[0]) if bench0 and pd.notna(bench0[0]) else None),
    }
