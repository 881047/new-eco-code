"""周频终端(app/weekly)的守卫测试

守三条不变量:
  1. 构型阶梯 —— 资金规模 → (top_n, 出带, 持仓加成) 与实测表一致(改了必须同步改文档);
  2. 目标选择 —— 窄进宽出: 持仓在带内必留、带外必走、总数不超过 top_n;
  3. 锚点定义 —— ISO 周最后交易日(周频决策点),不因节假日错位。
外加:禁运常数必须 ≥3(5 日标签跨期, 小于它即泄漏)。

2026-09-13 追加: **尾部加权训练必须是显式开关** —— 默认(train 的 tail_weight=0)时不得给
LightGBM 传 `weight`, 否则生产打分就不再是原来那份(这是 F29 那次实验的安全边界)。
"""
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.weekly import plan as wk_plan        # noqa: E402
from app.weekly import scores as wk_scores    # noqa: E402


class TestShapeLadder(unittest.TestCase):
    """资金 → 构型阶梯:每档的实测读数写在 app/weekly/plan.py 的 docstring 里。"""

    def test_ladder_matches_measured_table(self):
        self.assertEqual(wk_plan.pick_shape(20000), (6, 0.0, 0.20))      # 实测 −2.04pp
        self.assertEqual(wk_plan.pick_shape(200000), (50, 3.0, 0.0))     # 实测 +7.98pp
        self.assertEqual(wk_plan.pick_shape(500000), (100, 5.0, 0.0))    # 实测 +8.85pp
        self.assertEqual(wk_plan.pick_shape(1000000), (100, 8.0, 0.0))   # 实测 +9.40~+10.09pp

    def test_equity_weight_defaults_to_monthly_key(self):
        """不写 weekly.equity_weight 时必须**继承**月频那个键(行为不变)。"""
        cfg = {"regime_equity_weight": {"aggressive": {"attack": 0.873}}}
        self.assertAlmostEqual(wk_plan.equity_weight(cfg), 0.873, places=6)
        self.assertAlmostEqual(wk_plan.equity_weight({}), 0.873, places=6)

    def test_equity_weight_sleeve_override_is_isolated(self):
        """写了 weekly.equity_weight ⇒ 只改袖套, **不动**月频那个键(F25 的敞口刻度靠它)。"""
        cfg = {"regime_equity_weight": {"aggressive": {"attack": 0.873}},
               "weekly": {"equity_weight": 1.0}}
        self.assertAlmostEqual(wk_plan.equity_weight(cfg), 1.0, places=6)
        self.assertAlmostEqual(cfg["regime_equity_weight"]["aggressive"]["attack"], 0.873,
                               places=6)      # 月频配置一个字没改


class TestArtifactMeta(unittest.TestCase):
    """规划 §6.1-2: 任一产物可反查它的输入版本。"""

    def _meta(self, cfg=None):
        return wk_plan.artifact_meta(cfg or {"weekly": {"equity_weight": 0.873},
                                             "fees": {"commission_rate": 0.00025}},
                                     BASE / "data" / "weekly" / "scores_20260911.npz",
                                     0.873, 100, 8.0, 0.0, 1.5)

    def test_meta_has_version_pointers(self):
        m = self._meta()
        for k in ("built_at", "code", "scores_file", "scores_sha16", "config_sha16",
                  "equity_weight", "top_n", "band", "max_lot_cost_mult"):
            self.assertIn(k, m)
        self.assertEqual(m["scores_file"], "scores_20260911.npz")
        self.assertEqual(len(m["scores_sha16"]), 16)      # 打分存档的内容指纹
        self.assertEqual(len(m["config_sha16"]), 16)      # 配置子集指纹

    def test_config_change_moves_the_fingerprint(self):
        """改了影响出方案的配置(如敞口) ⇒ 配置指纹必须变, 否则"反查输入版本"是假的。"""
        a = self._meta({"weekly": {"equity_weight": 0.873}})
        b = self._meta({"weekly": {"equity_weight": 1.0}})
        self.assertNotEqual(a["config_sha16"], b["config_sha16"])

    def test_missing_scores_file_is_not_fatal(self):
        m = wk_plan.artifact_meta({}, BASE / "data" / "weekly" / "_no_such.npz",
                                  0.873, 100, 8.0, 0.0, 1.5)
        self.assertIsNone(m["scores_sha16"])

    def test_config_override_wins(self):
        cfg = {"weekly": {"shape": {"top_n": 30, "band": 4.0, "hold_bonus": 0.1}}}
        self.assertEqual(wk_plan.pick_shape(1000000, cfg), (30, 4.0, 0.1))

    def test_sleeve_capital_prefers_weekly_key(self):
        """审计 #1: config 写了 weekly.capital 却读顶层 capital ⇒ 袖套会静默按 2 万跑。"""
        self.assertEqual(wk_plan.sleeve_capital({"capital": 20000, "weekly": {"capital": 1000000}}),
                         1000000.0)
        self.assertEqual(wk_plan.sleeve_capital({"capital": 20000}), 20000.0)      # 回退
        self.assertEqual(wk_plan.sleeve_capital({"capital": 20000}, override=50000), 50000.0)


class TestExpectationFollowsExposure(unittest.TestCase):
    """预期必须**连着敞口**走（2026-09-13）

    事故原型：`EXPECTATION` 是 w=0.873 那一个点上的实测值，写死在方案里。把
    `weekly.equity_weight` 改成 1.0 之后，方案会一边写"投出 87%"、一边挂 +14.6pp ——
    拿旧标签描述新配置（配置漂移）。守四条：
      ① 实测点给实测数；② 点之间给插值并**标注是插值**；③ 区间外标注"仅外推, 不可当读数用"；
      ④ 样本外与 DSR 都只在 w=0.873 上测过 ⇒ 换敞口后必须写明, 且不许反推新敞口显著。
    """

    def test_measured_points(self):
        for w, excess, mdd in ((0.873, 14.61, -32.3), (0.95, 16.32, -35.1),
                               (1.0, 17.63, -36.6)):
            e = wk_plan.expectation_at({"weekly": {"equity_weight": w}})
            self.assertAlmostEqual(e["excess_pp"], excess, places=2, msg=f"w={w}")
            self.assertAlmostEqual(e["mdd_pct"], mdd, places=2, msg=f"w={w}")
            self.assertEqual(e["equity_weight"], w)

    def test_source_names_who_measured_each_point(self):
        """**"谁测的"必须逐点正确**（2026-09-13 自查评审抓到：w=1.0 的数值已换成 F52，
        而 source 还写着 F25 —— 那是误标，和"研究构型≠生产构型"是同一类问题的另一半）。"""
        w0873 = wk_plan.expectation_at({"weekly": {"equity_weight": 0.873}})["source"]
        w100 = wk_plan.expectation_at({"weekly": {"equity_weight": 1.0}})["source"]
        self.assertIn("F25", w0873)
        self.assertIn("研究构型", w0873)
        self.assertIn("F52", w100, "w=1.0 的数值来自 F52，source 不许再写 F25")
        self.assertIn("生产构型", w100)
        self.assertNotIn("F25 实测点 w=1", w100)
        mid = wk_plan.expectation_at({"weekly": {"equity_weight": 0.9}})["source"]
        self.assertIn("插值", mid)

    def test_live_shape_numbers_replace_research_shape_numbers(self):
        """2026-09-13 F52：w=1.0 必须是**生产构型**（hold_bonus=0.0）读数，研究构型只作并列记录。

        起因：F25/F41 那批 100 万档实验都传了 `hyst=0.2`，而生产 `pick_shape(1e6)` 是 0.0
        ⇒ 方案里引用的 17.65pp/换手 0.092 描述的不是生产在跑的构型。
        """
        self.assertEqual(wk_plan.DIAL_POINTS[1.0]["excess_pp"], 17.63)
        self.assertEqual(wk_plan.DIAL_POINTS[1.0]["mdd_pct"], -36.6)
        self.assertIn("研究构型", wk_plan.DIAL_CONFIG_NOTE)
        self.assertIn("hyst=0.2", wk_plan.DIAL_CONFIG_NOTE)
        self.assertEqual(wk_plan.EXPECTATION["turnover"], 0.137,
                         "换手必须是生产构型实测（F52），不是 F25 研究构型的 0.092")

    def test_interpolated_point_is_labelled(self):
        e = wk_plan.expectation_at({"weekly": {"equity_weight": 0.90}})
        self.assertIn("插值", e["source"])
        self.assertLess(e["excess_pp"], 16.32)
        self.assertGreater(e["excess_pp"], 14.61)

    def test_out_of_range_is_labelled(self):
        e = wk_plan.expectation_at({"weekly": {"equity_weight": 0.60}})
        self.assertIn("超出", e["source"])

    def test_oos_and_dsr_carry_their_calibration_weight(self):
        """样本外是**逐敞口**测的：w=0.873 那份来自 F28，w=1.0 那份来自 F41（2026-09-13 对齐）。"""
        e = wk_plan.expectation_at({"weekly": {"equity_weight": 1.0}})
        self.assertEqual(e["oos_24m"]["measured_at_weight"], 1.0)
        self.assertEqual(e["oos_24m"]["measured_by"], "F52")
        self.assertEqual(e["oos_24m"]["excess_pp"], [9.21, 11.40])
        self.assertEqual(e["oos_24m"]["research_config_excess_pp"], [10.38, 11.80],
                         "研究构型读数必须并列保留（F41 的记录不许丢）")
        self.assertIn("C3", e["oos_24m"]["note"], "跨过 +10pp 线不等于 C3 通过 —— 必须写出来")
        self.assertEqual(e["multiplicity"]["computed_at_weight"], 0.873)
        self.assertIn("未重算", e["multiplicity"]["note"])

    def test_unmeasured_weight_falls_back_with_label(self):
        """w=0.95 的全史来自 F25 实测点，但**样本外**只在 0.873/1.0 上测过 ⇒ 退回最近那点并写明。"""
        e = wk_plan.expectation_at({"weekly": {"equity_weight": 0.95}})
        self.assertIn("F25 实测点", e["source"])
        self.assertEqual(e["oos_24m"]["measured_at_weight"], 1.0)
        self.assertIn("只在 w=1", e["oos_24m"]["note"])

    def test_old_weight_keeps_f28_reading(self):
        e = wk_plan.expectation_at({"weekly": {"equity_weight": 0.873}})
        self.assertEqual(e["oos_24m"]["measured_by"], "F28")
        self.assertEqual(e["oos_24m"]["excess_pp"], [7.34, 8.62])
        self.assertAlmostEqual(e["oos_24m"]["p_corrected"], 1.00)

    def test_monthly_line_follows_exposure_with_sample_size(self):
        """用户的目标函数是"月收益" ⇒ 月度那一行必须跟着敞口走, 且必须带"多久才够判"。"""
        e = wk_plan.expectation_at({"weekly": {"equity_weight": 1.0}})
        mo = e["monthly"]
        self.assertEqual(mo["measured_by"], "F53")
        self.assertEqual(mo["measured_at_weight"], 1.0)
        self.assertEqual(mo["full"]["mean_pp"], [1.34, 1.31])
        self.assertEqual(mo["oos"]["mean_pp"], [0.76, 0.91])
        self.assertEqual(mo["research_config"]["oos_mean_pp"], [0.84, 0.93],
                         "研究构型（F45）的月度读数要并列保留")
        self.assertGreaterEqual(mo["months_for_2se_at_0_83pp"], 60,
                                "必须写明要 ~6 年才够判, 否则 2~3 个月纸面样本会被误读")

    def test_monthly_falls_back_with_warning(self):
        e = wk_plan.expectation_at({"weekly": {"equity_weight": 0.95}})
        self.assertEqual(e["monthly"]["measured_at_weight"], 1.0)
        self.assertIn("只在 w=1", e["monthly"]["note"])

    def test_module_level_expectation_is_not_mutated(self):
        before = json.dumps(wk_plan.EXPECTATION, ensure_ascii=False, sort_keys=True)
        wk_plan.expectation_at({"weekly": {"equity_weight": 1.0}})
        after = json.dumps(wk_plan.EXPECTATION, ensure_ascii=False, sort_keys=True)
        self.assertEqual(before, after, "expectation_at 不许改模块级 EXPECTATION（它是历史记录）")


class TestSelectTargets(unittest.TestCase):
    def _sc(self, n=300):
        # 分数严格递减: c0 最高
        return pd.Series(np.linspace(1.0, 0.0, n), index=[f"c{i}" for i in range(n)])

    def test_fills_to_top_n_when_no_holdings(self):
        sc = self._sc()
        ok = np.ones(len(sc), dtype=bool)
        tgt, rk = wk_plan.select_targets(sc, ok, [], 10, 0.0, 0.0)
        self.assertEqual(tgt, [f"c{i}" for i in range(10)])
        self.assertEqual(int(rk["c0"]), 1)

    def test_band_keeps_held_names_inside_band(self):
        """持仓排名 35、top_n=10、band=5(带=50) ⇒ 必须保留。"""
        sc = self._sc()
        ok = np.ones(len(sc), dtype=bool)
        held = ["c35"]
        tgt, rk = wk_plan.select_targets(sc, ok, held, 10, 5.0, 0.0)
        self.assertIn("c35", tgt)
        self.assertEqual(len(tgt), 10)
        # 补满的九个是排名最高的非持仓
        self.assertEqual([c for c in tgt if c != "c35"], [f"c{i}" for i in range(9)])

    def test_band_drops_held_names_outside_band(self):
        """持仓排名 60、带=50 ⇒ 必须卖掉。"""
        sc = self._sc()
        ok = np.ones(len(sc), dtype=bool)
        tgt, _ = wk_plan.select_targets(sc, ok, ["c60"], 10, 5.0, 0.0)
        self.assertNotIn("c60", tgt)
        self.assertEqual(len(tgt), 10)

    def test_untradable_names_never_selected(self):
        sc = self._sc()
        ok = np.ones(len(sc), dtype=bool)
        ok[:5] = False                      # 前五名不可交易(停牌/ST/一字板等)
        tgt, _ = wk_plan.select_targets(sc, ok, [], 10, 0.0, 0.0)
        self.assertEqual(tgt, [f"c{i}" for i in range(5, 15)])

    def test_hold_bonus_keeps_marginal_holding(self):
        """持仓 c10 刚好掉出 top10;给 σ 加成后应被拉回(小资金构型靠这个降换手)。"""
        sc = self._sc(300)
        ok = np.ones(len(sc), dtype=bool)
        tgt_no, _ = wk_plan.select_targets(sc, ok, ["c10"], 10, 0.0, 0.0)
        self.assertNotIn("c10", tgt_no)
        tgt_bonus, _ = wk_plan.select_targets(sc, ok, ["c10"], 10, 0.0, 0.20)
        self.assertIn("c10", tgt_bonus)

    def test_band_is_pointless_without_held(self):
        """生产路径的教训(2026-09-13 回放实测): 不传 held 时, 退出带退化成"每周取前 N 名"
        ⇒ 实盘换手比回测高数倍。这条断言把该语义钉住, 提醒调用方必须传当前持仓。"""
        sc = self._sc(300)
        ok = np.ones(len(sc), dtype=bool)
        tgt_no_held, _ = wk_plan.select_targets(sc, ok, [], 10, 8.0, 0.0)
        self.assertEqual(tgt_no_held, [f"c{i}" for i in range(10)],
                         "held 为空时 band 不生效(目标=纯前 N 名)")


class TestAnchorsAndLeakage(unittest.TestCase):
    def test_plan_price_cap_default_on(self):
        """选股阶段的价格上限默认 1.5×(2026-09-13 F23):
        新账户在 100 万/top100 下实测把投出率从 63.5% 抬到 69.7%, 而回测里 ±0.1pp 无差异。
        0 = 关; 改这个默认值等于换构型, 必须重跑 F23 式对照后同步文档。"""
        import inspect
        self.assertEqual(
            inspect.signature(wk_plan.build).parameters["max_lot_cost_mult"].default, 1.5)

    def test_week_end_dates_picks_last_session_of_iso_week(self):
        # 2026-09-07(周一) ~ 2026-09-11(周五) + 下周一
        d = pd.DatetimeIndex(["2026-09-07", "2026-09-08", "2026-09-09",
                              "2026-09-10", "2026-09-11", "2026-09-14"])
        out = wk_scores.week_end_dates(d)
        self.assertEqual(list(out), [pd.Timestamp("2026-09-11"), pd.Timestamp("2026-09-14")])

    def test_holiday_week_uses_last_available_session(self):
        # 国庆周只有 9/30 开市
        d = pd.DatetimeIndex(["2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09"])
        out = wk_scores.week_end_dates(d)
        self.assertIn(pd.Timestamp("2026-09-30"), list(out))
        self.assertIn(pd.Timestamp("2026-10-09"), list(out))

    def test_embargo_prevents_label_overlap(self):
        """禁运 ≥3 是硬约束:5 日标签的价格窗跨到 t+6, 训练锚点必须比预测锚点早 ≥3 个。"""
        self.assertGreaterEqual(wk_scores.EMBARGO, 3)


class TestTailWeightIsOptIn(unittest.TestCase):
    """尾部加权(F29)必须是**显式开关**: 默认路径不传 weight ⇒ 生产打分逐位不变。"""

    @staticmethod
    def _panel(n_anchor=50, n_code=1000, n_feat=3):
        anchors = pd.date_range("2020-01-31", periods=n_anchor, freq=pd.offsets.MonthEnd())
        codes = [f"{i:06d}.SZ" for i in range(n_code)]
        dates = pd.bdate_range("2019-01-01", periods=n_anchor * 21)
        rng = np.random.default_rng(0)
        open_ = pd.DataFrame(rng.uniform(5, 20, (len(dates), n_code)), index=dates, columns=codes)
        return {"anchors": anchors, "cols": codes,
                "F": rng.normal(size=(n_anchor, n_code, n_feat)).astype(np.float32),
                "names": [f"f{i}" for i in range(n_feat)],
                "m": {"open": open_},
                "mask": pd.DataFrame(True, index=anchors, columns=codes)}

    def _capture(self, **kw):
        seen = []

        class _DS:                       # lgb.Dataset 替身: 只记 weight
            def __init__(self, *a, **k):
                seen.append(k.get("weight"))

        class _BO:
            def predict(self, X):
                return np.zeros(len(X))

        with mock.patch("lightgbm.Dataset", _DS), \
                mock.patch("lightgbm.train", return_value=_BO()), \
                mock.patch.object(wk_scores, "TRAIN_MONTHS", 600):
            wk_scores.train(self._panel(), {}, seeds=(1,), rounds=1, **kw)
        return seen

    def test_default_does_not_pass_weight(self):
        seen = self._capture()
        self.assertTrue(seen, "至少要训出一个模型, 否则这条测试没覆盖到")
        self.assertTrue(all(w is None for w in seen),
                        "默认(train tail_weight=0)不得给 LightGBM 传 weight")

    def test_tail_weight_passes_two_level_weights(self):
        seen = self._capture(tail_weight=4.0, tail_quantile=0.8)
        w = next((x for x in seen if x is not None), None)
        self.assertIsNotNone(w, "tail_weight>0 必须真的传权重")
        self.assertEqual(len(np.unique(w)), 2)          # 只有 1 与 1+tail_weight 两档
        self.assertAlmostEqual(float(np.max(w)), 5.0, places=6)
        self.assertLess(float(np.mean(w == 5.0)), 0.3)  # 只有约 20% 样本被放大


class TestPermuteLabelsOptIn(unittest.TestCase):
    """置换检验（F32）必须显式开启，且只切断"特征↔标签"的配对 —— 标签多重集不许变。"""

    def _capture(self, **kw):
        seen = []

        class _DS:
            def __init__(self, *a, **k):
                seen.append(np.array(k.get("label"), dtype=float))

        class _BO:
            def predict(self, X):
                return np.zeros(len(X))

        with mock.patch("lightgbm.Dataset", _DS), \
                mock.patch("lightgbm.train", return_value=_BO()), \
                mock.patch.object(wk_scores, "TRAIN_MONTHS", 600):
            wk_scores.train(TestTailWeightIsOptIn._panel(), {}, seeds=(1,), rounds=1, **kw)
        return seen

    def test_default_is_not_permuted(self):
        a, b = self._capture(), self._capture()
        self.assertTrue(a and b)
        np.testing.assert_allclose(a[0], b[0], equal_nan=True)   # 同面板同种子 ⇒ 逐位相同

    def test_permuted_keeps_multiset_but_breaks_pairing(self):
        base = self._capture()[0]
        perm = self._capture(permute_labels=True, permute_seed=7)[0]
        self.assertEqual(len(base), len(perm))
        np.testing.assert_allclose(np.sort(base[~np.isnan(base)]),
                                   np.sort(perm[~np.isnan(perm)]))   # 多重集不变
        self.assertFalse(np.allclose(base, perm, equal_nan=True))    # 配对被打乱


class TestBuildOrchestration(unittest.TestCase):
    """`plan.build` 的**编排层**（2026-09-13 关键路径测试第三批）。

    为什么非测不可：2026-09-13 回放抓到的真 bug 就在这一层 —— 已持有的票若"预算买不起一手"
    会被当成新票丢掉 ⇒ 实盘卖掉本该留的仓位、换手比回测高数倍。而在此之前
    `select_targets`（纯函数）有覆盖、`build`（真正出方案的编排）**一条测试都没有**。

    做法：mock 掉 DB / 行情 / PIT 掩码三处 IO，只验决策算术（整手、留存、丢弃计数、权重）。
    """

    CAPITAL = 1_000_000.0
    CODES = [f"{i:06d}.SZ" for i in range(1, 301)]      # 300 只 ⇒ 过"可交易 <200 拒绝出方案"那道闸

    def _conn(self, prices: dict):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE daily (ts_code TEXT, trade_date TEXT, close REAL)")
        conn.executemany("INSERT INTO daily VALUES (?,?,?)",
                         [(c, "20260911", prices.get(c, 10.0)) for c in self.CODES])
        return conn

    def _run(self, prices=None, positions=None, held=(), mask=None):
        """held=None ⇒ 走"从账本读持仓"的真实分支（留存逻辑只在这条分支里生效）。"""
        prices = prices or {}
        scores = np.array([[300.0 - i for i in range(len(self.CODES))]])   # 第 0 名分数最高
        anchors = pd.DatetimeIndex(["2026-09-11"])
        info = pd.DataFrame({"ts_code": self.CODES,
                             "close": [prices.get(c, 10.0) for c in self.CODES],
                             "name": ["N"] * len(self.CODES),
                             "industry": ["I"] * len(self.CODES)}).set_index("ts_code")
        m = np.ones(len(self.CODES), dtype=bool) if mask is None else mask
        ledger = mock.Mock()
        ledger.state = {"positions": positions or {}}
        cfg = {"weekly": {"equity_weight": 1.0}}
        with mock.patch.object(wk_plan.sc, "latest",
                               return_value=(scores, anchors, self.CODES, ["f1"], BASE / "_no_scores.npz")), \
             mock.patch.object(wk_plan.db, "get_conn", return_value=self._conn(prices)), \
             mock.patch.object(wk_plan, "_tradable_at", return_value=m), \
             mock.patch.object(wk_plan, "_latest_prices", return_value=info), \
             mock.patch("app.weekly.ledger.Ledger.load", return_value=ledger):
            return wk_plan.build(capital=self.CAPITAL, held=held, cfg=cfg, write=False)

    def test_held_name_too_expensive_for_one_lot_is_kept_not_sold(self):
        """回放实测的那个 bug：预算 1 万、股价 150 ⇒ 整手 0 股, 但**已持有要原样保留**。"""
        out = self._run(prices={"000001.SZ": 150.0},
                        positions={"000001.SZ": {"shares": 300}}, held=None)
        rows = {r["ts_code"]: r for r in out["rows"]}
        self.assertIn("000001.SZ", rows)
        self.assertEqual(rows["000001.SZ"]["shares"], 300)      # 不补仓、不清仓
        self.assertTrue(rows["000001.SZ"]["held"])
        self.assertEqual(out["dropped"]["unaffordable"], 0)

    def test_new_name_too_expensive_for_one_lot_is_dropped_and_counted(self):
        """同样的价格，但**不是持仓** ⇒ 买不起一手就丢弃，并计入 dropped.unaffordable。"""
        out = self._run(prices={"000001.SZ": 150.0}, held=[])
        codes = {r["ts_code"] for r in out["rows"]}
        self.assertNotIn("000001.SZ", codes)
        self.assertEqual(out["dropped"]["unaffordable"], 1)

    def test_price_cap_excludes_before_lot_sizing(self):
        """一手成本 > 1.5× 单票预算 ⇒ 在**选股阶段**就被剔除（不是等到整手计算）。"""
        out = self._run(prices={"000001.SZ": 200.0}, held=[])
        codes = {r["ts_code"] for r in out["rows"]}
        self.assertNotIn("000001.SZ", codes)
        self.assertEqual(out["dropped"]["unaffordable"], 0)     # 被价格上限挡掉, 不算"买不起"

    def test_weights_and_cash_identity(self):
        out = self._run(held=[])
        self.assertLessEqual(len(out["rows"]), out["top_n"])
        self.assertAlmostEqual(sum(r["weight"] for r in out["rows"]), 1.0, places=3)
        self.assertAlmostEqual(out["invested"] + out["cash_left"], self.CAPITAL, places=2)
        self.assertAlmostEqual(out["invested_ratio"], out["invested"] / self.CAPITAL, places=4)
        self.assertEqual(out["n_target"], len(out["rows"]))

    def test_refuses_plan_when_tradable_universe_too_small(self):
        """可交易且可打分的股票 <200 ⇒ 拒绝出方案（宁可不出，不许拿残池子硬出）。"""
        mask = np.zeros(len(self.CODES), dtype=bool)
        mask[:100] = True
        with self.assertRaises(RuntimeError):
            self._run(held=[], mask=mask)

    def test_corrupt_ledger_file_refuses_to_build_with_empty_holdings(self):
        """2026-09-14（与 07 #48 同族）：账本**文件存在但读不动** ⇒ 必须失败，不许按空仓出方案。

        按空仓出方案的后果不是"少一点信息"：退出带整体失效 ⇒ 目标退化成"前 N 名"，
        执行档会把所有不在新目标里的留存票卖掉（换手/费用双爆）。文件**不存在**才是正常
        首跑（`Ledger.load` 返回空账本，走不到这个分支）—— 故只有真损坏会触发。
        红检：把 raise 改回 `held = []` ⇒ 本用例当场红。
        """
        p = BASE / "data" / "weekly" / "_test_plan_corrupt_ledger.json"
        try:
            p.write_text('{"capital": 1000000.0, "cash": 1.0', encoding="utf-8")  # 半截 JSON
            with self.assertRaises(RuntimeError) as cm:
                wk_plan.build(asof="20260911", capital=1_000_000.0, ledger_path=p)
            self.assertIn("账本不可读", str(cm.exception))
        finally:
            if p.exists():
                p.unlink()


if __name__ == "__main__":
    unittest.main()
