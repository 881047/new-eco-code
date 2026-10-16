"""Alpha Health / Decay 单测 (2026-09-11 「芯片式异构架构」#37)

这个模块只有一条不可退让的规则, 整个文件都在守它:

> **描述性衰减永远不会触发退役。只有结构性变化会。**

守它的理由是**一整轮实验**: R1 电源门控(按滚动已实现表现路由)被证伪, 因为它等价于
追涨杀跌 —— `die_flow` 2023 年 +6.93%/月 让路由器一路供电, 正好供电进它 2024-26 最差
的那段。所以任何"表现掉 → 退役/降权"的路径都必须**在结构上不存在**, 而不是靠自觉。

运行: ./.venv/Scripts/python.exe -m pytest tests/test_health.py -q
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                                          # noqa: E402
import pandas as pd                                         # noqa: E402

from app.alpha import health as H                           # noqa: E402


def _wide(values, cols=None, index=None):
    idx = index if index is not None else pd.date_range("2020-01-31", periods=len(values),
                                                        freq="ME")
    cols = cols or ["s%02d" % i for i in range(len(values[0]))]
    return pd.DataFrame(values, index=idx, columns=cols)


class TestOnlyStructuralRetires(unittest.TestCase):
    """核心规则: 描述性衰减不许触发退役"""

    def test_decay_alone_is_not_retire_worthy(self):
        rep = H.HealthReport("a1", status="POOLED", reasons=[
            H.Reason(H.DESCRIPTIVE, "decay", "活跃区 head -3.20%/月, 低于登记门槛")])
        self.assertFalse(rep.retire_worthy(),
                         "**描述性衰减触发了退役** —— 这正是 R1 被证伪的那个失败模式")
        self.assertEqual(len(rep.structural()), 0)
        self.assertEqual(len(rep.descriptive()), 1)

    def test_structural_alone_is_retire_worthy(self):
        rep = H.HealthReport("a1", reasons=[
            H.Reason(H.STRUCTURAL, "coverage_break", "覆盖度断崖")])
        self.assertTrue(rep.retire_worthy())

    def test_mixed_report_is_retire_worthy_only_because_of_the_structural_part(self):
        rep = H.HealthReport("a1", reasons=[
            H.Reason(H.DESCRIPTIVE, "decay", "收益掉了"),
            H.Reason(H.STRUCTURAL, "redundancy", "与基线秩相关 0.99")])
        self.assertTrue(rep.retire_worthy())
        self.assertEqual([r.code for r in rep.structural()], ["redundancy"])

    def test_retire_candidates_filters_descriptive_out(self):
        reports = [
            H.HealthReport("decayed", reasons=[H.Reason(H.DESCRIPTIVE, "decay", "掉了")]),
            H.HealthReport("broken", reasons=[H.Reason(H.STRUCTURAL, "coverage_break", "断崖")]),
        ]
        got = [r.alpha_id for r in H.retire_candidates(reports)]
        self.assertEqual(got, ["broken"])

    def test_missing_values_is_descriptive(self):
        """拿不到打分序列 = **不知道**, 不是"结构性变化" ⇒ 不许退役"""
        with mock.patch("app.alpha.bank.get", return_value={"status": "POOLED"}), \
             mock.patch("app.alpha.bank.load_values", return_value=pd.DataFrame()):
            rep = H.audit("a1")
        self.assertFalse(rep.retire_worthy())
        self.assertEqual([r.code for r in rep.descriptive()], ["no_values"])


class TestCoverageBreak(unittest.TestCase):
    """覆盖度断崖探测器 —— 回归对象是 `hk_hold` 2024-08 那件事"""

    def test_detects_the_hk_hold_shape(self):
        """36 个月: 前 24 个月 ~4042 只, 之后 ~793 只(实测区间)"""
        vals = [4042] * 24 + [793] * 12
        brk = H.coverage_break(pd.Series(vals))
        self.assertIsNotNone(brk, "hk_hold 形状的断崖漏检了")
        self.assertAlmostEqual(brk["ratio"], 793 / 4042, places=2)
        self.assertEqual(brk["date"], 24, "断崖点定位错了")

    def test_flat_coverage_is_not_a_break(self):
        self.assertIsNone(H.coverage_break(pd.Series([3000] * 36)))

    def test_gradual_decline_is_not_a_cliff(self):
        """**缓降不是断崖** —— 一个每月稳定下滑的覆盖度是"慢慢变窄", 不是"含义换了"。

        这条划界很重要: 否则任何一次覆盖度下滑都会被当成结构性证据, 退役名单会失控。
        """
        self.assertIsNone(H.coverage_break(pd.Series(np.linspace(4000, 3000, 36))))

    def test_short_series_refuses_to_conclude(self):
        """序列太短 ⇒ 不下结论(而不是勉强报一个)"""
        self.assertIsNone(H.coverage_break(pd.Series([4000] * 5 + [800] * 5)))

    def test_curve_counts_non_nan_per_month(self):
        w = _wide([[1.0, np.nan, 3.0], [np.nan, np.nan, 3.0]])
        c = H.coverage_curve(w)
        self.assertEqual(list(c), [2, 1])


class TestRedundancy(unittest.TestCase):
    """冗余 = 结构性理由(方案 §7: 秩相关 > 0.95 则退役)"""

    def _patch(self, rho):
        return mock.patch("app.alpha.critic.corr_with_known",
                          return_value={"base": rho, "_max_abs": {"factor": "base",
                                                                  "rho": rho}})

    def test_high_correlation_is_structural(self):
        w = _wide([[1.0, 2.0, 3.0]])
        with self._patch(0.99):
            got = H.redundancy(w, {"base": w}, pd.DatetimeIndex(w.index))
        self.assertIsNotNone(got)
        self.assertEqual(got["factor"], "base")

    def test_low_correlation_is_not_flagged(self):
        w = _wide([[1.0, 2.0, 3.0]])
        with self._patch(0.40):
            self.assertIsNone(H.redundancy(w, {"base": w}, pd.DatetimeIndex(w.index)))

    def test_no_others_means_no_verdict(self):
        w = _wide([[1.0, 2.0, 3.0]])
        self.assertIsNone(H.redundancy(w, {}, pd.DatetimeIndex(w.index)))


class TestNoWeightingApi(unittest.TestCase):
    """**结构上**不提供加权/复活接口"""

    def test_weights_refuses(self):
        with self.assertRaises(NotImplementedError) as cm:
            H.weights("a1")
        msg = str(cm.exception)
        self.assertIn("追涨杀跌", msg)
        self.assertIn("R1", msg)

    def test_module_exposes_no_weighting_helper(self):
        """别的地方冒出一个 `weight`/`score`/`rank` 风格的**函数**就该被注意到。

        只看可调用对象 —— `NO_WEIGHTS_REASON` 是个说明为什么没有加权的常量, 它带着
        "weight" 字样正是重点, 不是违例。
        """
        bad = [n for n in dir(H)
               if any(k in n.lower() for k in ("weight", "score", "rank", "boost"))
               and not n.startswith("_") and callable(getattr(H, n))]
        self.assertEqual(bad, ["weights"], "多了加权类接口: %s" % bad)


class TestAuditIsReadOnly(unittest.TestCase):
    """体检**不得**改状态 —— 与 Critic 同一条纪律"""

    def test_audit_writes_nothing(self):
        w = _wide([[float(i), float(10 - i), 1.0] for i in range(1, 25)])
        with mock.patch("app.alpha.bank.get", return_value={"status": "POOLED"}), \
             mock.patch("app.alpha.bank.load_values", return_value=w), \
             mock.patch("app.alpha.bank.upsert") as up, \
             mock.patch("app.alpha.bank.retire") as rt, \
             mock.patch("app.alpha.bank.set_note") as sn:
            rep = H.audit("a1")
        up.assert_not_called()
        rt.assert_not_called()
        sn.assert_not_called()
        self.assertEqual(rep.alpha_id, "a1")
        self.assertEqual(rep.status, "POOLED")

    def test_audit_all_skips_retired(self):
        df = pd.DataFrame({"alpha_id": ["a1", "a2"], "status": ["POOLED", "RETIRED"]})
        w = _wide([[1.0, 2.0, 3.0]])
        with mock.patch("app.alpha.bank.list_alphas", return_value=df), \
             mock.patch("app.alpha.bank.get", return_value={"status": "POOLED"}), \
             mock.patch("app.alpha.bank.load_values", return_value=w):
            got = [r.alpha_id for r in H.audit_all()]
        self.assertEqual(got, ["a1"], "已退役的 alpha 不该再被体检")

    def test_unreadable_alpha_degrades_to_a_note_not_a_crash(self):
        """读不出打分序列 ⇒ 该条降级成 `no_values` **描述性**读数, 且**仍出现在报告里**。

        不把它丢掉是有意的: "这条 alpha 没有存档"本身就是要让人看见的事实 ——
        悄悄跳过会让体检结果看起来比实际更完整。
        """
        df = pd.DataFrame({"alpha_id": ["bad", "good"], "status": ["POOLED", "POOLED"]})
        w = _wide([[1.0, 2.0, 3.0]])
        with mock.patch("app.alpha.bank.list_alphas", return_value=df), \
             mock.patch("app.alpha.bank.get", return_value={"status": "POOLED"}), \
             mock.patch("app.alpha.bank.load_values",
                        side_effect=[RuntimeError("boom"), w]):
            got = {r.alpha_id: r for r in H.audit_all()}
        self.assertEqual(sorted(got), ["bad", "good"])
        self.assertEqual([r.code for r in got["bad"].descriptive()], ["no_values"])
        self.assertFalse(got["bad"].retire_worthy())

    def test_audit_all_survives_an_unexpected_error(self):
        """`audit()` 自己炸了(不是读数缺失, 是代码错)⇒ 跳过这一条, 不拖垮整轮"""
        df = pd.DataFrame({"alpha_id": ["bad", "good"], "status": ["POOLED", "POOLED"]})
        good = H.HealthReport("good", status="POOLED")
        with mock.patch("app.alpha.bank.list_alphas", return_value=df), \
             mock.patch("app.alpha.health.audit",
                        side_effect=[RuntimeError("boom"), good]):
            got = [r.alpha_id for r in H.audit_all()]
        self.assertEqual(got, ["good"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
