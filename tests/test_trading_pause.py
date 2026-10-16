"""交易总闸(paper.paused)回归测试 —— 2026-09-09 用户拍板「暂停实盘, 先修」

语义: 暂停期不按方案/信号挂单、不成交, 存量挂单作废(恢复后按最新方案自动重建);
数据链/训练/回测/监控/净值记账照常 —— 停的是「交易」, 不是「观测」。

运行: python -m unittest tests.test_trading_pause -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.paper import engine

_SCHEMA = """
CREATE TABLE paper_account (id INTEGER PRIMARY KEY, cash REAL, created_at TEXT,
                            last_applied_plan TEXT, initial_capital REAL);
CREATE TABLE paper_order (id INTEGER PRIMARY KEY AUTOINCREMENT, order_date TEXT,
                          ts_code TEXT, name TEXT, side TEXT, shares INTEGER,
                          reason TEXT, status TEXT DEFAULT 'pending', first_attempt TEXT,
                          created_at TEXT);
CREATE TABLE paper_position (ts_code TEXT PRIMARY KEY, name TEXT, shares INTEGER,
                             avg_cost REAL, entry_adj REAL);
CREATE TABLE paper_trade (id INTEGER PRIMARY KEY AUTOINCREMENT, exec_date TEXT,
                          ts_code TEXT, name TEXT, side TEXT, shares INTEGER,
                          price REAL, cost REAL, amount REAL, reason TEXT);
CREATE TABLE paper_nav (trade_date TEXT PRIMARY KEY, nav REAL, cash REAL,
                        market_value REAL, bench_nav REAL);
CREATE TABLE paper_interest (trade_date TEXT PRIMARY KEY, amount REAL);
CREATE TABLE repo_rate (trade_date TEXT PRIMARY KEY, overnight REAL, r7d REAL, r14d REAL);
CREATE TABLE trade_cal (cal_date TEXT PRIMARY KEY, is_open INTEGER);
CREATE TABLE daily (ts_code TEXT, trade_date TEXT, open REAL, pre_close REAL, vol REAL, close REAL);
CREATE TABLE adj_factor (ts_code TEXT, trade_date TEXT, adj_factor REAL);
CREATE TABLE index_daily (ts_code TEXT, trade_date TEXT, close REAL);
CREATE TABLE plan (plan_date TEXT, plan_type TEXT, ts_code TEXT, name TEXT,
                   weight REAL, shares INTEGER, industry TEXT, reason TEXT);
CREATE TABLE signal (signal_date TEXT, ts_code TEXT, name TEXT, action TEXT,
                     reason TEXT, score REAL);
"""

_D = "20260914"


class _PauseBase(unittest.TestCase):
    PAUSED = True

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(_SCHEMA)
        conn.execute("INSERT INTO paper_account (id, cash, initial_capital) VALUES (1, 100000.0, 100000.0)")
        conn.executemany("INSERT INTO trade_cal VALUES (?,1)", [("20260911",), (_D,)])
        # 行情: 600000 开盘 10 元
        conn.execute("INSERT INTO daily VALUES ('600000.SH',?,10.0,9.9,10000,10.5)", (_D,))
        conn.execute("INSERT INTO daily VALUES ('600000.SH','20260911',10.0,9.9,10000,10.0)")
        conn.execute("INSERT INTO adj_factor VALUES ('600000.SH',?,1.0)", (_D,))
        conn.execute("INSERT INTO adj_factor VALUES ('600000.SH','20260911',1.0)")
        conn.execute("INSERT INTO index_daily VALUES ('000906.SH',?,4000.0)", (_D,))
        # 最新方案: 目标持有 200 股(空仓时 unpaused 会补挂 200 股买单)
        conn.execute("INSERT INTO plan VALUES ('20260901','stock','600000.SH','TEST',0.5,200,'银行','')")
        # 暂停前已挂出的买单
        conn.execute("INSERT INTO paper_order (order_date, ts_code, name, side, shares, reason, "
                     "status, created_at) VALUES ('20260910','600000.SH','TEST','buy',100,'月度方案买入',"
                     "'pending','2026-09-10 18:00:00')")
        conn.commit()
        conn.close()
        self.patch_conn = mock.patch.object(engine.db, "get_conn",
                                            side_effect=lambda: sqlite3.connect(self.path))
        self.patch_cfg = mock.patch.object(
            engine.cfg_mod, "load_config",
            return_value={"single_side_cost": 0.003, "capital": 10000,
                          "paper": {"slip": 0.001, "paused": self.PAUSED}})
        self.patch_conn.start()
        self.patch_cfg.start()

    def tearDown(self):
        self.patch_conn.stop()
        self.patch_cfg.stop()
        os.unlink(self.path)

    def _one(self, sql, args=()):
        conn = sqlite3.connect(self.path)
        row = conn.execute(sql, args).fetchone()
        conn.close()
        return row

    def _all(self, sql, args=()):
        conn = sqlite3.connect(self.path)
        rows = conn.execute(sql, args).fetchall()
        conn.close()
        return rows


class TestPaused(_PauseBase):
    PAUSED = True

    def test_run_daily_no_trade_expires_pending_keeps_nav(self):
        r = engine.run_daily(_D)
        self.assertTrue(r["paused"])
        self.assertEqual(r["trades"], [])
        self.assertEqual(r["expired_orders"], 1)
        # 存量挂单作废(不再 pending), 且未成交
        s, reason = self._one(
            "SELECT status, reason FROM paper_order WHERE order_date='20260910'")
        self.assertEqual(s, "expired")
        self.assertTrue(reason.endswith("（暂停交易作废）"), reason)
        self.assertEqual(self._all("SELECT * FROM paper_trade"), [])
        # 不按方案挂新单
        self.assertEqual(self._one("SELECT COUNT(*) FROM paper_order")[0], 1)
        # 净值照记(无成交, 市值 0)
        nav = self._one("SELECT nav, cash, market_value FROM paper_nav WHERE trade_date=?", (_D,))
        self.assertIsNotNone(nav)
        self.assertEqual(nav[1], 100000.0)
        self.assertEqual(nav[2], 0.0)

    def test_expire_is_idempotent(self):
        engine.run_daily(_D)
        r2 = engine.run_daily(_D)
        self.assertEqual(r2["expired_orders"], 0)
        reason = self._one("SELECT reason FROM paper_order WHERE order_date='20260910'")[0]
        self.assertEqual(reason.count("（暂停交易作废）"), 1)

    def test_direct_entrypoints_blocked(self):
        # 暂停期任何路径都不得成交/挂单(脚本/网页直调也拦住)
        self.assertEqual(engine.execute_pending(_D), [])
        self.assertEqual(engine.execute_open(_D), [])
        self.assertEqual(engine.apply_plan("20260901"), 0)
        self.assertEqual(engine.apply_signals(_D), 0)
        self.assertEqual(self._one("SELECT status FROM paper_order WHERE order_date='20260910'")[0],
                         "pending")  # 未被执行, 也未被这三条路径改动
        self.assertEqual(self._all("SELECT * FROM paper_trade"), [])


class TestUnpausedControl(_PauseBase):
    """对照组: 同一场景不暂停 → 挂单成交 + 方案补挂差额。证明差异来自交易闸本身。"""

    PAUSED = False

    def test_run_daily_trades_and_replans(self):
        r = engine.run_daily(_D)
        self.assertFalse(r.get("paused"))
        self.assertEqual(len(r["trades"]), 1)
        self.assertEqual(self._one("SELECT status FROM paper_order WHERE order_date='20260910'")[0],
                         "done")
        self.assertEqual(self._one("SELECT shares FROM paper_position WHERE ts_code='600000.SH'")[0],
                         100)
        # 方案目标 200 股, 已持 100 → 补挂 100 股买单(恢复后自动重建的语义)
        pend = self._all("SELECT shares, reason FROM paper_order WHERE status='pending'")
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0][0], 100)
        self.assertTrue(pend[0][1].startswith("月度方案买入"))


class TestPauseDefault(unittest.TestCase):
    def test_missing_key_means_unpaused(self):
        # 既有单测/临时配置无 paper.paused 键时不得被误判为暂停
        with mock.patch.object(engine.cfg_mod, "load_config",
                               return_value={"paper": {"slip": 0.001}}):
            self.assertFalse(engine.trading_paused())

    def test_true_means_paused(self):
        with mock.patch.object(engine.cfg_mod, "load_config",
                               return_value={"paper": {"paused": True}}):
            self.assertTrue(engine.trading_paused())


if __name__ == "__main__":
    unittest.main()
