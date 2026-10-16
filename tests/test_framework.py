"""框架接线盒单测 (2026-09-11 「芯片式异构架构」#38)

这个模块唯一的主张是**它自己什么也不做**。整个文件都在守这一句:

> **全关时对生产链路是严格的 no-op; 开着时任何阶段的失败也不会传播。**

对一个"接线盒"来说这是全部价值 —— 一个会悄悄改生产表、或者会把异常泄进月度链路的
接线盒, 比没有接线盒坏得多。所以这里的测试大多在**证伪**它, 而不是证真它:
默认值关不关、有没有 import、阶段炸了会不会拖垮别的、会不会偷偷退役 alpha。

运行: ./.venv/Scripts/python.exe -m pytest tests/test_framework.py -q
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np                                          # noqa: E402
import pandas as pd                                         # noqa: E402

from app import config as cfg_mod                            # noqa: E402
from app import framework as FW                              # noqa: E402
from app.alpha import health as H                            # noqa: E402
from app.alpha import pipeline as P                          # noqa: E402


def _cfg(**stages) -> dict:
    """造一份"总开关开、只有点名的阶段开"的配置"""
    all_st = {s: False for s in FW.STAGES}
    all_st.update(stages)
    return {"framework": {"enabled": True, "stages": all_st}}


class TestAllOffIsANoOp(unittest.TestCase):
    """**头号不变量**: 默认全关, 且关的时候真的什么都不做"""

    def test_default_config_has_the_master_switch_and_every_stage_off(self):
        fw = cfg_mod.DEFAULT_CONFIG["framework"]
        self.assertFalse(fw["enabled"],
                         "框架总开关被默认打开了 —— 这等于未经拍板就改生产行为")
        on = [s for s, v in fw["stages"].items() if v]
        self.assertEqual(on, [], "这些阶段被默认打开了: %s" % on)

    def test_stages_tuple_agrees_with_the_stage_table_and_the_default_config(self):
        """三处必须一致 —— 否则有人"顺手"加一个阶段时不会有任何提示"""
        self.assertEqual(sorted(FW.STAGES), sorted(FW._STAGE_FUNCS))
        self.assertEqual(sorted(FW.STAGES),
                         sorted(cfg_mod.DEFAULT_CONFIG["framework"]["stages"]))

    def test_master_switch_dominates_the_sub_switches(self):
        """总开关关 ⇒ 子开关全开也不跑(防止"我只翻了一个子开关"造成意外)"""
        self.assertEqual(FW.enabled_stages(_cfg_with_master(False, health=True)), [])

    def test_missing_framework_key_means_off_not_crash(self):
        """老 config.yaml / 缺键 ⇒ 视为关闭, 不是 KeyError"""
        self.assertEqual(FW.enabled_stages({}), [])
        self.assertEqual(FW.enabled_stages({"framework": {}}), [])

    def test_run_stages_never_touches_the_context_when_off(self):
        """**上下文是惰性的** —— 全关时连行情面板都不该加载(加载要几分钟到几十分钟)"""
        calls = []

        def ctx():
            calls.append(1)
            return {}

        out = FW.run_stages({"framework": {"enabled": False}}, ctx=ctx)
        self.assertEqual(calls, [], "全关时还是去构造了上下文 —— 月链会白等几十分钟")
        self.assertEqual(out, {"enabled": [], "ran": {}, "failed": {}})

    def test_run_stages_imports_no_stage_module_when_off(self):
        """在**全新进程**里验证: 全关跑一遍, 那些阶段模块一个都不许进 `sys.modules`。

        用子进程是因为 pytest 进程里这些模块早就被别人 import 过了, 在进程内断言
        `x not in sys.modules` 会永远通过 —— 那种测试是假的。
        """
        mods = ("app.alpha.health", "app.alpha.pipeline", "app.models.fusion",
                "app.portfolio.optimizer", "app.alpha.factory")
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "from app import framework as FW\n"
            "FW.run_stages({'framework': {'enabled': False}}, ctx=lambda: {})\n"
            "hit = [m for m in %r if m in sys.modules]\n"
            "print('IMPORTED:' + ','.join(hit))\n" % (str(BASE), mods)
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, encoding="utf-8", timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        line = [l for l in r.stdout.splitlines() if l.startswith("IMPORTED:")][0]
        self.assertEqual(line, "IMPORTED:",
                         "全关时仍 import 了阶段模块: %s" % line)


def _cfg_with_master(master: bool, **stages) -> dict:
    c = _cfg(**stages)
    c["framework"]["enabled"] = master
    return c


class TestFailuresDoNotSpread(unittest.TestCase):
    """**第二条不变量**: 阶段炸了不影响别人, 更不影响月链"""

    def test_a_failing_stage_is_recorded_not_raised(self):
        cfg = _cfg(health=True)
        with mock.patch.dict(FW._STAGE_FUNCS,
                             {"health": mock.Mock(side_effect=RuntimeError("boom"))}):
            out = FW.run_stages(cfg, ctx={})       # 不抛 = 通过
        self.assertNotIn("health", out["ran"])
        self.assertIn("boom", out["failed"]["health"])

    def test_one_failure_does_not_stop_the_stages_after_it(self):
        cfg = _cfg(factory=True, health=True)
        good = mock.Mock(return_value={"ok": 1})
        with mock.patch.dict(FW._STAGE_FUNCS,
                             {"factory": mock.Mock(side_effect=RuntimeError("boom")),
                              "health": good}):
            out = FW.run_stages(cfg, ctx={})
        self.assertEqual(sorted(out["ran"]), ["health"], "失败把后面的阶段也带死了")
        self.assertEqual(good.call_count, 1)

    def test_a_broken_context_is_itself_reported_not_raised(self):
        """上下文构造失败(加载行情面板出错)⇒ 记成 `_ctx` 失败, 阶段全部跳过"""
        def ctx():
            raise RuntimeError("面板读不出来")

        with mock.patch.dict(FW._STAGE_FUNCS, {"health": mock.Mock()}):
            out = FW.run_stages(_cfg(health=True), ctx=ctx)
        self.assertEqual(out["ran"], {})
        self.assertIn("_ctx", out["failed"])

    def test_a_declared_stage_with_no_implementation_is_reported(self):
        """开关与实现表不一致时, 必须**报出来**而不是静默跳过"""
        cfg = _cfg()
        cfg["framework"]["stages"]["ghost"] = True
        # STAGES 是模块级白名单, 伪造不进 enabled_stages; 直接绕过去测 run_stages 的防御
        with mock.patch.object(FW, "enabled_stages", return_value=["ghost"]):
            out = FW.run_stages(cfg, ctx={})
        self.assertEqual(out["failed"], {"ghost": "没有实现"})

    def test_trace_failure_does_not_break_the_run(self):
        """留痕写不出去(磁盘满/权限)⇒ 吞掉, 返回值照旧"""
        with mock.patch.object(FW, "TRACE_DIR", Path(os.devnull) / "nope"), \
             mock.patch.dict(FW._STAGE_FUNCS, {"health": mock.Mock(return_value={})}):
            out = FW.run_stages(_cfg(health=True), ctx={})
        self.assertEqual(sorted(out["ran"]), ["health"])


class TestHealthStageDoesNotAutoRetire(unittest.TestCase):
    """健康阶段**只出候选** —— 自动退役是这条流水线上最危险的动作"""

    def _run(self, reports):
        with mock.patch("app.alpha.health.audit_all", return_value=reports), \
             mock.patch("app.alpha.bank.retire") as rt, \
             mock.patch("app.alpha.bank.upsert") as up:
            out = FW.run_stages(_cfg(health=True), ctx={})
        return out, rt, up

    def test_decayed_alpha_is_reported_but_not_a_candidate(self):
        rep = H.HealthReport("decayed",
                             reasons=[H.Reason(H.DESCRIPTIVE, "decay", "掉了")])
        out, rt, up = self._run([rep])
        rt.assert_not_called()
        up.assert_not_called()
        got = out["ran"]["health"]
        self.assertEqual(got["auto_retired"], 0)
        self.assertEqual(got["candidates"], [], "描述性衰减进了退役候选 —— R1 的失败模式")
        self.assertEqual(got["descriptive_only"], ["decayed"])

    def test_structural_break_is_only_a_candidate_never_an_action(self):
        rep = H.HealthReport("broken",
                             reasons=[H.Reason(H.STRUCTURAL, "coverage_break", "断崖")])
        out, rt, up = self._run([rep])
        rt.assert_not_called()
        up.assert_not_called()
        self.assertEqual(out["ran"]["health"]["candidates"], ["broken"])
        self.assertEqual(out["ran"]["health"]["auto_retired"], 0)


class TestOptimizerStageDoesNotTouchThePlan(unittest.TestCase):
    """优化器阶段**只解出持仓数上界**, 不落方案 —— 方案归 builder"""

    def test_unconfigured_capital_is_skipped(self):
        out = FW.run_stages({**_cfg(optimizer=True),
                             "framework": {**_cfg(optimizer=True)["framework"],
                                           "investable": 0}}, ctx={})
        self.assertIn("skipped", out["ran"]["optimizer"])

    def test_it_solves_the_position_count_but_writes_nothing(self):
        cfg = _cfg(optimizer=True)
        cfg["framework"]["investable"] = 12_000
        with mock.patch("app.portfolio.builder.build_plan") as bp:
            out = FW.run_stages(cfg, ctx={})
        bp.assert_not_called()
        self.assertEqual(out["ran"]["optimizer"]["feasible_n"], 5,
                         "2 万账户/1.2 万实投在 §10b 下解出 5 只 —— 算错说明费用模型漂了")


class TestBuildCtxWiring(unittest.TestCase):
    """`build_ctx` 的**接线** —— 只桩掉 `load_market`(它慢且在别处已被测),
    其余(`month_end_dates` / `forward_return` / `zone_of` / `load_scores`)全用真函数。

    不这么测的话, 这个函数在"框架全关"的默认世界里**一行都不会被执行**, 而它正是
    阶段真正启用那天第一件要做的事 —— 没跑过的接线属于"看着正常其实缺一条腿"。
    """

    def _market(self, n_days=400):
        idx = pd.bdate_range("2024-01-01", periods=n_days)
        n = 6
        close = pd.DataFrame(
            np.tile(np.linspace(10, 20, n), (n_days, 1)), index=idx,
            columns=["s%02d" % i for i in range(n)], dtype="float64")
        return {"close": close, "adj": pd.DataFrame(1.0, index=idx, columns=close.columns)}

    def _ctx(self, ref=None, **fw):
        """`ref=None` ⇒ 对照表读得出来(1 行); `ref=DataFrame()` ⇒ 读不出来"""
        cfg = _cfg(**fw)
        cfg["framework"]["others_tables"] = ["model_score"]
        if ref is None:
            ref = pd.DataFrame({"s00": [1.0]}, index=["2024-01-31"])
        with mock.patch("app.factors.factor_engine.load_market",
                        return_value=self._market()), \
             mock.patch("app.models.expert.load_scores", return_value=ref):
            return FW.build_ctx(cfg)

    def test_context_has_every_key_the_stages_read(self):
        ctx = self._ctx(health=True)
        for k in ("market", "md", "zones", "fwd", "others"):
            self.assertIn(k, ctx, "阶段要读 ctx[%r], build_ctx 却没给" % k)
        self.assertEqual(len(ctx["md"]), len(ctx["zones"]),
                         "月末日期与分区数组长度不一致 —— gate.evaluate 会错位")
        self.assertEqual(set(ctx["fwd"].index), set(ctx["market"]["close"].index))
        self.assertEqual(sorted(ctx["others"]), ["model_score"])

    def test_zones_land_in_the_dead_and_active_windows(self):
        ctx = self._ctx(health=True)
        z = set(ctx["zones"])
        self.assertTrue(z <= {"DEAD", "ACTIVE", "OTHER"}, z)
        self.assertTrue(z & {"DEAD", "ACTIVE"},
                        "2024 年的月末居然全落在 OTHER 区 —— 分区口径漂了")

    def test_a_configured_market_since_is_honoured(self):
        cfg = _cfg(health=True)
        cfg["framework"]["market_since"] = "20230101"
        with mock.patch("app.factors.factor_engine.load_market",
                        return_value=self._market()) as lm, \
             mock.patch("app.models.expert.load_scores", return_value=pd.DataFrame()):
            FW.build_ctx(cfg)
        self.assertEqual(lm.call_args.kwargs.get("since"), "20230101")

    def test_an_unreadable_reference_table_is_just_absent(self):
        """对照表读不出来 ⇒ 不进 `others`(冗余检验会据此不下结论), 但**不许**炸"""
        ctx = self._ctx(ref=pd.DataFrame(), health=True)
        self.assertEqual(ctx["others"], {})


class TestOosRunner(unittest.TestCase):
    """`make_oos_runner` 是 `Pipeline.oos` 缺的那块拼图"""

    def _fake_bt(self, cand_ann=0.20, cand_mdd=-0.30):
        base = {"sys_ann_return": 0.10, "sys_max_drawdown": -0.40,
                "run_date": "r0", "periods": 85}
        cand = {"sys_ann_return": cand_ann, "sys_max_drawdown": cand_mdd,
                "run_date": "r1", "periods": 85}

        def f(score_table, start, run_tag):
            return dict(base if score_table == FW.BASE_TABLE else cand)
        return f

    def test_windows_match_the_pipeline_contract(self):
        """runner 的窗口名必须与 `judge_oos` 认的那两个**逐字相同**"""
        self.assertEqual([w for w, _ in FW.WINDOWS], list(P.OOS_WINDOWS))

    def test_refuses_to_guess_a_table_for_an_unknown_alpha(self):
        """**不许猜表名**: 猜错 = 拿别的 alpha 的成绩判卷 = 假证据"""
        with self.assertRaises(KeyError) as cm:
            FW.make_oos_runner({})("mystery")
        self.assertIn("拒绝猜测表名", str(cm.exception))

    def test_output_is_accepted_by_judge_oos(self):
        """产出的形状必须真的能被判决器吃下 —— 否则整条 pipeline 阶段是摆设"""
        with mock.patch("app.backtest.engine.run_backtest",
                        side_effect=self._fake_bt()):
            res = FW.make_oos_runner({"a1": "model_score_die_x"})("a1")
        self.assertEqual(set(res["windows"]), set(P.OOS_WINDOWS))
        v = P.judge_oos(res)
        self.assertTrue(v.passed, v.reason)
        self.assertIn("4 个不等式全部成立", v.reason)

    def test_a_worse_candidate_is_actually_failed(self):
        """**判决器必须能说不** —— 否则上面那条测试只是在证明"总能通过" """
        with mock.patch("app.backtest.engine.run_backtest",
                        side_effect=self._fake_bt(cand_ann=0.01, cand_mdd=-0.60)):
            res = FW.make_oos_runner({"a1": "model_score_die_x"})("a1")
        v = P.judge_oos(res)
        self.assertFalse(v.passed)
        self.assertIn("0/4", v.reason)

    def test_baseline_arm_is_always_the_production_table(self):
        """基线臂**硬编码**为 `model_score` —— 与 `exp_die_stage_b.py` 的预注册一致"""
        self.assertEqual(FW.BASE_TABLE, "model_score")
        seen = []
        with mock.patch("app.backtest.engine.run_backtest",
                        side_effect=lambda score_table, start, run_tag:
                        seen.append((score_table, start)) or
                        {"sys_ann_return": 0.1, "sys_max_drawdown": -0.4,
                         "run_date": "r", "periods": 1}):
            FW.make_oos_runner({"a1": "model_score_die_x"})("a1")
        self.assertEqual([t for t, _ in seen],
                         [FW.BASE_TABLE, "model_score_die_x"] * len(FW.WINDOWS))


class TestPipelineStageIsActuallyWired(unittest.TestCase):
    """**开着能不能跑通** —— 而不是"它的零件单测过没过"

    2026-09-11 实测抓到: `_stage_pipeline` 给 `Pipeline.critic` 传的是 `others=`,
    而真实签名是 `known=` ⇒ **这个阶段一打开就是 TypeError**。它躲过了当时全部 505 个
    测试: 那些测试验的是 `make_oos_runner` 的**产出形状**, 从没让这个函数真正执行过一次。

    **"零件都测过" ≠ "线路接对了"** —— 接线盒的价值恰恰在线上, 不在零件上。
    """

    def _patches(self, md):
        """把"贵/要真数据"的部分换成桩, 但**保留 `Pipeline.critic/oos/commit` 本体** ——
        要测的正是 framework 调它们时那几个关键字能不能被接住。"""
        wide = pd.DataFrame([[1.0], [2.0]], index=md, columns=["600000.SH"])
        fake = {"windows": {w: {"cand": {"sys_ann": 0.2, "sys_mdd": -0.3},
                                "base": {"sys_ann": 0.1, "sys_mdd": -0.4}}
                            for w in P.OOS_WINDOWS}}
        return [
            mock.patch.object(FW, "_trace"),
            mock.patch.object(FW, "make_oos_runner",
                              return_value=lambda aid: dict(fake, alpha_id=aid)),
            mock.patch("app.alpha.gate.forward_return", return_value=pd.DataFrame()),
            mock.patch("app.factors.factor_engine.industry_map",
                       return_value=pd.Series(dtype=object)),
            mock.patch("app.db.get_conn", return_value=mock.MagicMock()),
            mock.patch("app.alpha.bank.load_values", return_value=wide),
            mock.patch("app.alpha.bank.set_note"),
            mock.patch("app.alpha.critic.attack_all",
                       return_value={"_disclaimer": "测试桩"}),
            mock.patch("app.alpha.bank.get",
                       return_value={"alpha_id": "a1", "status": "POOLED", "note": ""}),
            mock.patch("app.alpha.bank.upsert"),
            mock.patch("app.alpha.bank.list_alphas",
                       return_value=pd.DataFrame({"status": ["POOLED", "VALIDATED"]})),
        ]

    def test_enabling_the_pipeline_stage_actually_runs(self):
        cfg = _cfg(pipeline=True)
        cfg["framework"]["alpha_tables"] = {"a1": "model_score_die_x"}
        md = pd.DatetimeIndex(["2024-01-31", "2024-02-29"])
        ctx = {"market": {}, "md": md, "zones": np.array(["DEAD", "ACTIVE"]),
               "others": {}}
        for p in self._patches(md):
            p.start()
            self.addCleanup(p.stop)
        out = FW.run_stages(cfg, ctx=ctx)
        self.assertEqual(out["ran"].get("pipeline", {}).get("committed"), 1,
                         "pipeline 阶段没跑到 commit")
        self.assertEqual(out["failed"], {},
                         "pipeline 阶段被打开了却跑不通 —— 接线断了: %s"
                         % out["failed"])

    def test_the_critic_call_uses_a_keyword_the_callee_accepts(self):
        """把上一条的**成因**单独钉死: 关键字名必须真的在被调方的签名里。

        上一条是行为测试(要跑得起来), 这一条是更直接的守门 —— 以后有人把
        `known=` 改回别的名字, 报错信息会直接指向这里而不是一个 TypeError。
        """
        import inspect
        params = inspect.signature(P.Pipeline.critic).parameters
        self.assertIn("known", params)
        self.assertNotIn("others", params)


if __name__ == "__main__":
    unittest.main(verbosity=2)
