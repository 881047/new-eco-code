"""执行窗口（exec_window）分支守卫（2026-09-28，预注册 plans/exec-window-ab-prereg-20260928.md）

`app/backtest/engine.py::_exec_side` 新增 `exec_window` 形参后的四条不变量：

1. **缺省（空串）必须沿用全局 `FILL_AT_OPEN` 开关** —— 这是「不换 pin」的硬前提，
   由 G0（缺省 vs 显式 open 逐位一致）在生产回测上保证，这里用合成矩阵钉住分支本身。
2. **VWAP 的量纲**：`amount` 单位千元、`vol` 单位手 ⇒ 价 = `amount*10/vol`。
3. **封板判定随窗口换**：`vwap*` 用**全天封死**（买 low≥涨停 / 卖 high≤跌停），
   不是拿 VWAP 自己跟限价比（那会漏判盘中封死 = 一次改两个变量）。
4. **未注入尾盘表 ⇒ 该日顺延**（不是用别的价兜底）；未知窗口名 ⇒ 直接抛错。
"""
import unittest

import numpy as np
import pandas as pd

from app.backtest import engine as E


DATES = pd.DatetimeIndex(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
CODE = "000001.SZ"


def _mk(m):
    return pd.DataFrame(m, index=DATES, columns=[CODE], dtype=float)


def _market(low=9.0, high=11.0, amount=1000.0):
    """pre_close=10 ⇒ 主板 ±10% ⇒ 涨停 11.00 / 跌停 9.00；amount=1000 千元、vol=1000 手
    ⇒ 全日 VWAP = 1000*10/1000 = 10.00（与收盘价同）。"""
    return {
        "close": _mk([[10.0]] * 4),
        "open": _mk([[9.5]] * 4),
        "high": _mk([[high]] * 4),
        "low": _mk([[low]] * 4),
        "pre_close": _mk([[10.0]] * 4),
        "vol": _mk([[1000.0]] * 4),
        "amount": _mk([[amount]] * 4),
        "adj": _mk([[1.0]] * 4),
    }


class TestExecWindowBranch(unittest.TestCase):
    def setUp(self):
        self._old_open = E.FILL_AT_OPEN
        self._old_tail = E._MIN60_TAIL
        E.set_min60_tail(None)

    def tearDown(self):
        E.FILL_AT_OPEN = self._old_open
        E._MIN60_TAIL = self._old_tail

    def test_default_empty_follows_global_switch(self):
        """空串 ⇒ 沿用全局开关：True 用开盘价、False 用收盘价（缺省路径逐位不变的前提）。"""
        m = _market()
        E.FILL_AT_OPEN = True
        px_adj, px_raw, i = E._exec_side(m, DATES, 0, CODE, "buy")
        self.assertAlmostEqual(px_raw, 9.5)          # 开盘价
        self.assertEqual(i, 1)
        E.FILL_AT_OPEN = False
        px_adj, px_raw, i = E._exec_side(m, DATES, 0, CODE, "buy")
        self.assertAlmostEqual(px_raw, 10.0)         # 收盘价

    def test_explicit_window_overrides_global_switch(self):
        """显式参数优先于全局开关（否则 A/B 测不出窗口差异）。"""
        m = _market()
        E.FILL_AT_OPEN = False
        _, px_raw, _ = E._exec_side(m, DATES, 0, CODE, "buy", exec_window="open")
        self.assertAlmostEqual(px_raw, 9.5)
        E.FILL_AT_OPEN = True
        _, px_raw, _ = E._exec_side(m, DATES, 0, CODE, "buy", exec_window="close")
        self.assertAlmostEqual(px_raw, 10.0)

    def test_vwap_scale_is_amount_times_ten_over_vol(self):
        """量纲自检：amount 千元 / vol 手 ⇒ ×10。改 amount 一半 ⇒ VWAP 减半。"""
        m = _market()
        _, px_raw, _ = E._exec_side(m, DATES, 0, CODE, "buy", exec_window="vwap")
        self.assertAlmostEqual(px_raw, 10.0)
        m2 = _market(amount=500.0)
        _, px_raw2, _ = E._exec_side(m2, DATES, 0, CODE, "buy", exec_window="vwap")
        self.assertAlmostEqual(px_raw2, 5.0)

    def test_vwap_uses_all_day_limit_not_its_own_price(self):
        """全天封死：VWAP=10 < 涨停 11，但 low=11（全天封死涨停）⇒ 买不进 ⇒ 顺延。

        若沿用「拿成交价跟限价比」的旧判定，VWAP 10 会被判成可买 —— 那是一次改两个变量。
        """
        m_ok = _market(low=9.0)          # 未封死
        _, px, i = E._exec_side(m_ok, DATES, 0, CODE, "buy", exec_window="vwap")
        self.assertIsNotNone(px)
        self.assertEqual(i, 1)

        m_sealed = _market(low=11.0)     # low ≥ 涨停 ⇒ 全天封死
        out = E._exec_side(m_sealed, DATES, 0, CODE, "buy", exec_window="vwap")
        self.assertEqual(out, (None, None, None))

    def test_vwap_tail_defers_when_not_injected(self):
        """未注入尾盘表 ⇒ 该日不可成交（顺延），不得用收盘价/别的价兜底。"""
        m = _market()
        out = E._exec_side(m, DATES, 0, CODE, "buy", exec_window="vwap_tail")
        self.assertEqual(out, (None, None, None))

    def test_vwap_tail_uses_injected_price(self):
        d = DATES[1].strftime("%Y%m%d")
        E.set_min60_tail({(CODE, d): 10.4})
        try:
            m = _market()
            _, px_raw, i = E._exec_side(m, DATES, 0, CODE, "buy", exec_window="vwap_tail")
            self.assertAlmostEqual(px_raw, 10.4)
            self.assertEqual(i, 1)
        finally:
            E.set_min60_tail(None)

    def test_vwap_tail_quantized_to_cent(self):
        """与 open 分支同源：矩阵 float32 ⇒ 量化回分。"""
        d = DATES[1].strftime("%Y%m%d")
        E.set_min60_tail({(CODE, d): 10.456789})
        try:
            m = _market()
            _, px_raw, _ = E._exec_side(m, DATES, 0, CODE, "buy", exec_window="vwap_tail")
            self.assertAlmostEqual(px_raw, 10.46)
        finally:
            E.set_min60_tail(None)

    def test_unknown_window_raises(self):
        m = _market()
        with self.assertRaises(ValueError):
            E._exec_side(m, DATES, 0, CODE, "buy", exec_window="noon")


if __name__ == "__main__":
    unittest.main()
