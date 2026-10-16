"""风格读数单测 (2026-09-10): app/models/style.py

该模块是**描述性**指标, 不产生信号 —— 测试只锁口径(相对强弱算法/对齐/预热期),
不锁任何"阈值该触发什么"。别在这里加"信号正确性"断言: 那些阈值已被证伪(见模块 docstring)。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import style


def _series(daily_ret: float, n: int, start: str = "2020-01-01", base: float = 1000.0):
    """按固定日收益构造价格序列(索引为 YYYYMMDD 字符串, 与库内口径一致)。"""
    idx = pd.bdate_range(start, periods=n).strftime("%Y%m%d")
    return pd.Series(base * (1 + daily_ret) ** np.arange(n), index=idx)


class TestRelStrength(unittest.TestCase):
    def test_flat_bench_pool_compounds(self):
        pc = _series(0.001, 300)
        bc = _series(0.000, 300)
        r = style.style_frame(pc, bc)
        self.assertTrue(r["available"])
        # 接口契约: 读数按 4 位小数(万分之一)输出, 与期望值同口径比较
        self.assertEqual(r["rel_20"], round(1.001 ** 20 - 1, 4))
        self.assertEqual(r["rel_60"], round(1.001 ** 60 - 1, 4))

    def test_relative_is_symmetric(self):
        # 池子落后时读数必须为负(方向不能反)
        pc = _series(0.000, 300)
        bc = _series(0.001, 300)
        r = style.style_frame(pc, bc)
        self.assertLess(r["rel_60"], 0)
        self.assertEqual(r["rel_60"], round(1.0 / (1.001 ** 60) - 1, 4))

    def test_short_window_returns_none_not_zero(self):
        # 序列不足以算 250 日时给 None(前端显示 --), 不能拿不足的窗口当 0
        pc = _series(0.001, 100)
        bc = _series(0.000, 100)
        r = style.style_frame(pc, bc)
        self.assertIsNotNone(r["rel_20"])
        self.assertIsNone(r["rel_250"])


class TestAlignment(unittest.TestCase):
    def test_drops_dates_missing_on_either_side(self):
        # 只在共同交易日上比较 —— 错位对齐会把日期差当成收益差
        pc = _series(0.001, 300)
        bc = _series(0.001, 300).iloc[50:]          # 基准少前 50 天
        r = style.style_frame(pc, bc)
        self.assertEqual(r["n_days"], 250)
        self.assertEqual(r["as_of"], str(pc.index[-1]))

    def test_insufficient_overlap(self):
        pc = _series(0.001, 10)
        bc = _series(0.001, 10)
        r = style.style_frame(pc, bc)
        self.assertFalse(r["available"])
        self.assertIn("21", r["reason"])


class TestPoolWarmup(unittest.TestCase):
    def test_warmup_rows_dropped_not_called_defense(self):
        # 预热期(ma250 未满)NaN 比较恒 False → score=0 → 会被判成 defense。
        # 这正是 2026-09-08 在 _state_frame 修过的同类缺陷, 池子分支同样要剔。
        from app.models import regime
        pc = _series(0.001, 300)
        pf = regime._pool_state_frame(pc)
        self.assertEqual(len(pf), 51)                # 300 - 249 个完整 ma250 窗口
        self.assertTrue((pf["pool_state"] != "defense").any())  # 不是清一色 defense

    def test_warmup_state_is_undefined_for_short_series(self):
        # 序列不足 250 日 → 无任何有效读数, style_frame 必须给 None 而非编一个状态
        from app.models import regime
        pc = _series(0.001, 100)
        self.assertTrue(regime._pool_state_frame(pc).empty)
        r = style.style_frame(pc, _series(0.0, 100))
        self.assertIsNone(r["pool_state"])
        self.assertIsNone(r["pool_score"])


class TestSeries(unittest.TestCase):
    def test_series_normalized_at_window_start(self):
        pc = _series(0.001, 300)
        bc = _series(0.0005, 300)
        r = style.style_frame(pc, bc, lookback=120)
        self.assertEqual(len(r["series"]), 120)
        self.assertEqual(r["series"][0]["v"], 1.0)
        self.assertGreater(r["series"][-1]["v"], 1.0)   # 池子跑赢 → 曲线抬升

    def test_series_clamped_by_length(self):
        pc = _series(0.001, 300)
        bc = _series(0.000, 300)
        self.assertEqual(len(style.style_frame(pc, bc, lookback=9999)["series"]), 300)


class TestBias(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(style._bias(None), "未知")
        self.assertEqual(style._bias(0.061), "顺风")
        self.assertEqual(style._bias(-0.061), "逆风")
        self.assertEqual(style._bias(0.04), "中性")
        self.assertEqual(style._bias(-0.04), "中性")


class TestLoadStyleDegrade(unittest.TestCase):
    def test_empty_inputs_return_unavailable(self):
        with mock.patch("app.models.pool_index.load_pool_close",
                        return_value=pd.Series(dtype=float)):
            r = style.load_style()
        self.assertFalse(r["available"])
        self.assertIn("池子", r["reason"])


if __name__ == "__main__":
    unittest.main()
