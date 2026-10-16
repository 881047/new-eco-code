"""模拟盘执行引擎单测: 停机补跑/两阶段换仓/entry_adj 回填 (2026-09-04 审查修复)

运行: python -m unittest tests.test_paper_exec -v

基座(`_SCHEMA` + `_ExecBase`)自 2026-09-12 起挪到 `tests/_paper_base.py` ——
`tests/test_fees.py` 也在用它, 而让一个测试文件去 import 另一个测试文件的**私有**基类
太脆: 前者一改名/一挪目录, 后者在 collection 期就崩, 崩点还与被测行为毫无关系。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.paper import engine
from tests._paper_base import _ExecBase


class TestGapCatchup(_ExecBase):
    def test_first_run_after_long_gap_executes(self):
        # 0831 建单, 机器 0901-0913 停机, 0914 首次补跑: 不应一次性作废
        self._open_days(["20260901", "20260902", "20260903", "20260904", "20260907",
                         "20260908", "20260909", "20260910", "20260911", "20260914"])
        self._order("20260831", "600000.SH", "buy", 100)
        self._daily("600000.SH", "20260914", 10.0, 9.9)
        trades = engine.execute_pending("20260914")
        self.assertEqual(len(trades), 1)
        self.assertEqual(self._status("600000.SH", "buy")[0], "done")
        pos = self._one("SELECT shares FROM paper_position WHERE ts_code='600000.SH'")
        self.assertEqual(pos[0], 100)

    def test_same_day_order_skipped(self):
        # 当日建单不可当日执行, 也不记 first_attempt
        self._open_days(["20260914"])
        self._order("20260914", "600000.SH", "buy", 100)
        self._daily("600000.SH", "20260914", 10.0, 9.9)
        self.assertEqual(engine.execute_pending("20260914"), [])
        s, fa = self._status("600000.SH", "buy")
        self.assertEqual(s, "pending")
        self.assertIsNone(fa)


class TestExpiryClock(_ExecBase):
    def test_oneword_board_expires_after_5_attempts(self):
        # 一字涨停买不进: 顺延满 5 个交易日(自首次尝试起)才过期, 第 6 天不再尝试
        days = ["20260901", "20260902", "20260903", "20260904",
                "20260907", "20260908"]
        self._open_days(days)
        self._order("20260831", "600000.SH", "buy", 100)
        for d in days:
            self._daily("600000.SH", d, 11.0, 10.0)  # open=涨停价 → 买不进
        for d in days[:5]:
            engine.execute_pending(d)
            self.assertEqual(self._status("600000.SH", "buy")[0], "pending", d)
        engine.execute_pending(days[5])
        self.assertEqual(self._status("600000.SH", "buy")[0], "expired")
        # 过期后重跑不再成交
        self._exec("UPDATE paper_order SET status='pending' WHERE ts_code='600000.SH'")
        engine.execute_pending(days[5])
        self.assertEqual(self._status("600000.SH", "buy")[0], "expired")


class TestTwoPhase(_ExecBase):
    def test_sell_funds_same_day_buy_regardless_of_code_order(self):
        # 现金仅 500: 卖出 600519 筹资买入 000001 —— 买单排序在前也必须全额成交
        self._exec("UPDATE paper_account SET cash=500.0 WHERE id=1")
        self._open_days(["20260914"])
        self._order("20260831", "600519.SH", "sell", 1000)
        self._order("20260831", "000001.SZ", "buy", 900)
        self._exec("INSERT INTO paper_position VALUES ('600519.SH','TEST',1000,90.0,NULL)")
        self._daily("600519.SH", "20260914", 100.0, 99.5)
        self._daily("000001.SZ", "20260914", 110.0, 109.0)
        trades = engine.execute_pending("20260914")
        self.assertEqual(len(trades), 2)
        self.assertEqual(self._status("600519.SH", "sell")[0], "done")
        self.assertEqual(self._status("000001.SZ", "buy")[0], "done")
        pos = self._one("SELECT shares FROM paper_position WHERE ts_code='000001.SZ'")
        self.assertEqual(pos[0], 900)  # 卖单资金已入账, 买单不受行序影响

    def test_buy_unaffordable_extends_then_expires(self):
        # 2026-09-05 审查修复: 资金不足按"尝试"顺延(先卖后买的换仓场景里卖单顺延成交时
        # 买单资金才到位), 原首日即 expired 让信号买单永久丢失; 满 5 个交易日后作废
        self._exec("UPDATE paper_account SET cash=500.0 WHERE id=1")
        days = ["20260914", "20260915", "20260916", "20260917", "20260918", "20260921"]
        self._open_days(days)
        self._order("20260831", "000001.SZ", "buy", 900)
        for d in days:
            self._daily("000001.SZ", d, 110.0, 109.0)
        engine.execute_pending(days[0])
        self.assertEqual(self._status("000001.SZ", "buy")[0], "pending")  # 首日顺延不 expired
        for d in days[1:5]:
            engine.execute_pending(d)
            self.assertEqual(self._status("000001.SZ", "buy")[0], "pending", d)
        engine.execute_pending(days[5])
        self.assertEqual(self._status("000001.SZ", "buy")[0], "expired")


class TestNullEntryAdjMerge(_ExecBase):
    def test_backfill_from_earliest_buy_trade(self):
        # 旧仓 entry_adj=NULL, 但最早买入成交日有复权因子 → 回填该因子再调和合并
        self._open_days(["20260914"])
        self._order("20260831", "600000.SH", "buy", 100)
        self._exec("INSERT INTO paper_position VALUES ('600000.SH','TEST',100,10.0,NULL)")
        self._exec(
            "INSERT INTO paper_trade (exec_date, ts_code, name, side, shares, price) "
            "VALUES ('20260105','600000.SH','TEST','buy',100,10.0)")
        self._exec("INSERT INTO adj_factor VALUES ('600000.SH','20260105',2.0)")
        self._exec("INSERT INTO adj_factor VALUES ('600000.SH','20260914',2.5)")
        self._daily("600000.SH", "20260914", 10.0, 9.9)
        engine.execute_pending("20260914")
        ea = self._one("SELECT entry_adj FROM paper_position WHERE ts_code='600000.SH'")[0]
        expect = (100 + 100) / (100 / 2.0 + 100 / 2.5)
        self.assertAlmostEqual(ea, expect, places=6)  # 不是 1.0、也不是 None

    def test_no_history_falls_back_to_today_adj(self):
        # 无成交史: 用当日因子近似 → 合并当日口径连续(merge 前旧仓按未复权估值不变)
        self._open_days(["20260914"])
        self._order("20260831", "600000.SH", "buy", 100)
        self._exec("INSERT INTO paper_position VALUES ('600000.SH','TEST',100,10.0,NULL)")
        self._exec("INSERT INTO adj_factor VALUES ('600000.SH','20260914',2.5)")
        self._daily("600000.SH", "20260914", 10.0, 9.9)
        engine.execute_pending("20260914")
        ea = self._one("SELECT entry_adj FROM paper_position WHERE ts_code='600000.SH'")[0]
        self.assertAlmostEqual(ea, 2.5, places=6)

    def test_no_adj_at_all_keeps_null(self):
        # 完全无复权因子: 保持未复权口径(NULL), 不伪造基准
        self._open_days(["20260914"])
        self._order("20260831", "600000.SH", "buy", 100)
        self._exec("INSERT INTO paper_position VALUES ('600000.SH','TEST',100,10.0,NULL)")
        self._daily("600000.SH", "20260914", 10.0, 9.9)
        engine.execute_pending("20260914")
        ea = self._one("SELECT entry_adj FROM paper_position WHERE ts_code='600000.SH'")[0]
        self.assertIsNone(ea)


class TestCreatedAtGate(_ExecBase):
    """2026-09-05 创建时间闸: 只执行成交日严格晚于创建日的订单, 根治补跑/手动触发
    把次日挂单按当日开盘价成交(0904 早/晚两次事故同根因)"""

    def test_created_same_day_as_bar_skipped(self):
        # 事故场景: 0904 深夜建单(order_date=0831), 当日补跑 0904 —— 必须跳过而非按 0904 开盘成交
        self._open_days(["20260904"])
        self._order_created("20260831", "600000.SH", "buy", 100, "2026-09-04 23:18:00")
        self._daily("600000.SH", "20260904", 10.0, 9.9)
        self.assertEqual(engine.execute_pending("20260904"), [])
        s, fa = self._status("600000.SH", "buy")
        self.assertEqual(s, "pending")
        self.assertIsNone(fa)  # 未到执行日不算尝试

    def test_created_before_bar_executes(self):
        self._open_days(["20260907"])
        self._order_created("20260831", "600000.SH", "buy", 100, "2026-09-04 23:18:00")
        self._daily("600000.SH", "20260907", 10.0, 9.9)
        self.assertEqual(len(engine.execute_pending("20260907")), 1)
        self.assertEqual(self._status("600000.SH", "buy")[0], "done")

    def test_create_order_writes_created_at(self):
        conn = self._c()
        engine._create_order(conn, "20260831", "600000.SH", "T", "buy", 100, "r")
        row = conn.execute("SELECT created_at FROM paper_order WHERE ts_code='600000.SH'").fetchone()
        conn.close()
        self.assertIsNotNone(row[0])
        self.assertRegex(row[0], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class TestTradingDayGate(_ExecBase):
    """2026-09-05 深夜: 非交易日补跑不得按最近收盘价回放成交(0905 周六 00:34 实证 4 笔)"""

    def test_weekend_run_skips(self):
        # 0905 是周六(is_open=0): 挂单创建于 0904 之前也必须跳过, 且不记 first_attempt
        self._exec("INSERT OR REPLACE INTO trade_cal VALUES ('20260905',0)")
        self._order_created("20260831", "600000.SH", "buy", 100, "2026-09-04 23:18:00")
        self._daily("600000.SH", "20260904", 10.0, 9.9)
        self.assertEqual(engine.execute_pending("20260905"), [])
        s, fa = self._status("600000.SH", "buy")
        self.assertEqual(s, "pending")
        self.assertIsNone(fa)

    def test_missing_calendar_row_skips(self):
        # trade_cal 无该日记录: 无法确认开市 → 视为非交易日跳过
        self._order_created("20260831", "600000.SH", "buy", 100, "2026-09-04 23:18:00")
        self._daily("600000.SH", "20260904", 10.0, 9.9)
        self.assertEqual(engine.execute_pending("20260906"), [])
        s, fa = self._status("600000.SH", "buy")
        self.assertEqual(s, "pending")
        self.assertIsNone(fa)

    def test_weekend_catchup_fills_at_latest_data_day(self):
        # 第五轮复查 P5-4 语义锁定: 周四建单、周五停机、周六 run_daily(trade_date=
        # MAX(daily)=周五, 开市) → 按周五价回放成交是预期补跑语义, 闸不得拦截
        self._open_days(["20260904"])
        self._order_created("20260831", "600000.SH", "buy", 100, "2026-09-03 23:00:00")
        self._daily("600000.SH", "20260904", 10.0, 9.9)
        self.assertEqual(len(engine.execute_pending("20260904")), 1)
        self.assertEqual(self._status("600000.SH", "buy")[0], "done")


class TestApplyPlanReapply(_ExecBase):
    """2026-09-05 审查修复: 同 plan_date 方案重建后重新 apply 必须撤销旧挂单
    (原 last_applied_plan 守卫在撤销之前 return, 被剔除股票的旧买单照常成交)"""

    def test_reapply_revokes_stale_pending(self):
        self._exec("UPDATE paper_account SET last_applied_plan='20260831' WHERE id=1")
        self._exec("INSERT INTO plan (plan_date, plan_type, ts_code, name, shares) "
                   "VALUES ('20260831','stock','000001.SZ','新目标',200)")
        # 被新方案剔除的旧方案挂单(仍 pending; 2026-09-08 起撤销按 reason='月度方案%'
        # 过滤, 信号单不受影响 —— 夹具改用与生产一致的 reason 前缀)
        self._exec("INSERT INTO paper_order (order_date, ts_code, name, side, shares, reason, "
                   "status) VALUES ('20260831','600000.SH','旧股票','buy',100,'月度方案买入','pending')")
        engine.apply_plan("20260831")
        stale = self._one("SELECT COUNT(*) FROM paper_order WHERE ts_code='600000.SH' "
                          "AND status='pending'")[0]
        self.assertEqual(stale, 0)  # 旧挂单已撤销
        fresh = self._one("SELECT side, shares FROM paper_order WHERE ts_code='000001.SZ' "
                          "AND status='pending'")
        self.assertEqual(fresh, ("buy", 200))  # 新方案挂单已重建
        # 2026-09-08 审查 P2 回归: 同日信号单(非月度方案前缀)不被方案撤销误删
        self._exec("INSERT INTO paper_order (order_date, ts_code, name, side, shares, reason, "
                   "status) VALUES ('20260831','600001.SH','信号股','buy',100,'止损替换买入','pending')")
        engine.apply_plan("20260831")
        kept = self._one("SELECT COUNT(*) FROM paper_order WHERE ts_code='600001.SH' "
                         "AND status='pending'")[0]
        self.assertEqual(kept, 1)  # 信号单保留


class TestApplyPlanEmptyTargetGuard(_ExecBase):
    """2026-09-19 审查 A7：**本月没有股票方案 ≠ 决定清仓**。

    `apply_plan` 的调仓循环是"持仓不在目标集 ⇒ 挂全仓卖单"，所以目标集为空时会把
    **每一笔持仓**都清掉(理由"月度方案调出")。而空目标集在真实数据里出现在：
      · `plan` 表被清空/手工改库（`run_daily` 取 `MAX(plan_date) FROM plan`，可能拿到 NULL）；
      · 方案月只写出 etf/otc/cash/meta 行（builder.py:152 那条"无上月股票组合可沿用"的兜底）。
    builder 那侧的注释写着目的是"避免模拟盘误清仓" —— 守卫必须落在下单这一侧才算数。
    """

    def _held(self, code="600000.SH"):
        self._exec("INSERT INTO paper_position (ts_code, name, shares, avg_cost, entry_adj) "
                   "VALUES (?, 'TEST', 100, 10.0, 1.0)", (code,))

    def _n_orders(self):
        return self._one("SELECT COUNT(*) FROM paper_order")[0]

    def test_zero_stock_plan_places_no_orders(self):
        self._held()
        self._exec("INSERT INTO plan (plan_date, plan_type, ts_code, name) "
                   "VALUES ('20260831','etf','510300.SH','沪深300ETF')")
        self.assertEqual(engine.apply_plan("20260831"), 0, "空股票方案不许下任何单调仓")
        self.assertEqual(self._n_orders(), 0)
        self.assertEqual(self._one("SELECT COUNT(*) FROM paper_position")[0], 1,
                         "持仓必须原地不动")

    def test_null_plan_date_places_no_orders(self):
        self._held()
        self.assertEqual(engine.apply_plan(None), 0)
        self.assertEqual(self._n_orders(), 0)

    def test_normal_plan_still_rebalances(self):
        """守卫不许把**正常路径**一起拦掉：换仓照样要卖旧买新。"""
        self._held("600000.SH")
        self._exec("INSERT INTO plan (plan_date, plan_type, ts_code, name, shares) "
                   "VALUES ('20260831','stock','000001.SZ','新目标',200)")
        engine.apply_plan("20260831")
        self.assertEqual(self._one(
            "SELECT side FROM paper_order WHERE ts_code='600000.SH'")[0], "sell",
            "被方案剔除的持仓仍须挂卖出单")
        self.assertEqual(self._one(
            "SELECT side, shares FROM paper_order WHERE ts_code='000001.SZ'"), ("buy", 200))


class TestSameDaySameSideTrades(_ExecBase):
    """P0-3 回归: 跨信号日两笔同向订单同日成交, 成交记录不得互相覆盖(原主键三元组会丢一笔)"""

    def test_two_buy_orders_same_day_keep_both_trades(self):
        self._open_days(["20260901", "20260902"])
        self._order("20260831", "600000.SH", "buy", 100)
        self._order("20260901", "600000.SH", "buy", 100)
        self._daily("600000.SH", "20260902", 10.0, 9.9)
        trades = engine.execute_pending("20260902")
        self.assertEqual(len(trades), 2)
        rows = self._one("SELECT COUNT(*) FROM paper_trade WHERE exec_date='20260902' "
                         "AND ts_code='600000.SH' AND side='buy'")[0]
        self.assertEqual(rows, 2)  # 两笔成交各占一行, 不覆盖
        pos = self._one("SELECT shares FROM paper_position WHERE ts_code='600000.SH'")
        self.assertEqual(pos[0], 200)  # 账(持仓)与表(成交)一致

    def test_create_order_idempotent_same_day_same_side(self):
        # 同日同向重复挂单: 更新股数而非插重复行(主键改自增 id 后的显式去重)
        conn = self._c()
        engine._create_order(conn, "20260831", "600000.SH", "T", "buy", 100, "r1")
        engine._create_order(conn, "20260831", "600000.SH", "T", "buy", 300, "r2")
        n = conn.execute(
            "SELECT COUNT(*) FROM paper_order WHERE order_date='20260831' AND side='buy'"
        ).fetchone()[0]
        shares = conn.execute(
            "SELECT shares, reason FROM paper_order WHERE order_date='20260831' AND side='buy'"
        ).fetchone()
        conn.close()
        self.assertEqual(n, 1)
        self.assertEqual(shares[0], 300)
        self.assertEqual(shares[1], "r2")


class TestCashInterest(_ExecBase):
    """P0-6 回归: 模拟盘现金按回购隔夜利率计息(README 承诺), 同日重跑幂等"""

    def test_interest_accrues_by_calendar_days(self):
        self._open_days(["20260901", "20260902"])
        # 隔夜 3.65% → 每日历日 0.01%, 10 万元现金每日利息 10 元
        self._exec("INSERT INTO repo_rate VALUES ('20260901',3.65,3.7,3.8)")
        self._exec("INSERT INTO paper_nav (trade_date, nav, cash, market_value) "
                   "VALUES ('20260901',100000.0,100000.0,0.0)")
        engine.update_nav("20260902")
        nav = self._one("SELECT nav, cash FROM paper_nav WHERE trade_date='20260902'")
        self.assertAlmostEqual(nav[0], 100010.0, places=2)
        self.assertEqual(nav[1], 100000.0)  # 现金本列不动, 利息只在净值
        # 跨 3 个日历日(0902→0905, 含非交易日): 再计 30 元, 累计 40
        engine.update_nav("20260905")
        nav2 = self._one("SELECT nav FROM paper_nav WHERE trade_date='20260905'")
        self.assertAlmostEqual(nav2[0], 100040.0, places=2)

    def test_interest_idempotent_same_day_rerun(self):
        self._open_days(["20260901", "20260902"])
        self._exec("INSERT INTO repo_rate VALUES ('20260901',3.65,3.7,3.8)")
        self._exec("INSERT INTO paper_nav (trade_date, nav, cash, market_value) "
                   "VALUES ('20260901',100000.0,100000.0,0.0)")
        engine.update_nav("20260902")
        engine.update_nav("20260902")  # 同日重跑(手动触发+定时): 不重复计息
        nav = self._one("SELECT nav FROM paper_nav WHERE trade_date='20260902'")
        self.assertAlmostEqual(nav[0], 100010.0, places=2)
        rows = self._one("SELECT COUNT(*) FROM paper_interest")
        self.assertEqual(rows[0], 1)

    def test_interest_fallback_rate_when_no_repo_data(self):
        # 无 repo_rate: 固定 1.5% 假设, 100000×1.5%/365 = 4.11 元/日
        self._open_days(["20260901", "20260902"])
        self._exec("INSERT INTO paper_nav (trade_date, nav, cash, market_value) "
                   "VALUES ('20260901',100000.0,100000.0,0.0)")
        engine.update_nav("20260902")
        nav = self._one("SELECT nav FROM paper_nav WHERE trade_date='20260902'")
        self.assertAlmostEqual(nav[0], 100004.11, places=2)

    def test_no_prev_nav_no_interest(self):
        self._open_days(["20260902"])
        engine.update_nav("20260902")
        nav = self._one("SELECT nav FROM paper_nav WHERE trade_date='20260902'")
        self.assertEqual(nav[0], 100000.0)  # 首条 nav 无历史区间, 不计息


class TestOpenExec(_ExecBase):
    """开盘档(2026-09-07): 实时今开价撮合, 一字板/无行情/脏数据顺延晚间链, 与晚间链幂等"""

    def _quotes(self, **kw):
        q = {"open": 10.0, "prev_close": 9.9, "price": 10.2,
             "time": "20260907093101", "date": "20260907"}
        q.update(kw)
        return q

    def _qpatch(self, quotes):
        return mock.patch("app.datasource.quote_client.get_quotes", return_value=quotes)

    def test_open_executes_at_open_price(self):
        self._open_days(["20260907"])
        self._order_created("20260831", "600000.SH", "buy", 100, "2026-09-06 16:00:00")
        with self._qpatch({"600000.SH": self._quotes()}):
            trades = engine.execute_open("20260907")
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["price"], round(10.0 * 1.001, 3))  # 今开×滑点
        self.assertEqual(self._status("600000.SH", "buy")[0], "done")
        pos = self._one("SELECT shares FROM paper_position WHERE ts_code='600000.SH'")
        self.assertEqual(pos[0], 100)
        cash = self._one("SELECT cash FROM paper_account WHERE id=1")[0]
        self.assertAlmostEqual(cash, 100000 - 100 * round(10.0 * 1.001, 3) * 1.003, places=2)

    def test_open_limit_up_deferred(self):
        self._open_days(["20260907"])
        self._order_created("20260831", "600000.SH", "buy", 100, "2026-09-06 16:00:00")
        limit = round(9.9 * 1.10, 2)
        with self._qpatch({"600000.SH": self._quotes(open=limit)}):
            trades = engine.execute_open("20260907")
        self.assertEqual(trades, [])  # 一字涨停买不进, 顺延晚间链
        self.assertEqual(self._status("600000.SH", "buy")[0], "pending")

    def test_open_missing_quote_single_deferred(self):
        self._open_days(["20260907"])
        self._order_created("20260831", "600000.SH", "buy", 100, "2026-09-06 16:00:00")
        self._order_created("20260831", "000001.SZ", "buy", 100, "2026-09-06 16:00:00")
        quotes = {"600000.SH": self._quotes()}  # 000001.SZ 无行情(停牌/瞬时失败)
        with self._qpatch(quotes):
            trades = engine.execute_open("20260907")
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["ts_code"], "600000.SH")
        self.assertEqual(self._status("000001.SZ", "buy")[0], "pending")

    def test_open_no_quotes_defers_all_without_attempt(self):
        self._open_days(["20260907"])
        self._order_created("20260831", "600000.SH", "buy", 100, "2026-09-06 16:00:00")
        with self._qpatch(None):
            trades = engine.execute_open("20260907")
        self.assertEqual(trades, [])
        st = self._status("600000.SH", "buy")
        self.assertEqual(st[0], "pending")
        self.assertIsNone(st[1])  # 未计入尝试: 晚间链当日兜底, 过期时钟不被提前启动

    def test_open_dirty_price_deferred(self):
        # 今开相对昨收越界(±20%)视为脏数据 → 顺延
        self._open_days(["20260907"])
        self._order_created("20260831", "600000.SH", "buy", 100, "2026-09-06 16:00:00")
        with self._qpatch({"600000.SH": self._quotes(open=12.1)}):  # 12.1 > 9.9×1.2
            trades = engine.execute_open("20260907")
        self.assertEqual(trades, [])
        self.assertEqual(self._status("600000.SH", "buy")[0], "pending")

    def test_open_then_evening_idempotent(self):
        # 开盘档已成交 → 晚间链用日线开盘价重跑不重复成交
        self._open_days(["20260907"])
        self._order_created("20260831", "600000.SH", "buy", 100, "2026-09-06 16:00:00")
        with self._qpatch({"600000.SH": self._quotes()}):
            engine.execute_open("20260907")
        self._daily("600000.SH", "20260907", 10.0, 9.9)
        trades = engine.execute_pending("20260907")
        self.assertEqual(trades, [])
        self.assertEqual(self._one("SELECT COUNT(*) FROM paper_trade")[0], 1)


class TestDailySummary(_ExecBase):
    """日终汇总(2026-09-07 18:30 邮件档): 与面板同口径的当日台账快照"""

    def test_build_summary_with_position_and_trade(self):
        self._open_days(["20260907"])
        self._exec("UPDATE paper_account SET cash=89996.0 WHERE id=1")
        self._exec("INSERT INTO paper_position VALUES ('600000.SH','TEST',100,10.0,1.0)")
        self._daily("600000.SH", "20260907", 10.2, 10.0)
        self._exec("INSERT INTO adj_factor VALUES ('600000.SH','20260907',1.05)")
        self._exec(
            "INSERT INTO paper_trade (exec_date, ts_code, name, side, shares, price, cost, "
            "amount, reason) VALUES ('20260907','600000.SH','TEST','buy',100,10.01,3.0,1004.0,'t')")
        self._exec(
            "INSERT INTO paper_nav (trade_date, nav, cash, market_value, bench_nav) "
            "VALUES ('20260907',100004.0,89996.0,10000.0,5080.0)")
        s = engine.build_daily_summary("20260907")
        self.assertEqual(s["trade_date"], "20260907")
        self.assertEqual(s["cash"], 89996.0)
        self.assertEqual(s["interest"], 0.0)
        self.assertEqual(s["bench_nav"], 5080.0)
        self.assertEqual(len(s["positions"]), 1)
        p = s["positions"][0]
        # 总收益口径: tr=close×(adj/entry_adj)=10.2×1.05=10.71, pnl=100×(10.71-10.0)
        self.assertEqual(p["pnl"], 71.0)
        self.assertEqual(p["pnl_pct"], 7.1)
        self.assertEqual(len(s["trades"]), 1)
        self.assertEqual(s["trades"][0]["price"], 10.01)

    def test_no_nav_returns_empty(self):
        # 当日尚未记账(18:00 链未跑完): 返回空 dict, 调用方跳过发送
        self.assertEqual(engine.build_daily_summary("20260907"), {})


class TestNavBackfill(_ExecBase):
    """开盘档成交时当日复权因子未发布 → 晚间 update_nav 回填 entry_adj (2026-09-07)"""

    def test_backfills_entry_adj_for_open_slot_buys(self):
        self._open_days(["20260907"])
        self._exec("UPDATE paper_account SET cash=6686.89 WHERE id=1")
        self._exec("INSERT INTO paper_position VALUES ('600000.SH','TEST',100,10.0,NULL)")
        self._exec(
            "INSERT INTO paper_trade (exec_date, ts_code, name, side, shares, price, cost, "
            "amount, reason) VALUES ('20260907','600000.SH','TEST','buy',100,10.01,3.0,1004.0,'t')")
        self._daily("600000.SH", "20260907", 10.2, 10.0, close=10.2)
        self._exec("INSERT INTO adj_factor VALUES ('600000.SH','20260907',1.05)")
        self._exec("INSERT INTO index_daily VALUES ('000906.SH','20260907',5080.0)")
        engine.update_nav("20260907")
        ea = self._one("SELECT entry_adj FROM paper_position WHERE ts_code='600000.SH'")[0]
        self.assertEqual(ea, 1.05)
        # 当日净值即用总收益口径: 100×10.2×(1.05/1.05)=1020
        mv = self._one("SELECT market_value FROM paper_nav WHERE trade_date='20260907'")[0]
        self.assertEqual(mv, 1020.0)

    def test_no_adj_row_keeps_null(self):
        # 当日 adj_factor 缺失(数据未发布): 回填为无害空操作, 按未复权口径记账
        self._open_days(["20260907"])
        self._exec("INSERT INTO paper_position VALUES ('600000.SH','TEST',100,10.0,NULL)")
        self._exec(
            "INSERT INTO paper_trade (exec_date, ts_code, name, side, shares, price, cost, "
            "amount, reason) VALUES ('20260907','600000.SH','TEST','buy',100,10.01,3.0,1004.0,'t')")
        self._daily("600000.SH", "20260907", 10.2, 10.0, close=10.2)
        self._exec("INSERT INTO index_daily VALUES ('000906.SH','20260907',5080.0)")
        engine.update_nav("20260907")
        ea = self._one("SELECT entry_adj FROM paper_position WHERE ts_code='600000.SH'")[0]
        self.assertIsNone(ea)
        mv = self._one("SELECT market_value FROM paper_nav WHERE trade_date='20260907'")[0]
        self.assertEqual(mv, 1020.0)  # 未复权: 100×10.2


class TestApplyPlanSignalSoldSuppression(_ExecBase):
    """2026-09-30 修复: 方案期内被动态层卖出(止损/弱势替换/减仓)的股票, apply_plan
    不再按 delta 补买。

    生产案例(光韵达 300227.SZ): 08-31 方案目标 200 股 → 09-15 买入 → 09-28 止损信号
    (-15.5%) → 09-29 开盘卖出 → 09-29 晚 apply_plan 见"在方案、已持 0"重新挂
    "月度方案买入" → 09-30 又买回, 白付一轮双边成本且架空止损。动态层卖出是方案日
    之后更晚的风险决策; 口径与回测 R13-1 对齐(月内止损释放的资金不再入场)。
    边界: 上期(方案日之前)卖出、本期方案重新纳入是新的月度决策, 不得误伤。
    """

    def _plan(self, code="300227.SZ", shares=200):
        self._exec("INSERT INTO plan (plan_date, plan_type, ts_code, name, shares) "
                   "VALUES ('20260831','stock',?,'光韵达',?)", (code, shares))

    def _signal(self, date, code="300227.SZ", action="sell"):
        self._exec("INSERT INTO signal (signal_date, ts_code, name, action, reason) "
                   "VALUES (?,?, '光韵达', ?, '止损: 较买入成本 -15.5%')", (date, code, action))

    def test_stop_loss_sold_not_rebought(self):
        self._plan()
        self._signal("20260915")          # 方案期内止损, 仓位已清(持仓表为空)
        self.assertEqual(engine.apply_plan("20260831"), 0)
        self.assertEqual(self._one(
            "SELECT COUNT(*) FROM paper_order WHERE ts_code='300227.SZ'")[0], 0,
            "止损卖出后方案层不得再挂买入单")

    def test_residual_pending_plan_buy_cancelled(self):
        # 止损信号当晚, 在途的方案补挂买单(如部分成交补挂)必须撤掉 —— 否则次日
        # "信号卖单+方案买单"先后成交, 止损当天白吃一轮双边成本
        self._plan()
        self._signal("20260928")
        self._exec("INSERT INTO paper_order (order_date, ts_code, name, side, shares, reason, "
                   "status) VALUES ('20260831','300227.SZ','光韵达','buy',100,'月度方案买入','pending')")
        engine.apply_plan("20260831")
        self.assertEqual(self._one(
            "SELECT COUNT(*) FROM paper_order WHERE ts_code='300227.SZ' AND status='pending'")[0],
            0, "在途方案买单须随止损撤销")

    def test_sell_weak_and_reduce_also_suppressed(self):
        self._plan()
        self._signal("20260910", action="sell_weak")
        self.assertEqual(engine.apply_plan("20260831"), 0)
        self._exec("DELETE FROM signal")
        self._signal("20260910", action="reduce")
        self.assertEqual(engine.apply_plan("20260831"), 0)
        self.assertEqual(self._one(
            "SELECT COUNT(*) FROM paper_order WHERE ts_code='300227.SZ'")[0], 0)

    def test_prev_period_sell_still_buys(self):
        # 上期止损(方案日之前)、本期方案重新纳入 = 新月度决策, 照常买入
        self._plan()
        self._signal("20260720")
        engine.apply_plan("20260831")
        self.assertEqual(self._one(
            "SELECT side, shares FROM paper_order WHERE ts_code='300227.SZ'"), ("buy", 200))

    def test_held_signal_sold_stock_no_duplicate_plan_sell(self):
        # 止损信号已挂卖单但尚未成交(持仓还在): 该股仍在目标集 ⇒ 持仓循环不挂
        # "月度方案调出"(避免与信号卖单重复), 信号卖单原样保留
        self._plan()
        self._signal("20260928")
        self._exec("INSERT INTO paper_position (ts_code, name, shares, avg_cost, entry_adj) "
                   "VALUES ('300227.SZ','光韵达',200,11.932,1.0)")
        self._exec("INSERT INTO paper_order (order_date, ts_code, name, side, shares, reason, "
                   "status) VALUES ('20260928','300227.SZ','光韵达','sell',200,"
                   "'止损: 较买入成本 -15.5%','pending')")
        engine.apply_plan("20260831")
        self.assertEqual(self._one(
            "SELECT COUNT(*) FROM paper_order WHERE ts_code='300227.SZ' "
            "AND reason LIKE '月度方案%'")[0], 0, "信号卖单在途时方案层不得叠加方案单")
        self.assertEqual(self._one(
            "SELECT COUNT(*) FROM paper_order WHERE ts_code='300227.SZ' AND status='pending'")[0],
            1, "信号卖单保留")


class TestApplyPlanDynamicBoughtExemption(_ExecBase):
    """2026-09-30 修复: 本方案期内经动态层买入(候选替换/加仓)的仓位, 方案层不调出。

    churn 的反向变体: 动态层候选替换买入的股票几乎必然不在月度方案目标集里
    (方案=top6, 候选=top20 减持仓), apply_plan 的持仓循环在买入成交次日就会给它挂
    「月度方案调出」⇒ 替换买入活不过一天, 替换机制形同虚设且白付双边成本。
    豁免后这些仓位由动态层自己的风控(止损/弱势)管理, 下次月度方案重建时不在
    新目标集 ⇒ 正常调出(月度收敛)。识别口径=本期买入成交 reason 带
    「(自动替换买入)」后缀(engine 模块单一常量)。
    """

    def _plan(self):
        self._exec("INSERT INTO plan (plan_date, plan_type, ts_code, name, shares) "
                   "VALUES ('20260831','stock','000001.SZ','新目标',200)")

    def _held(self, code="600000.SH"):
        self._exec("INSERT INTO paper_position (ts_code, name, shares, avg_cost, entry_adj) "
                   "VALUES (?, 'TEST', 100, 10.0, 1.0)", (code,))

    def _buy_trade(self, code, exec_date, reason):
        self._exec("INSERT INTO paper_trade (exec_date, ts_code, side, shares, price, cost, "
                   "amount, reason) VALUES (?,?,'buy',100,10.0,0,0,?)", (exec_date, code, reason))

    def test_replacement_bought_this_period_not_evicted(self):
        self._plan()
        self._held()
        self._buy_trade("600000.SH", "20260910", "新进前20: 分位 5.0 (自动替换买入)")
        engine.apply_plan("20260831")
        sells = self._one("SELECT COUNT(*) FROM paper_order WHERE ts_code='600000.SH' "
                          "AND side='sell'")[0]
        self.assertEqual(sells, 0, "动态层本期买入的仓位不得被方案层调出")

    def test_prev_period_replacement_still_evicted(self):
        # 上期的替换买入: 本期方案重建后不在目标集 ⇒ 正常调出(月度收敛语义)
        self._plan()
        self._held()
        self._buy_trade("600000.SH", "20260720", "新进前20: 分位 5.0 (自动替换买入)")
        engine.apply_plan("20260831")
        sells = self._one("SELECT COUNT(*) FROM paper_order WHERE ts_code='600000.SH' "
                          "AND side='sell'")[0]
        self.assertEqual(sells, 1, "上期动态买入不豁免, 月度重建照常收敛")

    def test_plain_position_still_evicted(self):
        # 无替换买入记录的普通持仓不在目标集: 既有调出行为不变(对照臂)
        self._plan()
        self._held()
        self._buy_trade("600000.SH", "20260910", "月度方案买入")
        engine.apply_plan("20260831")
        sells = self._one("SELECT COUNT(*) FROM paper_order WHERE ts_code='600000.SH' "
                          "AND side='sell' AND reason='月度方案调出'")[0]
        self.assertEqual(sells, 1)


if __name__ == "__main__":
    unittest.main()
