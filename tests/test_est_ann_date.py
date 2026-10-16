"""`akshare_client._est_ann_date` —— **前视风险的入口**，必须有行为测试（2026-09-13）

为什么这个函数值得单独测（独立审查 §4.1 第一批）：

1. 它是 **AkShare 兜底路径**的披露日来源（主源没有 `ann_date` 时用它）；
2. 它还是**污染钳制器**：`updater.py:425` 实测遇到过"上游 `ann_date` 早于报告期末"
   （例：某股半年报的 ann_date 写成年报披露日）——那是**前视污染**，用本函数的法定截止日
   把它钳回去。方向必须"**宁可滞后、不可前视**"。

据此守三条：
  ① 四季规则逐条钉死（年报→次年 4/30、一季报→4/30、半年报→8/31、三季报→10/31）；
  ② **不可前视**：任何合法期末的估计日都必须 ≥ 期末本身；
  ③ 非法输入返回空串（缺失 ≠ 前视，且不许抛异常炸断整批 fallback）。
"""
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.datasource.akshare_client import _est_ann_date        # noqa: E402


class TestEstAnnDate(unittest.TestCase):
    def test_statutory_deadlines(self):
        cases = {
            "20241231": "20250430",      # 年报 → 次年 4/30
            "20250331": "20250430",      # 一季报 → 当年 4/30
            "20250630": "20250831",      # 半年报 → 8/31
            "20250930": "20251031",      # 三季报 → 10/31
            "20231231": "20240430",      # 跨年
            "20260331": "20260430",
        }
        for end, want in cases.items():
            with self.subTest(end=end):
                self.assertEqual(_est_ann_date(end), want)

    def test_never_looks_ahead(self):
        """**核心不变量**：估计披露日不许早于报告期末（早一分钟都是前视）。"""
        ends = [f"{y}{m:02d}{d:02d}"
                for y in (2018, 2020, 2022, 2024, 2025, 2026)
                for m, d in ((3, 31), (6, 30), (9, 30), (12, 31))]
        for end in ends:
            with self.subTest(end=end):
                est = _est_ann_date(end)
                self.assertNotEqual(est, "")
                self.assertGreaterEqual(est, end, "估计披露日早于报告期末 = 前视")

    def test_invalid_input_returns_empty(self):
        for bad in ("", "nan", "2024", "20241301", "2024-12-31", None, 20241231):
            with self.subTest(bad=bad):
                self.assertEqual(_est_ann_date(bad), "")

    def test_unknown_month_still_not_ahead(self):
        """非季末月份（上游脏数据）走最后一条分支 → 当年 10/31，仍不许早于期末。"""
        self.assertEqual(_est_ann_date("20250715"), "20251031")
        self.assertGreaterEqual(_est_ann_date("20250715"), "20250715")


if __name__ == "__main__":
    unittest.main()
