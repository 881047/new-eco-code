"""数据更新修复单测: 指数按代码补缺口 / 盘后两日重拉窗 (2026-09-04 审查修复)

运行: python -m unittest tests.test_updater_fix -v
"""
import datetime
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from app.datasource import updater

_SCHEMA = """
CREATE TABLE trade_cal (cal_date TEXT PRIMARY KEY, is_open INTEGER);
CREATE TABLE daily (ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
                    close REAL, pre_close REAL, change REAL, pct_chg REAL,
                    vol REAL, amount REAL);
CREATE TABLE adj_factor (ts_code TEXT, trade_date TEXT, adj_factor REAL);
CREATE TABLE index_daily (ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
                          close REAL, pre_close REAL, pct_chg REAL, vol REAL, amount REAL);
CREATE TABLE data_status (name TEXT PRIMARY KEY, last_date TEXT, updated_at TEXT, rows INTEGER);
CREATE TABLE stock_basic (ts_code TEXT PRIMARY KEY);
CREATE TABLE fina_indicator (ts_code TEXT, ann_date TEXT, end_date TEXT, roe REAL,
    netprofit_yoy REAL, or_yoy REAL, grossprofit_margin REAL, debt_to_assets REAL, eps REAL);
CREATE TABLE namechange (ts_code TEXT, name TEXT, start_date TEXT, end_date TEXT,
    ann_date TEXT, change_reason TEXT, PRIMARY KEY (ts_code, start_date));
"""


class _UpdaterBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()
        # DataUpdater 不走 __init__(避免真实 TushareClient), 手工注入 ts
        self.up = updater.DataUpdater.__new__(updater.DataUpdater)
        self.ts = mock.Mock()
        self.up.ts = self.ts
        self.up._uni = None
        self.patch_db = mock.patch.object(
            updater.db, "get_conn", side_effect=lambda: sqlite3.connect(self.path))
        self.patch_db.start()

    def tearDown(self):
        self.patch_db.stop()
        os.unlink(self.path)

    def _c(self):
        return sqlite3.connect(self.path)

    def _exec(self, sql, args=()):
        conn = self._c()
        conn.execute(sql, args)
        conn.commit()
        conn.close()

    def _one(self, sql, args=()):
        conn = self._c()
        row = conn.execute(sql, args).fetchone()
        conn.close()
        return row


class _FakeNow(datetime.datetime):
    """固定 2026-09-04 18:00 (盘后), 用于强制重拉窗测试"""
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 4, 18, 0, 0)


class _FakeNoon(datetime.datetime):
    """固定 2026-09-04 14:00 (盘中), 不触发盘后强制重拉"""
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 4, 14, 0, 0)


class TestIndexPerCodeMissing(_UpdaterBase):
    def test_gap_only_pulled_for_missing_code(self):
        # 除 000300.SH 外所有指数已有 D2; 修复前 have 按日期并集会把 000300 的缺口漏掉
        self._exec("INSERT INTO trade_cal VALUES ('20260903',1)")
        self._exec("INSERT INTO trade_cal VALUES ('20260904',1)")
        for code in updater.INDEX_CODES:
            self._exec("INSERT INTO index_daily (ts_code, trade_date, close) VALUES (?,?,1.0)",
                       (code, "20260903"))
            if code != "000300.SH":
                self._exec("INSERT INTO index_daily (ts_code, trade_date, close) VALUES (?,?,1.0)",
                           (code, "20260904"))

        def _fake_call(api_name, **kwargs):
            d = kwargs.get("trade_date")
            return pd.DataFrame({"ts_code": kwargs["ts_code"], "trade_date": [d],
                                 "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
                                 "pre_close": [1.0], "pct_chg": [0.0], "vol": [1.0], "amount": [1.0]})
        self.ts.call.side_effect = _fake_call
        with mock.patch.object(updater.datetime, "datetime", _FakeNoon):
            self.up.init_index(end_date="20260904")
        called = {c.kwargs["ts_code"] for c in self.ts.call.call_args_list}
        self.assertIn("000300.SH", called)      # 缺 D2 的代码被补拉
        self.assertNotIn("000906.SH", called)   # 已有 D2 的代码不再拉(盘中, 无强制重拉)

    def test_failed_day_retried_next_run(self):
        # 某指数某日瞬时失败: 下次运行仍会尝试(缺口按 (code,date) 跟踪)。
        # 2026-09-05 第七轮 N7-1: 盘后强制重拉窗改为"日历末尾两个真实交易日"
        # (与 sync_daily_bulk 同规则) —— 18 点运行每指数拉 0903+0904 两天
        self._exec("INSERT INTO trade_cal VALUES ('20260903',1)")
        self._exec("INSERT INTO trade_cal VALUES ('20260904',1)")
        for code in updater.INDEX_CODES:
            self._exec("INSERT INTO index_daily (ts_code, trade_date, close) VALUES (?,?,1.0)",
                       (code, "20260903"))
        self.ts.call.side_effect = Exception("瞬时失败")
        with mock.patch.object(updater.datetime, "datetime", _FakeNow):
            self.up.init_index(end_date="20260904")
        first_calls = {c.kwargs["ts_code"] for c in self.ts.call.call_args_list}
        self.assertIn("000300.SH", first_calls)
        # 第二次运行(仍无 0904 行): 依然会再次尝试拉取(缺口日+强制重拉窗)
        self.ts.call.reset_mock()
        self.ts.call.side_effect = Exception("瞬时失败")
        with mock.patch.object(updater.datetime, "datetime", _FakeNow):
            self.up.init_index(end_date="20260904")
        self.assertEqual(len(self.ts.call.call_args_list), 2 * len(updater.INDEX_CODES))


class TestForceRefetchWindow(_UpdaterBase):
    def test_after_17_pulls_today_and_prev_day(self):
        # 17点后强制重拉今天+前一交易日: 17:23 上游未发布完的当日残缺批次日自愈
        self._exec("INSERT INTO trade_cal VALUES ('20260901',1)")
        self._exec("INSERT INTO trade_cal VALUES ('20260902',1)")
        self._exec("INSERT INTO trade_cal VALUES ('20260903',1)")
        self._exec("INSERT INTO trade_cal VALUES ('20260904',1)")
        # 已有 0901-0903 的行情, 缺 0904
        for d in ("20260901", "20260902", "20260903"):
            self._exec("INSERT INTO daily (ts_code, trade_date, close) VALUES ('600000.SH',?,10.0)", (d,))

        def _fake_call(api_name, **kwargs):
            d = kwargs.get("trade_date")
            return pd.DataFrame({"ts_code": ["600000.SH"], "trade_date": [d],
                                 "open": [10.0], "high": [10.0], "low": [10.0], "close": [10.0],
                                 "pre_close": [10.0], "change": [0.0], "pct_chg": [0.0],
                                 "vol": [1.0], "amount": [1.0]})
        self.ts.call.side_effect = _fake_call
        with mock.patch.object(updater.datetime, "datetime", _FakeNow):
            self.up.sync_daily_bulk("daily", "daily", cols=updater.STOCK_DAILY_COLS,
                                    end_date="20260904")
        pulled = [c.kwargs.get("trade_date") for c in self.ts.call.call_args_list]
        # DATA-04①(2026-09-16): 强制重拉窗 2→4 个交易日(覆盖「残缺半批+停跑一日」滞留)
        self.assertEqual(pulled, ["20260901", "20260902", "20260903", "20260904"])


    def test_force_only_when_end_covers_today(self):
        # 18 点但 end_date 停在 0903(盘中补跑传前一交易日): 不强制重拉今天
        self._exec("INSERT INTO trade_cal VALUES ('20260903',1)")
        for d in ("20260903",):
            self._exec("INSERT INTO daily (ts_code, trade_date, close) VALUES ('600000.SH',?,10.0)", (d,))
        with mock.patch.object(updater.datetime, "datetime", _FakeNow):
            self.up.sync_daily_bulk("daily", "daily", cols=updater.STOCK_DAILY_COLS,
                                    end_date="20260903")
        self.assertEqual(self.ts.call.call_args_list, [])  # 无缺失日 → 零外呼


class TestPerStockCallException(_UpdaterBase):
    """2026-09-07 全面检查: call() 限流重试耗尽 raise RuntimeError, 逐只拉取循环
    必须按失败计数继续而非炸断整条每日链(compute_regime 同型问题已单独兜底)"""

    def setUp(self):
        super().setUp()
        self._exec("INSERT INTO stock_basic (ts_code) VALUES ('600000.SH')")
        self._exec("INSERT INTO stock_basic (ts_code) VALUES ('000001.SZ')")

    def test_init_fina_runtime_error_not_propagated(self):
        self.ts.available.return_value = True
        self.ts.call.side_effect = RuntimeError("fina_indicator 重试耗尽")
        try:
            self.up.init_fina(recent_only=False)
        except Exception as e:  # pragma: no cover
            self.fail(f"init_fina 不应抛出异常: {e}")
        # 全部按失败计数(2 只 < 20 阈值), 水位写入 0 条
        self.assertEqual(
            self._one("SELECT rows FROM data_status WHERE name='fina_indicator'")[0], 0)

    def test_init_fina_probe_exception_proceeds_with_tushare(self):
        # 探测异常(瞬时)按"可用"继续, 不误触发 40 分钟 AkShare 兜底
        self.ts.available.side_effect = RuntimeError("限流重试耗尽")
        self.ts.call.return_value = pd.DataFrame()
        self.ts.last_status = "empty"
        self.up.init_fina(recent_only=False)
        # 走了 tushare 逐只路径(call 被调用), 而非 fallback
        self.assertTrue(self.ts.call.called)

    def test_init_fina_ann_before_end_clamped(self):
        # 上游偶发 ann_date 早于报告期末(前视污染): 按法定披露截止日保守钳制
        self.ts.available.return_value = True
        df = pd.DataFrame({
            "ts_code": ["600000.SH"],
            "ann_date": ["20260422"],  # 上游错误: 半年报写成 4 月披露日
            "end_date": ["20260630"],
            "roe": [3.69], "netprofit_yoy": [-46.8], "or_yoy": [1.0],
            "grossprofit_margin": [30.0], "debt_to_assets": [50.0], "eps": [0.3]})
        self.ts.call.return_value = df
        self.ts.last_status = "ok"
        self.up.init_fina(recent_only=False)
        self.assertEqual(
            self._one("SELECT ann_date FROM fina_indicator WHERE ts_code='600000.SH'")[0],
            "20260831")

    def test_init_namechange_runtime_error_not_propagated(self):
        self.ts.call.side_effect = RuntimeError("namechange 重试耗尽")
        try:
            n = self.up.init_namechange(recent_only=False)
        except Exception as e:  # pragma: no cover
            self.fail(f"init_namechange 不应抛出异常: {e}")
        self.assertEqual(n, 0)


class TestFinaResumedStockFloor(_UpdaterBase):
    """2026-09-09 审查项 ⑥: 500 日地板外的「仍在交易」复牌股必须纳入财务增量

    原口径只看最新报告期: 长期停牌后复牌/恢复披露的股票, 最新报告期落后 >500 日 →
    被永久排除在增量集合外 → 新报告永不被拉取(自锁)。
    """

    def setUp(self):
        super().setUp()
        for c in ("A.SZ", "B.SZ", "C.SZ", "D.SZ", "E.SZ"):
            self._exec("INSERT INTO stock_basic (ts_code) VALUES (?)", (c,))
        # 报告期水位 latest_ok=20260630 → 地板 ≈ 20250215
        for c, ed in (("A.SZ", "20260630"),    # 已最新, 不该拉
                      ("B.SZ", "20251231"),    # 地板内落后 → 照旧拉
                      ("C.SZ", "20240331"),    # 地板外, 但近期仍在交易(复牌股) → 修复后拉
                      ("D.SZ", "20240331")):   # 地板外且已无成交(退市) → 仍不拉
            self._exec("INSERT INTO fina_indicator (ts_code, ann_date, end_date) VALUES (?,?,?)",
                       (c, ed, ed))
        # E.SZ 库内无记录(次新) → 照旧拉
        for d in ("20260831", "20260901", "20260902"):
            self._exec("INSERT INTO daily (ts_code, trade_date, close) VALUES ('C.SZ',?,10.0)",
                       (d,))
        self.ts.available.return_value = True
        self.ts.last_status = "empty"
        self.ts.call.return_value = pd.DataFrame()

    def test_resumed_stock_pulled_delisted_still_skipped(self):
        self.up.init_fina(recent_only=True)
        called = {c.kwargs["ts_code"] for c in self.ts.call.call_args_list}
        self.assertIn("B.SZ", called)          # 地板内落后: 原口径即拉
        self.assertIn("C.SZ", called)          # 地板外但仍在交易: 修复新增
        self.assertIn("E.SZ", called)          # 库内无记录
        self.assertNotIn("D.SZ", called)       # 地板外且无成交: 保持排除(省配额)
        self.assertNotIn("A.SZ", called)       # 已是最新报告期

    def test_no_daily_rows_degrades_to_old_scope(self):
        # daily 无数据(新装机/初始化早期): 活跃性判据不可用 → 退回原口径, 不误纳入
        self._exec("DELETE FROM daily")
        self.up.init_fina(recent_only=True)
        called = {c.kwargs["ts_code"] for c in self.ts.call.call_args_list}
        self.assertNotIn("C.SZ", called)
        self.assertIn("B.SZ", called)


if __name__ == "__main__":
    unittest.main()


class TestTradeCalRefreshWindow(_UpdaterBase):
    """trade_cal 保鲜窗（P2-9，2026-10-01 收紧 30→7 天）：临时休市/节假日修订的
    最坏传播延迟 = 该窗。旧 30 天窗下"8 天前刷新"会跳过 ⇒ 收紧后必须重拉（先红后绿）。"""

    def _seed(self, updated_at: str):
        self._exec("INSERT INTO trade_cal VALUES ('20261231', 1)")   # 覆盖未来
        self._exec("INSERT INTO data_status VALUES ('trade_cal', '20261231', ?, 1)",
                   (updated_at,))

    def _run(self):
        now = datetime.datetime(2026, 10, 1, 14, 0, 0)
        with mock.patch.object(updater.timeutil, "today",
                               return_value=datetime.date(2026, 10, 1)), \
             mock.patch.object(updater.timeutil, "now", return_value=now):
            return self.up.init_trade_cal()

    def test_within_window_skips_repull(self):
        self._seed("2026-09-29 10:00:00")            # 2 天前刷新 < 7
        self.assertEqual(self._run(), 0)
        self.ts.call.assert_not_called()

    def test_after_window_repulls_even_with_future_coverage(self):
        self._seed("2026-09-23 10:00:00")            # 8 天前 ≥ 7 ⇒ 旧窗(30)会跳过=红
        self.ts.call.return_value = pd.DataFrame({
            "cal_date": ["20261230", "20261231", "20270101"],
            "is_open": [1, 1, 0]})
        n = self._run()
        self.ts.call.assert_called_once()
        self.assertGreater(n, 0)
        self.assertEqual(
            self._one("SELECT last_date FROM data_status WHERE name='trade_cal'")[0],
            "20270101")

    def test_rate_limit_failure_skips_cleanly(self):
        self._seed("2026-09-23 10:00:00")
        self.ts.call.side_effect = RuntimeError("限流重试耗尽")
        self.assertEqual(self._run(), 0)             # 09-08 P1 语义: 不炸断日链
