"""限价入参量化(P0-03 / E5)的行为锁 —— 2026-09-12

背景(证据链见 `experiments/01_data/E5_float32_limit.py` 与 `board.round2` docstring):
市场矩阵 `load_market()` 是 **float32**, 而 `round2` 的 1e-9 容差按 float64 标定:

    round2(2.10 × 0.95)                 = 2.00   ← float64, 与交易所公布值一致
    round2(np.float32(2.10) × 0.95)     = 1.99   ← 修复前引擎运行时的值, 低一分

后果不是"数字差一点": 限价被取低 ⇒ `close <= limit_dn + 0.001` 判 False ⇒ **一字跌停
的票被判成卖得出去**(002638.SZ 连续 5 个, 组合层差 1.07pp/年)。

E5 预注册判决 = PASS(开启后与真值臂位级一致), 开关 `PRE_CLOSE_QUANTIZE` 默认已翻开。
本文件的三个锁:
  ① 默认值锁 —— 开关被悄悄改回 False 会红;
  ② 行为锁 —— 半进位格子上, "封死跌停卖不出"在开启态成立、关闭态不成立;
  ③ 窄面锁 —— 非半进位格子上两种口径**同一个结果**(改动面恰恰就是半进位那批)。
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backtest import engine as bt            # noqa: E402
from app.portfolio.board import round2           # noqa: E402

CODE = "002638.SZ"            # 真案里的那只(*ST勤上): 主板 ST 5% 档
D0, D1 = "2020-06-30", "2020-07-01"


def _market(pre_close, close, adj=1.0, vol=1_000_000.0, open_=None):
    """两日 × 一只的最小市场矩阵; 执行日 = 第 2 行(`_exec_side` 从 pos+1 起找)。

    2026-09-28(P1.5 换 pin): 补 `open` 格。撮合口径翻 `FILL_AT_OPEN=True` 后
    `_exec_side` 会读 `m["open"]`, 缺键直接 KeyError —— 本文件建矩阵时没有它,
    换 pin 后 4 个用例全红。**默认 open_ = close**(无日内波动), 故收盘价口径下
    所有既有断言逐位不变。
    """
    dates = pd.to_datetime([D0, D1])
    op = close if open_ is None else open_
    return {
        "close": pd.DataFrame({CODE: [np.nan, close]}, index=dates, dtype=np.float32),
        "pre_close": pd.DataFrame({CODE: [np.nan, pre_close]}, index=dates,
                                  dtype=np.float32),
        "open": pd.DataFrame({CODE: [np.nan, op]}, index=dates, dtype=np.float32),
        "adj": pd.DataFrame({CODE: [1.0, adj]}, index=dates, dtype=np.float32),
        "vol": pd.DataFrame({CODE: [np.nan, vol]}, index=dates, dtype=np.float32),
    }


def _st(code, ds):
    """ST 判定替身: 把执行日全判成 ST ⇒ 主板走 5% 档(真案的口径)。"""
    return True


def _side(m, side, flag, fill_open=False):
    """在指定口径下跑一次 `_exec_side`(pos=0 ⇒ 执行日 = D1)。

    2026-09-28: 撮合口径必须**显式指定**而非继承全局默认。`FILL_AT_OPEN` 自本次
    重钉起默认为 True, 而本文件锁的是"**收盘价**口径下的限价精度"(E5 原始口径);
    若继承默认, 这些锁会在换 pin 当天静默改测另一套口径 —— 那不是"更新基线",
    是把守卫测的东西换掉了。故显式钉 False; 开盘价口径另由
    `TestOpenFillLimit` 覆盖。
    """
    old_q, old_f = bt.PRE_CLOSE_QUANTIZE, bt.FILL_AT_OPEN
    bt.PRE_CLOSE_QUANTIZE = flag
    bt.FILL_AT_OPEN = fill_open
    try:
        return bt._exec_side(m, pd.to_datetime([D0, D1]), 0, CODE, side,
                             st_codes=_st)
    finally:
        bt.PRE_CLOSE_QUANTIZE, bt.FILL_AT_OPEN = old_q, old_f


class TestSwitchDefault(unittest.TestCase):
    def test_default_is_on(self):
        """① 默认值锁: 开关默认必须是 True(E5 判决后的现行口径)。

        改回 False = 回退成"限价比交易所低一分"的口径, 那是**修复前**的行为,
        只允许在复现历史读数时临时置位(见 engine.py 的注释), 不许改默认值。
        """
        self.assertIs(bt.PRE_CLOSE_QUANTIZE, True)

    def test_quantize_recovers_cent_semantics(self):
        """量化回分后与 float64 公式同源(这正是 P2 位级一致的数据前提)。"""
        pv = np.float32(2.10)
        self.assertNotEqual(round2(float(pv) * 0.95), round2(2.10 * 0.95))
        self.assertEqual(round(round(float(pv), 2), 2), 2.10)
        self.assertEqual(round2(round(float(pv), 2) * 0.95), round2(2.10 * 0.95))


class TestHalfCentBoundary(unittest.TestCase):
    """② 行为锁: 2.10 × 0.95 = 1.995 恰好是"半进位"格, 也是 float32 掉一分的格子。"""

    def test_limit_down_fill_is_blocked_when_on(self):
        # close = 2.00 正是**真值**跌停价 ⇒ 封死, 卖不出; 修复后必须拒单
        m = _market(2.10, close=2.00)
        self.assertEqual(_side(m, "sell", True), (None, None, None))

    def test_limit_down_fill_slips_through_when_off(self):
        # 修复前: 引擎自算 dn=1.99 ⇒ "2.00 > 1.991" ⇒ 判可卖并以 2.00 成交(虚高)
        m = _market(2.10, close=2.00)
        got = _side(m, "sell", False)
        self.assertEqual(got[1], 2.0)
        self.assertEqual(got[2], 1)

    def test_limit_up_buy_is_blocked_when_on(self):
        # 对称方向: 真值涨停价 = round2(2.10 × 1.05) = 2.21(不落在半进位格上),
        # 关闭态同样拦得住 —— 记下来是为了说明"只有半进位格会漏"
        m = _market(2.10, close=2.21)
        self.assertEqual(_side(m, "buy", True), (None, None, None))
        self.assertEqual(_side(m, "buy", False), (None, None, None))


class TestNarrowSurface(unittest.TestCase):
    """③ 窄面锁: 非半进位格子上, 两种口径给同一个结果 —— 修的是"半进位那批"。"""

    def test_non_boundary_cell_is_identical(self):
        m = _market(10.00, close=9.00)          # 10.00 × 0.95 = 9.5 → 不触发半进位
        self.assertEqual(_side(m, "sell", True), _side(m, "sell", False))
        m2 = _market(10.00, close=9.60)
        self.assertEqual(_side(m2, "sell", True), _side(m2, "sell", False))
        self.assertEqual(_side(m2, "sell", True)[1], 9.60)

    def test_exactly_representable_prices_are_untouched(self):
        # 10.00 在 float32 里精确可表示 ⇒ 量化回分是恒等变换(修的是表示误差, 不是算法)
        self.assertEqual(float(np.float32(10.00)), 10.00)
        self.assertEqual(_side(_market(10.00, 9.60), "sell", True)[1], 9.6)


class TestSuspensionStillDelays(unittest.TestCase):
    """量化只碰限价，不能碰停牌顺延(顺延与限价是 `_exec_side` 里两条独立分支)。"""

    def test_zero_vol_still_no_fill(self):
        m = _market(2.10, close=2.00, vol=0.0)
        self.assertEqual(_side(m, "sell", True), (None, None, None))
        self.assertEqual(_side(m, "sell", False), (None, None, None))


if __name__ == "__main__":
    unittest.main()
