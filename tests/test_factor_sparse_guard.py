"""稀疏面板保护门的守卫（D 案 ③-4，2026-09-22）

F35 的教训不是"报错"，是**静默**：事件面板非零占比 < 2% 时 `neutralize` 的 2%/98%
winsorize 把整行归零 ⇒ z-score 全 NaN ⇒ 下游 IC 一条都算不出（2024+ 有 557 条增发事件，
一条 IC 都没有）。所以门的价值在于"吵"，不在于"修"。

守三件事：
  1. 判据口径正确 —— **非零 / 非缺失**（winsorize 的分位数跳过 NaN，缺失不会被 clip 归零）；
  2. `strict=True` 必须拦得住（新路径应当这样接），`strict=False` 必须只记不改；
  3. **`neutralize` 自己不许改行为** —— 改成分走 sparse 口径会改变存量因子值 ⇒ 那是换 pin。
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.factors import factor_engine as fe  # noqa: E402

CODES = [f"{i:06d}.SZ" for i in range(1, 401)]
DATES = pd.bdate_range("2026-01-05", periods=6)


def _panel(nonzero_ratio: float, seed: int = 3) -> pd.DataFrame:
    """一个 `nonzero_ratio` 比例的格子是 1.0、其余是 0.0 的事件面板。"""
    rng = np.random.default_rng(seed)
    v = rng.random((len(DATES), len(CODES)))
    return pd.DataFrame(np.where(v < nonzero_ratio, 1.0, 0.0), index=DATES, columns=CODES)


class TestSparseRatio(unittest.TestCase):

    def test_ratio_counts_nonzero_over_non_null(self):
        self.assertAlmostEqual(fe.sparse_ratio(_panel(0.5)), 0.5, delta=0.03)
        self.assertAlmostEqual(fe.sparse_ratio(_panel(0.01)), 0.01, delta=0.01)
        self.assertEqual(fe.sparse_ratio(_panel(1.0)), 1.0)

    def test_missing_cells_are_not_counted_as_zeros(self):
        """缺失**不会**被 clip 归零 ⇒ 分母取非缺失。把 NaN 算成 0 会让门对稀疏数据过度告警。"""
        df = pd.DataFrame([[1.0, np.nan], [np.nan, np.nan]], index=DATES[:2], columns=["a", "b"])
        self.assertEqual(fe.sparse_ratio(df), 1.0)

    def test_all_nan_panel_is_worst_case(self):
        df = pd.DataFrame(np.nan, index=DATES[:2], columns=CODES[:10])
        self.assertEqual(fe.sparse_ratio(df), 0.0)
        self.assertTrue(fe.is_sparse_panel(df))


class TestGuardBehavior(unittest.TestCase):

    def test_sparse_panel_is_caught_and_advises_the_sparse_path(self):
        g = fe.guard_sparse("evt", _panel(0.005))
        self.assertTrue(g["sparse"])
        self.assertIn("neutralize_sparse", g["advice"])
        self.assertIn("F35", g["advice"])

    def test_dense_panel_passes(self):
        self.assertFalse(fe.guard_sparse("illiq", _panel(0.9))["sparse"])

    def test_strict_raises(self):
        with self.assertRaises(ValueError):
            fe.guard_sparse("evt", _panel(0.005), strict=True)

    def test_non_strict_does_not_change_values(self):
        """门**只判不改**：调用它前后因子值必须一字不动（改了就是换 pin）。"""
        df = _panel(0.005)
        before = df.copy()
        fe.guard_sparse("evt", df)
        pd.testing.assert_frame_equal(df, before)


class TestNeutralizeBehaviorIsUntouched(unittest.TestCase):
    """`neutralize` 里那个门**只许打日志**：换成 sparse 口径会改存量因子值。"""

    def test_sparse_panel_still_goes_through_winsorize(self):
        """红检：如果在 `neutralize` 里分流到 sparse 口径 ⇒ 本用例（全 NaN）当场红。"""
        ind = pd.Series(["A"] * len(CODES), index=CODES)
        out = fe.neutralize(_panel(0.005), ind)
        self.assertTrue(out.isna().all().all(),
                        "F35 的退化行为被保留：这就是我们要在日志里喊出来的那件事")

    def test_dense_panel_is_unchanged_by_the_guard(self):
        """密集面板的结果不许因为加了守卫而变（守卫若改 dtype/顺序就会漂）。"""
        ind = pd.Series(["A", "B"] * (len(CODES) // 2), index=CODES)
        df = pd.DataFrame(np.random.default_rng(5).normal(size=(len(DATES), len(CODES))),
                          index=DATES, columns=CODES)
        out = fe.neutralize(df, ind)
        self.assertFalse(out.isna().all().all())
        self.assertTrue(np.isfinite(out.to_numpy()[np.isfinite(out.to_numpy())]).all())


class TestNeutralizeSparse(unittest.TestCase):
    """sparse 口径本身：不 winsorize ⇒ 稀疏事件面板能算出 z-score。"""

    def test_sparse_panel_survives(self):
        ind = pd.Series(["A"] * len(CODES), index=CODES)
        out = fe.neutralize_sparse(_panel(0.01), ind)
        self.assertFalse(out.isna().all().all(), "稀疏面板在 sparse 口径下必须有值")
        self.assertGreater(out.notna().to_numpy().sum(), 0)

    def test_dense_panel_agrees_with_neutralize_up_to_winsorize(self):
        """两种口径在密集面板上**只**应差在 winsorize（截尾那几个格子）。

        ⚠️ 不要用逐格容差去钉：截尾格的差值是 1~2 个 z 单位（正是 winsorize 存在的理由），
        逐格 `assert_allclose` 会在随机数据上偶发失败 —— 那是测试写得比判据严，不是代码坏了。
        这里钉的是**秩序**（>0.99）与**中位数差**（<0.05）：结构一致 + 只有尾部被截。
        """
        ind = pd.Series(["A", "B"] * (len(CODES) // 2), index=CODES)
        df = pd.DataFrame(np.random.default_rng(7).normal(size=(len(DATES), len(CODES))),
                          index=DATES, columns=CODES)
        a = fe.neutralize(df, ind)
        b = fe.neutralize_sparse(df, ind)
        self.assertEqual(a.shape, b.shape)
        va, vb = a.to_numpy().ravel(), b.to_numpy().ravel()
        corr = pd.Series(va).corr(pd.Series(vb))
        self.assertGreater(corr, 0.99, f"两种口径的秩序必须一致，实得相关 {corr}")
        self.assertLess(float(np.nanmedian(np.abs(va - vb))), 0.05,
                        "除截尾外不应有系统性差异")


if __name__ == "__main__":
    unittest.main()
