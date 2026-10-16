"""标签口径的**交叉校验测试**（2026-09-14；KB 14 未结事项 #4 的"更安全的第一步"）。

项目里有两份"未来收益"标签，且**有意不同**：

  生产（`app.models.trainer.forward_return(delist_last_price=True)`）
      = `close×adj.shift(-h) / (close×adj) − 1`，但当窗口末端**没有价格**（退市/长期停牌 ⇒
        `daily` 表直接缺行）时，回退用"窗口内最后有效价"；那批尾部样本因此**不是 NaN**
        （2026-09-09 修复，实测影响 327 个样本）。
  门禁/研究（`app.alpha.gate.forward_return`）
      = 朴素 `shift(-h)/cur − 1`（尾部就是 NaN）。两者的 docstring 都写明"有意不同"。

这条测试**不是**要统一它们（统一必须预注册 A/B，会改候选判决），而是把差异**钉死**：
  ① 重叠域（两者都非 NaN）必须**逐位一致**；
  ② 生产标签不许比朴素标签**更缺**（`only_naive` 必须为空）；
  ③ 差异必须**只**出现在"窗口内还有最后有效价"的尾部格子上，且位置恰好是预期的那两格
     （改了任一实现 ⇒ 当场红）。
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.alpha import gate as alpha_gate      # noqa: E402
from app.models import trainer               # noqa: E402


class TestLabelCrossCheck(unittest.TestCase):
    H = 2

    @staticmethod
    def _ac() -> pd.DataFrame:
        """A=完整；B=第 3 天起停牌（窗口内仍有最后有效价 22）；C=整段无价（未上市）。"""
        idx = pd.date_range("2026-01-01", periods=6, freq="D")
        return pd.DataFrame({
            "A": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0],
            "B": [20.0, 21.0, 22.0, np.nan, np.nan, np.nan],
            "C": [np.nan] * 6,
        }, index=idx)

    def test_production_matches_the_naive_label_on_the_overlap_domain(self):
        ac = self._ac()
        prod = trainer.forward_return(ac, horizon=self.H, delist_last_price=True)
        naive = ac.shift(-self.H) / ac - 1.0                     # 与 gate.forward_return 同一算式
        both = prod.notna() & naive.notna()
        self.assertTrue(both.any().any())
        mask = both.to_numpy()
        self.assertTrue(np.array_equal(prod.to_numpy()[mask], naive.to_numpy()[mask]),
                        "重叠域必须逐位一致（两条标签只是尾部口径不同）")

    def test_gate_uses_the_plain_formula(self):
        """`gate.forward_return` 的输入是 `m["close"] * m["adj"]` —— 用同一份面板对齐。"""
        ac = self._ac()
        m = {"close": ac, "adj": pd.DataFrame(1.0, index=ac.index, columns=ac.columns)}
        naive = ac.shift(-self.H) / ac - 1.0
        got = alpha_gate.forward_return(m, horizon=self.H)
        pd.testing.assert_frame_equal(got, naive)

    def test_difference_is_exactly_the_delisting_tail(self):
        ac = self._ac()
        prod = trainer.forward_return(ac, horizon=self.H, delist_last_price=True)
        naive = ac.shift(-self.H) / ac - 1.0
        only_naive = naive.notna() & prod.isna()
        self.assertFalse(only_naive.any().any(), "生产标签不该比朴素标签更缺")
        only_prod = prod.notna() & naive.isna()
        got = {(str(ac.index[i].date()), c)
               for i, j in zip(*np.where(only_prod.to_numpy()))
               for c in [ac.columns[j]]}
        # B 在 t1（窗口尾只有最后有效价 22）与 t2（窗口内没有新价 ⇒ 收益 0）两格
        self.assertEqual(got, {("2026-01-02", "B"), ("2026-01-03", "B")}, got)
        self.assertAlmostEqual(float(prod.loc[ac.index[1], "B"]), 22.0 / 21.0 - 1.0, places=12)
        self.assertAlmostEqual(float(prod.loc[ac.index[2], "B"]), 0.0, places=12)


if __name__ == "__main__":
    unittest.main()
