"""**实盘路径里不许写死历史常量**（2026-09-13，同类缺陷第二次出现后的守卫）

背景（两次真实缺陷，同一类）：
  ① `health.BACKTEST_TURNOVER` 写死 0.092（研究构型 hyst=0.2 的换手）⇒ 阈值偏紧 ~50%，
     会把正常换手判成 `degrading`，而 `risk.apply` 会**自动停买**（07 号 #25）；
  ② `attrib.current()` 的看板文案写死 "权益权重(0.873)"，而现役 `weekly.equity_weight` 是 **1.0**
     ⇒ 用户看到的说明与实盘配置不符。

本文件守两条：
  1. **文案必须跟着实时配置**：`attrib.invested_ratio_note(w)` 用传入的权重，且用现役配置调用时
     不许出现 0.873 这类历史值；
  2. **静态扫描**：`app/**/*.py` 里不许再出现"权益权重(0.873)"这类把历史敞口写进用户可见文案的字样。

运行: python -m unittest tests.test_no_stale_live_constants -v
"""
import re
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfg_mod                  # noqa: E402
from app.weekly import attrib as wk_attrib         # noqa: E402
from app.weekly import plan as wk_plan             # noqa: E402


class TestInvestedRatioNote(unittest.TestCase):
    def test_note_follows_the_weight_it_is_given(self):
        self.assertIn("(1.0)", wk_attrib.invested_ratio_note(1.0))
        self.assertNotIn("0.873", wk_attrib.invested_ratio_note(1.0))

    def test_note_uses_live_config_weight(self):
        live = float(wk_plan.equity_weight(cfg_mod.load_config()))
        note = wk_attrib.invested_ratio_note(live)
        self.assertIn(f"({live})", note, "看板文案必须写当前权益权重")
        self.assertNotIn("0.873", note,
                         "现役敞口是 1.0 ⇒ 文案里不许再出现旧敞口 0.873（07 号 #25 同类缺陷）")


class TestNoHardcodedHistoricWeightInStrings(unittest.TestCase):
    """静态扫描：用户可见文案里不许写死历史敞口（注释/历史台账不在此列）。"""

    PATTERN = re.compile(r"权益权重\(\s*0\.873\s*\)")

    def test_no_string_hardcodes_old_weight(self):
        hits = []
        for p in sorted((BASE / "app").rglob("*.py")):
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue                      # 注释可以是历史记录
                if self.PATTERN.search(line):
                    hits.append(f"{p.relative_to(BASE)}:{i}")
        self.assertEqual(hits, [], f"用户可见文案里写死了历史敞口: {hits}")


if __name__ == "__main__":
    unittest.main()
