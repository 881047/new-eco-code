"""`regime` 三态映射的**决策逻辑**测试（2026-09-13，独立审查 §4.1）

`compute_regime()` 自己是集成函数（读库 + 可选联网拉 PE），测试它要拖一堆桩；而**真正决定
攻/守/中的是三个纯函数**：

    _apply_state_rules(score, dd, vol, score_max)  —— 三态映射
    _combine_states(base, other, mode)             —— 风格防守两路合并
    _pool_state_frame(pc)                          —— 池子分支（纯价格、score_max=6）

这块驱动 `state_stocks`（持仓只数）与回撤防守 —— 也就是说，**这三条规则错了，终端会在该防守
的时候进攻**。所以钉死它们，包括边界与"更防守优先"的方向性。
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.models import regime as rg                              # noqa: E402


def _s(*vals):
    return pd.Series(vals, dtype="float64")


class TestApplyStateRules(unittest.TestCase):
    def test_truth_table(self):
        # score / dd / vol / 期望
        cases = [
            (6.0, 0.00, 0.10, "attack"),        # 满分 → 进攻
            (5.0, -0.05, 0.20, "attack"),       # score_max-1 = 5 → 进攻
            (4.0, -0.05, 0.20, "neutral"),      # 既不满足进攻也不满足防守
            (2.0, 0.00, 0.10, "defense"),       # 分数地板
            (6.0, -0.21, 0.10, "defense"),      # **回撤否决满分**（保命规则）
            (6.0, 0.00, 0.36, "defense"),       # **波动否决满分**
        ]
        for score, dd, vol, want in cases:
            with self.subTest(score=score, dd=dd, vol=vol):
                got = rg._apply_state_rules(_s(score), _s(dd), _s(vol), 6).iloc[0]
                self.assertEqual(got, want)

    def test_thresholds_are_strict(self):
        """边界必须"不触发"：dd 恰好 −20% 与 vol 恰好 35% 都不算防守（严格小于/大于）。"""
        self.assertEqual(rg._apply_state_rules(_s(6), _s(-0.20), _s(0.10), 6).iloc[0], "attack")
        self.assertEqual(rg._apply_state_rules(_s(6), _s(0.0), _s(0.35), 6).iloc[0], "attack")
        self.assertEqual(rg._apply_state_rules(_s(3), _s(0.0), _s(0.10), 6).iloc[0], "neutral")

    def test_defense_wins_over_attack_in_same_row(self):
        """同一行同时满足进攻与防守 ⇒ 必须是防守（保守优先）。"""
        got = rg._apply_state_rules(_s(6), _s(-0.30), _s(0.50), 6).iloc[0]
        self.assertEqual(got, "defense")


class TestCombineStates(unittest.TestCase):
    def test_worse_takes_more_defensive(self):
        base = pd.Series(["attack", "neutral", "defense"])
        other = pd.Series(["defense", "neutral", "attack"])
        got = rg._combine_states(base, other, "worse")
        self.assertEqual(list(got), ["defense", "neutral", "defense"],
                         "worse 只能加防守，不许放松进攻")

    def test_replace_uses_other(self):
        base = pd.Series(["attack", "defense"])
        other = pd.Series(["defense", "attack"])
        self.assertEqual(list(rg._combine_states(base, other, "replace")), ["defense", "attack"])

    def test_unknown_mode_falls_back_to_worse_with_warning(self):
        base = pd.Series(["attack"])
        other = pd.Series(["defense"])
        with self.assertLogs("app.models.regime", level="WARNING") as cm:
            got = rg._combine_states(base, other, "typo_mode")
        self.assertEqual(got.iloc[0], "defense", "未知 mode 必须保守退回 worse")
        self.assertIn("未知", "".join(cm.output))

    def test_missing_dates_keep_base_unchanged(self):
        """池子指数覆盖不到的早期日期：不许凭空改变基准状态。"""
        base = pd.Series(["attack", "attack"], index=["20150105", "20150106"])
        other = pd.Series(["defense"], index=["20150106"])       # 只有后一天有池子读数
        got = rg._combine_states(base, other, "worse")
        self.assertEqual(got.loc["20150105"], "attack")
        self.assertEqual(got.loc["20150106"], "defense")


class TestPoolStateFrame(unittest.TestCase):
    def _series(self, n=400, trend=0.0005):
        idx = pd.bdate_range("2020-01-01", periods=n)
        px = pd.Series(100 * np.cumprod(1 + np.full(n, trend)), index=idx)
        return px

    def test_warmup_rows_are_dropped(self):
        px = self._series(400)
        out = rg._pool_state_frame(px)
        self.assertEqual(len(out), 400 - 249,
                         "ma250 未满的行必须剔除（否则回撤那条防守规则整段失效）")
        self.assertFalse(out["pool_state"].isna().any())

    def test_uptrend_scores_high_and_attacks(self):
        out = rg._pool_state_frame(self._series(400, trend=0.002))
        self.assertEqual(out["pool_score"].iloc[-1], 6)
        self.assertEqual(out["pool_state"].iloc[-1], "attack")

    def test_crash_triggers_defense_even_with_good_score(self):
        """先涨后崩：回撤 < −20% 时必须是 defense（哪怕均线结构还在）。"""
        px = self._series(400, trend=0.002)
        px.iloc[-15:] = px.iloc[-16] * 0.7          # 一次性 −30%
        out = rg._pool_state_frame(px)
        self.assertEqual(out["pool_state"].iloc[-1], "defense")


if __name__ == "__main__":
    unittest.main()
