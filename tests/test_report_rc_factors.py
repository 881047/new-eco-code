"""第 12 族（分析师研报预测）因子构造的契约测试（2026-09-13）

补库要跑 9 小时，所以**因子口径必须先钉死**，否则等数据到了再发现"窗口算错/前视"就白等一夜。
这里守四件事：

  ① **PIT**：`report_date > d` 的研报绝不许影响第 d 天的因子（不许用 `create_time`/`quarter`）；
  ② **滚动窗**：只算近 30 自然日；窗口外的旧研报不许漏进来；
  ③ 评级映射与目标价采信（`tp`→缺则 `max_price`；越界 [−50%,+200%] 记 NaN 而不是硬截断）；
  ④ 表空/缺表 ⇒ 返回 `{}` + 告警（补库未完成时不许静默产出"全 NaN 因子"当结论）。

运行: python -m pytest tests/test_report_rc_factors.py -q
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db                                                   # noqa: E402
from app.factors import flow_factors as ff                           # noqa: E402


def _market(dates, cols) -> dict:
    return {"close": pd.DataFrame(20.0, index=dates, columns=cols)}


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(db.SCHEMA)
        conn.commit()
        conn.close()
        self.patch = mock.patch.object(ff.db, "get_conn",
                                       side_effect=lambda: sqlite3.connect(self.path))
        self.patch.start()
        self.dates = pd.bdate_range("2026-01-05", "2026-03-02")
        self.m = _market(self.dates, ["600000.SH", "000001.SZ", "300001.SZ"])

    def tearDown(self):
        self.patch.stop()
        Path(self.path).unlink(missing_ok=True)

    def _insert(self, rows):
        # 主键四列 (report_date, ts_code, org_name, report_title) 在 WITHOUT ROWID 表里是 NOT NULL
        # ⇒ 测试也要给标题，按序号造唯一标题（同机构同日多篇 = 不同标题，正是主键的设计意图）
        conn = sqlite3.connect(self.path)
        payload = [(r[0], r[1], r[2], f"标题{i}", r[3], r[4], r[5], r[6])
                   for i, r in enumerate(rows)]
        conn.executemany(
            "INSERT INTO report_rc (ts_code, report_date, org_name, report_title, rating, "
            "tp, max_price, eps) VALUES (?,?,?,?,?,?,?,?)", payload)
        conn.commit()
        conn.close()

    def _factors(self):
        # 显式指定 Tushare 源：本类测的是**因子构造语义**，不该让它去 auto 选源
        return ff.compute_report_rc_factors(self.m, self.dates, source="rc")


class TestPitWindow(_Base):
    def test_no_lookahead_and_30d_window(self):
        self._insert([("600000.SH", "20260120", "中信", "买入", 30.0, None, 1.0),
                      ("600000.SH", "20260105", "银河", "增持", 24.0, None, 1.0)])
        f = self._factors()["rc_cnt_30"]
        self.assertEqual(float(f.loc[pd.Timestamp("2026-01-15"), "600000.SH"]), 1.0,
                         "1-05 的研报在 1-15 应可见；1-20 的那篇不许提前可见")
        self.assertEqual(float(f.loc[pd.Timestamp("2026-01-20"), "600000.SH"]), 2.0,
                         "1-20 当天发布的研报当天可见（按 report_date 对齐）")
        self.assertEqual(float(f.loc[pd.Timestamp("2026-03-02"), "600000.SH"]), 0.0,
                         "两篇都超出 30 日窗 ⇒ 回到 0（不是 NaN：没研报是真实信息）")
        self.assertEqual(float(f.loc[pd.Timestamp("2026-01-15"), "000001.SZ"]), 0.0,
                         "没被覆盖的票取 0")

    def test_rating_mapping_and_unrecognized(self):
        self._insert([("600000.SH", "20260120", "中信", "买入", None, None, 1.0),
                      ("600000.SH", "20260120", "银河", "减持", None, None, 1.0)])
        f = self._factors()["rc_rating_30"]
        self.assertAlmostEqual(float(f.loc[pd.Timestamp("2026-01-20"), "600000.SH"]), 0.5,
                               places=6, msg="买=2 与 减=−1 的均值 = 0.5")
        self._insert([("000001.SZ", "20260120", "中信", "看不懂的评级", None, None, 1.0)])
        f2 = self._factors()["rc_rating_30"]
        self.assertTrue(np.isnan(float(f2.loc[pd.Timestamp("2026-01-20"), "000001.SZ"])),
                        "未识别评级 → NaN（不猜，也不计进均值）")

    def test_upside_uses_tp_then_max_price_and_bounds(self):
        # close=20：tp=30 ⇒ +50%；只有 max_price=25 ⇒ +25%
        self._insert([("600000.SH", "20260120", "中信", "买入", 30.0, None, 1.0),
                      ("000001.SZ", "20260120", "中信", "买入", None, 25.0, 1.0)])
        f = self._factors()["rc_upside_30"]
        d = pd.Timestamp("2026-01-20")
        self.assertAlmostEqual(float(f.loc[d, "600000.SH"]), 0.5, places=6)
        self.assertAlmostEqual(float(f.loc[d, "000001.SZ"]), 0.25, places=6)

    def test_upside_out_of_bounds_is_nan(self):
        """窗口内目标价越界 ⇒ NaN（不许硬截断成 +200%，那会造出假信号）。

        注意用**此前无目标价**的票：滚动窗取中位数，单条脏值会被稀释（那是刻意的稳健性），
        只有整个窗口都是脏值时才该判 NaN —— 这里构造的正是后者。
        """
        self._insert([("300001.SZ", "20260202", "银河", "买入", 1000.0, None, 1.0)])
        f2 = self._factors()["rc_upside_30"]
        self.assertFalse(np.isfinite(float(f2.loc[pd.Timestamp("2026-02-02"), "300001.SZ"])),
                         "1000/20−1 = +4900% 越界 ⇒ 必须 NaN")


class TestEmptyTable(unittest.TestCase):
    def test_empty_or_missing_table_returns_empty_with_warning(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        path = tmp.name
        tmp.close()
        conn = sqlite3.connect(path)
        conn.executescript(db.SCHEMA)
        conn.commit()
        conn.close()
        dates = pd.bdate_range("2026-01-05", "2026-01-09")
        m = _market(dates, ["600000.SH"])
        try:
            with mock.patch.object(ff.db, "get_conn", side_effect=lambda: sqlite3.connect(path)):
                with self.assertLogs(ff.log, level="WARNING") as cm:
                    out = ff.compute_report_rc_factors(m, dates)
            self.assertEqual(out, {}, "两个源都没就绪必须返回 {} 而不是产出全 NaN 因子")
            self.assertTrue(any("没就绪" in x for x in cm.output), cm.output)
        finally:
            Path(path).unlink(missing_ok=True)


class TestEastmoneySource(_Base):
    """主力源（东财 `research_report_em`）的因子构造：评级/目标价口径与 Tushare 不同，必须单独钉。"""

    def setUp(self):
        super().setUp()
        self.ff = ff

    def _insert_em(self, rows):
        conn = sqlite3.connect(self.path)
        conn.executemany(
            "INSERT INTO research_report_em (info_code, ts_code, report_date, org_name, "
            "report_title, rating, aim_price_t, aim_price_l) VALUES (?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        conn.close()

    def test_em_rating_and_target_midpoint(self):
        # close=20：目标价区间 [30, 34] ⇒ 中点 32 ⇒ 空间 +60%
        self._insert_em([("AP1", "600000.SH", "20260120", "东吴证券", "点评", "买入", 34.0, 30.0),
                         ("AP2", "600000.SH", "20260120", "国信证券", "点评2", "增持", 26.0, 22.0)])
        f = ff.compute_report_rc_factors(self.m, self.dates, source="em")
        d = pd.Timestamp("2026-01-20")
        self.assertEqual(float(f["rc_cnt_30"].loc[d, "600000.SH"]), 2.0)
        self.assertAlmostEqual(float(f["rc_rating_30"].loc[d, "600000.SH"]), 1.5,
                               places=6, msg="买=2 与 增=1 的均值")
        # 中位数：两条目标价中点 32 与 24 ⇒ 取中位数 = 28 ⇒ 28/20−1 = +40%
        self.assertAlmostEqual(float(f["rc_upside_30"].loc[d, "600000.SH"]), 0.40, places=6)

    def test_em_single_sided_target(self):
        self._insert_em([("AP3", "000001.SZ", "20260120", "中信", "点评", "买入", 25.0, None)])
        f = ff.compute_report_rc_factors(self.m, self.dates, source="em")
        self.assertAlmostEqual(float(f["rc_upside_30"].loc[pd.Timestamp("2026-01-20"),
                                                           "000001.SZ"]), 0.25, places=6)


class TestReadyGate(unittest.TestCase):
    """补库完成度闸门：半张表不许放行（否则门禁会产出"覆盖不足 ⇒ 无效"的假结论）

    2026-09-13：主力源换成东财 `research_report_em`（Tushare 该接口实测 10 次/天），
    闸门改为 `analyst_ready()` —— 东财优先、行数不够再退回 Tushare。"""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(db.SCHEMA)
        conn.commit()
        conn.close()

    def tearDown(self):
        Path(self.path).unlink(missing_ok=True)

    def _ready(self, **kw):
        with mock.patch.object(ff.db, "get_conn", side_effect=lambda: sqlite3.connect(self.path)):
            return ff.analyst_ready(**kw)

    def _insert(self, n, report_date, table="report_rc"):
        cols = ("ts_code, report_date, org_name, report_title" if table == "report_rc"
                else "info_code, ts_code, report_date, org_name, report_title")
        ph = "?,?,?,?" if table == "report_rc" else "?,?,?,?,?"
        conn = sqlite3.connect(self.path)
        rows = ([(f"{i:06d}.SZ", report_date, "中信", f"t{i}") for i in range(n)]
                if table == "report_rc"
                else [(f"AP{i}", f"{i:06d}.SZ", report_date, "中信", f"t{i}") for i in range(n)])
        conn.executemany(f"INSERT INTO {table} ({cols}) VALUES ({ph})", rows)
        conn.commit()
        conn.close()

    def test_empty_is_not_ready(self):
        ok, why, src = self._ready()
        self.assertFalse(ok)
        self.assertIsNone(src)
        self.assertIn("两源都未就绪", why)

    def test_partial_is_not_ready(self):
        self._insert(50, "20260910")
        ok, why, src = self._ready(min_rows_em=400_000, min_rows_rc=400_000, today="20260913")
        self.assertFalse(ok)
        self.assertIn("两源都未就绪", why)

    def test_stale_is_not_ready(self):
        self._insert(500, "20260801", table="research_report_em")
        ok, why, src = self._ready(min_rows_em=100, min_rows_rc=100, max_stale_days=10,
                                   today="20260913")
        self.assertFalse(ok)
        self.assertEqual(src, "em", "东财行数够 ⇒ 优先报东财")
        self.assertIn("中途停了", why)

    def test_complete_and_fresh_is_ready(self):
        self._insert(500, "20260910")
        ok, why, src = self._ready(min_rows_em=100, min_rows_rc=100, max_stale_days=10,
                                   today="20260913")
        self.assertTrue(ok, why)
        self.assertIn("500", why)

    def test_prefers_eastmoney_when_both_ready(self):
        """两源都够行数时**优先东财**（Tushare 那份只作第二次交叉核对）"""
        self._insert(500, "20260910", table="report_rc")
        self._insert(500, "20260910", table="research_report_em")
        ok, why, src = self._ready(min_rows_em=100, min_rows_rc=100, today="20260913")
        self.assertTrue(ok)
        self.assertEqual(src, "em")


if __name__ == "__main__":
    unittest.main()
