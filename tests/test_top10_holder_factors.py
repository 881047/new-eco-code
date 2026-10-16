"""第 13 族（十大流通股东）因子构造的契约测试（2026-09-13）

守三件事：
  ① **PIT**：`ann_date > d` 的报告期绝不许影响第 d 天的因子（不许用 `end_date` 对齐 —— 季报滞后 1~4 个月）；
  ② 构造：合计比例 / **机构类**比例（按 `holder_type` 关键词）/ 相对上一报告期的变动；
  ③ 完成度闸门 `holder_ready()` 四条分支。

运行: python -m pytest tests/test_top10_holder_factors.py -q
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
        self.dates = pd.bdate_range("2026-04-01", "2026-05-10")
        self.m = {"close": pd.DataFrame(20.0, index=self.dates,
                                        columns=["600000.SH", "000001.SZ", "300001.SZ"])}

    def tearDown(self):
        self.patch.stop()
        Path(self.path).unlink(missing_ok=True)

    def _insert(self, rows):
        conn = sqlite3.connect(self.path)
        conn.executemany(
            "INSERT INTO top10_holder (ts_code, ann_date, end_date, holder_name, "
            "hold_float_ratio, holder_type) VALUES (?,?,?,?,?,?)", rows)
        conn.commit()
        conn.close()

    def _f(self):
        return ff.compute_top10_holder_factors(self.m, self.dates)


class TestPitAndShape(_Base):
    def test_pit_uses_ann_date_not_end_date(self):
        """同一报告期的数据在**公告日**之前不可见（用 end_date 对齐就会提前 1 个月看到）"""
        self._insert([("600000.SH", "20260430", "20260331", "A基金", 8.0, "证券投资基金"),
                      ("600000.SH", "20260430", "20260331", "某企业", 5.0, "一般企业")])
        f = self._f()["ih_conc"]
        self.assertTrue(np.isnan(float(f.loc[pd.Timestamp("2026-04-20"), "600000.SH"])),
                        "4-20 还没公告(4-30 才公告) ⇒ 必须 NaN")
        self.assertAlmostEqual(float(f.loc[pd.Timestamp("2026-04-30"), "600000.SH"]), 0.13,
                               places=6, msg="13% 合计比例 ⇒ 0.13")
        self.assertAlmostEqual(float(f.loc[pd.Timestamp("2026-05-08"), "600000.SH"]), 0.13,
                               places=6, msg="公告后按 ffill 一直有效")

    def test_inst_ratio_and_change(self):
        self._insert([("600000.SH", "20260430", "20260331", "A基金", 8.0, "证券投资基金"),
                      ("600000.SH", "20260430", "20260331", "某企业", 5.0, "一般企业"),
                      # 上一期（2025 年报）合计 10%，机构 2%
                      ("600000.SH", "20260401", "20251231", "B基金", 2.0, "证券投资基金"),
                      ("600000.SH", "20260401", "20251231", "某企业", 8.0, "一般企业")])
        f = self._f()
        d_new = pd.Timestamp("2026-04-30")
        d_old = pd.Timestamp("2026-04-10")
        self.assertAlmostEqual(float(f["ih_fund_ratio"].loc[d_new, "600000.SH"]), 0.08, places=6)
        self.assertAlmostEqual(float(f["ih_fund_ratio"].loc[d_old, "600000.SH"]), 0.02, places=6)
        self.assertAlmostEqual(float(f["ih_conc_chg"].loc[d_new, "600000.SH"]), 0.03, places=6,
                               msg="13% − 10% = +3pp")
        self.assertTrue(np.isnan(float(f["ih_conc_chg"].loc[d_old, "600000.SH"])),
                        "首个报告期没有上一期可比 ⇒ NaN（不许当成 0）")

    def test_uncovered_stock_is_nan(self):
        self._insert([("600000.SH", "20260430", "20260331", "A基金", 8.0, "证券投资基金")])
        f = self._f()
        self.assertTrue(np.isnan(float(f["ih_conc"].loc[pd.Timestamp("2026-04-30"), "300001.SZ"])),
                        "没被覆盖的票是 NaN（'不知道谁在持有'≠'没人持有'）")

    def test_same_day_two_periods_uses_latest(self):
        """A 股常见"年报+一季报同日披露" ⇒ 同一 (ts_code, ann_date) 会有两个报告期。

        这会让 pivot 报 "Index contains duplicate entries"（2026-09-13 实测把门禁跑崩过），
        必须去重且**取最新报告期**。"""
        self._insert([("600000.SH", "20260430", "20251231", "A基金", 5.0, "证券投资基金"),
                      ("600000.SH", "20260430", "20260331", "A基金", 9.0, "证券投资基金")])
        f = self._f()["ih_conc"]
        self.assertAlmostEqual(float(f.loc[pd.Timestamp("2026-04-30"), "600000.SH"]), 0.09,
                               places=6, msg="同日两期 ⇒ 取最新报告期(2026Q1)")


class TestReadyGate(_Base):
    def _ready(self, **kw):
        return ff.holder_ready(**kw)

    def test_empty_not_ready(self):
        ok, why = self._ready()
        self.assertFalse(ok)
        self.assertIn("为空", why)

    def test_partial_not_ready(self):
        self._insert([(f"{i:06d}.SZ", "20260901", "20260630", "A基金", 1.0, "证券投资基金")
                      for i in range(50)])
        ok, why = self._ready(min_rows=1000, today="20260913")
        self.assertFalse(ok)
        self.assertIn("补库未完成", why)

    def test_stale_not_ready(self):
        self._insert([(f"{i:06d}.SZ", "20260101", "20251231", "A基金", 1.0, "证券投资基金")
                      for i in range(50)])
        ok, why = self._ready(min_rows=10, max_stale_days=120, today="20260913")
        self.assertFalse(ok)
        self.assertIn("中途停了", why)

    def test_ready(self):
        self._insert([(f"{i:06d}.SZ", "20260831", "20260630", "A基金", 1.0, "证券投资基金")
                      for i in range(50)])
        ok, why = self._ready(min_rows=10, max_stale_days=120, today="20260913")
        self.assertTrue(ok, why)


if __name__ == "__main__":
    unittest.main()
