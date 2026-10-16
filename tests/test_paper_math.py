"""模拟盘总收益口径数学性质单测 (P0-3 回归, 审查发现修复)"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.paper.engine import _merge_entry_adj


class TestEntryAdjMerge(unittest.TestCase):
    def test_share_weighted_harmonic(self):
        # 100股@10元 ea=1.0 持有, 期间 10送10(因子2.0) 后加仓 100股 → ea 应为 1.333, 而非 1.2
        ea = _merge_entry_adj(100, 1.0, 100, 2.0)
        self.assertAlmostEqual(ea, 200 / (100 / 1.0 + 100 / 2.0), places=6)  # =1.3333
        self.assertAlmostEqual(ea, 1.3333, places=3)

    def test_valuation_additivity(self):
        # 性质: Σ s_i×price×adj/ea_i = S×price×adj/ea_merged 在任意 adj 下成立
        ea = _merge_entry_adj(100, 1.0, 100, 2.0)
        for adj in (1.0, 2.0, 3.7):
            lhs = 100 * adj / 1.0 + 100 * adj / 2.0
            rhs = 200 * adj / ea
            self.assertAlmostEqual(lhs, rhs, places=9)

    def test_same_ea_unchanged(self):
        self.assertAlmostEqual(_merge_entry_adj(100, 1.5, 300, 1.5), 1.5, places=9)

    def test_tr_continuity_through_ex_div(self):
        # 除权日连续性: prev_close×adj_prev = close×adj_new(调整后价格连续) ⇒ 估值不跳变
        prev_close, adj_prev = 10.0, 1.0
        # 分红0.5: 价跌到9.5, adj 升到 10/9.5
        close_new, adj_new = 9.5, 10.0 / 9.5
        ea = 1.0
        v_before = 100 * prev_close * (adj_prev / ea)
        v_after = 100 * close_new * (adj_new / ea)
        self.assertAlmostEqual(v_before, v_after, places=9)


if __name__ == "__main__":
    unittest.main()
