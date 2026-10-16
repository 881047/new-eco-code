"""迟滞带 R11-5 测试 (2026-09-08)

1. _hysteresis_keep 纯函数: 保留名单语义(池内/截断/排序/关闭)
2. golden 宇宙集成: hyst=0 显式传参与默认路径逐位一致(快照不扰动);
   hyst=2 行为改变(换手下降、净值不同), 且不改变 IC(同评分同候选池口径)
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.backtest import engine as bt_engine
from test_backtest_golden import _universe, _SCHEMA, _FIXED_CFG, SNAP


class TestHysteresisKeep(unittest.TestCase):
    """_hysteresis_keep 保留名单语义"""

    def _df(self, codes, scores):
        return pd.DataFrame({"score": scores, "close": 10.0}, index=codes)

    def test_off_or_no_prev_returns_empty(self):
        lot = self._df(["A", "B"], [1.0, 2.0])
        self.assertEqual(bt_engine._hysteresis_keep(None, lot, 4, 2), [])
        self.assertEqual(bt_engine._hysteresis_keep(["A"], lot, 4, 0), [])
        self.assertEqual(bt_engine._hysteresis_keep([], lot, 4, 2), [])

    def test_keeps_only_in_pool_sorted_by_score(self):
        lot = self._df(["A", "C"], [3.0, 2.0])
        # 上月持仓 B/C/A, B 已跌出候选池(不在 lot 索引) → 排除; 剩余按池内分数降序 A, C
        self.assertEqual(bt_engine._hysteresis_keep(["B", "C", "A"], lot, 4, 2), ["A", "C"])

    def test_cap_at_n_when_state_shrinks(self):
        lot = self._df(["A", "B", "C", "D"], [4.0, 3.0, 2.0, 1.0])
        # 状态收缩 defense n=3: 只保留分数最高的 3 只
        self.assertEqual(bt_engine._hysteresis_keep(["D", "C", "B", "A"], lot, 3, 2),
                         ["A", "B", "C"])


class TestHysteresisGolden(unittest.TestCase):
    """golden 宇宙集成: 默认路径不变性 + 迟滞行为改变"""

    def _run(self, hysteresis=None):
        dfs = _universe()
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        path = tmp.name
        tmp.close()
        conn = sqlite3.connect(path)
        conn.executescript(_SCHEMA)
        for name, df in dfs.items():
            df.to_sql(name, conn, if_exists="append", index=False)
        conn.commit()
        conn.close()
        try:
            with mock.patch.object(bt_engine.db, "get_conn",
                                   side_effect=lambda: sqlite3.connect(path)), \
                 mock.patch.object(bt_engine.cfg_mod, "load_config",
                                   return_value=_FIXED_CFG):
                metrics = bt_engine.run_backtest(run_tag="golden_hyst",
                                                 hysteresis=hysteresis)
            conn = sqlite3.connect(path)
            nav = pd.read_sql(
                "SELECT trade_date, nav FROM backtest_nav ORDER BY trade_date",
                conn)["nav"].tolist()
            conn.close()
        finally:
            os.unlink(path)
        return metrics, nav

    def test_hyst0_explicit_matches_snapshot(self):
        """hyst=0 显式传参必须与默认路径逐位一致(golden 快照不扰动)"""
        metrics, nav = self._run(hysteresis=0)
        snap = json.loads(SNAP.read_text(encoding="utf-8"))
        for g, e in zip(nav, [r["nav"] for r in snap["nav"]]):
            self.assertAlmostEqual(g, e, places=9)
        for k in ("ann_return", "avg_turnover", "ic_mean", "sys_ann_return"):
            self.assertAlmostEqual(metrics[k], snap["metrics"][k], places=6)

    def test_hyst2_changes_behavior(self):
        """hyst=2: 净值改变(分支生效)、换手不升、IC 不变、指标指纹记录参数"""
        metrics, nav = self._run(hysteresis=2)
        snap = json.loads(SNAP.read_text(encoding="utf-8"))
        self.assertEqual(metrics["hysteresis"], 2)
        self.assertNotEqual(nav[-1], snap["nav"][-1]["nav"],
                            "迟滞带开启后净值应与默认路径不同")
        # 保留名单只会增加重叠持仓 → 换手 ≤ 基线(同评分口径, IC 不受影响)
        self.assertLessEqual(metrics["avg_turnover"], snap["metrics"]["avg_turnover"])
        self.assertEqual(metrics["ic_mean"], snap["metrics"]["ic_mean"])


if __name__ == "__main__":
    unittest.main()
