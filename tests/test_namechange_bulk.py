"""`namechange` 批量增量守卫（2026-09-13）

背景（实测缺口，不是假想）：`init_namechange(recent_only=True)` 过去只拉
`universe - 表里已有记录的代码` ⇒ **老股再次改名/被 ST 永远补不到**。
20260901~20260911 实测：批量接口 18 行、库里缺 4 行（300351/300562/301560/301339）。
若缺的那只恰好变成 ST，PIT 名称链仍按旧名判"非 ST" ⇒ 可能买进 ST 股。

本测试用临时库 + 假 ts 客户端，守三条：
  1. 批量返回的行**真的写进库**（含"库里已有该股历史记录"的老股 —— 这正是原死角的形状）；
  2. 批量调用失败（抛异常 / 返回空）**不许影响**后面的逐股增量；
  3. 重复跑幂等（`namechange` 的 PK=(ts_code,start_date)）。

运行: python -m unittest tests.test_namechange_bulk -v
"""
import contextlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd                                       # noqa: E402

from app.datasource import updater as up                  # noqa: E402

_SCHEMA = """
CREATE TABLE stock_basic (ts_code TEXT PRIMARY KEY, symbol TEXT, name TEXT, area TEXT,
    industry TEXT, market TEXT, list_date TEXT, delist_date TEXT);
CREATE TABLE namechange (ts_code TEXT, name TEXT, start_date TEXT, end_date TEXT,
    ann_date TEXT, change_reason TEXT, PRIMARY KEY (ts_code, start_date)) WITHOUT ROWID;
CREATE TABLE data_status (name TEXT PRIMARY KEY, last_date TEXT, updated_at TEXT, rows INTEGER);
"""
BULK_COLS = ["ts_code", "name", "start_date", "end_date", "ann_date", "change_reason"]


class _FakeTs:
    """假客户端：记录调用；可配置批量失败 / 逐股返回。"""

    def __init__(self, bulk_df, bulk_exc=None, per_stock_df=None):
        self.bulk_df, self.bulk_exc, self.per_stock_df = bulk_df, bulk_exc, per_stock_df
        self.calls = []
        self.last_status = "empty"

    def call(self, api, **kw):
        self.calls.append((api, kw))
        if "start_date" in kw:          # 批量（不带 ts_code）
            if self.bulk_exc:
                raise self.bulk_exc
            self.last_status = "ok" if self.bulk_df is not None else "empty"
            return self.bulk_df
        # 逐股路径按真实客户端的契约置 last_status（updater 靠它区分 ok / empty / 失败）
        self.last_status = ("ok" if self.per_stock_df is not None
                            and not self.per_stock_df.empty else "empty")
        return self.per_stock_df


class TestNamechangeBulkIncrement(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(_SCHEMA)
        conn.executemany("INSERT INTO stock_basic (ts_code, name) VALUES (?,?)",
                         [("300351.SZ", "永贵电器"), ("600000.SH", "浦发银行")])
        # 老股 300351 早已有历史改名记录 —— 正是"逐股增量永远看不到它"的形状
        conn.execute("INSERT INTO namechange VALUES "
                     "('300351.SZ','永贵电器','20150101',NULL,'20150101','其他')")
        conn.commit()
        conn.close()

        @contextlib.contextmanager
        def _ctx():
            c = sqlite3.connect(self.path)
            try:
                yield c
                c.commit()
            finally:
                c.close()
        self._p = mock.patch.object(up.db, "conn_ctx", _ctx)
        self._p.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(lambda: Path(self.path).unlink(missing_ok=True))

    def _rows(self):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute("SELECT ts_code, name, start_date FROM namechange "
                                "ORDER BY ts_code, start_date").fetchall()
        finally:
            conn.close()

    def _updater(self, fake):
        u = up.DataUpdater.__new__(up.DataUpdater)     # 不跑 __init__（会建真客户端）
        u.ts = fake
        u._uni = {"300351.SZ", "600000.SH"}
        u.universe_codes = lambda: u._uni
        return u

    def test_bulk_rows_are_written_even_for_stocks_with_existing_history(self):
        bulk = pd.DataFrame([["300351.SZ", "永贵智能", "20260909", None, "20260908", "其他"]],
                            columns=BULK_COLS)
        u = self._updater(_FakeTs(bulk, per_stock_df=None))
        u.init_namechange(recent_only=True)
        rows = self._rows()
        self.assertIn(("300351.SZ", "永贵智能", "20260909"), rows,
                      "批量增量没把老股的新改名写进去（原死角复发）")

    def test_bulk_failure_does_not_block_per_stock_path(self):
        per = pd.DataFrame([["600000.SH", "浦发银行", "20150101", None, "20150101", "其他"]],
                           columns=BULK_COLS)
        u = self._updater(_FakeTs(None, bulk_exc=RuntimeError("权限不足"), per_stock_df=per))
        u.init_namechange(recent_only=True)          # 不许抛
        self.assertIn(("600000.SH", "浦发银行", "20150101"), self._rows(),
                      "批量失败后逐股增量没有继续跑")

    def test_rerun_is_idempotent(self):
        bulk = pd.DataFrame([["300351.SZ", "永贵智能", "20260909", None, "20260908", "其他"]],
                            columns=BULK_COLS)
        u = self._updater(_FakeTs(bulk, per_stock_df=None))
        u.init_namechange(recent_only=True)
        u.init_namechange(recent_only=True)
        rows = [r for r in self._rows() if r[2] == "20260909"]
        self.assertEqual(len(rows), 1, f"重复跑产生了重复行: {rows}")


if __name__ == "__main__":
    unittest.main()
