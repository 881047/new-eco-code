"""Alpha 流水线单测 (2026-09-11 「芯片式异构架构」阶段 1)

重点在 **OOS 判据**(唯一有权改状态的证据)与 **Critic 无权改状态**这两条。
判据一旦被放松, 整条流水线就变成"自动把噪声晋升成生产因子"的机器 ——
本项目已有 4 个「门禁通过却对策略无用」的反例, 其中有的是**在知道全部读数之后**
仍然进了阶段 B 的。所以这里把判据锁死。

运行: ./.venv/Scripts/python.exe -m pytest tests/test_pipeline.py -q
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.alpha import pipeline as P                          # noqa: E402


def _res(aid="a1", prod=(0.20, -0.19), base_prod=(0.19, -0.20),
         full=(0.11, -0.41), base_full=(0.11, -0.42)):
    return {"alpha_id": aid, "windows": {
        "prod": {"cand": {"sys_ann": prod[0], "sys_mdd": prod[1]},
                 "base": {"sys_ann": base_prod[0], "sys_mdd": base_prod[1]}},
        "full": {"cand": {"sys_ann": full[0], "sys_mdd": full[1]},
                 "base": {"sys_ann": base_full[0], "sys_mdd": base_full[1]}}}}


class TestFrozenOOSCriteria(unittest.TestCase):
    """4 个不等式(2 窗 × 2 轴)**全成立**才 PASS; 任何一条不成立都不许替代"""

    def test_all_four_hold(self):
        v = P.judge_oos(_res())
        self.assertTrue(v.passed)
        self.assertIn("4 个不等式", v.reason)

    def test_each_single_inequality_blocks(self):
        """逐条打破每一个不等式, 都必须判否 —— 4 条没有一条是装饰"""
        cases = {
            "prod 年化劣": dict(prod=(0.10, -0.19)),
            "prod 回撤劣": dict(prod=(0.20, -0.30)),
            "full 年化劣": dict(full=(0.05, -0.41)),
            "full 回撤劣": dict(full=(0.11, -0.55)),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                v = P.judge_oos(_res(**kw))
                self.assertFalse(v.passed, "%s 竟然通过了" % name)
                self.assertIn("/4", v.reason)

    def test_missing_window_is_rejected_not_substituted(self):
        """缺一个窗 ⇒ 判否。**不许用另一个窗替代** —— 那正是"看到结果后选窗口\""""
        r = _res()
        del r["windows"]["full"]
        v = P.judge_oos(r)
        self.assertFalse(v.passed)
        self.assertIn("缺窗口", v.reason)

    def test_missing_metric_is_rejected(self):
        r = _res()
        del r["windows"]["prod"]["cand"]["sys_mdd"]
        self.assertFalse(P.judge_oos(r).passed)

    def test_criteria_are_the_two_axes_only(self):
        """判据表里只许有 2 窗 × 2 轴 —— 加第三条要改这里并说明理由"""
        self.assertEqual(tuple(P.OOS_WINDOWS), ("prod", "full"))
        self.assertEqual(tuple(P.OOS_METRICS), ("sys_ann", "sys_mdd"))


class TestCriticHasNoPowerOverStatus(unittest.TestCase):
    """Critic 只写 note。**结构上**没有改状态的路径 —— 不是靠自觉"""

    def test_critic_writes_note_but_never_status(self):
        """跑一遍 Critic: `set_note` 必须被调用, `upsert`/`retire` 必须**一次都没有**"""
        import pandas as pd
        pl = P.Pipeline(trace=False)
        with mock.patch("app.alpha.bank.load_values") as lv, \
             mock.patch("app.alpha.bank.set_note") as sn, \
             mock.patch("app.alpha.bank.upsert") as up, \
             mock.patch("app.alpha.bank.retire") as rt, \
             mock.patch("app.alpha.critic.attack_all") as at, \
             mock.patch("app.alpha.gate.forward_return",
                        return_value=pd.DataFrame()), \
             mock.patch("app.factors.factor_engine.industry_map"):
            lv.return_value = pd.DataFrame()
            at.return_value = {"alpha_id": "a1", "_disclaimer": "读数而已"}
            pl.critic(["a1"], {}, pd.DatetimeIndex([]), None)
        sn.assert_called_once()
        up.assert_not_called()
        rt.assert_not_called()

    def test_pipeline_has_no_status_api_taking_critic_output(self):
        """`commit()` 的入参是 `Verdict` —— Critic 的返回值进不去"""
        import inspect
        sig = inspect.signature(P.Pipeline.commit)
        self.assertEqual(list(sig.parameters), ["self", "verdicts"])
        ann = str(sig.parameters["verdicts"].annotation)
        self.assertIn("Verdict", ann)


class TestCommitRules(unittest.TestCase):
    """状态流转的守卫"""

    def _pipe(self):
        return P.Pipeline(trace=False)

    def test_oos_failure_does_not_retire(self):
        """OOS 不通过 ⇒ 状态不变(POOLED 仍是研究资产), 退役是显式动作"""
        pl = self._pipe()
        with mock.patch("app.alpha.bank.get",
                        return_value={"alpha_id": "a1", "status": "POOLED",
                                      "note": "n"}), \
             mock.patch("app.alpha.bank.upsert") as up:
            df = pl.commit([P.Verdict("a1", False, {}, "否")])
        up.assert_not_called()
        self.assertEqual(df.iloc[0]["to"], "POOLED")
        self.assertIn("状态不变", df.iloc[0]["action"])

    def test_pass_promotes_pooled_to_validated(self):
        pl = self._pipe()
        with mock.patch("app.alpha.bank.get",
                        return_value={"alpha_id": "a1", "status": "POOLED",
                                      "note": "n"}), \
             mock.patch("app.alpha.bank.upsert") as up:
            df = pl.commit([P.Verdict("a1", True, {}, "4 个不等式全部成立")])
        self.assertEqual(df.iloc[0]["to"], "VALIDATED")
        self.assertEqual(up.call_args.kwargs.get("status"), "VALIDATED")

    def test_retired_is_never_auto_revived(self):
        """退役只进不出; 复活必须走**显式的新预注册轮次**, 流水线不代劳"""
        pl = self._pipe()
        with mock.patch("app.alpha.bank.get",
                        return_value={"alpha_id": "a1", "status": "RETIRED",
                                      "note": "n"}), \
             mock.patch("app.alpha.bank.upsert") as up:
            df = pl.commit([P.Verdict("a1", True, {}, "碰巧过了")])
        up.assert_not_called()
        self.assertEqual(df.iloc[0]["to"], "RETIRED")
        self.assertIn("拒绝自动复活", df.iloc[0]["action"])

    def test_oos_without_runner_refuses(self):
        with self.assertRaises(RuntimeError):
            P.Pipeline(trace=False).oos(["a1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
