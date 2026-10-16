"""两个"日期闸"的回归测试（2026-09-14）

它们决定**日链/月链拉哪一天的数据**，判错会把错误永久写进库（不是显示问题）：

  · `_safe_end()`：**17 点之前只到上一收盘日** —— 盘中/盘前触发（misfire、手动、
    服务重启补跑）若把"今天的半天线"拉进库，日线就不完整，而它会**永久留在** `daily` 里，
    标签/因子/回测全被污染（2026-09-04 审查发现并加的闸）。
  · `_last_month_end(conn)`：返回**上一个已完结自然月**的月末交易日 —— 2026-09-05 修复：
    原实现返回**当月**月末（如 8/31 当天返回 8/31），与 `month_end_dates(today=)` 排除当月的
    口径矛盾 ⇒ 月末最后交易日因子门控必失败、每次开机空跑整条月链后中止。

运行: python -m pytest tests/test_scheduler_date_guards.py -q
"""
import datetime as dt
import sqlite3
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import scheduler                              # noqa: E402

DDL = "CREATE TABLE trade_cal (cal_date TEXT, is_open INTEGER)"


def _conn(rows) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute(DDL)
    c.executemany("INSERT INTO trade_cal (cal_date, is_open) VALUES (?,?)", rows)
    c.commit()
    return c


class TestLastMonthEnd(unittest.TestCase):
    def test_returns_last_open_day_of_the_previous_month(self):
        today = dt.datetime.now(scheduler._TZ)
        this_month_first = today.strftime("%Y%m") + "01"
        prev = (today.replace(day=1) - dt.timedelta(days=1))
        prev_ym = prev.strftime("%Y%m")
        rows = [(f"{prev_ym}27", 1), (f"{prev_ym}28", 1), (f"{prev_ym}29", 0),
                (f"{prev_ym}30", 1), (this_month_first, 1)]
        c = _conn(rows)
        try:
            got = scheduler._last_month_end(c)
        finally:
            c.close()
        self.assertEqual(got, f"{prev_ym}30",
                         "必须返回**已完结**自然月的最后一个交易日（不能含当月、也不能含休市日）")

    def test_empty_calendar_returns_none(self):
        c = _conn([])
        try:
            self.assertIsNone(scheduler._last_month_end(c))
        finally:
            c.close()


class TestSafeEnd(unittest.TestCase):
    def _patch_db(self, rows):
        c = _conn(rows)
        self.addCleanup(c.close)

        @contextmanager
        def _ctx():
            yield c
        return mock.patch.object(scheduler.db, "conn_ctx", _ctx)

    def test_before_17h_returns_previous_closed_day(self):
        """盘中/盘前 ⇒ 绝不返回今天（否则不完整的当日日线会永久入库）。

        2026-09-14 修（既有测试缺陷，非当日改动引入）：原先用**真实时钟**算期望日，
        于是这条用例只在 17:00 之前跑才通过 —— 18:4x 跑全量时 `_safe_end()` 正确地返回了
        今天，测试却断言"上一收盘日"，变成**假红**（全量套件每天傍晚必红，绿信号被稀释）。
        现在与下面那条 17 点后的用例同一套写法：时钟**注入**，任何时刻跑都同理。
        """
        class _FakeDatetimeModule:
            class datetime:
                @staticmethod
                def now(tz=None):
                    return dt.datetime(2026, 9, 14, 10, 0)      # 周一上午 10 点（盘中）
        with mock.patch.object(scheduler, "datetime", _FakeDatetimeModule), \
                self._patch_db([("20260911", 1), ("20260914", 1)]):
            got = scheduler._safe_end()
        self.assertEqual(got, "20260911", "17 点前必须只到上一收盘日")

    def test_empty_calendar_falls_back_to_previous_day(self):
        """日历查不到上一个开市日时退化为"今天"（不能返回 None，调用方要的是日期串）。"""
        with self._patch_db([]):
            got = scheduler._safe_end()
        today = dt.datetime.now(scheduler._TZ).strftime("%Y%m%d")
        self.assertEqual(got, today)

    def test_after_17h_returns_today_without_touching_the_db(self):
        """17 点后（已收盘）⇒ 直接返回今天，且 **不查库**（该分支用注入的假时钟覆盖）。"""
        class _FakeDatetimeModule:
            class datetime:
                @staticmethod
                def now(tz=None):
                    return dt.datetime(2026, 9, 14, 19, 0)
        def _boom():
            raise AssertionError("17 点后不该查库")
        with mock.patch.object(scheduler, "datetime", _FakeDatetimeModule), \
                mock.patch.object(scheduler.db, "conn_ctx", _boom):
            self.assertEqual(scheduler._safe_end(), "20260914")


class TestDailyBriefSlot(unittest.TestCase):
    """「今日待办」档（08:30，2026-09-18 邮件优化新增）的自门控。

    这是本轮**唯一新增的调度档**，也是最容易发错的一档：它跑在开盘前，
    周末/长假早上如果照发，收到的是一封"今天要干的活其实不存在"的空信。
    判据与 18:30 日终档同源（`trade_cal.is_open`），不另写一套 weekday 判断。
    """

    def _conn(self, is_open):
        """`daily_brief_slot` 用的是 `conn = db.get_conn()` + try/finally（不是 conn_ctx），
        所以这里直接返回连接替身本身。"""
        conn = mock.MagicMock()
        conn.execute.return_value.fetchone.return_value = is_open
        return conn

    def test_non_trading_day_sends_nothing(self):
        for is_open in (None, (0,), ()):          # 日历缺行 / 明确不开市
            with self.subTest(is_open=is_open):
                with mock.patch.object(scheduler.db, "get_conn", return_value=self._conn(is_open)), \
                        mock.patch("app.notify.send_daily_brief_email",
                                   return_value=False) as p_send:
                    out = scheduler.daily_brief_slot()
                self.assertFalse(out["sent"])
                self.assertIn("非交易日", out.get("skipped", ""))
                p_send.assert_not_called()

    def test_trading_day_sends_once(self):
        with mock.patch.object(scheduler.db, "get_conn", return_value=self._conn((1,))), \
                mock.patch("app.notify.send_daily_brief_email", return_value=True) as p_send:
            out = scheduler.daily_brief_slot()
        self.assertTrue(out["sent"])
        p_send.assert_called_once()

    def test_send_failure_is_swallowed(self):
        """发信异常不许把调度档炸成 error —— 这档在 `_retryable` 里，炸了会触发重试+告警，
        而"今天没有待办"从来不是需要重试的故障。"""
        with mock.patch.object(scheduler.db, "get_conn", return_value=self._conn((1,))), \
                mock.patch("app.notify.send_daily_brief_email",
                           side_effect=RuntimeError("smtp down")):
            out = scheduler.daily_brief_slot()
        self.assertFalse(out["sent"])


class TestDailySummarySlotGates(unittest.TestCase):
    """18:30 日终总结档的两道门（2026-09-18 补守卫，此前**完全没有测试**）。

    ① 非交易日不发；② 当日还没记账（18:00 链没跑完/失败）不发，也**不补发旧日** ——
    成交邮件当时已即时发过，汇总只是当日快照，补发旧日会让人以为账是今天记的。
    """

    def _conn(self, is_open):
        conn = mock.MagicMock()
        conn.execute.return_value.fetchone.return_value = is_open
        return conn

    def test_non_trading_day_sends_nothing(self):
        with mock.patch.object(scheduler.db, "get_conn", return_value=self._conn((0,))), \
                mock.patch("app.notify.send_daily_summary_email") as p_send:
            out = scheduler.daily_summary_slot()
        self.assertFalse(out["sent"])
        p_send.assert_not_called()

    def test_unaccounted_day_skips_without_sending(self):
        """`build_daily_summary` 返回空 dict ⇒ 今天还没记账 ⇒ 跳过（这是 18:00 链失败的信号）。"""
        with mock.patch.object(scheduler.db, "get_conn", return_value=self._conn((1,))), \
                mock.patch("app.paper.engine.build_daily_summary", return_value={}), \
                mock.patch("app.notify.send_daily_summary_email") as p_send:
            out = scheduler.daily_summary_slot()
        self.assertFalse(out["sent"])
        p_send.assert_not_called()

    def test_accounted_day_sends_with_that_day_only(self):
        s = {"trade_date": "20260918", "nav": 1.0}
        with mock.patch.object(scheduler.db, "get_conn", return_value=self._conn((1,))), \
                mock.patch("app.paper.engine.build_daily_summary",
                           return_value=s) as p_build, \
                mock.patch("app.notify.send_daily_summary_email",
                           return_value=True) as p_send:
            out = scheduler.daily_summary_slot()
        self.assertTrue(out["sent"])
        # 取数必须按"今天"这个日期去要，不能让它默认取库里最新一天
        p_build.assert_called_once_with(out["trade_date"])
        p_send.assert_called_once_with(s)


if __name__ == "__main__":
    unittest.main()
