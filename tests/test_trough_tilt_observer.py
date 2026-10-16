"""tests/test_trough_tilt_observer.py — 低谷加仓 T1 前瞻观察器的纯函数单测。

覆盖（冻结定义 plans/trough-tilt-prereg-20260930.md §一/§二 的实现层）：
冷判定窗口语义 / 连续 2 期激活 / 事件聚类 / 新事件计数 / 拍板门双时钟 /
已实现镜像价差 / 状态幂等合并与截断。不联网、不读生产库、不写真实状态文件。
"""
import importlib.util
import json
import sys
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

_spec = importlib.util.spec_from_file_location(
    "trough_observer", BASE / "scripts" / "trough_tilt_observer.py")
ob = importlib.util.module_from_spec(_spec)
sys.modules["trough_observer"] = ob
_spec.loader.exec_module(ob)

DR = pd.date_range("2024-01-31", periods=31, freq="ME")


class TestColdFlags(unittest.TestCase):
    def test_frozen_formula_flips_after_negative_recent(self):
        ic = pd.Series([0.05] * 24 + [-0.05] * 7, index=DR)
        cold = ob.cold_flags(ic)
        self.assertFalse(cold.iloc[:28].any(), "前 28 锚点不得冷")
        self.assertTrue(bool(cold.iloc[28]), "recent 转负且 |long|≥0.01 ⇒ 冷")
        self.assertTrue(bool(cold.iloc[30]), "持续冷")

    def test_insufficient_history_is_false(self):
        ic = pd.Series([0.05] * 23, index=DR[:23])
        self.assertFalse(ob.cold_flags(ic).any())

    def test_long_below_bar_blocks_cold(self):
        # long=0.005 < 0.01 ⇒ 即使 recent 反向也不冷
        ic = pd.Series([0.002] * 24 + [-0.05] * 7, index=DR)
        cold = ob.cold_flags(ic)
        self.assertFalse(bool(cold.iloc[28]))

    def test_ext_index_evaluates_fresh_anchor_without_own_ic(self):
        ic = pd.Series([0.05] * 24 + [-0.05] * 7, index=DR)   # 最后锚点(2026-07-31)无 IC
        ext = DR.append(pd.DatetimeIndex(["2026-08-31"]))
        cold = ob.cold_flags(ic, ext_index=ext)
        self.assertIn(pd.Timestamp("2026-08-31"), cold.index)
        self.assertTrue(bool(cold.loc[pd.Timestamp("2026-08-31")]),
                        "新鲜锚点用前序 IC 评估（无前视）")


class TestActivationAndEpisodes(unittest.TestCase):
    def test_consecutive_two_cold_activates(self):
        idx = pd.date_range("2024-01-31", periods=7, freq="ME")
        cold = pd.Series([False, True, True, False, False, True, True], index=idx)
        act = ob.activation_flags(cold)
        self.assertEqual([i for i, v in act.items() if v],
                         [idx[2], idx[6]], "连续 2 期冷的第 2 期激活")
        self.assertFalse(bool(act.iloc[0]), "首锚点永不可能激活")

    def test_group_episodes(self):
        eps = ob.group_episodes(["2025-09", "2024-06", "2025-10", "2024-07", "2025-03"])
        self.assertEqual(eps, [["2024-06", "2024-07"], ["2025-03"],
                               ["2025-09", "2025-10"]])

    def test_new_episode_count(self):
        self.assertEqual(ob.new_episode_count([["2024-06", "2024-07"]]), 0)
        self.assertEqual(ob.new_episode_count([["2024-07"], ["2025-09", "2025-10"]]), 1)


class TestGate(unittest.TestCase):
    def test_two_new_episodes_ready(self):
        self.assertEqual(ob.gate_status(2, date(2026, 10, 1)), ("READY", 0))

    def test_twelve_month_clock(self):
        self.assertEqual(ob.gate_status(0, date(2026, 10, 15)), ("WAITING", 0))
        self.assertEqual(ob.gate_status(0, date(2027, 10, 1)), ("READY", 12))


class TestRealizedSpread(unittest.TestCase):
    def test_quintile_spread(self):
        mv = pd.Series([0.1, 0.5, 0.9, 0.3], index=list("ABCD"))
        fwd = pd.Series([0.04, 0.01, -0.02, 0.02], index=list("ABCD"))
        sp = ob.realized_spread(mv, fwd)
        self.assertEqual(sp["n_small"], 1)          # 只有 A 在最低五分位
        self.assertAlmostEqual(sp["r_small"], 0.04, places=6)
        self.assertAlmostEqual(sp["spread"], 0.0275, places=6)


class TestMergeState(unittest.TestCase):
    def test_history_appended_and_capped(self):
        state = {}
        for i in range(ob.HISTORY_CAP + 5):
            state = ob.merge_state(state, {"ran_at": f"2026-10-{i + 1:02d}",
                                           "new_episode_count": i,
                                           "gate": "WAITING", "months_elapsed": i})
        self.assertEqual(len(state["history"]), ob.HISTORY_CAP)
        self.assertEqual(state["history"][-1]["new_episode_count"],
                         ob.HISTORY_CAP + 4, "保留最新")
        self.assertNotIn("gate", state, "逐运行字段不进顶层")


if __name__ == "__main__":
    unittest.main()
