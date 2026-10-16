"""实时行情成交价 `_exec_price_open` + 账本实时档接线的守卫测试（2026-09-13）

为什么单测它：周频袖套的 **09:32 开盘档**走的正是这条路径
（`ledger.execute_plan(use_quotes=True)` → `get_quotes` → `_exec_price_open`），
而在此之前**零覆盖** —— 已测的是注入 `fill_fn` 的路径与日线版 `_exec_price`。
首个纸面周期就在 2026-09-14 09:32，这条线上任何一个错价都直接写进账本。

守五条：
  1. 正常：成交价 = 今开 ×(1±slip)，买卖方向相反；
  2. **一字涨停买不进 / 一字跌停卖不出** → `None`（顺延晚间档），反向方向照常成交；
  3. 缺行情/缺今开/缺昨收/价格非正 → `None`（停牌与瞬时失败都走这条）；
  4. **脏数据防护**：今开超出本板块涨跌停带 → `None`（不许把错价当成交价）；
  5. 限幅按板块与**当日**规则：主板 10% / 主板 ST 5%（2026-07-06 前）/ 科创 20% / 北交 30%；
     账本层：实时行情拿不到时**一单不成交**且挂单顺延（不是按错误价硬成交）。

运行: python -m unittest tests.test_exec_price_open -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.paper import engine as pe                      # noqa: E402
from app.weekly.ledger import Ledger                    # noqa: E402

D = "20260914"


def _q(open_, prev_close):
    return {"open": open_, "prev_close": prev_close}


class TestExecPriceOpen(unittest.TestCase):
    def setUp(self):
        # 滑点固定成 0.001，免得断言跟着 config 漂
        self._p = mock.patch.object(pe, "_slip", return_value=0.001)
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_normal_buy_and_sell_apply_slip_in_opposite_directions(self):
        quotes = {"600000.SH": _q(10.0, 10.0)}
        self.assertAlmostEqual(pe._exec_price_open(quotes, "600000.SH", D, "buy"), 10.01, places=3)
        self.assertAlmostEqual(pe._exec_price_open(quotes, "600000.SH", D, "sell"), 9.99, places=3)

    def test_explicit_slip_argument_is_honoured(self):
        """2026-09-14：显式传入的 `slip` 必须被采纳（原先写死 `_slip()`，参数被静默忽略）。

        回测/实验里大量"零滑点臂"会传 `slip=0.0` —— 忽略它等于把无滑点实验偷偷跑成
        有滑点（与 `_exec_price` 的语义也不一致）。红检：把 `slip` 换回 `_slip()` ⇒ 本用例红。
        """
        quotes = {"600000.SH": _q(10.0, 10.0)}
        self.assertAlmostEqual(pe._exec_price_open(quotes, "600000.SH", D, "buy", slip=0.0),
                               10.0, places=3)
        self.assertAlmostEqual(pe._exec_price_open(quotes, "600000.SH", D, "sell", slip=0.0),
                               10.0, places=3)
        self.assertAlmostEqual(pe._exec_price_open(quotes, "600000.SH", D, "buy", slip=0.02),
                               10.2, places=3)

    def test_limit_up_blocks_buy_but_not_sell(self):
        quotes = {"600000.SH": _q(11.0, 10.0)}          # 主板涨停价 = 11.00
        self.assertIsNone(pe._exec_price_open(quotes, "600000.SH", D, "buy"))
        self.assertAlmostEqual(pe._exec_price_open(quotes, "600000.SH", D, "sell"), 10.989, places=3)

    def test_limit_down_blocks_sell_but_not_buy(self):
        quotes = {"600000.SH": _q(9.0, 10.0)}           # 主板跌停价 = 9.00
        self.assertIsNone(pe._exec_price_open(quotes, "600000.SH", D, "sell"))
        self.assertAlmostEqual(pe._exec_price_open(quotes, "600000.SH", D, "buy"), 9.009, places=3)

    def test_missing_quote_fields_defer(self):
        self.assertIsNone(pe._exec_price_open({}, "600000.SH", D, "buy"))
        self.assertIsNone(pe._exec_price_open({"600000.SH": _q(None, 10.0)}, "600000.SH", D, "buy"))
        self.assertIsNone(pe._exec_price_open({"600000.SH": _q(10.0, None)}, "600000.SH", D, "buy"))
        self.assertIsNone(pe._exec_price_open({"600000.SH": _q(0.0, 10.0)}, "600000.SH", D, "buy"))

    def test_dirty_open_beyond_limit_band_is_rejected(self):
        """+20% 的开盘价对主板是脏数据 ⇒ 顺延，不许当成交价（回看 07 号那类脏值事故）。"""
        quotes = {"600000.SH": _q(12.0, 10.0)}
        self.assertIsNone(pe._exec_price_open(quotes, "600000.SH", D, "buy"))
        self.assertIsNone(pe._exec_price_open(quotes, "600000.SH", D, "sell"))

    def test_star_market_has_20pct_band(self):
        ok = {"688111.SH": _q(12.0, 10.0)}              # +20% 恰在带内（涨停 ⇒ 买不进）
        self.assertIsNone(pe._exec_price_open(ok, "688111.SH", D, "buy"))
        self.assertAlmostEqual(pe._exec_price_open(ok, "688111.SH", D, "sell"), 11.988, places=3)
        self.assertIsNone(pe._exec_price_open({"688111.SH": _q(12.5, 10.0)}, "688111.SH", D, "buy"))

    def test_bse_has_30pct_band(self):
        quotes = {"830799.BJ": _q(12.9, 10.0)}          # 北交 ±30% ⇒ 12.9 在带内
        self.assertAlmostEqual(pe._exec_price_open(quotes, "830799.BJ", D, "buy"), 12.913, places=3)
        self.assertIsNone(pe._exec_price_open({"830799.BJ": _q(13.5, 10.0)},
                                              "830799.BJ", D, "buy"))

    def test_main_board_st_band_is_5pct_before_20260706_and_10pct_after(self):
        """ST 档按**当日**规则：2026-07-06 起主板 ST 放宽到 ±10%（10 号文件有实测）。"""
        quotes = {"600000.SH": _q(10.4, 10.0)}          # +4%：两个时代都在带内
        before = pe._exec_price_open(quotes, "600000.SH", "20260501", "buy",
                                     name_fn=lambda c, d: "ST测试")
        self.assertAlmostEqual(before, 10.410, places=3)
        # +6%：5% 档时代是脏数据 ⇒ 顺延；10% 档时代合法
        dirty = {"600000.SH": _q(10.6, 10.0)}
        self.assertIsNone(pe._exec_price_open(dirty, "600000.SH", "20260501", "buy",
                                              name_fn=lambda c, d: "ST测试"))
        self.assertAlmostEqual(pe._exec_price_open(dirty, "600000.SH", D, "buy",
                                                   name_fn=lambda c, d: "ST测试"), 10.611, places=3)


class TestLedgerLiveSlotWiring(unittest.TestCase):
    """账本 `use_quotes=True` 的接线：行情拿不到 ⇒ 一单不成交（顺延），不许按错价成交。"""

    def setUp(self):
        self.path = BASE / "data" / "weekly" / "_test_ledger_live.json"
        if self.path.exists():
            self.path.unlink()
        self.addCleanup(lambda: self.path.exists() and self.path.unlink())
        self.led = Ledger(100_000.0, self.path)
        self.plan = {"rows": [{"ts_code": "600000.SH", "shares": 1000}], "capital": 100_000.0}

    def _run(self, quotes):
        with mock.patch("app.datasource.quote_client.get_quotes", return_value=quotes):
            return self.led.execute_plan(self.plan, D, use_quotes=True)

    def test_no_quotes_means_no_fill_and_position_stays_flat(self):
        done = self._run({})
        self.assertEqual(done, [])
        self.assertEqual(self.led.state["positions"], {})
        self.assertAlmostEqual(self.led.state["cash"], 100_000.0, places=2)

    def test_quotes_present_fills_at_open_with_slip(self):
        done = self._run({"600000.SH": _q(10.0, 10.0)})
        self.assertEqual(len(done), 1)
        self.assertAlmostEqual(done[0]["price"], 10.01, places=3)
        self.assertEqual(self.led.state["positions"]["600000.SH"]["shares"], 1000)


if __name__ == "__main__":
    unittest.main()
