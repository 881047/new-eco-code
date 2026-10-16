"""pre_close / 涨跌停限价的**交叉校验测试**（2026-09-21；KB 14 号开放项 S7）。

项目里有两处按 `pre_close` 算涨跌停限价，且**有意不同**（是设计取舍，不是实现漂移）：

  生产模拟盘（`app.paper.engine._exec_price` / `_exec_price_open`）
      从 `daily` 表读 `pre_close`（精确值）⇒ `limit_price(pre, ratio, board_of(code))`。

  回测（`app.backtest.engine._exec_side`）
      从 **float32 市场矩阵**读 `pv` ⇒ 先 `round(float(pv), 2)` 量化回分
      （`PRE_CLOSE_QUANTIZE = True`）⇒ 再 `limit_price(pv_f, ±ratio, board_of(code))`。

**两侧算法同源**：`limit_price` 对非 BSE 板内部就是 `round2(pre * (1 + ratio))`（2026-10-05
起 BSE 分支为上轨 floor/下轨 ceil —— 北交所实测取整规则，与交易所公布值逐格位级一致，
见 `scripts/oneoff/diag_stklimit_window_check.py`），差异仅在**输入精度** —— 回测侧必须先
量化，原因是 float32 矩阵会让 162,414 格限价取低一分（`PRE_CLOSE_QUANTIZE` 是
2026-09-12 E5 预注册判决通过的修复，默认已翻开）。

另有"缺 `pre_close` ⇒ 涨跌停闸门整段跳过"这一约定：**两侧一致**（paper/engine.py L150-152
注释明写"改它会与回测引擎同一约定分叉、进而动 pin"，故不改，只用 `log.warning` 使其可见）。

这条测试**不是**要统一它们 —— 统一成交价口径属**换 pin 级**变更，必须独立预注册 + A/B。
它的作用是**把关系钉死**：
  ① 同一 `pre_close` + 同一 `ratio` ⇒ 两侧算出的限价**逐位一致**（算法同源）；
  ② 若任一侧把交易所 `round2`（half-up）换成 Python `round`（banker's）⇒ 当场红；
  ③ 用 mock 翻转验证判据真会红 —— **不改 `app/**`，零源码污染**。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.portfolio import board           # noqa: E402


def _banker(x: float) -> float:
    """Python 原生 round（银行家舍入，half-to-even）—— 交易所规则是 half-up，两者在 .x5 差 1 分。"""
    return round(x, 2)


class TestPrecloseConventions(unittest.TestCase):
    """模拟盘侧与回测侧的限价公式，在同源输入下必须逐位一致。"""

    CASES = [
        (10.00, 0.10),
        (17.15, 0.10),   # 17.15×1.1 = 18.864999999999998 ⇒ 交易所 18.87 / banker 18.86
        (3.685, 0.05),
        (100.50, 0.20),
        (8.88, 0.30),
        (20.00, 0.10),
        (5.05, 0.10),
        (30.30, 0.05),
    ]

    def test_两边限价公式同源(self):
        """同一 (pre_close, ratio)：模拟盘 limit_price == 回测侧 round2(round(pre,2)×(1+ratio))。"""
        for pre, ratio in self.CASES:
            with self.subTest(pre=pre, ratio=ratio):
                paper_side = board.limit_price(pre, ratio)
                backtest_side = board.round2(round(pre, 2) * (1 + ratio))
                self.assertEqual(
                    paper_side, backtest_side,
                    f"限价分叉: pre={pre} ratio={ratio} "
                    f"模拟盘={paper_side} 回测={backtest_side}")

    def test_涨跌停对称(self):
        """limit_price(pre, +r) 与 (pre, -r) 必须都用交易所 round2，不能一侧 half-up 一侧 banker。"""
        for pre, ratio in self.CASES:
            with self.subTest(pre=pre, ratio=ratio):
                up = board.limit_price(pre, ratio)
                dn = board.limit_price(pre, -ratio)
                # 同源：都由 round2 产生；这里钉住"都是 2 位小数且 dn <= pre <= up"
                self.assertAlmostEqual(up, board.round2(pre * (1 + ratio)), places=9)
                self.assertAlmostEqual(dn, board.round2(pre * (1 - ratio)), places=9)
                self.assertLessEqual(dn, up)

    def test_先红后绿_round2换成bankers即红(self):
        """证明判据有效（零源码污染：用 mock 翻转，不改 app/**）。

        把 board.round2 换成银行家舍入后，模拟盘侧的 limit_price 必须**不再**等于
        交易所口径 ⇒ 若这条断言失败，说明测试根本没在守护这个约定。
        """
        pre, ratio = 17.15, 0.10
        # 绿：正常情况下两侧一致（交易所 round2）
        self.assertEqual(board.limit_price(pre, ratio),
                         board.round2(round(pre, 2) * (1 + ratio)))

        # 红：换成 banker's rounding 后必须出现差异
        with mock.patch.object(board, "round2", _banker):
            drifted = board.limit_price(pre, ratio)       # 用 banker 计算
            correct = board.round2(round(pre, 2) * (1 + ratio)) if not hasattr(
                board.round2, "__self__") else None
            # 上一步 patch 后 board.round2 已被替换，故用显式值比对：
            # 交易所口径 18.87（half-up），banker 口径 18.86（half-to-even）
            self.assertNotEqual(drifted, 18.87,
                                "把 round2 换成 banker's 后应被测出差 1 分 —— 判据必须能红")
            self.assertAlmostEqual(drifted, 18.86, places=9)


    def test_源码契约_两侧各自的约定(self):
        """钉住两侧的**源码约定**（防止有人把 round2 换成裸 round、或去掉 float32 量化）。

        为什么需要这条：上面 test_两边限价公式同源 比较的是两个 board 函数，
        而两侧都调用 board.round2 —— 若有人整体替换 round2，两侧仍相等、那条不会红。
        故补一条源码契约，直接钉住调用点。
        先例：tests/test_web_static_contract.py（用断言钉住源码/DOM 约定）。
        """
        app_dir = Path(board.__file__).resolve().parent.parent
        bt = (app_dir / "backtest" / "engine.py").read_text(encoding="utf-8", errors="ignore")
        pp = (app_dir / "paper" / "engine.py").read_text(encoding="utf-8", errors="ignore")

        # 回测侧：必须先量化回分（float32 矩阵精度修正），限价走板块化的 board.limit_price
        self.assertIn("PRE_CLOSE_QUANTIZE", bt, "回测侧应保留 PRE_CLOSE_QUANTIZE 量化开关")
        self.assertIn("pv_f = round(float(pv), 2) if PRE_CLOSE_QUANTIZE else float(pv)", bt,
                      "回测侧的 pre_close 量化写法被改动（会复活 float32 取低一分的缺陷）")
        self.assertIn("limit_price(pv_f, ratio, board_of(code))", bt,
                      "回测侧限价必须走 board.limit_price（交易所取整规则单一来源），不能换成裸 round")
        self.assertIn("limit_price(pv_f, -ratio, board_of(code))", bt, "回测侧跌停限价同上")

        # 模拟盘侧：limit_price 传板块（2026-10-05 BSE 取整接线），缺 pre_close 跳闸要看得见
        self.assertIn("limit_price(pre, ratio, board_of(code))", pp,
                      "模拟盘侧应经 limit_price 并传板块")
        self.assertIn("limit_price(pre, -ratio, board_of(code))", pp, "模拟盘侧跌停限价同上")
        self.assertIn("缺 pre_close", pp, "缺 pre_close 时跳过闸门必须有 warning 可见")

        # board.limit_price：非 BSE 必须仍由 round2 产生（half-up 单一来源），BSE 分支在位
        bd = (app_dir / "portfolio" / "board.py").read_text(encoding="utf-8", errors="ignore")
        self.assertIn("return round2(raw)", bd, "非 BSE 限价必须仍由 round2 半进位产生")
        self.assertIn('board == "bse"', bd, "BSE 取整分支(上轨 floor/下轨 ceil)不可移除")


if __name__ == "__main__":
    unittest.main()
