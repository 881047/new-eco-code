"""`PRE_CLOSE_QUANTIZE` 的**机制**测试（2026-09-13）

## 为什么不做成 golden 快照

独立审查 §4.2 建议"把 `PRE_CLOSE_QUANTIZE=False` 那一档加进 golden"。我实测后发现**那条建议
落不了地**：用现有合成夹具跑 True / False 两档，**指标与逐日净值完全相同** —— 夹具从没触发
涨跌停闸门，开关无处生效。补这样一份快照只会得到默认档的复制品，锁不住任何东西。

⇒ 改成**定向测机制**（下面三条）。这同时也是"补快照"这件事的正确替代：快照保的是**某个宇宙
的数值**，而这里保的是**开关为什么存在、以及它修好了什么**。

## 机制（P0-03，2026-09-12 E5 预注册判决通过）

`pv`（pre_close）来自 **float32** 市场矩阵。不量化回分时，
`round2(float32(pv) * (1+ratio))` 会和真值**差一分**（实测只向下）—— 原始缺陷里
**162,414 格**限价取低一分，组合层代价 = 比真值限价高 **+1.07pp/年**（某股连续 5 个一字跌停
被判成"卖得出去"）。量化回分（`round(float(pv), 2)`）后与真值一致。

`PRE_CLOSE_QUANTIZE=False` 只用于**复现修复前的历史读数**（上一档 pin `20260912_fullhist`
= 满仓 23.77% / 系统 20.47%）。
"""
import sys
import unittest
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.backtest import engine as bt                        # noqa: E402
from app.portfolio.board import round2                       # noqa: E402


def _cent_bias_points(lo_cents: int = 100, hi_cents: int = 600) -> list:
    """返回"不量化就差一分"的价位列表。"""
    out = []
    for cents in range(lo_cents, hi_cents):
        pv = cents / 100.0
        p32 = float(np.float32(pv))
        if round2(p32 * 1.1) != round2(round(p32, 2) * 1.1):
            out.append(pv)
    return out


class TestPreCloseQuantizeMechanism(unittest.TestCase):
    def test_flag_defaults_to_true(self):
        self.assertTrue(bt.PRE_CLOSE_QUANTIZE, "生产默认必须是量化回分(修复后口径)")

    def test_float32_pre_close_would_bias_limit_price_by_one_cent(self):
        """开关关掉时会出问题 —— 这条就是"开关为什么存在"的证据。"""
        pts = _cent_bias_points()
        self.assertGreater(len(pts), 10,
                           f"1~6 元区间应有多处一分偏差, 实测 {len(pts)} 处 —— "
                           f"若为 0, 说明 round2 或 float32 行为变了, 整个 P0-03 结论要重审")
        pv = pts[0]
        p32 = float(np.float32(pv))
        self.assertNotEqual(round2(p32 * 1.1), round2(round(p32, 2) * 1.1))

    def test_quantizing_matches_the_true_limit_price(self):
        """量化回分后必须与**真值**（float64 原价）算出的限价一致 —— 这正是修复的内容。"""
        for pv in _cent_bias_points()[:20]:
            with self.subTest(pv=pv):
                p32 = float(np.float32(pv))
                self.assertEqual(round2(round(p32, 2) * 1.1), round2(pv * 1.1))
                self.assertEqual(round2(round(p32, 2) * 0.9), round2(pv * 0.9))


if __name__ == "__main__":
    unittest.main()
