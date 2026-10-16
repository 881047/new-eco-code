"""重钉工具 `scripts/pin_baseline.py` 的产出物测试（2026-09-13，独立审查 §4.1 第一批）

这个工具**重钉基线 pin** —— 而 pin 是"改动有没有动基线"的唯一判据。它本身原来零测试：
它打印的那段注释块是**人要粘进 `config.yaml` 的产物**，格式/数字错了就等于把错误基线写进注释。

守三条：① 注释块里的数字必须来自传入的 metrics（不是硬编码）；② 全史/评估窗两组数各自出现；
③ **工具不许写 config**（纪律：只测量不决策 —— 换 pin 必须是有意识的人工动作）。
"""
import importlib.util
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

_spec = importlib.util.spec_from_file_location("pin_baseline_tool", BASE / "scripts" / "pin_baseline.py")


class TestCommentBlock(unittest.TestCase):
    """只加载函数、不执行 main（模块顶层只有 setup_logger/api 初始化，无需回测）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = importlib.util.module_from_spec(_spec)
        sys.modules["pin_baseline_tool"] = cls.mod
        _spec.loader.exec_module(cls.mod)

    MP = {"run_date": "20260912_limitquant", "periods": 85, "ann_return": 0.2262,
          "max_drawdown": -0.2410, "sharpe": 0.853, "sys_ann_return": 0.1940,
          "sys_max_drawdown": -0.1966, "sys_mdd_daily": -0.3172, "ic_mean": 0.0959,
          "icir": 0.859, "avg_stocks": 4.94, "avg_cash_idle": 0.2871, "avg_turnover": 0.119}
    MF = {"run_date": "20260912_limitquant_full", "periods": 115, "ann_return": 0.1159,
          "max_drawdown": -0.5126, "sharpe": 0.399, "sys_ann_return": 0.1016,
          "sys_max_drawdown": -0.4210, "sys_mdd_daily": -0.4264, "ic_mean": 0.08,
          "icir": 0.7, "avg_stocks": 4.74, "avg_cash_idle": 0.324, "avg_turnover": 0.11}

    def _block(self):
        return "\n".join(self.mod.comment_block(self.MP, self.MF, "20190722"))

    def test_numbers_come_from_metrics_not_hardcoded(self):
        txt = self._block()
        self.assertIn("85 期", txt)
        self.assertIn("22.62%", txt)          # 满仓年化
        self.assertIn("19.40%", txt)          # 系统年化
        self.assertIn("20190722", txt)        # 评估窗
        self.assertIn("20260912", txt)        # 重钉日期取自 run_date

    def test_full_window_line_present(self):
        txt = self._block()
        self.assertIn("全史窗(115 期)", txt)
        self.assertIn("11.59%", txt)          # 全史满仓年化

    def test_tool_does_not_write_config(self):
        src = (BASE / "scripts" / "pin_baseline.py").read_text(encoding="utf-8")
        self.assertNotIn("save_config", src,
                         "重钉工具不许写 config —— 换 pin 必须是有意识的人工动作")
        for bad in ('open(ENV_FILE', "open(str(cfg_mod", "yaml.safe_dump"):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
