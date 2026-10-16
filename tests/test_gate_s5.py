"""S5 时效轴（IC 半衰期）的守卫测试（2026-09-27）。

预注册 `plans/s5-half-life-prereg-20260927.md`（跑数前冻结）。
每组判据都带"摘掉必须当场变红"的那条：
  · 摘掉"半衰期 < 2 判否" ⇒ M1 型假阳性（存在性显著但 α 只活一期）会假 PASS；
  · 摘掉"IC_0 NaN/0 判否(fail-closed)" ⇒ 数据残缺的候选会静默放行（2026-09-12
    "基线读不到反而放行"同类坑）；
  · 摘掉"锚点不足记未测" ⇒ 短面板候选会被噪声半衰期误杀。
ic_by_lag 的 L=0 与 evaluate() 的 ic_full 机器同源，另有逐位一致性测试自证。
纯函数/合成面板测试，不碰生产库。
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.alpha import gate  # noqa: E402


def _curve(pairs, n=40):
    """{L: (ic, n)} 造数辅助：pairs = {L: ic}"""
    return {L: (ic, n) for L, ic in pairs.items()}


class TestHalfLifeFromCurve(unittest.TestCase):
    def test_cross_at_2_passes(self):
        """边缘恰在 L=2 跨线 ⇒ HL=2 ≥ bar ⇒ 通过（2× 调仓周期的判据语义）"""
        r = gate.half_life_from_curve(_curve({0: 0.05, 1: 0.03, 2: 0.024, 3: 0.01,
                                              4: 0.005, 5: 0.002}))
        self.assertEqual(r["half_life_months"], 2.0)
        self.assertFalse(r["censored"])
        self.assertTrue(r["s5_half_life"])

    def test_cross_at_1_vetoed(self):
        """M1 型：首期后立刻腰斩 ⇒ HL=1 < 2 ⇒ 判否（摘掉此判据此例会假 PASS）"""
        r = gate.half_life_from_curve(_curve({0: 0.05, 1: 0.02, 2: 0.01,
                                              3: 0.005, 4: 0.002, 5: 0.001}))
        self.assertEqual(r["half_life_months"], 1.0)
        self.assertFalse(r["s5_half_life"])

    def test_never_crosses_censored_passes(self):
        r = gate.half_life_from_curve(_curve({0: 0.05, 1: 0.048, 2: 0.045,
                                              3: 0.04, 4: 0.038, 5: 0.03}))
        self.assertEqual(r["half_life_months"], 6.0)
        self.assertTrue(r["censored"])
        self.assertTrue(r["s5_half_life"])

    def test_ic0_nan_fail_closed(self):
        """IC_0 NaN ⇒ HL=0 判否（fail-closed，不许静默放行）"""
        r = gate.half_life_from_curve(_curve({0: np.nan, 1: 0.03, 2: 0.02,
                                              3: 0.01, 4: 0.01, 5: 0.01}))
        self.assertEqual(r["half_life_months"], 0.0)
        self.assertFalse(r["s5_half_life"])

    def test_ic0_zero_fail_closed(self):
        r = gate.half_life_from_curve(_curve({0: 0.0, 1: 0.03, 2: 0.02,
                                              3: 0.01, 4: 0.01, 5: 0.01}))
        self.assertEqual(r["half_life_months"], 0.0)
        self.assertFalse(r["s5_half_life"])

    def test_mid_curve_nan_unmeasurable(self):
        """非首 lag NaN ⇒ HL=None（不可测），s5=None 不否决"""
        r = gate.half_life_from_curve(_curve({0: 0.05, 1: 0.04, 2: np.nan,
                                              3: 0.01, 4: 0.01, 5: 0.01}))
        self.assertIsNone(r["half_life_months"])
        self.assertIsNone(r["s5_half_life"])

    def test_insufficient_anchors_unmeasured(self):
        """锚点不足 ⇒ s5=None（未测不否决），HL 读数照报"""
        r = gate.half_life_from_curve(_curve({0: 0.05, 1: 0.02, 2: 0.01,
                                              3: 0.005, 4: 0.002, 5: 0.001}, n=10))
        self.assertEqual(r["half_life_months"], 1.0)
        self.assertIsNone(r["s5_half_life"])

    def test_bar_overridable_with_trace(self):
        r = gate.half_life_from_curve(_curve({0: 0.05, 1: 0.03, 2: 0.024, 3: 0.01,
                                              4: 0.005, 5: 0.002}), hl_bar=3.0)
        self.assertFalse(r["s5_half_life"])
        self.assertEqual(r["hl_bar"], 3.0)

    def test_sign_invariant(self):
        """|IC| 口径：反向因子同样按时效判定（排除依据也会衰减）"""
        a = gate.half_life_from_curve(_curve({0: -0.05, 1: -0.02, 2: -0.01,
                                              3: -0.005, 4: -0.002, 5: -0.001}))
        b = gate.half_life_from_curve(_curve({0: 0.05, 1: 0.02, 2: 0.01,
                                              3: 0.005, 4: 0.002, 5: 0.001}))
        self.assertEqual(a["half_life_months"], b["half_life_months"])
        self.assertFalse(a["s5_half_life"])


class TestVerdictSingleS5(unittest.TestCase):
    def test_backward_compat_no_s5_arg(self):
        """不传 half_life_months 的既有调用：S1-S4 判决逐位不变，s5=None 不否决"""
        r = gate.verdict_single(0.005, 3.0, 0.004, 0.5)
        self.assertTrue(r["PASS_single"])
        self.assertIsNone(r["s5_half_life"])
        self.assertTrue(r["s1_head"] and r["s2_t"] and r["s3_same_sign"])

    def test_s5_pass_keeps_pass(self):
        r = gate.verdict_single(0.005, 3.0, 0.004, 0.5, half_life_months=3.0)
        self.assertTrue(r["PASS_single"])
        self.assertTrue(r["s5_half_life"])

    def test_s5_short_half_life_vetoes(self):
        """红检：S1-S4 全过但 HL=1 ⇒ 不给 PASS（摘掉 S5 此例会假 PASS）"""
        r = gate.verdict_single(0.005, 3.0, 0.004, 0.5, half_life_months=1.0)
        self.assertTrue(r["s1_head"] and r["s2_t"] and r["s3_same_sign"])
        self.assertFalse(r["PASS_single"])

    def test_s5_insufficient_anchors_none_not_vetoed(self):
        """S5 未测（None）不否决：判据退化为 S1-S4"""
        r = gate.verdict_single(0.005, 3.0, 0.004, 0.5, half_life_months=None)
        self.assertTrue(r["PASS_single"])
        self.assertIsNone(r["s5_half_life"])

    def test_s5_hl_bar_overridable(self):
        r = gate.verdict_single(0.005, 3.0, 0.004, 0.5,
                                half_life_months=2.5, hl_bar=3.0)
        self.assertFalse(r["PASS_single"])


class TestIcByLag(unittest.TestCase):
    """合成面板：IC_0 与 evaluate() 逐位一致 + lag 语义正确性 + 端到端接线。"""

    def _panel(self, n_dates=6, n_stocks=250, seed=7):
        rng = np.random.default_rng(seed)
        dates = pd.DatetimeIndex(["2021-01-29", "2021-02-26", "2021-03-31",
                                  "2021-04-30", "2021-05-31", "2021-06-30"])
        codes = [f"S{i:03d}" for i in range(n_stocks)]
        fwd = pd.DataFrame(rng.normal(0, 0.02, (n_dates, n_stocks)),
                           index=dates, columns=codes)
        factor = pd.DataFrame(rng.normal(0, 1, (n_dates, n_stocks)),
                              index=dates, columns=codes)
        factor.iloc[0, :5] = np.nan            # 一些缺口
        factor.iloc[2, 5:10] = np.nan
        tradable = pd.DataFrame(True, index=dates, columns=codes)
        tradable.iloc[1, :10] = False          # 一些不可交易
        tradable.iloc[4, 10:15] = False
        return dates, codes, factor, tradable, fwd

    def test_lag0_matches_evaluate(self):
        """L=0 与 gate.evaluate 的 ic_full 逐位一致（机器同源自证，预注册 §二）"""
        dates, codes, factor, tradable, fwd = self._panel()
        zones = gate.zone_of(dates)
        mine = gate.ic_by_lag(factor, tradable, fwd, dates, zones, lags=(0,))
        pm = factor.where(tradable)
        det = gate.evaluate(pm, fwd, dates, zones)
        self.assertEqual(len(mine[0]), len(det))
        np.testing.assert_allclose(mine[0].to_numpy(),
                                   det["ic_full"].to_numpy(), atol=1e-12)

    def test_lag_uses_older_row(self):
        """lag=L 确实用 t_{i−L} 的因子行：fwd 各行相同 + 信号只在一行 ⇒
        IC_0 在该行锚点 =1，IC_1 在下一锚点 =1（且 IC_0 在下一锚点缺失）"""
        dates, codes, factor, tradable, fwd = self._panel()
        zones = gate.zone_of(dates)
        fwd_same = pd.DataFrame(np.tile(fwd.iloc[3].to_numpy(), (len(dates), 1)),
                                index=dates, columns=codes)
        perfect = pd.DataFrame(np.nan, index=dates, columns=codes)
        perfect.iloc[3] = fwd_same.iloc[3].rank()
        cur = gate.ic_by_lag(perfect, tradable, fwd_same, dates, zones, lags=(0, 1))
        self.assertAlmostEqual(cur[0].loc[dates[3]], 1.0, places=6)
        self.assertNotIn(dates[4], cur[0].index)      # 当期无信号 ⇒ IC_0 缺失
        self.assertAlmostEqual(cur[1].loc[dates[4]], 1.0, places=6)  # 晚一期执行仍有

    def test_persistent_signal_stable_across_lags(self):
        """持续信号（每期同一截面信号 + 微漂移，fwd 与之强相关）⇒ 各 lag IC 同量级"""
        dates, codes, factor, tradable, fwd = self._panel()
        zones = gate.zone_of(dates)
        rng = np.random.default_rng(11)
        sig = factor.iloc[0].fillna(0.0)
        fwd_p = pd.DataFrame(rng.normal(0, 0.5, (len(dates), len(codes))),
                             index=dates, columns=codes) + sig
        persistent = factor.copy()
        for i in range(len(dates)):
            persistent.iloc[i] = factor.iloc[0] + 0.01 * i
        cur = gate.ic_by_lag(persistent, tradable, fwd_p, dates, zones,
                             lags=(0, 1, 2))
        self.assertLess(abs(cur[2].mean() - cur[0].mean()), 0.15)

    def test_min_names_gate_skips(self):
        """交集 < MIN_NAMES 的锚点跳过（不是 0 塞入）"""
        dates, codes, factor, tradable, fwd = self._panel(n_stocks=150)  # < 200
        zones = gate.zone_of(dates)
        cur = gate.ic_by_lag(factor, tradable, fwd, dates, zones, lags=(0,))
        self.assertEqual(len(cur[0]), 0)

    def test_end_to_end_half_life_flow(self):
        """管线端到端：曲线 → half_life_from_curve → verdict_single"""
        dates, codes, factor, tradable, fwd = self._panel()
        zones = gate.zone_of(dates)
        rng = np.random.default_rng(11)
        sig = factor.iloc[0].fillna(0.0)
        fwd_p = pd.DataFrame(rng.normal(0, 0.5, (len(dates), len(codes))),
                             index=dates, columns=codes) + sig
        persistent = factor.copy()
        for i in range(len(dates)):
            persistent.iloc[i] = factor.iloc[0] + 0.01 * i
        cur = gate.ic_by_lag(persistent, tradable, fwd_p, dates, zones)
        ic_dead = {L: (float(s.mean()), len(s)) for L, s in cur.items()}
        hl = gate.half_life_from_curve(ic_dead)
        v = gate.verdict_single(0.005, 3.0, 0.004, 0.5,
                                half_life_months=hl["half_life_months"])
        # 持续信号半衰期应删失(≥6)；与 verdict_single 接线后仍 PASS
        self.assertGreaterEqual(hl["half_life_months"], 2.0)
        self.assertTrue(hl["censored"])
        self.assertTrue(v["PASS_single"])


if __name__ == "__main__":
    unittest.main()
