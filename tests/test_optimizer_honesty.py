"""optimizer 不许"说假话"（2026-09-13，独立审查 #8）

optimizer 是框架图里声明的最优路径，但**只有 `feasible_n` 接进了生产**（其余未接线）。
未接线不等于可以不诚实 —— 它照样会被人读、被未来接线。四处"会说假话"：

  A) `framework` 调 `feasible_n` 没传配置费率 ⇒ 按**默认费率**算"该持几只"；
  B) `feasible_n` 被 `cap` 钳住时，`why` 只说"等分 X 元 ⇒ 费率达标"，**隐瞒还能持更多只**；
  C) `Allocation.shares` 硬编码 `lots × 100` ⇒ **科创板（688/689）最低 200 股**规则失效；
  D) `cvxpy` 不可用时回退 topn，等于丢掉协方差/单票/行业三类约束，却不留痕；
     同理 QP 的 diag 不写"风险项用了外部协方差还是单位阵"。

守四条：① capped 时 why 必须写明被钳、并给出按费率的 n；② shares 走 board.lot_shares；
③ 回退必须记 constraints_dropped；④ QP diag 必须有 cov_provided。
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.portfolio import fees as fees_mod                    # noqa: E402
from app.portfolio import optimizer as O                      # noqa: E402


class TestFeasibleNHonesty(unittest.TestCase):
    def test_capped_is_reported(self):
        # 资金足够大到"按费率能持远超 cap 只"
        r = O.feasible_n(50_000_000.0, target_rate=0.005, cap=40)
        self.assertTrue(r["capped_by_policy"])
        self.assertGreater(r["n_by_rate"], 40)
        self.assertEqual(r["n"], 40)
        self.assertIn("钳住", r["why"])
        self.assertIn("策略上限", r["why"])

    def test_not_capped_has_no_clamp_note(self):
        # 2 万（真实账户量级）：默认费率下每只门槛 ≈2232 元 ⇒ n≈8 < cap=40，不该被钳
        r = O.feasible_n(20_000.0, target_rate=0.005, cap=40)
        self.assertFalse(r["capped_by_policy"])
        self.assertNotIn("钳住", r["why"])

    def test_uses_configured_fee_params(self):
        """费率参数必须真的参与计算（传不同参数 ⇒ 门槛金额不同）。"""
        cheap = O.feasible_n(200_000.0, params={"commission_rate": 0.00025, "min_commission": 1.0,
                                                "stamp_tax": 0.0005, "stamp_tax_before": 0.001,
                                                "transfer_fee": 0.00001})
        pricey = O.feasible_n(200_000.0, params={"commission_rate": 0.003, "min_commission": 5.0,
                                                 "stamp_tax": 0.0005, "stamp_tax_before": 0.001,
                                                 "transfer_fee": 0.00001})
        self.assertGreater(pricey["min_amount"], cheap["min_amount"])
        self.assertLessEqual(pricey["n"], cheap["n"])


class TestSharesFollowBoardRule(unittest.TestCase):
    def test_star_board_minimum_200(self):
        a = O.Allocation(pd.Series(dtype="float64"), lots=pd.Series({"688111.SH": 1}))
        self.assertEqual(int(a.shares["688111.SH"]), 0, "科创板 1 手(100 股)买不到 ⇒ 应为 0")
        b = O.Allocation(pd.Series(dtype="float64"), lots=pd.Series({"688111.SH": 2}))
        self.assertEqual(int(b.shares["688111.SH"]), 200)

    def test_main_board_is_100_per_lot(self):
        a = O.Allocation(pd.Series(dtype="float64"), lots=pd.Series({"600000.SH": 3}))
        self.assertEqual(int(a.shares["600000.SH"]), 300)


class TestFallbackLeavesTraces(unittest.TestCase):
    def test_cvxpy_missing_records_dropped_constraints(self):
        sc = pd.Series({"600000.SH": 1.0, "000001.SZ": 0.9, "300750.SZ": 0.8})
        orig = O._cvxpy
        O._cvxpy = lambda: None                       # 模拟缺 cvxpy
        try:
            a = O.optimize(sc, n=2)
        finally:
            O._cvxpy = orig
        self.assertEqual(a.diag.get("fallback"), "cvxpy_unavailable")
        self.assertIn("max_weight", a.diag.get("constraints_dropped", []))


if __name__ == "__main__":
    unittest.main()
