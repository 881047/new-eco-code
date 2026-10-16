"""Ensemble / Pure Alpha Score 单测 (2026-09-11 「芯片式异构架构」阶段 1)

测三件在真数据上很难一眼看出来的事:

1. **分位归一真的是截面内的**, 且 NaN 保持 NaN(缺分 = 让权, 不许填 0 伪造"看多")
2. **相关性标签逐月切**, 且单调 —— 收益越高标签越大(lambdarank 的方向搞反了整个融合器就废了,
   而它在真数据上表现为"融合后收益变差", 很容易被误读成"融合没用")
3. **落表守卫**: 融合器永远不许写 `model_score`(生产表), 表名必须合契约

运行: ./.venv/Scripts/python.exe -m pytest tests/test_fusion.py -q
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                                          # noqa: E402
import pandas as pd                                         # noqa: E402

from app.models import fusion as fu                          # noqa: E402


class TestPercentileBus(unittest.TestCase):
    """NoC 总线: 不同量纲的 die 分数必须先被映到同一标度"""

    def test_percentile_is_cross_sectional(self):
        """两个月**排序相同但量纲差 1000 倍** ⇒ 分位必须逐位相同。

        这正是 NoC 总线要解决的问题: die 之间量纲不可比(概率 / 回归值 / 只覆盖子域),
        但**截面内的次序**可比。
        """
        idx = pd.to_datetime(["2024-01-31", "2024-02-29"])
        cols = ["a", "b", "c", "d"]
        w = pd.DataFrame([[1.0, 2.0, 3.0, 4.0],
                          [1000.0, 2000.0, 3000.0, -4000.0]],
                         index=idx, columns=cols)
        w.iloc[1] = [1000.0, 2000.0, 3000.0, 4000.0]      # 同序, 不同量纲
        p = fu.to_percentile(w)
        np.testing.assert_allclose(p.iloc[0].to_numpy(), p.iloc[1].to_numpy())
        np.testing.assert_allclose(p.iloc[0].to_numpy(), [25.0, 50.0, 75.0, 100.0])
        self.assertTrue((p.max(axis=1) <= fu.PCT_SCALE).all())
        self.assertTrue((p.min(axis=1) >= 0).all())

    def test_percentile_reverses_when_order_reverses(self):
        """次序反过来 ⇒ 分位必须反过来(防"排序方向搞反"这类静默错误)"""
        idx = pd.to_datetime(["2024-01-31"])
        w = pd.DataFrame([[4.0, 3.0, 2.0, 1.0]], index=idx, columns=list("abcd"))
        p = fu.to_percentile(w)
        np.testing.assert_allclose(p.iloc[0].to_numpy(), [100.0, 75.0, 50.0, 25.0])

    def test_nan_stays_nan(self):
        """缺分必须保持缺分 —— 填 0 会被元学习器读成"强烈看空"而不是"不知道\""""
        idx = pd.to_datetime(["2024-01-31"])
        w = pd.DataFrame([[1.0, np.nan, 3.0, 4.0]], index=idx,
                         columns=["a", "b", "c", "d"])
        p = fu.to_percentile(w)
        self.assertTrue(np.isnan(p.iloc[0]["b"]))
        self.assertFalse(np.isnan(p.iloc[0]["a"]))

    def test_rank_mean_gives_lower_weight_to_sparse_die(self):
        """等权秩平均的实际后果, 用**覆盖域不同**的两个 die 说明:

        - `x` 被两个 die 都覆盖 ⇒ 取两者均值
        - `y` 只有 d1 覆盖   ⇒ 均值 = d1 自己的分位, **不被 d2 的缺失拉低**

        这个语义是 `skipna=True` 给的, 也正是等权法被证伪的机制之一:
        稀疏 die 在**它覆盖的少数股票**上拿到了全额票。"""
        idx = pd.to_datetime(["2024-01-31"])
        a = pd.DataFrame([[1.0, 2.0, 3.0]], index=idx, columns=["x", "y", "z"])
        b = pd.DataFrame([[1.0, np.nan, 3.0]], index=idx, columns=["x", "y", "z"])
        feats = pd.concat([
            fu._to_long(fu.to_percentile(a), "d1"),
            fu._to_long(fu.to_percentile(b), "d2")], axis=1)
        self.assertEqual(list(feats.columns), ["d1", "d2"])
        import unittest.mock as mock
        with mock.patch.object(fu, "build_features", return_value=feats):
            out = fu.rank_mean({"d1": "t1", "d2": "t2"})
        # d1 非 NaN 集 = {x,y,z} ⇒ x→1/3, y→2/3, z→1
        # d2 非 NaN 集 = {x,z}   ⇒ x→1/2, z→1  (**y 缺分**)
        # 故 x = (1/3 + 1/2)/2, y = 2/3(只有 d1 投票, 不被缺失拉低), z = 1
        np.testing.assert_allclose(out.iloc[0]["x"],
                                   (100.0 / 3 + 100.0 / 2) / 2, rtol=1e-9)
        np.testing.assert_allclose(out.iloc[0]["y"], 200.0 / 3, rtol=1e-9)
        np.testing.assert_allclose(out.iloc[0]["z"], 100.0, rtol=1e-9)


class TestRelevanceLabels(unittest.TestCase):
    """lambdarank 的标签: 非负整数、逐月切、单调(收益越高档位越大)"""

    def test_monotone_within_month(self):
        md = pd.DatetimeIndex([pd.Timestamp("2024-01-31")])
        cols = [f"s{i:02d}" for i in range(50)]
        r = pd.DataFrame([np.arange(50, dtype=float)], index=md, columns=cols)
        lab = fu.relevance_labels(r, md)
        v = lab.loc[pd.Timestamp("2024-01-31")].reindex(cols).to_numpy()
        self.assertEqual(np.nanmin(v), 0)
        self.assertEqual(np.nanmax(v), fu.N_LABELS - 1)
        self.assertTrue(np.all(np.diff(v) >= 0), "标签必须随收益单调不减")

    def test_labels_are_per_month(self):
        """同一个收益值在两个月里应当拿到同一个档位(逐月切, 不是全局切)"""
        md = pd.DatetimeIndex([pd.Timestamp("2024-01-31"), pd.Timestamp("2024-02-29")])
        cols = [f"s{i:02d}" for i in range(40)]
        base = np.arange(40, dtype=float)
        r = pd.DataFrame([base, base * 1e6], index=md, columns=cols)
        lab = fu.relevance_labels(r, md)
        a = lab.loc[pd.Timestamp("2024-01-31")].reindex(cols).to_numpy()
        b = lab.loc[pd.Timestamp("2024-02-29")].reindex(cols).to_numpy()
        np.testing.assert_array_equal(a, b, "两个月形状相同 ⇒ 档位必须相同")

    def test_small_cross_section_skipped(self):
        md = pd.DatetimeIndex([pd.Timestamp("2024-01-31")])
        r = pd.DataFrame([[1.0, 2.0, 3.0]], index=md, columns=list("abc"))
        self.assertTrue(fu.relevance_labels(r, md).empty)


class TestWriteGuards(unittest.TestCase):
    """融合器永远不许写生产表"""

    def test_refuses_production_table(self):
        w = pd.DataFrame([[1.0]], index=pd.to_datetime(["2024-01-31"]), columns=["x"])
        with self.assertRaises(PermissionError):
            fu.write_scores(w, table="model_score")

    def test_refuses_illegal_name(self):
        w = pd.DataFrame([[1.0]], index=pd.to_datetime(["2024-01-31"]), columns=["x"])
        for bad in ("1abc", "a-b", "A_upper", "a b"):
            with self.assertRaises(ValueError, msg=bad):
                fu.write_scores(w, table=bad)


class TestWrittenTableHasNoNullScores(unittest.TestCase):
    """落表契约: **只落有分的格**。

    这是 2026-09-11「通电」首跑踩出来的真 bug: `wide` 由 `pd.DataFrame(scores).T` 构造,
    各期股票集合不同 ⇒ DataFrame 按**并集**对齐, 未被任何 die 打分的 (月份, 股票) 是 NaN。
    而 **pandas 3.0 起 `stack()` 是保留 NA**(不是丢) —— 与本项目
    `scripts/oneoff/diag_allnan_samples.py`(2026-09-09)记录的行为一致, 而本文件的
    原注释把方向写反了。后果: `model_score_ens` 落了 39,924 行 `score IS NULL`(占 11.8%)。

    引擎用 `nlargest` 选股(丢弃 NaN) ⇒ 不改回测读数; 但它污染证据表、违反方案 §4
    「过滤责任在打分侧」的契约, 且 `builder`/`api`/`monitor` 会读 `pred_ret`。
    """

    class _KeepOpen:
        """代理连接: 转发一切, 但 `close()` 变空操作(否则测完就没法查了)。"""

        def __init__(self, c):
            self._c = c

        def __getattr__(self, n):
            return getattr(self._c, n)

        def close(self):
            pass

    def _write(self, wide):
        import sqlite3
        from unittest import mock
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE t (ts_code TEXT, trade_date TEXT, score REAL, "
                  "pred_ret REAL, PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID")
        with mock.patch.object(fu.db, "get_conn", return_value=self._KeepOpen(c)), \
             mock.patch.object(fu.expert, "ensure_score_table", lambda *a, **k: None):
            fu.write_scores(wide, table="t")
        rows = c.execute("SELECT ts_code, trade_date, score, pred_ret FROM t").fetchall()
        c.close()
        return rows

    def test_union_aligned_nan_cells_are_not_written(self):
        md = pd.to_datetime(["2024-01-31", "2024-02-29"])
        # 1 月有 a/b, 2 月只有 b ⇒ (a, 2024-02-29) 是并集对齐凭空造出来的空格
        wide = pd.DataFrame({md[0]: pd.Series([1.0, 2.0], index=["a", "b"]),
                             md[1]: pd.Series([3.0], index=["b"])}).T
        self.assertEqual(int(wide.isna().sum().sum()), 1, "前提: 空格必须真的存在")

        rows = self._write(wide)
        self.assertEqual(sorted(r[0] for r in rows), ["a", "b", "b"])
        self.assertEqual(sum(1 for r in rows if r[2] is None), 0, "不许落 NULL score")
        self.assertEqual(sum(1 for r in rows if r[3] is None), 0, "不许落 NULL pred_ret")

    def test_disjoint_months_do_not_multiply_rows(self):
        """两个月股票集完全不相交 ⇒ 落表行数 = 两边之和, 不是笛卡尔积。"""
        md = pd.to_datetime(["2024-01-31", "2024-02-29"])
        wide = pd.DataFrame({md[0]: pd.Series([1.0], index=["a"]),
                             md[1]: pd.Series([2.0], index=["z"])}).T
        self.assertEqual(len(self._write(wide)), 2)      # 并集对齐下有 2 个空格, 必须全丢


class TestMetaLearnerDiscipline(unittest.TestCase):
    """元学习器的训练窗口必须与生产模型**共用同一个函数**"""

    def test_uses_trainer_train_window(self):
        import inspect
        src = inspect.getsource(fu.stack)
        self.assertIn("trainer._train_window", src,
                      "融合器另写了一份窗口逻辑 ⇒ 融的时候一套纪律、判的时候另一套")

    def test_lgb_objective_is_lambdarank(self):
        p = fu._lgb_params()
        self.assertEqual(p["objective"], "lambdarank")
        self.assertIn(6, p["ndcg_eval_at"], "策略买 top-6, 评估点必须含 @6")
        self.assertEqual(len(p["label_gain"]), fu.N_LABELS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
