"""新信息源同步单测 (2026-09-11 「芯片式异构架构」阶段 0a)

覆盖 flow_client 的**契约与易错分支**, 而不是把 sync_daily_bulk 的既有逻辑重测一遍:

  · 列契约: 客户端的目标列必须与 `db.SCHEMA` 的 DDL **逐字一致**。这不是形式检查 ——
    `upsert_df` 是 INSERT OR REPLACE 整行重插, 少一列就会把该列写成 NULL **覆盖旧值**
    (`db.py:365` 的契约注已明文警告)。上游加一列而客户端没跟上, 是静默数据损坏。
  · `_agg_block_trade`: 一笔一行 → 一股一行的成交量加权, 及其除零/脏行分支。
  · `sync_index_weight`: **按年循环的回归护栏** —— 不显式给日期范围时上游只返回近 ~2 年,
    静默丢失更早历史(2026-09-11 实测踩到)。
  · `sync_holder_num`: 最近 4 期强制重拉(更正披露), 以及权限失败即终止。
  · `sync_new_sources`: 单个源异常不得阻塞其余源。

运行: ./.venv/Scripts/python.exe -m pytest tests/test_flow_sources.py -q
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from app import db
from app.datasource import flow_client as fc


def _ddl_cols(table: str) -> list:
    """从**真实 SCHEMA** 建库取列 —— 不用正则解析 DDL(主键子句里的逗号会骗过正则)"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = tmp.name
    tmp.close()
    try:
        conn = sqlite3.connect(path)
        conn.executescript(db.SCHEMA)
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        conn.close()
    finally:
        Path(path).unlink(missing_ok=True)
    return cols


class _FakeUpdater:
    """最小 DataUpdater 替身: 只需 .ts.call / .ts.last_status

    perm_after: 第 N 次调用起返回 perm 状态(模拟权限失败)
    """

    def __init__(self, responses=None, perm_after=None):
        self.ts = mock.MagicMock()
        self.calls = []
        self.responses = responses or {}
        self.perm_after = perm_after

        def _call(api, **kw):
            self.calls.append((api, kw))
            if self.perm_after is not None and len(self.calls) > self.perm_after:
                self.ts.last_status = "perm"
                return pd.DataFrame()
            r = self.responses.get(api)
            self.ts.last_status = "ok"
            if isinstance(r, Exception):
                raise r
            if callable(r):
                return r(**kw)
            return r

        self.ts.call.side_effect = _call


class _DbBase(unittest.TestCase):
    """把 flow_client.db.get_conn 指到临时库, 库内建全量 SCHEMA"""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(db.SCHEMA)
        conn.commit()
        conn.close()
        self.patch = mock.patch.object(
            fc.db, "get_conn", side_effect=lambda: sqlite3.connect(self.path))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        Path(self.path).unlink(missing_ok=True)

    def _exec(self, sql, args=()):
        conn = sqlite3.connect(self.path)
        conn.execute(sql, args)
        conn.commit()
        conn.close()

    def _rows(self, sql, args=()):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def _cols(self, table):
        conn = sqlite3.connect(self.path)
        try:
            return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
class TestColumnContracts(unittest.TestCase):
    """客户端列 ↔ DDL 列必须相等 —— 少一列 = 静默 NULL 覆盖"""

    def test_margin_cols_match_ddl(self):
        self.assertEqual(sorted(fc.MARGIN_COLS), sorted(_ddl_cols("margin")))

    def test_moneyflow_cols_match_ddl(self):
        self.assertEqual(sorted(fc.MONEYFLOW_COLS), sorted(_ddl_cols("moneyflow")))

    def test_hk_hold_cols_match_ddl(self):
        self.assertEqual(sorted(fc.HK_HOLD_COLS), sorted(_ddl_cols("hk_hold")))

    def test_block_trade_transform_output_matches_ddl(self):
        # block_trade 走 cols=None, 入库的就是 transform 的输出 → 必须与 DDL 相等
        df = pd.DataFrame([{"trade_date": "20200102", "ts_code": "600000.SH",
                            "price": 10.0, "vol": 100.0}])
        out = fc._agg_block_trade(df)
        self.assertEqual(sorted(out.columns), sorted(_ddl_cols("block_trade")))


# ---------------------------------------------------------------------------
class TestAggBlockTrade(unittest.TestCase):
    """大宗交易: 一笔一行 → 一股一行"""

    def test_volume_weighted_price(self):
        # 10 元成交 100 万股 + 20 元成交 300 万股 → 加权 17.5, 而非算术 15 或末笔 20
        df = pd.DataFrame([
            {"trade_date": "20200102", "ts_code": "600000.SH", "price": 10.0, "vol": 100.0},
            {"trade_date": "20200102", "ts_code": "600000.SH", "price": 20.0, "vol": 300.0},
        ])
        g = fc._agg_block_trade(df).set_index("ts_code")
        self.assertAlmostEqual(g.loc["600000.SH", "avg_price"], 17.5, places=6)
        self.assertEqual(g.loc["600000.SH", "n_trades"], 2)
        self.assertAlmostEqual(g.loc["600000.SH", "total_vol"], 400.0, places=6)
        self.assertAlmostEqual(g.loc["600000.SH", "total_amount"], 7000.0, places=6)

    def test_different_stocks_and_days_not_merged(self):
        df = pd.DataFrame([
            {"trade_date": "20200102", "ts_code": "600000.SH", "price": 10.0, "vol": 100.0},
            {"trade_date": "20200103", "ts_code": "600000.SH", "price": 10.0, "vol": 100.0},
            {"trade_date": "20200102", "ts_code": "000001.SZ", "price": 10.0, "vol": 100.0},
        ])
        g = fc._agg_block_trade(df)
        self.assertEqual(len(g), 3)
        self.assertEqual(g["n_trades"].tolist(), [1, 1, 1])

    def test_dirty_rows_dropped_and_zero_vol_no_crash(self):
        # 价/量非数值 → 丢弃; 全为 0 量 → avg_price 为 NaN(除零)而不是 inf/异常
        df = pd.DataFrame([
            {"trade_date": "20200102", "ts_code": "600000.SH", "price": None, "vol": 100.0},
            {"trade_date": "20200102", "ts_code": "000001.SZ", "price": 10.0, "vol": 0.0},
            {"trade_date": "20200102", "ts_code": "000002.SZ", "price": 10.0, "vol": 50.0},
        ])
        g = fc._agg_block_trade(df).set_index("ts_code")
        self.assertNotIn("600000.SH", g.index)
        self.assertEqual(len(g), 2)
        self.assertTrue(pd.isna(g.loc["000001.SZ", "avg_price"]))


# ---------------------------------------------------------------------------
class TestIndexWeight(unittest.TestCase):
    """指数成分: 必须按年显式给日期范围"""

    def test_loops_every_year(self):
        u = _FakeUpdater({"index_weight": pd.DataFrame(
            columns=["index_code", "con_code", "trade_date", "weight"])})
        fc.sync_index_weight(u, codes=["000300.SH"], start_year=2019)
        years = sorted(kw["start_date"][:4] for _, kw in u.calls)
        end_year = pd.Timestamp.today().year
        self.assertEqual(years, [str(y) for y in range(2019, end_year + 1)])
        # 每一年都必须**显式**带区间: 不带时上游静默只返回近 ~2 年
        for _, kw in u.calls:
            self.assertTrue(kw.get("start_date"), "缺 start_date 会静默丢历史")
            self.assertTrue(kw.get("end_date"))

    def test_perm_aborts(self):
        u = _FakeUpdater({"index_weight": pd.DataFrame()}, perm_after=0)
        fc.sync_index_weight(u, codes=["000300.SH"], start_year=2024)
        self.assertEqual(len(u.calls), 1)  # 首次即因权限终止, 不空转其余年份


# ---------------------------------------------------------------------------
class TestHolderNum(_DbBase):
    """股东户数: 最近 4 期强制重拉(更正披露)"""

    @staticmethod
    def _call_ok(enddate=None, **kw):
        return pd.DataFrame([{"ts_code": "600000.SH", "end_date": enddate,
                              "ann_date": enddate, "holder_num": 50000.0}])

    def test_recent_quarters_repulled(self):
        # 库里已有 2023Q1~2024Q4 全部报告期 → 最近 4 期仍必须重拉
        for p in ("20230331", "20230630", "20230930", "20231231",
                  "20240331", "20240630", "20240930", "20241231"):
            self._exec("INSERT INTO holder_num VALUES ('600000.SH',?,?,50000)", (p, p))
        u = _FakeUpdater({"stk_holdernumber": self._call_ok})
        fc.sync_holder_num(u, start="20230331", end="20241231")
        pulled = {kw["enddate"] for _, kw in u.calls}
        self.assertTrue({"20240331", "20240630", "20240930", "20241231"} <= pulled)

    def test_perm_breaks(self):
        u = _FakeUpdater({"stk_holdernumber": self._call_ok}, perm_after=0)
        fc.sync_holder_num(u, start="20240101", end="20241231")
        self.assertEqual(len(u.calls), 1)

    def test_writes_rows(self):
        u = _FakeUpdater({"stk_holdernumber": self._call_ok})
        fc.sync_holder_num(u, start="20240331", end="20240630")
        self.assertGreaterEqual(
            self._rows("SELECT COUNT(*) FROM holder_num")[0][0], 1)


# ---------------------------------------------------------------------------
class TestSwIndustry(_DbBase):
    """申万行业: L1+L2 都要, 且成分股逐行业拉"""

    _CLS = pd.DataFrame([
        {"index_code": "801010.SI", "industry_name": "农林牧渔", "level": "L1",
         "industry_code": "801010", "parent_code": None, "src": "SW2021"},
        {"index_code": "801011.SI", "industry_name": "林业", "level": "L2",
         "industry_code": "801011", "parent_code": "801010", "src": "SW2021"},
    ])
    @staticmethod
    def _mem(index_code=None, **kw):
        # 按请求的行业返回不同成员 —— 若各行业返回同一份, 主键 (con_code, index_code,
        # in_date) 会让 upsert 去重, 测试就看不出"每个行业都拉了成分股"
        return pd.DataFrame([
            {"con_code": "600000.SH", "index_code": index_code,
             "in_date": "20140101", "out_date": None, "is_new": "Y"},
        ])

    def test_both_levels_and_members(self):
        u = _FakeUpdater({"index_classify": self._CLS, "index_member": self._mem})
        fc.sync_sw_industry(u, force_members=True)
        levels = {kw["level"] for api, kw in u.calls if api == "index_classify"}
        self.assertEqual(levels, {"L1", "L2"})
        self.assertEqual(self._rows("SELECT COUNT(*) FROM sw_classify")[0][0], 2)
        # 2 个行业各拉一次成分股 → 2 行(主键含 index_code, 不会互相覆盖)
        self.assertEqual(self._rows("SELECT COUNT(*) FROM sw_member")[0][0], 2)
        self.assertEqual(
            {r[0] for r in self._rows("SELECT DISTINCT index_code FROM sw_member")},
            {"801010.SI", "801011.SI"})


# ---------------------------------------------------------------------------
class TestOrchestratorIsolation(unittest.TestCase):
    """单源异常不得阻塞其余源"""

    def test_one_source_failure_does_not_block_others(self):
        called = []

        def _bad(u, **kw):
            called.append("bad")
            raise RuntimeError("boom")

        def _ok(u, **kw):
            called.append("ok")
            return 1

        with mock.patch.object(fc, "DAILY_SOURCES", (("bad", _bad), ("ok", _ok))), \
             mock.patch.object(fc, "sync_holder_num", _ok), \
             mock.patch.object(fc, "sync_index_weight", _ok), \
             mock.patch.object(fc, "sync_sw_industry", _ok):
            fc.sync_new_sources(mock.MagicMock())
        # 日频坏源之后的日频好源 1 次 + 3 个期频源各 1 次 = 4 次, 且 bad 之后没有中断
        self.assertEqual(called, ["bad", "ok", "ok", "ok", "ok"])


if __name__ == "__main__":
    unittest.main()
