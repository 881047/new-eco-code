"""逐期 IC 落盘的守卫测试（2026-09-13）

为什么要有它：F49 用第三方回测体检对照本项目时，"Sharpe ≈ IC_IR×0.3~0.5"这条
**算不了** —— 项目只落盘均值 IC，没有序列。现在 `scores.rank_ic_by_anchor` 在打分时
顺手算出来并落进 npz；本文件守三件事：

  1. 秩相关算得对（单调同向 ≈ +1、反向 ≈ −1、打乱 ≈ 0 附近）；
  2. 口径不掺假：标签 NaN 的格子被排除；有效样本不足 `min_n` 的锚点记 NaN（不硬算、不填 0）；
  3. `save()` 真的把 `ic_by_anchor` / `n_by_anchor` 写进 npz，且 meta 里带 IC_IR 摘要。

运行: python -m unittest tests.test_weekly_ic -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.weekly import scores as sc      # noqa: E402


class TestRankIcByAnchor(unittest.TestCase):
    def test_monotone_positive_and_negative(self):
        y = np.array([[1.0, 2.0, 3.0, 4.0, 5.0]])
        s_pos = np.array([[0.1, 0.2, 0.3, 0.4, 0.5]])
        s_neg = np.array([[0.5, 0.4, 0.3, 0.2, 0.1]])
        ic_pos, n_pos = sc.rank_ic_by_anchor(s_pos, y, min_n=5)
        ic_neg, _ = sc.rank_ic_by_anchor(s_neg, y, min_n=5)
        self.assertAlmostEqual(ic_pos[0], 1.0, places=9)
        self.assertAlmostEqual(ic_neg[0], -1.0, places=9)
        self.assertEqual(n_pos[0], 5)

    def test_nan_labels_are_excluded_and_thin_anchors_are_nan(self):
        y = np.array([[1.0, np.nan, 3.0, 4.0, 5.0],
                      [1.0, 2.0, 3.0, np.nan, np.nan]])
        s = np.array([[0.1, 0.9, 0.3, 0.4, 0.5],
                      [0.5, 0.4, 0.3, 0.2, 0.1]])
        ic, n_ok = sc.rank_ic_by_anchor(s, y, min_n=4)
        self.assertEqual(n_ok[0], 4)
        self.assertTrue(np.isfinite(ic[0]))              # 4 个有效点 ≥ min_n
        self.assertEqual(n_ok[1], 3)
        self.assertTrue(np.isnan(ic[1]), "有效样本 3 < min_n=4 时不许硬算 IC")

    def test_ic_summary_math(self):
        out = sc.ic_summary(np.array([0.10, 0.20, 0.30]))
        self.assertAlmostEqual(out["ic_mean"], 0.2, places=6)
        self.assertAlmostEqual(out["ic_sd"], 0.1, places=6)
        self.assertAlmostEqual(out["ic_ir"], 2.0, places=6)
        self.assertEqual(out["ic_n"], 3)
        self.assertEqual(sc.ic_summary(np.array([np.nan, np.nan])), {})


class TestSavePersistsIc(unittest.TestCase):
    def test_npz_contains_series_and_meta(self):
        with tempfile.TemporaryDirectory() as td:
            panel = {"anchors": pd.DatetimeIndex(["2026-09-11", "2026-09-04"]),
                     "cols": ["000001.SZ", "600000.SH"],
                     "names": ["f1"]}
            S = np.array([[0.2, 0.8], [0.6, 0.4]], dtype=np.float32)
            ic = np.array([0.11, np.nan])
            n_ok = np.array([2, 1])
            with mock.patch.object(sc, "DATA_DIR", Path(td)):
                p = sc.save("20260911", panel, S, ic=ic, n_by_anchor=n_ok)
                with np.load(p, allow_pickle=True) as z:     # Windows: 不关句柄删不掉临时目录
                    self.assertIn("ic_by_anchor", z.files)
                    self.assertIn("n_by_anchor", z.files)
                    np.testing.assert_allclose(z["ic_by_anchor"], ic, equal_nan=True)
                    self.assertEqual(list(z["n_by_anchor"]), [2, 1])
                    meta = [str(x) for x in z["meta"]]
            # 只有 1 个有限 IC ⇒ 摘要为空（不产生误导性的 IC_IR）
            self.assertFalse([m for m in meta if m.startswith("ic_ir=")])

    def test_no_ic_means_no_extra_arrays(self):
        with tempfile.TemporaryDirectory() as td:
            panel = {"anchors": pd.DatetimeIndex(["2026-09-11"]),
                     "cols": ["000001.SZ"], "names": ["f1"]}
            with mock.patch.object(sc, "DATA_DIR", Path(td)):
                p = sc.save("20260911", panel, np.array([[0.5]], dtype=np.float32))
                with np.load(p, allow_pickle=True) as z:
                    self.assertNotIn("ic_by_anchor", z.files)
                    self.assertNotIn("n_by_anchor", z.files)

    def test_skipped_ic_is_recorded_not_silent(self):
        """算不出 IC 时必须在 meta 留痕 —— "没有 IC" 与 "忘了算" 要能分开。"""
        with tempfile.TemporaryDirectory() as td:
            panel = {"anchors": pd.DatetimeIndex(["2026-09-11"]),
                     "cols": ["000001.SZ"], "names": ["f1"]}
            with mock.patch.object(sc, "DATA_DIR", Path(td)):
                p = sc.save("20260911", panel, np.array([[0.5]], dtype=np.float32),
                            ic_skipped="KeyError: 'open'")
                with np.load(p, allow_pickle=True) as z:
                    meta = [str(x) for x in z["meta"]]
            self.assertTrue([m for m in meta if m.startswith("ic_skipped=")],
                            f"meta 里没有 ic_skipped 留痕: {meta}")


if __name__ == "__main__":
    unittest.main()
