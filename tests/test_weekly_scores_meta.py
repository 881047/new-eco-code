"""周频打分存档的 meta 必须**自证本次调用参数**（2026-09-13）

独立审查 #6：`scores.save()` 原来把**模块常量**写进 meta（`HORIZON/EMBARGO/TRAIN_MONTHS/
SEEDS`），而 `run_multi` 连 horizons / die 权重都不记 ⇒ 用非默认超参或种子跑出来的存档会
**自称是默认配置** —— 可回溯纪律（规划 §6.1-2「任一产物可反查其输入版本」）直接漏掉。

守三条：① 默认调用仍写全默认键；② **非默认参数必须如实落盘**；③ 参数指纹存在且随参数变化。
"""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.weekly import scores as wsc                   # noqa: E402


class TestScoresMeta(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="neweco_scores_meta_"))
        self._orig_dir = wsc.DATA_DIR
        wsc.DATA_DIR = self.tmp
        self.panel = {"anchors": pd.DatetimeIndex(["2026-09-11"]), "cols": ["600000.SH"],
                      "names": ["size"]}
        self.S = np.zeros((1, 1), dtype=np.float32)

    def tearDown(self):
        wsc.DATA_DIR = self._orig_dir
        for p in self.tmp.glob("*"):
            p.unlink()
        self.tmp.rmdir()

    def _meta(self, name):
        z = np.load(self.tmp / name, allow_pickle=True)
        return {str(m).split("=", 1)[0]: str(m).split("=", 1)[1] for m in z["meta"]}

    def test_default_run_records_module_defaults(self):
        wsc.save("20260911", self.panel, self.S)
        m = self._meta("scores_20260911.npz")
        for k in ("horizon", "embargo", "train_months", "seeds", "rounds", "params_sha16"):
            self.assertIn(k, m, f"meta 缺 {k}")

    def test_non_default_params_are_recorded_verbatim(self):
        """这条是关键：非默认参数必须如实写进去，而不是写模块常量。"""
        wsc.save("20260911", self.panel, self.S, horizons=[5, 21], seeds=[1, 2, 3],
                 w5_weight=3, pit_weight=2, with_production=True)
        m = self._meta("scores_20260911.npz")
        self.assertEqual(m["horizons"], "[5, 21]")
        self.assertEqual(m["seeds"], "[1, 2, 3]")
        self.assertEqual(m["w5_weight"], "3")
        self.assertEqual(m["pit_weight"], "2")
        self.assertEqual(m["with_production"], "True")

    def test_params_fingerprint_changes_with_params(self):
        wsc.save("20260911", self.panel, self.S)
        a = self._meta("scores_20260911.npz")["params_sha16"]
        wsc.save("20260911", self.panel, self.S, seeds=[9])
        b = self._meta("scores_20260911.npz")["params_sha16"]
        self.assertNotEqual(a, b, "换了参数但指纹没变 ⇒ 存档仍无法自证")

    def test_multi_horizon_does_not_keep_singular_horizon(self):
        """独立复核抓到的自述瑕疵：多 horizon 时若同时写单数 `horizon`，meta 自相矛盾。"""
        wsc.save("20260911", self.panel, self.S, horizons=[5, 21])
        m = self._meta("scores_20260911.npz")
        self.assertEqual(m["horizons"], "[5, 21]")
        self.assertNotIn("horizon", m, "多 horizon 时不许再写单数 horizon（假自述）")

    def test_dies_are_recorded_as_declared(self):
        """哪几路 die 真的跑了要被记下来（原来 industry 是硬编码，会谎报）。"""
        wsc.save("20260911", self.panel, self.S, horizons=[5], dies=["W5(h=5)"],
                 industry=["snapshot"])
        m = self._meta("scores_20260911.npz")
        self.assertEqual(m["dies"], "['W5(h=5)']")
        self.assertEqual(m["industry"], "['snapshot']")


if __name__ == "__main__":
    unittest.main()
