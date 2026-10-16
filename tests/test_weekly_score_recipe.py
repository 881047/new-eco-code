"""周频生产打分**配方**测试（2026-09-13，独立审查 §4.1）

`weekly.scores.run_multi()` 是周频主力链路的入口，但它要真行情 + 真训练（分钟级）⇒ 零覆盖。
真正需要被**钉住**的是它的**配方**，而不是训练本身：

    die 组合与权重：**2 × W5(5 日标签) + 1 × pit(申万一级 PIT 中性化)**   ← F22 判决
    融合方式：**分位排名加权平均**（不是等权、不是 stacking —— 那两条路已被证伪）
    存档 meta：必须如实记录 dies / horizons / 权重（2026-09-13 修的那个"自证"缺口）

本测试把重活（载行情 / 建面板 / 训练 / 落盘）全替换成桩，让**真实的融合算术**跑起来，
然后断言融合结果恰好等于"2×W5 与 1×pit 的分位排名加权平均"，并检查存档参数。
这样：配方被改（权重、die 数、排名口径）测试立刻红，而不用跑一次真训练。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.weekly import scores as wsc                              # noqa: E402


def _market(n_days=30, n_cols=8):
    idx = pd.bdate_range("2026-06-01", periods=n_days)
    cols = [f"{600000 + i}.SH" for i in range(n_cols)]
    return {"close": pd.DataFrame(np.ones((n_days, n_cols)), index=idx, columns=cols)}


class TestRunMultiRecipe(unittest.TestCase):
    def _run(self, horizons=(5,), w5_weight=2, pit_weight=1, with_production=False):
        m = _market()
        dates = pd.to_datetime(m["close"].index)
        anchors = wsc.week_end_dates(dates)
        cols = list(m["close"].columns)
        rng = np.random.default_rng(0)
        w5 = rng.random((len(anchors), len(cols)))
        pit = rng.random((len(anchors), len(cols)))
        panel = {"anchors": anchors, "cols": cols, "names": ["f"], "F": None,
                 "mask": None, "pit": None, "m": m, "industry": "snapshot"}
        calls = {"train": [], "saved": {}}

        def _fake_train(panel_, cfg_, seeds=None, horizon=5, **kw):
            calls["train"].append(horizon)
            # ⚠️ 契约：`train()` 返回的**已经是分位排名矩阵**（内部 `_rank_pct` 逐种子平均），
            # 不是原始预测值。而 `run_multi` 随后还会对它再 `.rank(pct=True)` 一次 ——
            # 即生产线上存在**双重排名**。两次都是单调变换，所以不改数值结论；
            # 但"改掉这层多余排名"会改变融合后的分布 ⇒ 属改口径，需 A/B，不许顺手删。
            raw = pit if panel_["industry"] == "pit" else w5
            return pd.DataFrame(raw).rank(axis=1, pct=True).to_numpy()

        def _fake_save(asof, panel_, S, **info):
            calls["saved"] = {"asof": asof, "S": S, "info": info}
            return Path("fake.npz")

        # ⚠️ 桩必须**如实反映 industry 参数** —— 否则两次 build_panel(snapshot/pit) 会返回同
        # 一个面板，两路 die 拿到同一份分数，融合结果退化成单路排名（本测试第一版就这么错过）。
        def _fake_panel(_m, _anchors, _cfg, industry="snapshot", **k):
            return {**panel, "industry": industry}

        with mock.patch.object(wsc.fe, "load_market", return_value=m), \
                mock.patch.object(wsc, "build_panel", side_effect=_fake_panel), \
                mock.patch.object(wsc, "train", side_effect=_fake_train), \
                mock.patch.object(wsc, "save", side_effect=_fake_save):
            wsc.run_multi(asof="20260911", cfg={}, horizons=horizons,
                          seeds=(1, 2), w5_weight=w5_weight, pit_weight=pit_weight,
                          with_production=with_production)
        return calls, (w5, pit, anchors, cols)

    def test_fusion_is_weighted_rank_average_of_w5_and_pit(self):
        calls, (w5, pit, anchors, cols) = self._run()
        # 期望：对 train 已经给出的分位排名**再取一次排名**（生产就是这么写的），
        # 然后 2×W5 + 1×pit 加权平均。
        w5r = pd.DataFrame(w5).rank(axis=1, pct=True).rank(axis=1, pct=True)
        pitr = pd.DataFrame(pit).rank(axis=1, pct=True).rank(axis=1, pct=True)
        exp = ((w5r * 2 + pitr * 1) / 3).to_numpy()
        got = calls["saved"]["S"]
        # 存档是 **float32**（生产 `S = (...).to_numpy(dtype=np.float32)`）：先钉 dtype，
        # 再用 float32 级容差 —— 用 1e-12 会因舍入噪声假红（本测试第一版就这么错过一次）。
        self.assertEqual(got.dtype, np.float32, "打分存档必须是 float32（口径的一部分）")
        np.testing.assert_allclose(got, exp, rtol=0, atol=1e-6)

    def test_trains_exactly_two_dies_w5_and_pit(self):
        calls, _ = self._run()
        # 两次训练：一次 snapshot(horizon=5)、一次 pit(horizon=HORIZON)；顺序即 die 顺序
        self.assertEqual(calls["train"], [5, wsc.HORIZON])

    def test_pit_weight_zero_drops_the_pit_die(self):
        calls, (w5, pit, anchors, cols) = self._run(pit_weight=0)
        self.assertEqual(calls["train"], [5], "pit_weight=0 时不该训练 pit die")
        exp = pd.DataFrame(w5).rank(axis=1, pct=True).rank(axis=1, pct=True).to_numpy()
        np.testing.assert_allclose(calls["saved"]["S"], exp, rtol=0, atol=1e-6)
        self.assertEqual(calls["saved"]["info"]["dies"], ["W5(h=5)"])

    def test_saved_meta_records_the_recipe(self):
        calls, _ = self._run()
        info = calls["saved"]["info"]
        self.assertEqual(info["dies"], ["W5(h=5)", "pit"])
        self.assertEqual(info["w5_weight"], 2)
        self.assertEqual(info["pit_weight"], 1)
        self.assertEqual(info["horizons"], [5])
        self.assertEqual(info["industry"], ["snapshot", "pit"])


if __name__ == "__main__":
    unittest.main()
