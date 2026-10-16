"""Portfolio Optimizer 单测 (2026-09-11 「芯片式异构架构」#36)

这个模块的头号主张**不是**"凸优化比 top-N 好", 而是:
**「该持几只」是一个由费用模型解出来的数, 不是拍脑袋。**

所以这里最重要的一组测试是把 `feasible_n` / `round_trip_rate` 钉在
**方案 §10b 那张已经写进工程记录的表**上 —— 费用模型一改, 这里立刻红,
逼人回去重算 §10b, 而不是让表悄悄过期。

第二组是 `to_lots` 的**可执行性**: A 股买的是整手, 且 ¥5 佣金地板让"小仓位"
不是"收益少一点"而是"费率翻几倍"。落整手必须真的买得起、且不越界。

运行: ./.venv/Scripts/python.exe -m pytest tests/test_optimizer.py -q
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                                          # noqa: E402
import pandas as pd                                         # noqa: E402

from app.portfolio import fees, optimizer as O              # noqa: E402


class TestFeeArithmeticPinsSection10b(unittest.TestCase):
    """模块必须能**不加参数地复现** §10b 的表 —— 把读数钉在工程记录上"""

    def test_round_trip_matches_the_recorded_table(self):
        """2 万账户、约 1.2 万实投、等分 —— §10b 的五个读数逐条复现"""
        for amount, want_pct in ((400, 2.552), (600, 1.719), (1200, 0.885),
                                 (2000, 0.552), (2400, 0.469)):
            with self.subTest(amount=amount):
                self.assertAlmostEqual(100 * O.round_trip_rate(amount), want_pct,
                                       places=3)

    def test_five_yuan_floor_pins_the_rate_floor(self):
        """¥5 地板把往返费率钉死在 ≈0.102%, **投多少钱都下不去**。

        这正是用户 2026-09-11 的永久约束(「券商没有免五的」)在算术上的样子:
        超过 2 万元一笔之后, 费率不再随规模下降。"""
        floor = 100 * O.round_trip_rate(20_000)
        self.assertAlmostEqual(floor, 0.102, places=3)
        for big in (60_000, 200_000, 1_000_000):
            self.assertAlmostEqual(100 * O.round_trip_rate(big), floor, places=6)

    def test_rate_is_monotone_decreasing_in_amount(self):
        """金额越大费率越低(在地板之前)—— 这是"资金规模解锁持仓数"的机制"""
        rates = [O.round_trip_rate(a) for a in (200, 400, 800, 1600, 3200, 8000)]
        self.assertTrue(all(a > b for a, b in zip(rates, rates[1:])), rates)

    def test_feasible_n_reproduces_the_current_five_holdings(self):
        """**这条最重要**: 喂进当前账户的真实数字, 模块必须自己得出"5 只"。

        2 万账户、9 月方案实投 ≈1.19 万、目标往返 ≤0.5% ⇒ 解出 n=5、每只 2400 元、
        往返 0.469%。**与生产实际的 5 只逐位吻合** —— 也就是说现状不是拍的, 是可导出的。
        """
        got = O.feasible_n(12_000, target_rate=0.005)
        self.assertEqual(got["n"], 5)
        self.assertAlmostEqual(got["per_position"], 2400.0, places=6)
        self.assertAlmostEqual(100 * got["round_trip"], 0.469, places=3)

    def test_more_capital_unlocks_more_positions(self):
        """「Top 20~30 不是错的, 是被账户规模卡死的」—— 规模到了就解锁"""
        # 放开 cap 看算术本身(默认 cap=40 会在 12 万实投就顶到)
        ns = [O.feasible_n(eq, target_rate=0.005, cap=10_000)["n"]
              for eq in (12_000, 60_000, 120_000, 600_000)]
        self.assertTrue(all(a < b for a, b in zip(ns, ns[1:])), ns)
        self.assertGreaterEqual(ns[-1], 20, "60 万实投仍撑不到 20 只, 与 §10b 的结论不符")
        # 默认上限也必须真的生效 —— 否则"资金规模解锁"会一路滑向几千只等权,
        # 而 §10b 说的"解锁"是有边界的
        self.assertEqual(O.feasible_n(10_000_000, target_rate=0.005)["n"], 40)

    def test_target_below_the_fee_floor_is_impossible(self):
        """目标费率低于地板(2c+2t+s)时不许给一个假的 n —— 必须说"做不到\""""
        got = O.feasible_n(12_000, target_rate=0.0001)
        self.assertEqual(got["n"], 0)
        self.assertIn("地板", got["why"])

    def test_min_amount_is_the_closed_form(self):
        """`min_amount = 2m / (target − 2t − s)` —— 抽查两个点"""
        for target in (0.004, 0.005, 0.01):
            with self.subTest(target=target):
                a = O.min_amount_for_rate(target)
                self.assertAlmostEqual(O.round_trip_rate(a), target, places=9)


class TestHRPIgnoresScores(unittest.TestCase):
    """HRP 是"只用协方差、不看分数"的稳健对照 —— 这条必须真的是行为, 不是注释"""

    def _corr(self, n=6):
        rnd = np.random.RandomState(0)
        a = rnd.rand(n, n)
        c = a @ a.T
        d = np.sqrt(np.diag(c))
        c = c / np.outer(d, d)
        np.fill_diagonal(c, 1.0)
        cols = ["s%02d" % i for i in range(n)]
        return pd.DataFrame(c, index=cols, columns=cols)

    def test_weights_are_a_distribution(self):
        w = O.hrp_weights(self._corr())
        self.assertAlmostEqual(float(w.sum()), 1.0, places=9)
        self.assertTrue((w > 0).all(), w.to_dict())

    def test_two_asset_symmetric_case_splits_evenly(self):
        corr = pd.DataFrame([[1.0, 0.0], [0.0, 1.0]], index=["a", "b"], columns=["a", "b"])
        w = O.hrp_weights(corr)
        self.assertAlmostEqual(w["a"], 0.5, places=9)
        self.assertAlmostEqual(w["b"], 0.5, places=9)

    def test_hrp_allocation_is_score_independent(self):
        """把分数改动之后 HRP 的权重必须**一格不变**(候选集覆盖全部标的时)"""
        scores = pd.Series([9.0, 8, 7, 6, 5, 4], index=["s%02d" % i for i in range(6)])
        cov = self._corr()
        a = O.optimize(scores, cov=cov, n=6, method="hrp")
        b = O.optimize(scores * 0 + 1.0, cov=cov, n=6, method="hrp")
        pd.testing.assert_series_equal(a.weights.sort_index(),
                                       b.weights.sort_index())


class TestOptimize(unittest.TestCase):

    def _scores(self, n=20):
        return pd.Series(np.arange(n, 0, -1, dtype=float),
                         index=["s%02d" % i for i in range(n)])

    def test_topn_is_the_incumbent_baseline(self):
        """裸 top-N 等权 = 现行做法, 保留为对照组(不是候选)"""
        a = O.optimize(self._scores(), n=5, budget=0.9, method="topn")
        self.assertEqual(len(a.weights), 5)
        self.assertEqual(list(a.weights.index), ["s%02d" % i for i in range(5)])
        self.assertAlmostEqual(float(a.weights.sum()), 0.9, places=9)

    def test_qp_tilts_toward_high_scores_but_respects_the_cap(self):
        """QP 与 top-N 的行为差别必须真的存在: 高分票被加权, 但不超过单票上限"""
        a = O.optimize(self._scores(), n=6, budget=0.95, max_weight=0.35, method="qp")
        self.assertEqual(a.method, "qp", a.diag)
        self.assertTrue((a.weights >= -1e-9).all(), a.weights.to_dict())
        self.assertLessEqual(float(a.weights.sum()), 0.95 + 1e-6)
        self.assertLessEqual(float(a.weights.max()), 0.35 + 1e-6)
        # 低分票会被压到 ~0 而被截掉(这是对的), 所以只能问"最高分的那个是不是被加权了"
        self.assertEqual(a.weights.idxmax(), "s00", "QP 没有把钱给最高分的票")
        self.assertGreater(float(a.weights.max()), 0.95 / 6,
                           "最高分票的权重没超过等权, 那 QP 和 top-N 就没有区别")

    def test_qp_respects_industry_cap(self):
        """同一个行业里装不下超过 cap —— 否则"分散"是假的"""
        scores = self._scores(20)
        industry = pd.Series(["银行"] * 20, index=scores.index)
        a = O.optimize(scores, n=20, budget=0.95, industry=industry,
                       industry_cap=0.30, method="qp")
        if a.method == "qp":                     # cvxpy 不可用时回退 topn, 跳过
            self.assertLessEqual(float(a.weights.sum()), 0.30 + 1e-6, a.weights.to_dict())

    def test_empty_scores_returns_empty(self):
        a = O.optimize(pd.Series(dtype="float64"), n=5)
        self.assertEqual(len(a.weights), 0)

    def test_nan_scores_are_not_investable(self):
        """NaN = 不可投(与引擎"NaN = 不买"一致), 不许被当成 0 分参与排序"""
        s = self._scores(10)
        s.iloc[:3] = np.nan
        a = O.optimize(s, n=3, method="topn")
        for c in a.weights.index:
            self.assertFalse(np.isnan(s[c]), "%s 缺分却被买入了" % c)


class TestQpBudgetActuallyBinds(unittest.TestCase):
    """2026-09-12 审查 P2-07: 「`min(budget, wv.sum())` 是恒等变换 ⇒ budget 完全失效、
    QP 解常远低于目标仓位」—— **前半句属实(死代码), 后半句实测否证**。

    这组测试把三种情形钉死, 于是"到底怎么回事"不再靠读代码猜:
      · 上限不绑 → QP **自己就花满预算**(不是"远低于目标仓位");
      · 单票上限绑住 → 和 < budget 是**必须**的(放大就越界);
      · λ 很大 → 内点解, 解本身就要留现金(本项目 0910 那轮证明闲置现金是承重的)。
    并发一条: 审查建议的 `* budget` 写法在第二种情形下会把单票权重顶破上限 —— 用它来
    **记录否证**, 而不是真的去改(改了就不是"在口径内修 bug", 是"换口径")。
    """

    def _scores(self, n=20):
        return pd.Series(np.arange(n, 0, -1, dtype=float),
                         index=["s%02d" % i for i in range(n)])

    def _cov(self, codes, seed=0):
        rng = np.random.default_rng(seed)
        k = len(codes)
        a = rng.normal(size=(k, k))
        return pd.DataFrame(a @ a.T / k * 0.01, index=codes, columns=codes)

    def _qp(self, n=5, budget=1.0, max_weight=0.35, lam=1.0, n_cand=20):
        s = self._scores(n_cand)
        a = O.optimize(s, self._cov(s.index), None, n, budget, max_weight,
                       risk_aversion=lam, method="qp")
        return a

    def test_budget_binds_when_no_cap_is_hit(self):
        """上限不绑 ⇒ 解花满预算 —— "budget 完全失效"的反例"""
        a = self._qp(n=5, budget=1.0, max_weight=0.35)
        if a.method != "qp":
            self.skipTest("cvxpy 不可用, QP 回退 topn")
        self.assertAlmostEqual(float(a.weights.sum()), 1.0, places=6)
        self.assertAlmostEqual(a.diag["budget_gap"], 0.0, places=6)

    def test_cap_binding_leaves_budget_unused_on_purpose(self):
        """单票上限绑住 ⇒ 和 = n×cap < budget, 且**放大就是越界**"""
        a = self._qp(n=5, budget=1.0, max_weight=0.05)
        if a.method != "qp":
            self.skipTest("cvxpy 不可用, QP 回退 topn")
        self.assertAlmostEqual(float(a.weights.sum()), 0.25, places=6)
        self.assertGreater(a.diag["budget_gap"], 0.7)
        self.assertTrue(a.diag["max_w_hit"], "上限明明绑住了, 读数却说没绑")
        # ← 审查建议的修法 `wv / wv.sum() * budget` 在这一格上会得到 0.2/只,
        #   把 0.05 的单票上限顶破 4 倍。**不许那么改。**
        scaled = a.weights / a.weights.sum() * 1.0
        self.assertGreater(float(scaled.max()), 0.05 * 3,
                           "这条测试存在的意义: 记录'放大就越界'这半个否证")

    def test_large_lambda_holds_cash_by_objective_not_by_bug(self):
        """λ 很大 ⇒ 内点解: 边际收益=边际风险, 解**主动**留现金(承重行为)"""
        a = self._qp(n=3, budget=1.0, max_weight=0.35, lam=1000.0, n_cand=30)
        if a.method != "qp":
            self.skipTest("cvxpy 不可用, QP 回退 topn")
        self.assertLess(float(a.weights.sum()), 0.9)
        self.assertFalse(a.diag["max_w_hit"], "这一格的成因不是上限, 别把读数读错")
        self.assertGreater(a.diag["budget_gap"], 0.1)

    def test_gap_is_reported_not_hidden(self):
        """无论哪种成因, 「差多少」都必须是个**被观测到的读数**(不是靠读代码相信)"""
        a = self._qp(n=5, budget=1.0, max_weight=0.05)
        if a.method != "qp":
            self.skipTest("cvxpy 不可用, QP 回退 topn")
        self.assertAlmostEqual(a.diag["budget_gap"], 1.0 - float(a.weights.sum()),
                               places=9)


class TestToLotsIsActuallyExecutable(unittest.TestCase):
    """落整手: 真的买得起、真的是整手、真的尊重 ¥5 地板"""

    def _px(self, codes, price=10.0):
        return pd.Series(price, index=codes, dtype="float64")

    def test_lots_are_whole_hands(self):
        """`lots` 是**手数**, `shares` 才是股数 —— 两者都不许出现非整数手"""
        codes = ["a", "b", "c", "d", "e"]
        w = pd.Series(0.2, index=codes)
        al = O.to_lots(w, self._px(codes), equity=12_000)
        self.assertTrue(len(al.lots) > 0)
        for c, n in al.lots.items():
            self.assertGreater(int(n), 0, "%s 买了 0 手" % c)
            self.assertEqual(int(n), float(n), "%s 的手数 %s 不是整数" % (c, n))
        self.assertTrue((al.shares.to_numpy() % O.LOT == 0).all(), al.shares.to_dict())
        np.testing.assert_array_equal(al.shares.to_numpy(),
                                      al.lots.to_numpy() * O.LOT)

    def test_never_overspends(self):
        """**可执行性**: 独立重算买入费用, 总支出不得超过可用资金。

        这是唯一能抓住"循环里忘了扣手续费"的断言。"""
        codes = ["a", "b", "c", "d", "e", "f"]
        w = pd.Series(0.18, index=codes)
        px = pd.Series([7.3, 12.9, 41.5, 3.07, 88.8, 22.2], index=codes)
        equity, cash = 12_000.0, 0.0
        al = O.to_lots(w, px, equity=equity, cash=cash)
        invested = sum(int(n) * O.LOT * px[c] for c, n in al.lots.items())
        outlay = invested + al.fees_est
        self.assertLessEqual(outlay, equity + cash + 1e-6,
                             "买入支出 %.2f + 费用 %.2f 超过了可用资金 %.2f"
                             % (invested, al.fees_est, equity + cash))
        self.assertAlmostEqual(al.cash, equity + cash - outlay, places=6)

    def test_fee_estimate_matches_the_real_fee_model(self):
        """`fees_est` 必须真的是 `fees.trade_cost` 的和, 不是按比率估的"""
        codes = ["a", "b", "c"]
        w = pd.Series(0.3, index=codes)
        px = self._px(codes, 15.0)
        al = O.to_lots(w, px, equity=12_000)
        want = sum(fees.trade_cost(int(n) * O.LOT * 15.0, "buy") for n in al.lots.values)
        self.assertAlmostEqual(al.fees_est, want, places=6)

    def test_drops_positions_below_the_min_lot_value(self):
        """低于门槛的仓位**不买** —— 让 5 元地板把费率顶穿不如不做"""
        codes = ["big", "tiny"]
        w = pd.Series({"big": 0.9, "tiny": 0.03})
        px = self._px(codes, 10.0)
        al = O.to_lots(w, px, equity=12_000, min_lot_value=3000.0)
        self.assertIn("big", al.lots.index)
        self.assertNotIn("tiny", al.lots.index)

    def test_unaffordable_price_is_skipped_not_overspent(self):
        """一手就超过全部资金 ⇒ 不买, 而不是买一手把现金买成负数"""
        codes = ["cheap", "expensive"]
        w = pd.Series({"cheap": 0.5, "expensive": 0.5})
        px = pd.Series({"cheap": 8.0, "expensive": 5000.0})
        al = O.to_lots(w, px, equity=12_000)
        self.assertIn("cheap", al.lots.index)
        self.assertNotIn("expensive", al.lots.index)
        self.assertGreaterEqual(al.cash, -1e-6)

    def test_idle_cash_is_the_result_not_a_bug(self):
        """整手粒度必然剩现金 —— 0920 那轮已证明机械性闲置的现金是承重的,
        所以这里**断言它存在**, 防止有人为了"提高资金利用率"把粒度抹掉。"""
        codes = ["a", "b", "c"]
        px = pd.Series([33.3, 77.7, 11.1], index=codes)
        al = O.to_lots(pd.Series(0.31, index=codes), px, equity=12_000)
        self.assertGreater(al.cash, 0.0, "整手约束下不该出现零现金")

    def test_no_prices_means_nothing_bought(self):
        al = O.to_lots(pd.Series({"a": 1.0}), pd.Series(dtype="float64"), equity=12_000)
        self.assertEqual(len(al.lots), 0)
        self.assertAlmostEqual(al.cash, 12_000.0, places=6)


class TestLiquidityPinsTheUnlockScale(unittest.TestCase):
    """`Liquidity` 子盒在 2026-09-11 审计时是**唯一命名了却没落地**的约束。

    补它的方式不是"加上去保护", 而是"加上去**并证明它没生效**" —— 与
    `framework._stage_health` 的 `auto_retired: 0` 同一手法: 让"什么都没发生"
    成为一个被**观测**到的读数, 而不是一句靠读代码相信的断言。

    ⚠️ 这里用的 `ADV` 是**库里实测的 20260831 方案持仓**的 20 日均额。账户规模或
    持仓结构一变, 下面那条"解锁规模"就会红 —— 那就该回去重算模块 docstring 的表。
    """

    #: 20260831 方案的实际权重(`plan` 表)
    PLAN_W = pd.Series({"000571.SZ": 0.2071, "002513.SZ": 0.2320,
                        "300125.SZ": 0.1631, "300227.SZ": 0.2281,
                        "300787.SZ": 0.1698})
    #: 对应 20 日均成交额, **千元**(与 `daily.amount` 同单位)
    PLAN_ADV = pd.Series({"000571.SZ": 127176.86, "002513.SZ": 43494.14,
                          "300125.SZ": 87622.15, "300227.SZ": 300340.36,
                          "300787.SZ": 49886.62})

    def test_participation_at_the_real_scale_is_ten_thousandths_of_a_percent(self):
        p = O.participation_of(self.PLAN_W, 20_000.0, self.PLAN_ADV)
        self.assertAlmostEqual(100 * float(p.max()), 0.01067, places=4)
        self.assertEqual(p.idxmax(), "002513.SZ")

    def test_the_documented_unlock_scale_is_arithmentically_right(self):
        """docstring 写「账户放大到约 1 875 万才 binding」—— 把那句话钉在算术上"""
        p = O.participation_of(self.PLAN_W, 20_000.0, self.PLAN_ADV)
        headroom = O.DEFAULT_MAX_PART / float(p.max())
        self.assertAlmostEqual(headroom, 937, delta=5)
        unlock = 20_000.0 * headroom
        self.assertAlmostEqual(unlock / 1e4, 1_875, delta=30)     # 万元

    def test_adv_unit_is_thousands_not_yuan(self):
        """**单位陷阱**: adv 是千元。差 1000 倍正好把"永不 binding"变成"永远 binding"。

        同一个金额, adv 记成「千元」与记成「元」得到的参与率必须差正好 1000 倍。
        """
        w = pd.Series({"a": 0.5})
        adv_k = pd.Series({"a": 10_000.0})          # 1000 万元
        p_k = float(O.participation_of(w, 1_000.0, adv_k).iloc[0])
        self.assertAlmostEqual(p_k, 500.0 / (10_000.0 * 1000.0), places=12)


class TestLiquidityConstraintActuallyWorks(unittest.TestCase):
    """约束本身: 该不生效时不生效, 该生效时**必须真的挡住**"""

    def _scores(self, n=20):
        return pd.Series(np.arange(n, 0, -1, dtype=float),
                         index=["s%02d" % i for i in range(n)])

    def test_it_does_not_bind_at_two_wan_and_changes_nothing(self):
        """2 万账户 + 上亿元的日均额 ⇒ 权重(逐位)与不加 adv 时**完全相同**"""
        s = self._scores()
        adv = pd.Series(1e5, index=s.index)          # 1 亿元/日
        base = O.optimize(s, n=6, budget=0.95, method="qp")
        got = O.optimize(s, n=6, budget=0.95, method="qp",
                         equity=20_000.0, adv=adv, max_participation=0.10)
        self.assertEqual(got.method, base.method, got.diag)
        pd.testing.assert_series_equal(got.weights.sort_index(),
                                       base.weights.sort_index())
        self.assertLess(got.diag["part_max"], 1e-3)
        self.assertGreater(got.diag["part_headroom"], 100)

    def test_it_binds_when_the_name_is_illiquid(self):
        """**反向对照**: 不验证"能挡住", 上面那条就只是在证明"约束是空的"。

        每只日均额 5 千元(5000 元) × 10% = 500 元/只 ⇒ 6 只最多投 3 000 元,
        远低于 2 万 × 0.95。故总权重必须被压到 ~0.15 而不是 0.95。
        """
        s = self._scores()
        adv = pd.Series(5.0, index=s.index)          # 5 千元 = 5 000 元/日
        a = O.optimize(s, n=6, budget=0.95, method="qp",
                       equity=20_000.0, adv=adv, max_participation=0.10)
        if a.method != "qp":                          # cvxpy 缺失时回退, 跳过
            self.skipTest("cvxpy 不可用, 回退 topn")
        cap_amt = 5.0 * O.ADV_UNIT * 0.10             # 500 元
        for code, wv in a.weights.items():
            self.assertLessEqual(wv * 20_000.0, cap_amt * (1 + 1e-6) + 1e-6,
                                 "%s 的金额越过了参与率上限" % code)
        self.assertLess(float(a.weights.sum()), 0.95,
                        "约束没生效 —— 总权重仍是满仓")

    def test_a_name_without_adv_is_excluded_and_counted(self):
        """无有效成交额 ⇒ 剔除(与 PIT `engine.py:185` 的 NaN 保守口径一致), 且**留痕**"""
        s = self._scores(10)
        adv = pd.Series(1e5, index=s.index)
        adv["s00"] = np.nan                           # 最高分的那只没有成交额
        a = O.optimize(s, n=3, method="topn", equity=20_000.0, adv=adv)
        self.assertNotIn("s00", a.weights.index)
        self.assertEqual(a.diag["part_excluded"], 1)

    def test_diagnostics_present_on_the_unconstrained_path_too(self):
        """topn 不吃约束, 但**读数照样要有** —— 否则"没算"与"算了为零"长得一样"""
        s = self._scores()
        adv = pd.Series(1e5, index=s.index)
        a = O.optimize(s, n=5, method="topn", equity=20_000.0, adv=adv)
        self.assertIn("part_max", a.diag)
        self.assertIn("part_note", a.diag)

    def test_without_equity_there_is_no_fabricated_participation(self):
        """只给 adv 不给资金 ⇒ **不许编一个参与率出来**, 必须缺席"""
        s = self._scores()
        adv = pd.Series(1e5, index=s.index)
        a = O.optimize(s, n=5, method="topn", adv=adv)
        self.assertNotIn("part_max", a.diag)


if __name__ == "__main__":
    unittest.main(verbosity=2)
