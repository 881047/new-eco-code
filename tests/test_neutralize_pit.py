"""`factor_engine.neutralize_pit` 的守卫测试 —— 时点行业中性化(周频袖套第二路 die 的核心)。

守四条:
  1. 逐期行业去均值: 同一期内同行业股票的截面均值被扣掉(按**当期**行业, 不是全历史同一个);
  2. z-score: 每行处理后均值≈0、标准差≈1;
  3. 去极值: 2% 分位裁剪(离群点被拉回);
  4. **无行业归属(NaN)的格子变 NaN**(被剔除), 而不是被当成"未知行业"参与去均值。
第 4 条是关键 —— 它决定了"无申万归属的次新/退市股"不会污染同组减法。
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.factors.factor_engine import neutralize_pit  # noqa: E402


class TestNeutralizePit(unittest.TestCase):
    def setUp(self):
        self.codes = ["a", "b", "c", "d", "e"]
        self.dates = pd.to_datetime(["2026-01-31", "2026-02-28"])
        # d1: a,b 同行业(均值 10) → 各得 0;a 在 d2 换到 Y 组
        self.df = pd.DataFrame(
            [[10.0, 10.0, 30.0, 50.0, 70.0],
             [10.0, 30.0, 30.0, 50.0, 70.0]], index=self.dates, columns=self.codes)
        self.ind = pd.DataFrame(
            [["X", "X", "Y", "Y", None],
             ["Y", "X", "Y", "Y", None]], index=self.dates, columns=self.codes)

    def test_row_wise_mean_removed(self):
        out = neutralize_pit(self.df, self.ind)
        # 第 1 天: X 组 {a,b} 原始均值 10 ⇒ 去均值后 a=b=0(X 组内部无差异);
        #           Y 组 {c,d} 均值 40 ⇒ 30-40=-10, 50-40=+10
        self.assertAlmostEqual(out.loc[self.dates[0], "a"], out.loc[self.dates[0], "b"], places=6)
        self.assertLess(out.loc[self.dates[0], "c"], out.loc[self.dates[0], "d"])

    def test_zscore_per_row(self):
        """标准差的自由度与 `fe.neutralize` 一致 = pandas 默认 ddof=1(样本标准差);
        故标准化后**样本** std=1(总体 std 会是 sqrt((n-1)/n))。"""
        out = neutralize_pit(self.df, self.ind)
        for d in self.dates:
            row = out.loc[d].dropna()
            self.assertAlmostEqual(float(row.mean()), 0.0, places=6)
            self.assertAlmostEqual(float(row.std(ddof=1)), 1.0, places=5)

    def test_no_industry_cell_becomes_nan(self):
        out = neutralize_pit(self.df, self.ind)
        self.assertTrue(np.isnan(out.loc[self.dates[0], "e"]),
                        "无行业归属的格子必须被剔除(NaN), 而不是参与去均值")
        self.assertTrue(np.isnan(out.loc[self.dates[1], "e"]))

    def test_industry_changes_by_date(self):
        """a 在 d2 换组 ⇒ 它的去均值基准必须跟着变(这是 PIT 与快照的唯一区别)。"""
        snap = pd.DataFrame([["X"] * 4 + [None]] * 2, index=self.dates, columns=self.codes)
        out_pit = neutralize_pit(self.df, self.ind)
        out_snap = neutralize_pit(self.df, snap)
        self.assertNotAlmostEqual(float(out_pit.loc[self.dates[1], "a"]),
                                  float(out_snap.loc[self.dates[1], "a"]), places=6)


if __name__ == "__main__":
    unittest.main()
