"""生产因子剔除 `_apply_exclusions` 的守卫测试（2026-09-27 第 6 次重钉接线）。

预注册 `plans/excl-pool-ab-prereg-20260927.md` + 05 号台账十九/二十补。
每组判据都带"摘掉必须当场变红"的那条：
  · 摘掉"未知名字忽略并告警" ⇒ 拼写错误的剔除名单会静默少剔（fail-silent）；
  · 摘掉"None/空原样返回" ⇒ 缺省配置会意外改变生产因子集（DEFAULT 关纪律）。
纯函数测试，不碰生产库。
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.trainer import _apply_exclusions  # noqa: E402


def _wide(factors=("bias_60", "mom_60", "size")) -> pd.DataFrame:
    idx = pd.to_datetime(["2021-01-29", "2021-02-26"])
    codes = ["S1", "S2"]
    parts = []
    for f in factors:
        df = pd.DataFrame(np.arange(4.0).reshape(2, 2), index=idx, columns=codes)
        df.columns = pd.MultiIndex.from_product([[f], codes],
                                                names=["factor", "ts_code"])
        parts.append(df)
    return pd.concat(parts, axis=1)


class TestApplyExclusions(unittest.TestCase):
    def test_none_returns_same(self):
        """缺省（None）原样返回 —— DEFAULT 关纪律，不传名单不改生产因子集"""
        w = _wide()
        out = _apply_exclusions(w, None)
        self.assertEqual(out.shape, w.shape)

    def test_empty_list_returns_same(self):
        w = _wide()
        out = _apply_exclusions(w, [])
        self.assertEqual(out.shape, w.shape)

    def test_drops_named_factors(self):
        w = _wide()
        out = _apply_exclusions(w, ["bias_60"])
        remaining = set(out.columns.get_level_values("factor"))
        self.assertEqual(remaining, {"mom_60", "size"})

    def test_unknown_names_ignored(self):
        """名单里含 factor 表不存在的名字 ⇒ 忽略不炸（少剔不会被静默掩盖：存在的照剔）"""
        w = _wide()
        out = _apply_exclusions(w, ["bias_60", "not_a_factor"])
        remaining = set(out.columns.get_level_values("factor"))
        self.assertEqual(remaining, {"mom_60", "size"})

    def test_all_excluded_gives_empty(self):
        """全剔 = 空表（调用方下游自然失败, 不在此层兜底造数据）"""
        w = _wide()
        out = _apply_exclusions(w, ["bias_60", "mom_60", "size"])
        self.assertEqual(len(out.columns), 0)


if __name__ == "__main__":
    unittest.main()
