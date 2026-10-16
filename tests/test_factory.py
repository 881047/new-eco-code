"""Alpha Factory 单测 (2026-09-11 「芯片式异构架构」阶段 1)

**这个模块存在的理由是「分母说不清, 门禁就形同虚设」** —— 所以这里测的重点不是
"能不能跑通", 而是**预注册闸门真的是硬的**:

1. `declare()` 之前不许 `run()`; `run()` 之后不许再加 proposer
2. 分母 = **声明值**(GP 的 `population × generations`), 不是实际求值数 ——
   缓存命中的重复个体照样计入, 分母只能被高估不能被低估
3. GP 的搜索空间必须与 `expr.py` 的算子表**逐条对应** —— 搜索空间一旦"偷偷加料",
   分母立刻失真
4. GP 的求值域物理上不含活跃区(面板切片在死区最后一天截断)

运行: ./.venv/Scripts/python.exe -m pytest tests/test_factory.py -q
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                                          # noqa: E402
import pandas as pd                                         # noqa: E402

from app.alpha import expr as E, factory as F, gate, zoos    # noqa: E402


class TestPreRegistrationIsHard(unittest.TestCase):
    """预注册闸门必须是断言, 不是文档"""

    def setUp(self):
        self.f = F.Factory(F.PreRegistration(intent="单测"))

    def test_run_without_declare_raises(self):
        with self.assertRaises(AssertionError):
            self.f.run()

    def test_add_after_declare_raises(self):
        self.f.add(F.ZooProposer(zoos.qlib158()))
        self.f.declare()
        with self.assertRaises(AssertionError):
            self.f.add(F.ZooProposer(zoos.qlib158()))

    def test_declare_sums_declared_not_evaluated(self):
        """分母来自 `declared()`(可预先声明), 不是来自 `propose()` 的实际产出"""
        f = F.Factory(F.PreRegistration(intent="单测"))
        f.add(F.ZooProposer(zoos.qlib158(), label="qlib158"))
        f.add(F.GPProposer(m=None, dead_dates=pd.DatetimeIndex([]),
                           budget=F.GPBudget(population=7, generations=3)))
        n = f.declare()
        self.assertEqual(n, 158 + 7 * 3)
        self.assertEqual(f._denominator, 21 + 158)

    def test_declare_zero_is_rejected(self):
        """说不清搜索空间大小的提议器不许进工厂"""
        f = F.Factory(F.PreRegistration(intent="单测"))
        f.add(F.Proposer())
        with self.assertRaises(AssertionError):
            f.declare()

    def test_prereg_is_frozen_and_recorded(self):
        p = F.PreRegistration(intent="验证不可变")
        with self.assertRaises(Exception):
            p.intent = "改一下"          # frozen dataclass
        note = p.to_note()
        self.assertIn("验证不可变", note)
        self.assertIn(F.SCHEMA_VERSION, note)

    def test_run_twice_rejected(self):
        # 2026-09-22（结项审计）：此处原有三行死代码 —— 一个空池子的 `f`
        # （`f = Factory(...)` / `f.add(ZooProposer([]))`）加一行**恒不执行**的
        # `self.assertEqual(f.declare(), 0) if False else None`。成因是早期尝试：
        # 空池子的 `declared() = 0` 会让 `declare()` 直接拒，**测不到本用例要测的东西**
        # ⇒ 作者改用"1 条候选让分母非 0"的路径，但把失败的那次尝试留在了原地。
        # 死断言比没有断言更坏（读的人会以为这里验过 `declare()==0`）⇒ 已删。
        f2 = F.Factory(F.PreRegistration(intent="单测"))
        f2.add(F.ZooProposer([("x", "$close", {})]))
        f2.declare()
        f2._ran = True                    # 模拟已跑过
        with self.assertRaises(AssertionError):
            f2.run()


class TestZooProposer(unittest.TestCase):

    def test_declared_matches_propose(self):
        items = zoos.qlib158()
        p = F.ZooProposer(items, label="qlib158")
        self.assertEqual(p.declared(), len(items))
        got = p.propose()
        self.assertEqual(len(got), len(items))
        self.assertEqual(p.n_evaluated, len(items))
        self.assertEqual(got[0].meta["source"], items[0][2]["source"])

    def test_candidate_needs_payload(self):
        with self.assertRaises(ValueError):
            F.Candidate(name="空的")

    def test_hk_excluded_from_flow_candidates(self):
        """hk_* 覆盖度断崖(不变量 #6)必须被硬排除, 不能只写在 docstring 里"""
        import app.factors.flow_factors as ff
        orig = ff.compute_flow_factors
        try:
            ff.compute_flow_factors = lambda *a, **k: {
                "margin_net_share": pd.DataFrame({"600000.SH": [1.0]}),
                "hk_hold_ratio": pd.DataFrame({"600000.SH": [1.0]}),
            }
            zp = F.ZooProposer.flow_proposer({}, pd.DatetimeIndex([]))
            got = zp.propose()
        finally:
            ff.compute_flow_factors = orig
        names = [c.name for c in got]
        self.assertIn("margin_net_share", names)
        self.assertNotIn("hk_hold_ratio", names)
        self.assertEqual(zp.declared(), 1)

    def test_panel_candidate_carries_panel(self):
        """面板类候选必须带上面板 —— 否则 `_evaluate` 会在求值时才炸"""
        df = pd.DataFrame({"600000.SH": [1.0]})
        zp = F.ZooProposer(panels={"f1": df})
        c = zp.propose()[0]
        self.assertIs(c.panel, df)
        self.assertEqual(c.meta["name"], "f1")

    def test_duplicate_name_between_items_and_panels_rejected(self):
        with self.assertRaises(ValueError):
            F.ZooProposer(items=[("f1", "$close", {})],
                          panels={"f1": pd.DataFrame()})


class TestLLMProposer(unittest.TestCase):
    """LLM 提议器只能是**离线装载器** —— 不联网、不调用任何在线模型"""

    def _write(self, obj):
        p = Path(tempfile.mkdtemp()) / "cands.json"
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        return p

    def test_load_and_validate(self):
        p = self._write([{"name": "a1", "expr": "Mean($close, 20)", "why": "动量"}])
        pr = F.LLMProposer(p)
        self.assertEqual(pr.declared(), 1)
        got = pr.propose()
        self.assertEqual(got[0].name, "a1")
        self.assertEqual(got[0].meta["info_source"], "llm_offline")

    def test_bad_schema_rejected(self):
        p = self._write([{"name": "a1"}])
        with self.assertRaises(ValueError):
            F.LLMProposer(p).declared()

    def test_illegal_expr_rejected_at_load(self):
        """前视/非法表达式必须在**装载时**炸, 不能拖到求值阶段烧预算"""
        p = self._write([{"name": "peek", "expr": "Ref($close, -1)"}])
        with self.assertRaises(E.ExprError):
            F.LLMProposer(p).propose()


class TestGPSearchSpaceFrozen(unittest.TestCase):
    """搜索空间 = DSL 算子表 − 显式排除表。**两个方向都要锁**:

    - 少注册一个 ⇒ 空间被悄悄收窄, "覆盖了全部算子"的说法失真
    - 多注册一个 ⇒ **加料**, 在 `declare()` 冻结的分母当场失真(最危险的方向)
    """

    @classmethod
    def setUpClass(cls):
        import deap.gp as gp
        cls.pset = F._build_pset(gp)

    def _registered(self):
        return {p.name for p in self.pset.primitives[F.SeriesT]}

    def test_matches_dsl_minus_exclusions(self):
        all_dsl = (E.ROLLING_OPS | E.PAIR_ROLLING_OPS | E.ELEM_UNARY | E.ELEM_BINARY)
        want = all_dsl - set(F.GP_EXCLUDED_OPS)
        self.assertEqual(self._registered(), want,
                         "多: %s / 少: %s" % (sorted(self._registered() - want),
                                             sorted(want - self._registered())))

    def test_every_registered_op_is_evaluable(self):
        """注册进去的每个算子, 引擎都必须**真的认得**(不是拼写错的鬼算子)"""
        for name in sorted(self._registered()):
            try:
                E.parse("%s($close, $open, 20, 0.5)"[:len(name) + 30])
            except E.ExprError as e:
                self.assertNotIn("未知算子", str(e), name)

    def test_exclusions_are_documented(self):
        for op, why in F.GP_EXCLUDED_OPS.items():
            self.assertTrue(why and len(why) > 8, "排除 %s 必须写明理由" % op)

    def test_budget_denominator(self):
        b = F.GPBudget(population=11, generations=4)
        self.assertEqual(b.n_declared, 44)
        self.assertEqual(b.n_declared, b.population * b.generations)


class TestGPGenerator(unittest.TestCase):
    """`_gen_tree` 必须能在**每一种类型**上都长出树来。

    回归对象: deap 的 `gp.genFull` 遇到"纯终结符类型"(窗口/分位 —— 它们的取值只能是
    字面量, 引擎 `_win()` 也要求常量)时会去找不存在的原语并 `IndexError`。
    2026-09-11 冒烟时才暴露, 单测当时是漏的 —— 补在这里。
    """

    def setUp(self):
        import deap.gp as gp
        self.gp = gp
        self.pset = F._build_pset(gp)

    def test_generates_parseable_trees(self):
        import random
        random.seed(7)
        for _ in range(300):
            tree = F._gen_tree(self.pset, min_=1, max_=3)
            s = F._tree_to_expr(tree, self.gp)
            E.parse(s)                       # 长出来的每一棵都必须是合法 DSL

    def test_covers_window_and_quantile_types(self):
        """窗口/分位型必须真的出现在树里(否则说明生成器一直在避开它们)"""
        import random
        random.seed(11)
        seen = set()
        for _ in range(300):
            s = F._tree_to_expr(F._gen_tree(self.pset, min_=1, max_=3), self.gp)
            seen |= {p.name for p in self.pset.primitives[F.SeriesT] if p.name in s}
        self.assertIn("Quantile", seen, "300 棵里一次 Quantile 都没出现, 分位型被饿死了")
        self.assertTrue(any(n in seen for n in ("Mean", "Std", "Ref", "Rank")),
                        "常见滚动算子没出现")

    def test_mutation_subtree_is_parseable(self):
        """`mutUniform` 走的是同一个生成器 —— 变异出来的子树也要合法"""
        import random
        from deap import base, tools
        random.seed(3)
        tb = base.Toolbox()
        tb.register("expr", F._gen_tree, pset=self.pset, min_=1, max_=3)
        tb.register("expr_mut", F._gen_tree, min_=0, max_=2)
        tb.register("mutate", self.gp.mutUniform, expr=tb.expr_mut, pset=self.pset)
        tb.register("mate", self.gp.cxOnePoint)
        for _ in range(120):
            a = tb.expr()
            b = tb.expr()
            if len(a) > 1 and len(b) > 1:
                tb.mate(a, b)
            if len(a) > 1:
                tb.mutate(a)
            E.parse(F._tree_to_expr(a, self.gp))


class TestGPTreePrinter(unittest.TestCase):
    """PrimitiveTree → DSL 串 → 能被 `expr.parse` 吃回去"""

    def setUp(self):
        import deap.gp as gp
        self.gp = gp
        self.pset = F._build_pset(gp)

    def test_roundtrip(self):
        # Mean($close, 20)  —— 手工构造前缀序
        tree = self.gp.PrimitiveTree.from_string("Mean($close, 20)", self.pset)
        s = F._tree_to_expr(tree, self.gp)
        self.assertEqual(E.parse(s), E.parse("Mean($close, 20)"))

    def test_nested_roundtrip(self):
        expr = "Div(Sub($close, Mean($close, 20)), Std($close, 20))"
        tree = self.gp.PrimitiveTree.from_string(expr, self.pset)
        s = F._tree_to_expr(tree, self.gp)
        self.assertEqual(E.parse(s), E.parse(expr))

    def test_quantile_roundtrip(self):
        expr = "Quantile($close, 60, 0.25)"
        tree = self.gp.PrimitiveTree.from_string(expr, self.pset)
        self.assertEqual(E.parse(F._tree_to_expr(tree, self.gp)), E.parse(expr))


class TestGPGenerationLoop(unittest.TestCase):
    """**完整的代循环必须真跑一遍**。

    回归对象: 2026-09-11 的 GP 冒烟在 `propose()` 的代循环里崩了 ——
    `tools.selTournamentDCD` 要读 `fitness.crowding_dist`, 而它只由 `selNSGA2` 赋值,
    代码在循环前只做了求值、没排序赋距。此前单测覆盖了树生成器、打印器、搜索空间,
    **却从没跑过代循环本身**, 于是这个缺口一路溜到冒烟。

    (另外 `selTournamentDCD` 在 `k == len(pop)` 时要求种群能被 4 整除 —— `--gp 10 5`
    这种合法预算会当场崩。本模块改用自己实现的同一条 Deb 规则, 一并测掉。)
    """

    def _m(self, n_days=60):
        dates = pd.bdate_range("2021-01-01", periods=n_days)
        cols = ["600000.SH", "000001.SZ"]
        rnd = np.random.RandomState(0)
        m = {k: pd.DataFrame(rnd.rand(n_days, 2) + 1.0, index=dates, columns=cols)
             for k in ("open", "high", "low", "close", "vol", "amount",
                       "total_mv", "circ_mv", "pb", "pe_ttm", "pre_close")}
        m["adj"] = pd.DataFrame(1.0, index=dates, columns=cols)
        return m

    def _proposer(self, population):
        # 2026-09-13: GP 的适应度必须在 **PIT 可交易域**上算（P0-05），而掩码的唯一来源是
        # Factory；这里用一个**全 True 的桩**把接线钉住（真实装配路径由 test_factory_add_binds 验证）。
        def _mask(tag, m, dates):
            return pd.DataFrame(True, index=pd.DatetimeIndex(dates),
                                columns=list(m["close"].columns))
        return F.GPProposer(
            self._m(),
            pd.DatetimeIndex(pd.bdate_range("2021-02-28", "2021-03-31", freq="ME")),
            budget=F.GPBudget(population=population, generations=1), mask_fn=_mask)

    def test_loop_runs_and_returns_parseable_candidates(self):
        got = self._proposer(8).propose()
        self.assertTrue(got, "代循环跑完却一条候选都没交付")
        for c in got:
            E.parse(c.expr)                  # 交付的每一条都必须是合法 DSL
            self.assertEqual(c.meta["source"], "GPProposer(NSGA-II)")

    def test_population_not_multiple_of_four_is_allowed(self):
        """`--gp 10 5` 这类预算不许因为 deap 的整除限制而崩"""
        for n in (5, 6, 10):
            with self.subTest(population=n):
                self.assertTrue(self._proposer(n).propose())

    def test_mating_selection_requires_crowding_dist(self):
        """把**前置条件**显式钉住: 没赋拥挤距离就调用交配选择 ⇒ 必须炸。

        原 bug 正是"没赋就调用", 只是它是在代循环深处炸的。这个测试让契约一目了然。
        """

        class _Fit:
            def __init__(self, cd=None):
                self.values = (0.0, 1.0)
                if cd is not None:
                    self.crowding_dist = cd

            def dominates(self, other):
                return False

        class _Ind:
            def __init__(self, cd=None):
                self.fitness = _Fit(cd)

        import random
        rnd = random.Random(0)
        with self.assertRaises(AttributeError):
            F._mating_selection([_Ind()] * 8, 8, rnd)
        self.assertEqual(len(F._mating_selection([_Ind(1.0)] * 8, 8, rnd)), 8)


class TestGPTradableWiring(unittest.TestCase):
    """GP 的适应度**必须**在 PIT 可交易域上算（2026-09-13 独立审查 + 本人复核确认的真事故）。

    事故链：`GPProposer`（继承 `Proposer`）里的 `_fitness_mask()` 去摸
    `self._mask_cache` / `self._tradable` —— 这两个名字**只在 `Factory` 上** ⇒ 每次适应度求值
    抛 `AttributeError` ⇒ 被 `_fitness` 的 `except Exception` 吞成"最差适应度"（并写进缓存）
    ⇒ 选择压力为零、符号回归退化成**随机搜索**，而日志里一个字都没有
    （DB 实证：`alpha_bank` 171 行里 0 行来自 GP）。

    守卫两条：① 没接线就**炸**（不许静默退化）；② `Factory.add()` 必须真的把域构造器绑进去。
    """

    def _m(self, n_days=40):
        dates = pd.bdate_range("2021-01-01", periods=n_days)
        cols = ["600000.SH", "000001.SZ"]
        rnd = np.random.RandomState(0)
        m = {k: pd.DataFrame(rnd.rand(n_days, 2) + 1.0, index=dates, columns=cols)
             for k in ("open", "high", "low", "close", "vol", "amount",
                       "total_mv", "circ_mv", "pb", "pe_ttm", "pre_close")}
        m["adj"] = pd.DataFrame(1.0, index=dates, columns=cols)
        return m

    def _gp(self):
        return F.GPProposer(
            self._m(),
            pd.DatetimeIndex(pd.bdate_range("2021-02-28", "2021-03-31", freq="ME")),
            budget=F.GPBudget(population=8, generations=1))

    def test_gp_without_mask_source_fails_loudly(self):
        """没接线时**必须抛错**，而不是把每个个体都判成最差、悄悄跑完。"""
        # 直接打掩码那一层：走 propose() 会先在表达式求值上炸(桩面板缺通道), 测不到接线。
        p = self._gp()
        p._m_gp = self._m()
        p._dead = pd.DatetimeIndex(pd.bdate_range("2021-02-28", "2021-03-31", freq="ME"))
        with self.assertRaises(RuntimeError) as ctx:
            p._fitness_mask()
        self.assertIn("PIT", str(ctx.exception))

    def test_factory_add_binds_tradable(self):
        f = F.Factory(F.PreRegistration(intent="单测"))
        p = self._gp()
        f.add(p)
        self.assertIsNotNone(p._mask_fn, "Factory.add 没把 PIT 域构造器绑给 GP")
        self.assertIs(p._mask_fn.__self__, f)

    def test_mask_is_lazily_built_once_and_cached(self):
        """掩码惰性算一次、全体个体复用（GP 每代几十个个体，重复构造会白烧机器）。"""
        calls = []

        def _mask(tag, m, dates):
            calls.append(tag)
            return pd.DataFrame(True, index=pd.DatetimeIndex(dates),
                                columns=list(m["close"].columns))

        dead = pd.DatetimeIndex(pd.bdate_range("2021-02-28", "2021-03-31", freq="ME"))
        p = F.GPProposer(self._m(), dead, budget=F.GPBudget(population=8, generations=1),
                         mask_fn=_mask)
        p._m_gp, p._dead = self._m(), dead
        first, second = p._fitness_mask(), p._fitness_mask()
        self.assertEqual(calls, ["gp"], "掩码被重复构造了")
        self.assertIs(first, second)


class TestGPActiveZoneIsUnreachable(unittest.TestCase):
    """活跃区在 GP 里必须**物理上不可达** —— 不是"记得别看\""""

    def test_panel_is_cut_at_dead_end(self):
        dates = pd.bdate_range("2020-01-01", "2026-08-31")
        cols = ["600000.SH", "000001.SZ"]
        m = {k: pd.DataFrame(1.0, index=dates, columns=cols)
             for k in ("open", "high", "low", "close", "vol", "amount",
                       "total_mv", "circ_mv", "pb", "pe_ttm", "pre_close")}
        m["adj"] = pd.DataFrame(1.0, index=dates, columns=cols)
        gp = F.GPProposer(m, pd.DatetimeIndex(
            pd.bdate_range("2020-09-30", "2023-12-29", freq="ME")),
            budget=F.GPBudget(population=2, generations=1))
        gp._prepare()
        last = next(iter(gp._panel.values())).index.max()
        self.assertLessEqual(last, pd.Timestamp(gate.DEAD[1]),
                             "GP 面板越过了死区末尾 ⇒ 活跃区泄漏进适应度")
        self.assertTrue((gp._dead <= pd.Timestamp(gate.DEAD[1])).all())


if __name__ == "__main__":
    unittest.main(verbosity=2)
