"""动态层「涨停买不进」守卫（2026-09-13）

独立审查抓到的休眠地雷：`monitor.run_monitor` 里 `daily.loc[..., "ts_code"]` —— 而 `daily`
在上面已经 `set_index("ts_code")` ⇒ **KeyError**；同类 bug 本文件 2026-09-08 修过另一处，
只修了一处。当前 `dynamic.enabled=false` 所以休眠，一旦打开每次必崩，且被 `_stage` 的宽
except 吞掉（表现为"监控没信号"而不是报错）。

抽成 `_limit_up_blocked()` 后直接测：① 一字涨停要剔除；② 未涨停不剔；③ **不许再抛 KeyError**。
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.portfolio import monitor                      # noqa: E402


class _NL:
    """NameLookup 桩：名称恒为普通股（不走 ST 分支）。"""

    def name_at(self, code, date):
        return "测试股"


class TestLimitUpBlocked(unittest.TestCase):
    def _daily(self):
        # index = ts_code（与 run_monitor 里的 set_index 之后同形）
        return pd.DataFrame(
            {"pre_close": [10.0, 10.0, 10.0],
             "close": [11.0, 10.99, 10.5]},          # 主板 +10% 涨停 = 11.00
            index=["600000.SH", "600001.SH", "600002.SH"])

    def test_limit_up_is_blocked_and_others_are_not(self):
        got = monitor._limit_up_blocked(self._daily(), "20260914", _NL())
        self.assertIn("600000.SH", got, "一字涨停(收=涨停价)必须剔除")
        self.assertNotIn("600002.SH", got, "未涨停不该被剔除")

    def test_does_not_raise_keyerror(self):
        """原实现取 daily["ts_code"] ⇒ 必抛 KeyError。这条先红后绿。"""
        try:
            monitor._limit_up_blocked(self._daily(), "20260914", _NL())
        except KeyError as e:                            # pragma: no cover - 回归即失败
            self.fail(f"又回到取 daily['ts_code'] 的老路: {e}")


class TestSoldRecentCodes(unittest.TestCase):
    """候选「买回锁」(2026-09-30): 冷却期内有过**卖出成交**的代码, 不得再进候选名单。

    光韵达(300227.SZ, 2026-09)止损次日被方案买回事故的动态层变体: 09-28 止损信号
    让 apply_signals 的候选买入闸打开(`当日有卖出信号`即开), 若该股仍在 top-20 且
    次日恰有别的卖出信号, 就会从候选路径被买回 —— 任一层刚做的卖出决策被自己的
    候选路径推翻。同理覆盖方案层「月度方案调出」后的候选买回。口径=卖出成交
    (paper_trade), 窗口=cooldown_days 个交易日(与同动作冷却同源)。
    """

    def setUp(self):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(
            "CREATE TABLE paper_trade (exec_date TEXT, ts_code TEXT, side TEXT, reason TEXT);"
            "CREATE TABLE trade_cal (cal_date TEXT PRIMARY KEY, is_open INTEGER);")
        days = ["20260907", "20260908", "20260909", "20260910", "20260911",
                "20260914", "20260915", "20260916", "20260917", "20260918"]
        self.conn.executemany("INSERT INTO trade_cal VALUES (?,1)", [(d,) for d in days])
        rows = [("20260917", "600000.SH", "sell", "止损: 较买入成本 -16.0%"),
                ("20260907", "600001.SH", "sell", "月度方案调出"),
                ("20260918", "600002.SH", "buy", "月度方案买入")]
        self.conn.executemany("INSERT INTO paper_trade VALUES (?,?,?,?)", rows)

    def tearDown(self):
        self.conn.close()

    def test_recent_sell_is_locked(self):
        got = monitor._sold_recent_codes(self.conn, "20260918", 5)
        self.assertIn("600000.SH", got, "1 个交易日前止损卖出的必须锁")

    def test_old_sell_is_not_locked(self):
        got = monitor._sold_recent_codes(self.conn, "20260918", 5)
        self.assertNotIn("600001.SH", got, "冷却窗口外的卖出不锁候选")

    def test_buy_rows_are_not_sells(self):
        got = monitor._sold_recent_codes(self.conn, "20260918", 5)
        self.assertNotIn("600002.SH", got)

    def test_window_counts_open_days_with_boundary(self):
        # 0911(五)卖出 → 0918: 开市日 0914..0918 恰 5 个 ⇒ 在窗内(<=5); 0910 卖出 = 6 个 ⇒ 出窗
        self.conn.executemany("INSERT INTO paper_trade VALUES (?,?,?,?)",
                              [("20260911", "600003.SH", "sell", "弱势"),
                               ("20260910", "600004.SH", "sell", "弱势")])
        got = monitor._sold_recent_codes(self.conn, "20260918", 5)
        self.assertIn("600003.SH", got)
        self.assertNotIn("600004.SH", got)


if __name__ == "__main__":
    unittest.main()
