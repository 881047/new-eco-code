"""Alpha Factory 单测 (2026-09-11 「芯片式异构架构」Alpha 层)

三条主线, 每一条都对应一个**已经出过错或几乎出错**的地方:

1. **算子语义**: `app/alpha/expr.py` 是 qlib Alpha158 的端口。移植时只对照了
   `loader.py` 的表达式字符串, 没对照 `qlib/data/ops.py` 的算子实现 —— 事后审计
   抓出 `Rank` 反号(与自己的文档矛盾)与 `IdxMax/IdxMin` 镜像。**方向错了化验单
   照样漂亮**, 所以这里把方向钉死: 价格单调递增的合成面板上, `RANK` 必须给 1.0、
   `IdxMax` 必须给 n(当日即最高点)。
2. **交叉校验**: `gate.evaluate` 与 `app/models/expert.py::daily_head_stats` 必须逐位
   一致 —— 而 `expert` 已被 `test_expert.py` 钉在**冻结的** `exp_deadzone_ic.py` 上。
   于是新门禁通过两条链被锚在同一把尺上(冻结的判据比 DRY 值钱)。
3. **门禁与登记的生命周期**: 新的第五条判据(死区 top6 不得为负)必须真的能一票否决;
   `alpha_bank` 必须拒绝对非 VALIDATED 的 alpha 开放生产晋升。

运行: ./.venv/Scripts/python.exe -m pytest tests/test_alpha.py -q
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# 2026-09-12 审查(测试质量项): 这里原先还有一行把 `scripts/oneoff/` 插进 sys.path ——
# 本文件**一个一次性脚本都没 import**(引用的都是 `app.*`), 纯属历史残留。已删: 留着会
# 让目录里几十个 `exp_*.py` 对整轮测试裸名可见, 谁的一句 `import exp_*` 都可能命中冻结
# 脚本, 命中与否只取决于 import 顺序。与本文件同源的交叉校验在 `tests/test_expert.py`,
# 那边改成按**文件路径**加载冻结脚本。

import numpy as np
import pandas as pd

from app import db
from app.alpha import bank, expr as E, gate, zoos
from app.alpha import factory as factory_mod
from app.models import expert as ex


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _close(values_by_code, start="2021-01-04"):
    idx = pd.bdate_range(start, periods=len(next(iter(values_by_code.values()))))
    return pd.DataFrame(values_by_code, index=idx)


def _eng(close, **over):
    """由收盘价造一个自洽面板(open/high/low/vol/... 都由 close 派生)。"""
    panel = {"open": close - 1.0, "high": close * 1.02, "low": close * 0.98,
             "close": close, "pre_close": close.shift(1),
             "vol": close * 10.0, "amount": close * 1000.0, "turnover": close,
             "total_mv": close, "circ_mv": close, "pb": close, "pe_ttm": close}
    panel.update(over)
    panel["vwap"] = (panel["amount"] * 10.0) / panel["vol"].replace(0, np.nan)
    return E.Engine(panel)


def _panel(seed=0, n_dates=6, n_stocks=400):
    """与 test_expert.py 同构的合成截面(便于两处对拍)"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-09-30", periods=n_dates, freq="ME")
    cols = [f"{i:06d}.SZ" for i in range(n_stocks)]
    f = pd.DataFrame(rng.normal(size=(n_dates, n_stocks)), index=idx, columns=cols)
    r = f * 0.01 + rng.normal(0, 0.05, size=(n_dates, n_stocks))
    return idx, f, pd.DataFrame(r, index=idx, columns=cols)


def _market(seed=0, n_dates=80, n_stocks=80):
    """`fe.load_market()` 的替身(给 build_panel / verify_causal 用)"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2021-01-04", periods=n_dates)
    cols = [f"{i:06d}.SZ" for i in range(n_stocks)]
    close = pd.DataFrame(
        100 * np.exp(np.cumsum(rng.normal(0, 0.02, (n_dates, n_stocks)), axis=0)),
        index=idx, columns=cols)
    vol = pd.DataFrame(rng.integers(1, 100, (n_dates, n_stocks)).astype(float),
                       index=idx, columns=cols)
    return {"open": close * 0.99, "high": close * 1.02, "low": close * 0.98,
            "close": close, "pre_close": close.shift(1), "vol": vol,
            "amount": close * 1000, "turnover": close, "total_mv": close,
            "circ_mv": close, "pb": close, "pe_ttm": close}


# ===========================================================================
# 1. 算子语义
# ===========================================================================
class TestExprSemantics(unittest.TestCase):

    def test_kmid_hand_computed(self):
        px = _close({"a": [10.0, 12.0, 11.0], "b": [20.0, 18.0, 22.0]})
        got = _eng(px).eval("($close-$open)/$open")
        np.testing.assert_allclose(got.to_numpy(), (1.0 / (px - 1.0)).to_numpy(),
                                   rtol=1e-12)

    def test_ref_is_shift_and_delta_is_difference(self):
        px = _close({"a": [10.0, 12.0, 11.0, 15.0], "b": [20.0, 18.0, 22.0, 19.0]})
        eng = _eng(px)
        np.testing.assert_allclose(eng.eval("Ref($close, 1)").to_numpy(),
                                   px.shift(1).to_numpy(), equal_nan=True)
        np.testing.assert_allclose(eng.eval("Delta($close, 2)").to_numpy(),
                                   (px - px.shift(2)).to_numpy(), equal_nan=True)

    def test_ref_negative_window_is_rejected(self):
        """负窗口 = 前视; 必须在**求值前**被拒, 而不是算出个好看的数字"""
        px = _close({"a": [10.0, 12.0, 11.0]})
        with self.assertRaises(E.ExprError):
            _eng(px).eval("Ref($close, -1)")

    def test_greater_is_elementwise_max_not_comparison(self):
        """qlib 的 `Greater` 是取大, `>` 才是比较 —— 这两个混了会静默算错"""
        px = _close({"a": [10.0, 12.0, 11.0], "b": [20.0, 18.0, 22.0]})
        eng = _eng(px)
        prev = px.shift(1)
        np.testing.assert_allclose(
            eng.eval("Greater($close, Ref($close, 1))").to_numpy(),
            np.maximum(px, prev).to_numpy(), equal_nan=True)
        np.testing.assert_allclose(
            eng.eval("$close>Ref($close, 1)").to_numpy(),
            (px > prev).astype(float).to_numpy(), equal_nan=True)

    def test_rank_is_increasing_in_price(self):
        """⚠️ 回归测试: 原实现统计的是"过去值 ≥ 当日", 使 RANK 与价格**反号**。

        单调递增 ⇒ 当日是窗口内最高 ⇒ 分位 1.0; 单调递减 ⇒ 只有自己 ≤ 自己 ⇒ 1/n。
        """
        up = _close({"a": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]})
        down = _close({"a": [6.0, 5.0, 4.0, 3.0, 2.0, 1.0]})
        self.assertAlmostEqual(_eng(up).eval("Rank($close, 5)").iloc[-1, 0], 1.0)
        self.assertAlmostEqual(_eng(down).eval("Rank($close, 5)").iloc[-1, 0], 1.0 / 5.0)

    def test_rank_range_and_warmup(self):
        px = _close({"a": np.linspace(10, 20, 30), "b": np.linspace(20, 10, 30)})
        v = _eng(px).eval("Rank($close, 5)")
        self.assertTrue(v.iloc[4:].notna().all().all(), "窗口满了就该有值")
        self.assertTrue(v.iloc[:4].isna().all().all(), "窗口不满必须是 NaN(不拿半截窗口出数)")
        self.assertTrue(((v.dropna() > 0) & (v.dropna() <= 1)).all().all())

    def test_idxmax_position_matches_qlib(self):
        """qlib `IdxMax` = `argmax()+1`(窗口按时间正序) ⇒ **n = 当日就是最高点**"""
        up = _close({"a": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]})
        down = _close({"a": [6.0, 5.0, 4.0, 3.0, 2.0, 1.0]})
        self.assertAlmostEqual(_eng(up).eval("IdxMax($high, 5)").iloc[-1, 0], 5.0)
        self.assertAlmostEqual(_eng(down).eval("IdxMax($high, 5)").iloc[-1, 0], 1.0)
        self.assertAlmostEqual(_eng(down).eval("IdxMin($low, 5)").iloc[-1, 0], 5.0)
        self.assertAlmostEqual(_eng(up).eval("IdxMin($low, 5)").iloc[-1, 0], 1.0)

    def test_idxmax_tie_keeps_oldest(self):
        """并列时取最旧位置(等价于 argmax 取首个出现的语义)"""
        flat = _close({"a": [7.0] * 6})
        self.assertAlmostEqual(_eng(flat).eval("IdxMax($high, 5)").iloc[-1, 0], 1.0)

    def test_slope_and_rsquare_match_numpy(self):
        px = _close({"a": [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0]})
        v = px["a"].to_numpy()
        w = 5
        want_slope = np.polyfit(np.arange(w), v[-w:], 1)[0]
        want_r2 = np.corrcoef(np.arange(w), v[-w:])[0, 1] ** 2
        got = _eng(px)
        self.assertAlmostEqual(got.eval("Slope($close, 5)").iloc[-1, 0], want_slope, places=9)
        self.assertAlmostEqual(got.eval("Rsquare($close, 5)").iloc[-1, 0], want_r2, places=9)

    def test_mad_is_deviation_from_mean_not_median(self):
        """qlib 的 `Mad` 是对**均值**的平均绝对偏差(名字有误导性)

        `Alpha158` 的 158 条里没有 Mad, 但算子库对外承诺"qlib 语义", 故一并钉死。
        ⚠️ 回归点: 原实现(残差再做一次 rolling)结构上**恒为 NaN** —— 本测试就是为了
        让那种"永远不会出数但也不报错"的实现红掉。
        """
        v = np.array([1.0, 2.0, 3.0, 4.0, 100.0])
        px = _close({"a": list(v)})
        want = float(np.mean(np.abs(v - v.mean())))
        got = _eng(px).eval("Mad($close, 5)").iloc[-1, 0]
        self.assertFalse(np.isnan(got), "Mad 恒为 NaN = 结构坏了, 不是数据问题")
        self.assertAlmostEqual(got, want, places=9)
        # 中位数版本会给出完全不同的数(20.2 vs 31.2) —— 确认实现确实是对均值的
        self.assertNotAlmostEqual(want, float(np.mean(np.abs(v - np.median(v)))), places=6)

    def test_quantile_and_corr(self):
        px = _close({"a": [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0],
                     "b": [6.0, 2.0, 9.0, 5.0, 1.0, 4.0, 1.0, 3.0]})
        eng = _eng(px)
        want_q = float(pd.Series(px["a"].to_numpy()[-5:]).quantile(0.8))
        self.assertAlmostEqual(eng.eval("Quantile($close, 5, 0.8)").iloc[-1, 0], want_q, places=9)
        # 第二个序列必须是**真**独立的: `_eng` 里 open/high/low/vol 全是 close 的仿射
        # 变换, 相关系数恒为 1 —— 那样这个测试什么都没验。
        vol = pd.DataFrame({"a": [5.0, 3.0, 8.0, 1.0, 9.0, 2.0, 7.0, 4.0],
                            "b": [2.0, 8.0, 1.0, 6.0, 3.0, 9.0, 5.0, 7.0]},
                           index=px.index)
        eng = _eng(px, vol=vol)
        want_c = float(pd.Series(px["a"].to_numpy()[-5:])
                       .corr(pd.Series(vol["a"].to_numpy()[-5:])))
        self.assertAlmostEqual(eng.eval("Corr($close, $volume, 5)").iloc[-1, 0],
                               want_c, places=9)

    def test_unknown_op_and_dangling_token_raise(self):
        px = _close({"a": [1.0, 2.0, 3.0]})
        eng = _eng(px)
        with self.assertRaises(E.ExprError):
            eng.eval("NoSuchOp($close, 5)")
        with self.assertRaises(E.ExprError):
            eng.eval("$close $close")
        with self.assertRaises(E.ExprError):
            eng.eval("Mean($close, $close)")     # 动态窗口不支持


class TestCausality(unittest.TestCase):
    """前视守卫(静态 + 动态)"""

    def test_zoo_is_causal(self):
        """抽样的 zoo 表达式在截断重算下必须逐位不变"""
        m = _market()
        sample = [e for n, e, _ in zoos.qlib158()
                  if n.rstrip("0123456789") in
                  ("ROC", "STD", "BETA", "RESI", "QTLU", "RANK", "RSV", "IMXD",
                   "CORD", "SUMD", "VSTD", "WVMA")]
        self.assertGreaterEqual(len(sample), 10)
        for expr in sample:
            r = E.verify_causal(expr, m)
            self.assertTrue(r["causal"], f"{expr} 疑似前视: {r}")
            self.assertEqual(r["max_abs_diff"], 0.0)

    def test_verify_causal_detects_injected_lookahead(self):
        """把守卫本身验一下: 手工构造一条"读未来"的表达式, 必须被抓住。

        引擎不允许 `Ref(…, -n)`, 但允许 `$close/$close` 这类无害式子 —— 为了让本测试
        真的能失败, 这里直接对**面板**做手脚: 把最后一天的值改掉, 截断面板的读数
        就会与全量不同。若 `verify_causal` 恒返回 True, 这条会红。
        """
        m = _market()
        m2 = {k: v.copy() for k, v in m.items()}
        m2["close"].iloc[-1] = m2["close"].iloc[-1] * 10.0
        a = E.Engine(E.build_panel(m)).eval("Mean($close, 5)")
        b = E.Engine(E.build_panel(m2)).eval("Mean($close, 5)")
        self.assertGreater(float((a.iloc[-1] - b.iloc[-1]).abs().max()), 0.0)


# ===========================================================================
# 2. 交叉校验(门禁 ↔ KGD 测试台 ↔ 冻结脚本)
# ===========================================================================
class TestGateMatchesExpert(unittest.TestCase):
    """`gate.evaluate` 与 `expert.daily_head_stats` 必须逐位一致

    `expert` 已被 `test_expert.py` 钉在冻结的 `exp_deadzone_ic.py` 上, 故这里是
    同一把尺的第二段链条: 任一处漂移都会让"门禁说通过、KGD 说退役"无法解释。
    """

    def test_head_and_top6_match(self):
        idx, f, r = _panel()
        det = gate.evaluate(f, r, idx, np.array(["DEAD"] * len(idx)))
        ref = ex.daily_head_stats(f, r, idx)
        self.assertEqual(len(det), len(ref))
        for col in ("head_excess", "top6_excess", "n"):
            np.testing.assert_allclose(
                det[col].reindex(ref.index).to_numpy(), ref[col].to_numpy(),
                rtol=0, atol=1e-12, err_msg=f"{col} 与 KGD 测试台不一致")

    def test_head_and_top6_match_with_nan(self):
        idx, f, r = _panel(seed=3)
        f = f.where(np.random.default_rng(1).random(f.shape) > 0.3)
        det = gate.evaluate(f, r, idx, np.array(["DEAD"] * len(idx)))
        ref = ex.daily_head_stats(f, r, idx)
        np.testing.assert_allclose(det["head_excess"].reindex(ref.index).to_numpy(),
                                   ref["head_excess"].to_numpy(), rtol=0, atol=1e-12)

    def test_zone_of_uses_preregistered_windows(self):
        d = pd.to_datetime(["2020-08-31", "2020-09-30", "2023-12-29", "2024-01-31",
                            "2026-08-31", "2026-09-11"])
        np.testing.assert_array_equal(
            gate.zone_of(d),
            np.array(["OTHER", "DEAD", "DEAD", "ACTIVE", "ACTIVE", "OTHER"]))


class TestVerdictSingle(unittest.TestCase):
    """单因子档判据（预注册 plans/single-factor-gate-prereg-20260926.md）:
    S1 |head|>=0.3% / S2 |t|>=2 / S3 同号 / S4 去相关(未测不否决)。"""

    def _v(self, **over):
        rec = {"dead_head": 0.004, "dead_t": 2.5, "active_head": 0.002,
               "shadow_abs_rho": 0.5}
        rec.update(over)
        return gate.verdict_single(**rec)

    def test_passes_when_all_hold(self):
        self.assertTrue(self._v()["PASS_single"])

    def test_each_criterion_vetoes(self):
        for over in ({"dead_head": 0.002}, {"dead_t": 1.5}, {"active_head": -0.002},
                     {"shadow_abs_rho": 0.9}):
            with self.subTest(over=over):
                self.assertFalse(self._v(**over)["PASS_single"])

    def test_unknown_rho_does_not_veto(self):
        self.assertTrue(self._v(shadow_abs_rho=None)["PASS_single"])

    def test_negative_head_abs_bar(self):
        self.assertTrue(self._v(dead_head=-0.004, active_head=-0.002)["PASS_single"])


class TestGateEvaluateNamedColumns(unittest.TestCase):
    """gate.evaluate 对「列名恰好叫 ts_code」的因子面板必须能算。

    背景(2026-09-26, GP 首跑 alpha_factory_gp1 首踩): S5 确定性 tie-break(2026-09-22)
    加了 `_tie["ts_code"] = _tie.index` —— 当面板 columns.name == "ts_code"
    (行情 pivot / build_panel 的产物就是这样)时, sort_values(by=[...,"ts_code"])
    报「既是索引层又是列名」⇒ 整条求值路径崩。此前 kg/flow 轮的 parquet 面板
    列名未命名所以没炸 —— 这是潜在缺陷不是理论问题。"""

    def test_named_ts_code_columns_do_not_crash(self):
        rng = np.random.default_rng(7)
        dates = pd.to_datetime(["2019-12-31", "2021-06-30", "2025-06-30"])
        codes = [f"{600000 + i}.SH" for i in range(gate.MIN_NAMES + 10)]
        fac = pd.DataFrame(rng.normal(size=(3, len(codes))), index=dates, columns=codes)
        fac.columns.name = "ts_code"          # 触发歧义的那一行
        fwd = pd.DataFrame(rng.normal(size=(3, len(codes))), index=dates, columns=codes)
        fwd.columns.name = "ts_code"   # 真实路径 fwd 与因子同源 ⇒ 两边都带名;
                                       # intersection 只在两边同名时保留名字(首次漏判的原因)
        zones = gate.zone_of(pd.DatetimeIndex(dates))
        det = gate.evaluate(fac, fwd, pd.DatetimeIndex(dates), zones)
        self.assertEqual(len(det), 2)          # OTHER(2019-12-31) 不出截面
        self.assertIn("head_excess", det.columns)


class TestGPProposerIndustryWiring(unittest.TestCase):
    """GP 首跑(2026-09-26, alpha_factory_gp1)实证的**第二处静默退化**(与 09-13
    bind_factory 同族): `_fitness` 的行业中性化用 `self._ind`, 而构造默认 None、
    `Factory.add()` 只绑 PIT 域 ⇒ 每次适应度抛 AttributeError 被吞成最差适应度
    (1e9) ⇒ 符号回归退化成随机搜索(gen 行"最好 -head"恒为 -1e9 是指纹)。
    `_prepare` 必须惰性加载行业(同一实现 fe.industry_map, 与 eval 路径同源)。"""

    def test_prepare_loads_industry_when_none(self):
        rng = np.random.default_rng(3)
        idx = pd.to_datetime(["2022-01-31", "2022-02-28", "2022-03-31"])
        cols = ["000001.SZ", "000002.SZ", "600000.SH"]
        m = {k: pd.DataFrame(rng.normal(size=(3, 3)) + 10.0, index=idx, columns=cols)
             for k in ("open", "high", "low", "close", "pre_close", "vol", "amount",
                       "turnover", "total_mv", "circ_mv", "pb", "pe_ttm", "adj")}
        prop = factory_mod.GPProposer(m=m, dead_dates=idx)
        self.assertIsNone(prop._ind)          # 前置: 构造确实没绑行业
        prop._prepare()
        self.assertIsNotNone(prop._ind)       # 修复点: 惰性加载
        self.assertIn("000001.SZ", prop._ind.index)


class TestGateVerdict(unittest.TestCase):
    """五条预注册判据 —— 重点是新加的更严那条(top6 >= 0)"""

    def _rec(self, **over):
        rec = {"DEAD_head": 0.010, "ACTIVE_head": 0.008, "DEAD_head_t": 2.50,
               "DEAD_top6": 0.004, "p_adj": 0.001,
               # 门禁 v2 ①（2026-09-14）：门槛 = 0.5%/月 + 自换手成本 ⇒ cost 必须给，
               # 否则 fail-closed（见 TestGateV2* 里那条专门的红检）
               "cost_per_month": 0.0005}
        rec.update(over)
        return rec

    def test_passes_when_all_five_hold(self):
        v = gate.verdict(self._rec())
        self.assertTrue(v["PASS"])
        self.assertTrue(all(v[k] for k in v if k != "PASS"))

    def test_negative_dead_top6_vetoes(self):
        """死区 head 漂亮但 top6 为负 ⇒ 否。`die_flow`/`head_vt`/`h10` 三个反例的护栏"""
        v = gate.verdict(self._rec(DEAD_top6=-1e-9))
        self.assertFalse(v["c_top6"])
        self.assertFalse(v["PASS"])

    def test_each_criterion_can_veto_alone(self):
        for bad in ({"DEAD_head": -0.010}, {"DEAD_head": 0.001}, {"DEAD_head_t": 1.99},
                    {"p_adj": 0.05}, {"DEAD_top6": -0.001}, {"ACTIVE_head": -0.008}):
            with self.subTest(bad=bad):
                self.assertFalse(gate.verdict(self._rec(**bad))["PASS"])

    def test_bonferroni_shrinks_significance(self):
        """分母是"实际测过的候选数" ⇒ 加库会让同一条 alpha 更难通过"""
        self.assertAlmostEqual(gate.bonferroni(0.01, 10), 0.1)
        self.assertEqual(gate.bonferroni(0.01, 1000), 1.0)


# ===========================================================================
# 3. Alpha Bank 生命周期
# ===========================================================================
class TestAlphaBank(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db, db.DB_PATH = db.DB_PATH, Path(self.tmp.name) / "t.db"
        self._pool, bank.POOL_DIR = bank.POOL_DIR, Path(self.tmp.name) / "pool"

    def tearDown(self):
        db.DB_PATH, bank.POOL_DIR = self._db, self._pool
        self.tmp.cleanup()

    def _rec(self, aid="X1", **over):
        rec = {"alpha_id": aid, "name": aid, "DEAD_head": 0.01, "DEAD_head_t": 3.0,
               "DEAD_top6": 0.002, "ACTIVE_head": 0.008, "p_adj": 0.001,
               "c_same_sign": True, "c_head_size": True, "c_t": True,
               "c_p_adj": True, "c_top6": True, "PASS": True}
        rec.update(over)
        return rec

    def test_status_inferred_from_gate(self):
        bank.upsert(self._rec("PASSED"))
        self.assertEqual(bank.get("PASSED")["status"], "POOLED")
        bank.upsert(self._rec("FAILED", PASS=False, c_t=False))
        self.assertEqual(bank.get("FAILED")["status"], "CANDIDATE")

    def test_upsert_is_idempotent_by_alpha_id(self):
        bank.upsert(self._rec("X1"))
        bank.upsert(self._rec("X1", DEAD_head=0.02))
        self.assertEqual(bank.get("X1")["DEAD_head"], 0.02)
        self.assertEqual(len(bank.list_alphas()), 1)

    def test_query_top6_negative_lists_the_gate_blind_spot(self):
        """五条判据里唯一会漏掉的组合: 其余四条全过、top6 为负"""
        bank.upsert(self._rec("BLIND", PASS=False, c_top6=False, DEAD_top6=-0.01))
        got = bank.query_top6_negative()
        self.assertEqual(list(got["alpha_id"]), ["BLIND"])

    def test_retire_requires_reason(self):
        bank.upsert(self._rec("X1"))
        with self.assertRaises(ValueError):
            bank.retire("X1", "")
        with self.assertRaises(ValueError):
            bank.retire("X1", "   ")
        bank.retire("X1", "2024-26 衰减")
        r = bank.get("X1")
        self.assertEqual(r["status"], "RETIRED")
        self.assertEqual(r["retire_reason"], "2024-26 衰减")

    def test_retire_unknown_id_raises(self):
        with self.assertRaises(KeyError):
            bank.retire("NOPE", "不存在")

    def test_promote_requires_validated(self):
        """门禁通过 = POOLED ≠ 可以进生产 `factor` 表"""
        bank.upsert(self._rec("X1"))
        bank.save_values("X1", pd.DataFrame(
            [[1.0, 2.0]], index=pd.to_datetime(["2024-01-31"]), columns=["a.SZ", "b.SZ"]))
        with self.assertRaises(PermissionError):
            bank.promote_to_factor("X1", dry_run=True)
        conn = db.get_conn()
        try:
            conn.execute("UPDATE alpha_bank SET status='VALIDATED' WHERE alpha_id='X1'")
            conn.commit()
        finally:
            conn.close()
        out = bank.promote_to_factor("X1", dry_run=True)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["factor"], "X1")
        self.assertEqual(out["rows"], 2)

    def test_values_roundtrip(self):
        wide = pd.DataFrame([[1.0, 2.0], [3.0, 4.0]],
                            index=pd.to_datetime(["2024-01-31", "2024-02-29"]),
                            columns=["a.SZ", "b.SZ"])
        bank.save_values("X1", wide)
        self.assertTrue(bank.has_values("X1"))
        back = bank.load_values("X1")
        pd.testing.assert_frame_equal(back, wide.astype("float32"))
        with self.assertRaises(FileNotFoundError):
            bank.load_values("NOPE")


if __name__ == "__main__":
    unittest.main()
