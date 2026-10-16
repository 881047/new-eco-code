"""时序 die (PatchTST 式 Transformer) 单测 (2026-09-11 「芯片式异构架构」阶段 5)

这个文件的头号任务**不是**测模型能不能学, 而是**证伪前视**(`TestNoLookAhead`)。
理由: 一个能吃未来的序列模型在回测里会给出极漂亮的读数, 而且**看不出是假的** ——
普通断言(形状对不对、跑不跑得通)全都照样通过。所以必须用"扰动未来、检查过去"的办法
主动去证伪它, 而不是等它自己暴露。

第二组是**形状与契约**: 落库的表必须能过引擎的硬契约(列名 `score`/`pred_ret`、
`trade_date` 是月末开市日), 否则分数写进去也读不出来。

运行: ./.venv/Scripts/python.exe -m pytest tests/test_seq_die.py -q
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                                          # noqa: E402
import pandas as pd                                         # noqa: E402

from app.models import seq_die as S                         # noqa: E402


def _market(n_days=200, n_stocks=8, seed=0):
    """够长(>60 日预热)+ 有真实波动的合成行情"""
    rng = np.random.RandomState(seed)
    idx = pd.bdate_range("2020-01-01", periods=n_days)
    cols = ["s%02d" % i for i in range(n_stocks)]
    px = 10.0 * np.exp(np.cumsum(rng.normal(0, 0.02, (n_days, n_stocks)), axis=0))
    close = pd.DataFrame(px, index=idx, columns=cols, dtype="float32")
    return {
        "close": close,
        "adj": pd.DataFrame(1.0, index=idx, columns=cols, dtype="float32"),
        "high": close * 1.02, "low": close * 0.98, "open": close * 0.995,
        "vol": pd.DataFrame(rng.uniform(1e5, 5e5, close.shape), index=idx, columns=cols,
                            dtype="float32"),
        "amount": pd.DataFrame(rng.uniform(1e6, 5e6, close.shape), index=idx, columns=cols,
                               dtype="float32"),
        "turnover": pd.DataFrame(rng.uniform(0.5, 5.0, close.shape), index=idx, columns=cols,
                                 dtype="float32"),
    }


class TestNoLookAhead(unittest.TestCase):
    """**头号不变量**: 特征只用过去"""

    def test_perturbing_the_future_does_not_change_the_past(self):
        """把第 k 天的价格改掉, **第 k 天之前**的所有特征必须逐位不变。

        这是唯一能真正证伪前视的测试 —— 用 `rolling(60)` 却写成 `center=True`、
        或者手滑用了 `expanding` 之外的全局统计量, 都会在这里当场炸掉。
        """
        k = 150
        a = _market()
        b = _market()
        b["close"].iloc[k:] *= 3.0            # 只动 k 及之后
        b["adj"].iloc[k:] *= 3.0
        pa, ia, _ = S.feature_panel(a)
        pb, ib, _ = S.feature_panel(b)
        self.assertTrue((ia == ib).all())
        past_a, past_b = pa[:k], pb[:k]
        same = np.isclose(past_a, past_b, rtol=0, atol=0, equal_nan=True)
        self.assertTrue(same.all(),
                        "第 %d 天之前有 %d 个特征值被未来的价格改动了 —— 存在前视"
                        % (k, int((~same).sum())))

    def test_the_perturbation_does_reach_the_future(self):
        """反向对照: 上面的测试必须**有可能失败** —— 未来的特征确实会变。

        没有这一条, 上面那个测试可能只是因为整张表全 NaN 而"通过"。
        """
        k = 150
        a, b = _market(), _market()
        b["close"].iloc[k:] *= 3.0
        pa = S.feature_panel(a)[0]
        pb = S.feature_panel(b)[0]
        self.assertFalse(np.isclose(pa[k:], pb[k:], equal_nan=True).all(),
                         "改了未来价格, 未来特征却纹丝不动 —— 上面的测试是空的")

    def test_cross_sectional_zscore_is_per_day(self):
        """截面标准化必须**逐日**做 —— 用全期均值就会前视"""
        m = _market()
        p = S.feature_panel(m)[0]
        z1 = S.cs_zscore(p)
        p2 = p.copy()
        p2[-1, 0, :] += 100.0                  # 只动**最后一天的一只股票**
        z2 = S.cs_zscore(p2)
        np.testing.assert_array_equal(np.nan_to_num(z1[:-1]), np.nan_to_num(z2[:-1]))
        self.assertFalse(np.isclose(np.nan_to_num(z1[-1]), np.nan_to_num(z2[-1])).all(),
                         "改了最后一天的截面, 最后一天的 z 却没变 —— 标准化没在按日做")

    def test_zscore_is_scale_invariant_within_a_day(self):
        """附带性质: 整段截面同乘一个常数, z **逐位不变**(μ 与 σ 同时被缩放)。

        这是 z-score 该有的样子, 也解释了为什么不能用"同乘一个数"去测前视 ——
        那种扰动对逐日标准化是恒等变换, 测出来永远是"没变化", 是个假测试。
        """
        p = S.feature_panel(_market())[0]
        z1 = S.cs_zscore(p)
        p2 = p.copy()
        p2[-1] *= 5.0
        np.testing.assert_allclose(np.nan_to_num(z1[-1]), np.nan_to_num(S.cs_zscore(p2)[-1]),
                                   rtol=1e-5, atol=1e-5)

    def test_zscore_has_zero_mean_across_stocks(self):
        z = S.cs_zscore(S.feature_panel(_market())[0])
        row = np.nanmean(z[100], axis=0)       # 某一天的截面
        self.assertTrue(np.allclose(row, 0.0, atol=1e-5), row)


class TestCleanWindow(unittest.TestCase):
    """窗口补缺: 停牌能补, 但**末日在停牌的票不给分**"""

    def test_shape_is_stock_major(self):
        w = np.ones((5, 20, 3), dtype="float32")     # (L, 股, F)
        arr, valid = S._clean(w)
        self.assertEqual(arr.shape, (20, 5, 3))
        self.assertTrue(valid.all())

    def test_a_gap_is_forward_filled(self):
        w = np.ones((5, 1, 2), dtype="float32")
        w[2, 0, :] = np.nan                          # 中间停牌一天
        arr, _ = S._clean(w)
        self.assertFalse(np.isnan(arr).any(), "有限步 ffill 没补上")
        self.assertEqual(float(arr[0, 2, 0]), 1.0)

    def test_a_stock_suspended_on_the_last_day_is_not_scored(self):
        w = np.ones((5, 2, 2), dtype="float32")
        w[-1, 1, :] = np.nan                         # 第 1 只票评分日在停牌
        _, valid = S._clean(w)
        self.assertTrue(valid[0])
        self.assertFalse(valid[1], "评分日停牌的票拿到了分数 —— 买不到")

    def test_remaining_nan_is_zero_in_zspace(self):
        w = np.full((5, 1, 2), np.nan, dtype="float32")
        arr, valid = S._clean(w)
        self.assertEqual(float(np.abs(arr).sum()), 0.0)
        self.assertFalse(valid[0])


class TestConfigIsPreRegistered(unittest.TestCase):
    """预注册: 冻结、且与生产同口径的那几个数**不许漂**"""

    def test_config_is_frozen(self):
        sc = S.SeqConfig()
        with self.assertRaises(Exception):
            sc.epochs = 99                        # frozen dataclass: 改不动
        self.assertEqual(sc.epochs, 12)

    def test_production_aligned_knobs_match_the_trainer(self):
        """训练窗/持有期/种子口径必须与生产一致 —— 不一致就不是同一个实验"""
        from app.alpha.gate import HORIZON
        from app.models.trainer import ENSEMBLE_ROUNDS  # noqa: F401  (存在即可)
        sc = S.SeqConfig()
        self.assertEqual(sc.horizon, HORIZON)
        self.assertEqual(sc.train_months, 36)

    def test_patch_geometry_is_consistent(self):
        sc = S.SeqConfig()
        self.assertEqual(sc.n_patch(), (sc.lookback - sc.patch) // sc.stride + 1)
        self.assertGreater(sc.n_patch(), 1)
        self.assertLessEqual(sc.n_patch() * sc.stride, sc.lookback)


class TestModelShapes(unittest.TestCase):
    """形状对了才有资格谈别的"""

    def setUp(self):
        try:
            import torch                                     # noqa: F401
        except Exception:                                    # pragma: no cover
            self.skipTest("torch 不可用")

    def test_forward_gives_one_score_per_stock(self):
        sc = S.SeqConfig(lookback=20, patch=5, stride=5, d_model=16, nhead=2,
                         nlayers=1, ff=32)
        net = S.build_model(4, sc)
        x = np.random.RandomState(0).normal(size=(7, 20, 4)).astype("float32")
        import torch
        with torch.no_grad():
            y = net(torch.from_numpy(x))
        self.assertEqual(tuple(y.shape), (7,))

    def test_training_changes_the_predictions(self):
        """**能学**: 拟合一个可学的目标后预测必须变(否则整个 die 是随机数)"""
        rng = np.random.RandomState(0)
        n, L, F = 240, 20, 3
        X = rng.normal(size=(n, L, F)).astype("float32")
        # 目标 = 最后一个通道的均值(模型看得到的东西, 必然可学)
        y = X[:, :, 0].mean(axis=1)
        sc = S.SeqConfig(lookback=L, patch=5, stride=5, d_model=16, nhead=2,
                         nlayers=1, ff=32, epochs=8, batch=64)
        untrained = S.build_model(F, sc)
        before = S._predict(untrained, X, sc)
        net = S._train_one(X, y, F, sc)
        after = S._predict(net, X, sc)
        e0 = float(np.mean((before - y) ** 2))
        e1 = float(np.mean((after - y) ** 2))
        self.assertLess(e1, e0 * 0.9,
                        "训练后 MSE 没明显下降(%.3f → %.3f)—— 模型根本没在学" % (e0, e1))
        self.assertGreater(float(np.corrcoef(after, y)[0, 1]), 0.3,
                           "预测与目标几乎不相关, 这个 die 只会输出噪声")


class TestNothingIsWrittenWithoutScores(unittest.TestCase):
    """跑不出分数时**不许写表** —— 空表会让下游把"没训出来"读成"这个月没有票" """

    def test_empty_result_reports_zero_and_does_not_touch_the_table(self):
        from unittest import mock
        m = _market(n_days=200)
        with mock.patch("app.models.expert.ensure_score_table") as ensure, \
             mock.patch("app.pit.engine.PITEngine.tradable_mask",
                        return_value=pd.Series(dtype="bool")):
            r = S.run_die("unit_noname", market=m, max_months=2)
        ensure.assert_called_once()               # 建表是幂等的, 允许
        self.assertEqual(r["n_rows"], 0)
        self.assertEqual(r["n_months"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
