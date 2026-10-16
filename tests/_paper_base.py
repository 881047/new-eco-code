"""执行引擎单测的**共享基座**(2026-09-12 审查: 测试质量项)

`tests/test_paper_exec.py` 与 `tests/test_fees.py` 原本后者 `from tests.test_paper_exec
import _ExecBase` —— 测试之间互相 import **私有**基类是脆的: 前者改名/拆分/挪目录,
后者会在导入期就崩, 而崩的地方(collection)与被测行为毫无关系。

挪到这里后两边都从本模块取。**文件名以 `_` 开头是故意的**: `unittest discover` 的默认
pattern 是 `test*.py`, 所以本模块不会被当成一个测试文件收进来(它也确实没有自己的用例)。

内容: `_SCHEMA`(纸条库最小表结构) + `_ExecBase`(临时文件库 + 桩 config)。桩里把费率
显式钉成**旧扁平 0.3%** —— `test_paper_exec` 的现金算术断言都按此口径写死; 生产口径的
逐笔真实费率由 `tests/test_fees.py` 自己的基座覆盖。
"""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

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
CREATE TABLE signal (signal_date TEXT, ts_code TEXT, name TEXT, action TEXT, reason TEXT,
                     PRIMARY KEY (signal_date, ts_code, action));
"""


class _ExecBase(unittest.TestCase):
    """纸面撮合的夹具（`test_fees.py` / `test_paper_exec.py` 用）。

    ⚠️ 2026-09-22（结项审计复核）：`tests/test_board.py` 里有一个**同名但不同物**的
    `_ExecBase` —— 不是重复代码，见那边的 docstring 列出的三处实质差异
    （建库内容 / config 桩口径 / 辅助方法）。**不要合并**。
    """
    def setUp(self):
        # 临时文件库: 引擎会 conn.close(), in-memory 会被销毁, 每次 get_conn 给新连接
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(_SCHEMA)
        conn.execute("INSERT INTO paper_account (id, cash) VALUES (1, 100000.0)")
        conn.commit()
        conn.close()
        self.patch_conn = mock.patch.object(engine.db, "get_conn",
                                            side_effect=lambda: sqlite3.connect(self.path))
        # 桩 config: 显式钉住旧扁平 0.3% 单边(本文件的断言数字都按此口径写完), 免得
        # 逐笔真实费用(2026-09-10 起生产默认: 万2.5/最低 5 元/印花税/过户)改写本文件
        # 全部现金算术。真实费用通道由 tests/test_fees.py 单独覆盖。生产 config 恒带
        # fees 段(DEFAULT_CONFIG 深合并), 不会落到这里的取值。
        self.patch_cfg = mock.patch.object(
            engine.cfg_mod, "load_config",
            return_value={"single_side_cost": 0.003, "paper": {"slip": 0.001},
                          "capital": 10000,
                          "fees": {"commission_rate": 0.003, "min_commission": 0.0,
                                   "stamp_tax": 0.0, "stamp_tax_before": 0.0,
                                   "transfer_fee": 0.0}})
        self.patch_conn.start()
        self.patch_cfg.start()

    def tearDown(self):
        self.patch_conn.stop()
        self.patch_cfg.stop()
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

    def _open_days(self, days):
        conn = self._c()
        conn.executemany("INSERT OR REPLACE INTO trade_cal VALUES (?,1)", [(d,) for d in days])
        conn.commit()
        conn.close()

    def _daily(self, code, d, open_, pre, vol=10000, close=None):
        self._exec("INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?)",
                   (code, d, open_, pre, vol, open_ if close is None else close))

    def _order(self, d, code, side, shares, name="TEST"):
        self._exec(
            "INSERT INTO paper_order (order_date, ts_code, name, side, shares, reason) "
            "VALUES (?,?,?,?,?,'t')", (d, code, name, side, shares))

    def _order_created(self, d, code, side, shares, created):
        self._exec(
            "INSERT INTO paper_order (order_date, ts_code, name, side, shares, reason, created_at) "
            "VALUES (?,?,?,?,?,'t',?)", (d, code, "TEST", side, shares, created))

    def _status(self, code, side):
        return self._one(
            "SELECT status, first_attempt FROM paper_order WHERE ts_code=? AND side=?",
            (code, side))

