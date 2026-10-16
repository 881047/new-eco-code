"""月频滚动训练 `trainer.run` 的集成测试（2026-09-13）

为什么单测它：`trainer.run` 是**月频基线**的主路径（每月 1 日 18:33 那条链跑它），
而它此前覆盖率只有 31%（纯函数被覆盖、**编排主干零覆盖**）—— 也就是说"训练→推分→落库→
归档"这条真正产出 `model_score` 的链路从没被端到端验证过。它坏了不会报错，只会**没有分数**。

本测试用**真 schema 的临时库** + 合成行情/因子 + 迷你 LightGBM 参数（不碰生产库/生产模型目录），
守四条：
  1. 增量训练：该训的月份都训了，`score_table`/`importance_table`/`data_status` 三类落库齐全，
     且**按月归档**到调用方指定目录（不污染生产 `data/models/`）；
  2. **幂等**：紧接着再跑一次增量 ⇒ `trained == 0`（不重复训、不重复写）；
  3. **确定性**：同数据 + 同种子跑两次全量 ⇒ 分数**逐位相同**（`deterministic=True` + 固定 seed
     是生产可复现的前提）；
  4. 护栏：非生产表名且未指定 `archive_dir` ⇒ 直接 `ValueError`（2026-09-11 事故的守卫：
     实验重训曾覆盖 464 个生产月度归档）；
  5. `sample_weight_mode`（2026-09-19 P0-1/F76 实验参数）：未知口径在进训练循环**前**拒绝；
     `"uniqueness"`（López de Prado 平均唯一性）端到端跑通且早停路径的验证权重不缺位；
     默认 None 不进任何权重逻辑（上面确定性测试就在 None 路径上）。

运行: python -m unittest tests.test_trainer_run -v
"""
import contextlib
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import db                                  # noqa: E402
from app.models import trainer as tr                # noqa: E402

N_STOCKS = 200
N_DAYS = 1050
FACTORS = ("f_alpha", "f_beta", "f_gamma")


def _market() -> dict:
    """合成行情面板：200 只 × ~4 年日线（随机游走），字段与 `fe.load_market` 同键。"""
    rng = np.random.default_rng(11)
    dates = pd.bdate_range("2016-01-04", periods=N_DAYS)
    cols = [f"{i:06d}.SZ" for i in range(1, N_STOCKS + 1)]
    ret = rng.normal(0.0004, 0.018, size=(N_DAYS, N_STOCKS))
    close = 20 * np.cumprod(1 + ret, axis=0)
    df = lambda a: pd.DataFrame(a, index=dates, columns=cols)          # noqa: E731
    m = {"close": df(close), "open": df(close * (1 + rng.normal(0, 0.002, close.shape))),
         "high": df(close * 1.01), "low": df(close * 0.99),
         "pre_close": df(np.vstack([close[:1], close[:-1]])),
         "vol": df(np.full(close.shape, 1e6)), "amount": df(np.full(close.shape, 2e7)),
         "adj": df(np.ones(close.shape)), "pct": df(np.full(close.shape, 0.0)),
         "turnover": df(np.full(close.shape, 1.0)), "pe_ttm": df(np.full(close.shape, 15.0)),
         "pb": df(np.full(close.shape, 1.5)), "dv_ttm": df(np.full(close.shape, 1.0)),
         "total_mv": df(np.full(close.shape, 5e9)), "circ_mv": df(np.full(close.shape, 4e9)),
         "bench": pd.Series(4000 * np.cumprod(1 + rng.normal(0.0002, 0.01, N_DAYS)), index=dates)}
    return m


def _factors(m: dict) -> dict:
    """合成因子（日期 × 股票）：与行情同索引，含少量 NaN（走"全 NaN 行剔除"那条路）。"""
    rng = np.random.default_rng(5)
    out = {}
    for i, name in enumerate(FACTORS):
        a = rng.normal(0, 1, size=m["close"].shape)
        a[::97, :3] = np.nan                     # 稀疏 NaN 不该炸
        out[name] = pd.DataFrame(a, index=m["close"].index, columns=m["close"].columns)
    return out


class TestTrainerRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _market()
        cls.factors = _factors(cls.m)

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(db.SCHEMA)
        # 实验表：结构照抄生产表（列/类型不漂移），但名不同 ⇒ 不碰生产
        conn.execute("CREATE TABLE model_score_test AS SELECT * FROM model_score WHERE 0")
        conn.execute("CREATE TABLE model_importance_test AS "
                     "SELECT * FROM model_importance WHERE 0")
        rows = [(f"{i:06d}.SZ", "000001", "测试股", "北", "银行", "主板", "20100101", None)
                for i in range(1, N_STOCKS + 1)]
        conn.executemany("INSERT INTO stock_basic VALUES (?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        conn.close()
        self.archive = Path(tempfile.mkdtemp(prefix="tr_arch_"))
        # 2026-09-24 补：这个归档目录**此前完全没有清理** ⇒ 本机 `%TEMP%` 累积了
        # **1727 个** `tr_arch_*`（约 15 MB，是全库最大的一处残留）。
        # 同族教训见 07 号 #64：`mkdtemp` 了就要 `rmtree`。
        self.addCleanup(lambda: shutil.rmtree(self.archive, ignore_errors=True))

        @contextlib.contextmanager
        def _ctx():
            c = sqlite3.connect(self.path)
            try:
                yield c
                c.commit()
            finally:
                c.close()
        self._p1 = mock.patch.object(db, "conn_ctx", _ctx)
        self._p2 = mock.patch.object(db, "get_conn", side_effect=lambda: sqlite3.connect(self.path))
        self._p3 = mock.patch.object(tr, "load_market", return_value=self.m)
        self._p4 = mock.patch.object(tr, "PARAMS", dict(tr.PARAMS, num_leaves=4,
                                                        min_data_in_leaf=10, n_jobs=1))
        for p in (self._p1, self._p2, self._p3, self._p4):
            p.start()
        self.addCleanup(lambda: [p.stop() for p in (self._p1, self._p2, self._p3, self._p4)])
        self.addCleanup(lambda: Path(self.path).unlink(missing_ok=True))

    def _run(self, **kw):
        args = dict(train_months=36, backtest_start="2016-01-01", factors_override=self.factors,
                    score_table="model_score_test", importance_table="model_importance_test",
                    archive_dir=self.archive, num_boost_round=5, early_stopping_rounds=0,
                    ensemble_seeds=[7], holdout_recent=6)
        args.update(kw)
        return tr.run(**args)

    def _rows(self, sql):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def test_incremental_run_writes_scores_status_and_archives(self):
        out = self._run()
        self.assertGreater(out["trained"], 0, "增量训练一个月份都没训出来")
        n_scores = self._rows("SELECT COUNT(*) FROM model_score_test")[0][0]
        self.assertEqual(n_scores, out["trained"] * N_STOCKS,
                         "每个训练月份都该落满全票池的分数")
        self.assertEqual(self._rows("SELECT COUNT(DISTINCT train_date) FROM model_importance_test")[0][0],
                         out["trained"])
        status = dict(self._rows("SELECT name, last_date FROM data_status"))
        self.assertIn("model_score_test", status)
        self.assertEqual(status["model_score_test"].replace("-", ""), out["last_date"])
        files = sorted(p.name for p in self.archive.glob("lgb_*.txt"))
        self.assertGreaterEqual(len(files), out["trained"],
                                "按月归档没写全（审计链要求每期模型可复现）")
        # 归档只能落在调用方指定目录：生产 models/ 目录不许出现本测试的月份文件
        self.assertFalse(any(p.name.startswith("lgb_latest") for p in self.archive.iterdir()),
                         "实验表不该写 lgb_latest（会污染线上推分模型）")

    def test_second_incremental_run_is_idempotent(self):
        first = self._run()
        second = self._run()
        self.assertGreater(first["trained"], 0)
        self.assertEqual(second["trained"], 0, "已覆盖的月份不该重训/重写")
        self.assertEqual(self._rows("SELECT COUNT(*) FROM model_score_test")[0][0],
                         first["trained"] * N_STOCKS)

    def test_full_still_retrains_covered_months(self):
        """对照：`full=True` 即使所有月份都已覆盖也要重训 —— 这条同时证明上一条"幂等"断言不是空话
        （若增量跳过逻辑坏掉，上一条就会看到 trained>0 而变红）。"""
        self._run()
        again = self._run(full=True)
        self.assertGreater(again["trained"], 0, "full=True 必须重训全部月份")

    def test_full_retrain_is_deterministic(self):
        """同数据 + 同种子 ⇒ 分数**逐位相同**（生产可复现性的前提）。"""
        self._run(full=True)
        first = self._rows("SELECT ts_code, trade_date, score FROM model_score_test "
                           "ORDER BY trade_date, ts_code")
        self._run(full=True)
        second = self._rows("SELECT ts_code, trade_date, score FROM model_score_test "
                            "ORDER BY trade_date, ts_code")
        self.assertEqual(len(first), len(second))
        self.assertTrue(all(a == b for a, b in zip(first, second)),
                        "同一份数据+同一种子两次全量重训结果不同 ⇒ 复现性破坏了")

    def test_experiment_table_without_archive_dir_is_rejected(self):
        """2026-09-11 事故守卫：实验表未指定 archive_dir 会静默覆盖生产月度归档 ⇒ 必须拒绝。"""
        with self.assertRaises(ValueError) as cm:
            self._run(archive_dir=None)
        self.assertIn("archive_dir", str(cm.exception))

    def test_unknown_sample_weight_mode_rejected(self):
        """未知 sample_weight_mode 必须在进训练循环之前就报错（防静默按默认口径跑完才发现）。"""
        with self.assertRaises(ValueError) as cm:
            self._run(sample_weight_mode="bogus")
        self.assertIn("sample_weight_mode", str(cm.exception))

    def test_uniqueness_mode_runs_end_to_end(self):
        """uniqueness 权重：默认路径（holdout 切分、无早停）端到端，落库行数与 None 模式同构。"""
        out = self._run(sample_weight_mode="uniqueness")
        self.assertGreater(out["trained"], 0)
        self.assertEqual(self._rows("SELECT COUNT(*) FROM model_score_test")[0][0],
                         out["trained"] * N_STOCKS)

    def test_uniqueness_mode_with_early_stopping(self):
        """早停路径：dvalid 也要吃到同一 vmask 切出的验证权重（错位会静默改变选轮）。"""
        out = self._run(sample_weight_mode="uniqueness", early_stopping_rounds=2)
        self.assertGreater(out["trained"], 0)

    # ---- head_weight（2026-09-29 B1, plans/head-weight-prereg-20260929.md）----

    def test_head_weight_invalid_rejected(self):
        """head_weight 只收 None 或 >1.0 的数（1.0=无意义权重、字符串/负数=笔误）⇒ 进训练循环前拒。"""
        for bad in (1.0, 0.5, "3", -2):
            with self.assertRaises(ValueError) as cm:
                self._run(head_weight=bad)
            self.assertIn("head_weight", str(cm.exception))

    def test_head_weight_runs_end_to_end(self):
        """head_weight 端到端跑通且落库行数与 None 模式同构（加权只改梯度不改样本集）。"""
        out = self._run(head_weight=3.0)
        self.assertGreater(out["trained"], 0)
        self.assertEqual(self._rows("SELECT COUNT(*) FROM model_score_test")[0][0],
                         out["trained"] * N_STOCKS)

    def test_head_weight_changes_scores_vs_none(self):
        """同种子下 head_weight=3 的分数必须与 None 不同（权重真的进了训练）。反向守卫：
        参数缺省（None）时绝不走加权路径 —— 确定性测试就在 None 路径上。"""
        self._run(head_weight=3.0, full=True)
        hw = self._rows("SELECT ts_code, trade_date, score FROM model_score_test "
                        "ORDER BY trade_date, ts_code")
        self._run(full=True)
        base = self._rows("SELECT ts_code, trade_date, score FROM model_score_test "
                          "ORDER BY trade_date, ts_code")
        self.assertEqual(len(hw), len(base))
        self.assertFalse(all(a == b for a, b in zip(hw, base)),
                         "head_weight=3 与不加权分数逐位相同 ⇒ 权重没生效")


class TestUniquenessWeightsHelper(unittest.TestCase):
    """`_uniqueness_weights_by_anchor` 的手算用例（F76/trainer 同构实现的行为锚）。"""

    def test_overlapping_windows_hand_case(self):
        """21 天日历、锚点在 0/10、horizon=21：c(1..10)=1, c(11..20)=2
        ⇒ u(锚0)=(10×1+10×0.5)/20=0.75；u(锚10)=0.5（全窗 c=2）。"""
        cal = pd.bdate_range("2024-01-01", periods=21)
        anchors = pd.DatetimeIndex([cal[0], cal[10]])
        w = tr._uniqueness_weights_by_anchor(anchors, cal, horizon=21)
        self.assertAlmostEqual(float(w.loc[cal[0]]), 0.75)
        self.assertAlmostEqual(float(w.loc[cal[10]]), 0.5)

    def test_no_overlap_all_ones(self):
        """锚点间距 30 > horizon 21 ⇒ 窗互不相交，全部 u=1.0；末尾截断锚点也照常。"""
        cal = pd.bdate_range("2024-01-01", periods=91)
        anchors = pd.DatetimeIndex([cal[0], cal[30], cal[60], cal[85]])
        w = tr._uniqueness_weights_by_anchor(anchors, cal, horizon=21)
        self.assertTrue(np.allclose(w.to_numpy(), 1.0), w.to_numpy())


if __name__ == "__main__":
    unittest.main()
