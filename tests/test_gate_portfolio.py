"""组合层档判据 verdict_portfolio 的守卫测试（2026-09-27）。

预注册 `plans/portfolio-admission-gate-prereg-20260927.md`（写死在实现之前）。
门禁第三层：单因子档/打分层 head 过闸 ≠ 组合层可用（nf 全量重训线实证）。
每组判据都带"摘掉必须当场变红"的那条：
  · 摘掉双种子集要求（只看第一套）⇒ 同向性检查失效，混合符号的候选会假 PASS；
  · 摘掉 P2 回撤轴 ⇒ 收益好但回撤崩的候选会假 PASS；
  · 摘掉 P3 换手轴 ⇒ 成本侵蚀型候选会假 PASS。
纯函数测试，不碰生产库。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.alpha import gate  # noqa: E402


def _d(prod, full, mdd_prod=-0.010, mdd_full=-0.015, turnover=0.02):
    """一套种子集的合成读数（本来能通过的量级）"""
    d = {"sys_ann_prod": prod, "sys_ann_full": full,
         "sys_mdd_prod": mdd_prod, "sys_mdd_full": mdd_full}
    if turnover is not None:
        d["turnover"] = turnover
    return d


class TestVerdictPortfolio(unittest.TestCase):
    def test_pass_all_axes(self):
        r = gate.verdict_portfolio([_d(0.025, 0.012), _d(0.022, 0.011)])
        self.assertTrue(r["PASS_portfolio"])
        self.assertEqual(r["verdict"], "PASS")
        self.assertTrue(r["p1_ann"] and r["p2_mdd"] and r["p3_turnover"])

    def test_reject_negative_both_seeds(self):
        r = gate.verdict_portfolio([_d(-0.03, -0.02), _d(-0.01, -0.005)])
        self.assertEqual(r["verdict"], "REJECT")
        self.assertFalse(r["p1_ann"])

    def test_indeterminate_mixed_sign(self):
        """双种子集收益轴异号 ⇒ 不可辨（哪怕一套幅度巨大）"""
        r = gate.verdict_portfolio([_d(0.05, 0.02), _d(-0.01, 0.02)])
        self.assertEqual(r["verdict"], "INDETERMINATE")
        self.assertFalse(r["PASS_portfolio"])

    def test_reject_drawdown_axis(self):
        """收益轴过但回撤劣化超容差 ⇒ REJECT（摘掉 P2 此例会假 PASS）"""
        r = gate.verdict_portfolio([_d(0.03, 0.02, mdd_prod=-0.05, mdd_full=-0.06),
                                    _d(0.025, 0.015, mdd_prod=-0.04, mdd_full=-0.05)])
        self.assertEqual(r["verdict"], "REJECT")
        self.assertTrue(r["p1_ann"])
        self.assertFalse(r["p2_mdd"])

    def test_reject_turnover_axis(self):
        """收益/回撤都过但换手超 +5pp ⇒ REJECT（摘掉 P3 此例会假 PASS）"""
        r = gate.verdict_portfolio([_d(0.03, 0.02, turnover=0.08),
                                    _d(0.025, 0.015, turnover=0.06)])
        self.assertEqual(r["verdict"], "REJECT")
        self.assertFalse(r["p3_turnover"])

    def test_turnover_missing_not_vetoed(self):
        """未测换手不否决（沿 S4 惯例）"""
        r = gate.verdict_portfolio([_d(0.03, 0.02, turnover=None),
                                    _d(0.025, 0.015, turnover=None)])
        self.assertEqual(r["verdict"], "PASS")
        self.assertIsNone(r["p3_turnover"])

    def test_single_seed_set_would_fake_pass(self):
        """红检：第一套种子集单看是 PASS，双种子集要求下必须不给 PASS"""
        good = _d(0.025, 0.012)
        noisy = _d(-0.005, 0.008)
        r = gate.verdict_portfolio([good, noisy])
        self.assertNotEqual(r["verdict"], "PASS")
        self.assertEqual(r["verdict"], "INDETERMINATE")

    def test_requires_two_seed_sets(self):
        with self.assertRaises(ValueError):
            gate.verdict_portfolio([_d(0.03, 0.02)])

    def test_missing_field_raises(self):
        with self.assertRaises(ValueError):
            gate.verdict_portfolio([{"sys_ann_prod": 0.03}, {"sys_ann_prod": 0.03}])

    def test_thresholds_overridable(self):
        """阈值可显式覆写（覆写须在预注册/台账留痕）"""
        r = gate.verdict_portfolio([_d(0.015, 0.008), _d(0.014, 0.008)],
                                   ann_bar_prod=0.010, ann_bar_full=0.005)
        self.assertEqual(r["verdict"], "PASS")


if __name__ == "__main__":
    unittest.main()
