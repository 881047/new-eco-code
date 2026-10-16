"""风格防守测试 (2026-09-10)

1. _combine_states: worse 只加防守不放松 / replace 完全替换 / 未知 mode 退回 worse /
   缺数据日期保持 base
2. _state_frame: 关闭时行为与现行完全一致(池子列全 NaN); 开启后
   「中证800 涨而池子崩」→ worse 判 defense(2024-02 场景的缩微版)
3. build_pool_index: SQL 护栏 —— 北交(4/8/9 前缀, 含 920xxx 脏数据)、科创(688)、
   |pct_chg|>21、无成交、无 pct_chg 一律不入指数
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.models import pool_index as pool_mod
from app.models.regime import _combine_states, _state_frame


def _mk(vals):
    """按交易日字符串索引构造序列(与生产 trade_date 同为 YYYYMMDD 字符串)"""
    idx = pd.bdate_range("2020-01-01", periods=len(vals)).strftime("%Y%m%d")
    return pd.Series(np.asarray(vals, dtype=float), index=idx)


class TestCombineStates(unittest.TestCase):
    def setUp(self):
        self.idx = _mk([1, 2, 3, 4]).index
        self.base = pd.Series(["attack", "neutral", "defense", "attack"], index=self.idx)

    def test_worse_takes_defensive_side(self):
        other = pd.Series(["defense", "attack", "attack", "neutral"], index=self.idx)
        got = _combine_states(self.base, other, "worse")
        self.assertEqual(list(got), ["defense", "neutral", "defense", "neutral"])

    def test_worse_never_upgrades(self):
        """base 防守时, other 再进攻也不能放松(只加防守)"""
        base = pd.Series(["defense"] * 3, index=self.idx[:3])
        other = pd.Series(["attack"] * 3, index=self.idx[:3])
        self.assertEqual(list(_combine_states(base, other, "worse")), ["defense"] * 3)

    def test_replace_uses_other(self):
        other = pd.Series(["defense", "attack", "attack", "neutral"], index=self.idx)
        self.assertEqual(list(_combine_states(self.base, other, "replace")),
                         ["defense", "attack", "attack", "neutral"])

    def test_unknown_mode_falls_back_to_worse(self):
        other = pd.Series(["defense"] * 4, index=self.idx)
        self.assertEqual(list(_combine_states(self.base, other, "typo")),
                         list(_combine_states(self.base, other, "worse")))

    def test_missing_dates_keep_base(self):
        """池子未覆盖的日期不应凭空改变状态(降级=关闭)"""
        other = pd.Series(["attack", "attack"], index=self.idx[:2])
        got = _combine_states(self.base, other, "worse")
        # idx1: base=neutral 不被 attack 抬升; idx2/3: 池子无数据 → 保持 base
        self.assertEqual(list(got), ["attack", "neutral", "defense", "attack"])


class TestStateFrameStyleGuard(unittest.TestCase):
    """_state_frame: 池子分支与合并(用 mock 提供池子收盘序列, 不碰库)"""

    def setUp(self):
        n = 400
        self.c = _mk(100.0 * np.cumprod(np.full(n, 1.002)))       # 中证800 稳步上涨
        up = 100.0 * np.cumprod(np.full(n, 1.002))                # 池子同样上涨...
        crash = up[-40] * np.linspace(1.0, 0.55, 40)              # ...但尾部崩 45%
        self.pool = _mk(np.concatenate([up[:-40], crash]))

    def _frame(self, guard):
        with mock.patch.object(pool_mod, "load_pool_close", return_value=self.pool):
            return _state_frame(self.c, None, guard)

    def test_disabled_matches_current_behaviour(self):
        out = self._frame({})
        self.assertTrue((out["state"] == "attack").all())
        self.assertTrue(out["pool_state"].isna().all())
        self.assertTrue(out["pool_score"].isna().all())

    def test_worse_guards_when_pool_crashes(self):
        """中证800 一路上涨(六层全多), 但池子已从高点跌 45% → 防守必须触发"""
        out = self._frame({"enabled": True, "mode": "worse"}).set_index("trade_date")
        self.assertTrue((out["pool_state"].tail(10) == "defense").all())
        self.assertTrue((out["state"].tail(10) == "defense").all())
        # 崩盘之前两路一致(都是 attack), 合并后不变
        self.assertTrue((out["state"].head(10) == "attack").all())
        self.assertTrue((out["pool_drawdown"].tail(1) < -0.20).all())

    def test_replace_uses_pool_only(self):
        out = self._frame({"enabled": True, "mode": "replace"}).set_index("trade_date")
        self.assertTrue((out["state"].tail(10) == "defense").all())
        # 池子前段也是上涨 → attack; 替换后与中证800 分支一致
        self.assertTrue((out["state"].head(10) == "attack").all())

    def test_empty_pool_degrades_to_current(self):
        """池子表为空(pool_index 未建)时保持现行行为, 不报错"""
        with mock.patch.object(pool_mod, "load_pool_close",
                               return_value=pd.Series(dtype=float)):
            out = _state_frame(self.c, None, {"enabled": True, "mode": "worse"})
        self.assertTrue((out["state"] == "attack").all())


class TestBuildPoolIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = str(Path(self.tmp) / "t.db")
        conn = sqlite3.connect(self.path)
        conn.executescript(db.SCHEMA)
        conn.executemany(
            "INSERT INTO daily (ts_code, trade_date, pct_chg, amount) VALUES (?,?,?,?)",
            [("600000.SH", "20240102", 1.0, 1000.0),      # 计入
             ("000001.SZ", "20240102", 3.0, 1000.0),      # 计入
             ("920139.BJ", "20240102", 153300.0, 1000.0),  # 北交脏数据(pre_close=0.01)
             ("830799.BJ", "20240102", 50.0, 1000.0),     # 北交
             ("688981.SH", "20240102", 5.0, 1000.0),      # 科创
             ("300750.SZ", "20240102", -25.0, 1000.0),    # |pct_chg|>21 护栏
             ("600001.SH", "20240102", None, 1000.0),     # 无 pct_chg
             ("600002.SH", "20240102", 2.0, 0.0),         # 无成交
             ("600000.SH", "20240103", -2.0, 1000.0),     # 计入
             ("000001.SZ", "20240103", 0.0, 1000.0)])     # 计入
        conn.commit()
        conn.close()

    def test_sql_guardrails(self):
        with mock.patch.object(db, "get_conn",
                               side_effect=lambda: sqlite3.connect(self.path)):
            n = pool_mod.build_pool_index()
        self.assertEqual(n, 2)
        conn = sqlite3.connect(self.path)
        rows = conn.execute("SELECT trade_date, ret, close, n FROM pool_index "
                            "ORDER BY trade_date").fetchall()
        conn.close()
        # 月内权重漂移口径: 首日 eq-mean(1.01,1.03)=1.02 → 1020.0;
        # 次日两条路径 600000→1.01×0.98=0.9898 / 000001→1.03, 均值 1.0099 → 1009.9。
        # (若按"日频再平衡"错误口径会得 1020×0.99=1009.8 —— 差 0.1 就是月内漂移 vs
        #  日频再平衡的差, 长期累积约 +5pp/年, 会把防守信号系统性冲淡)
        self.assertEqual(rows[0][0], "20240102")
        self.assertAlmostEqual(rows[0][2], 1020.0, places=6)
        self.assertEqual(rows[0][3], 2)
        self.assertAlmostEqual(rows[1][2], 1009.9, places=6)
        self.assertAlmostEqual(rows[1][1], 1009.9 / 1020.0 - 1, places=10)
        self.assertEqual(rows[1][3], 2)

    def test_empty_daily_returns_zero(self):
        empty = str(Path(self.tmp) / "empty.db")
        conn = sqlite3.connect(empty)
        conn.executescript(db.SCHEMA)
        conn.close()
        with mock.patch.object(db, "get_conn",
                               side_effect=lambda: sqlite3.connect(empty)):
            self.assertEqual(pool_mod.build_pool_index(), 0)


if __name__ == "__main__":
    unittest.main()
