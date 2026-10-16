"""科创板口径只许有**一个真源**（2026-09-13）

独立审查 #2（本人已复核）：`board_of` 早把 **689xxx（科创板 CDR）** 归为 star（2026-09-12
D06/D07 验收），但"选股/回测/推分/池子指数"四处用的还是**字面 `688` 前缀** ⇒
`star_enabled=false` 时 689 被放行进入**选股域**，而 2026-09-08 用户拍板的是
"训练全市场、**选股排除科创板**"（起因：科创占位在 1 万本金下拖累 −87% 累计）。
库里就一只：`689009.SH`（daily 1427 行 / model_score 68 行 / daily_score 30 行 / 池子指数含它），
从未被真实买入（`paper_trade` 0 行）—— 但"训练域 ≠ 选股域"是项目写进 `pit/engine.py` 的硬不变量。

守卫：① `is_star` 覆盖 688 与 689；② 三处 Python 过滤不再出现字面 `688` 前缀（防回归）；
③ 池子指数 SQL 明确排除 689。
"""
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.portfolio.board import is_star, board_of        # noqa: E402


class TestIsStar(unittest.TestCase):
    def test_covers_star_and_cdr(self):
        self.assertTrue(is_star("688111.SH"))
        self.assertTrue(is_star("689009.SH"), "科创板 CDR(689) 必须算 star")

    def test_does_not_overreach(self):
        for code in ("600000.SH", "000001.SZ", "300750.SZ", "830799.BJ"):
            with self.subTest(code=code):
                self.assertFalse(is_star(code))

    def test_is_derived_from_board_of(self):
        """单一真源：is_star 必须等价于 board_of == 'star'（不许另写一套前缀）。"""
        for code in ("688001.SH", "689009.SH", "300750.SZ", "600000.SH", "830799.BJ"):
            self.assertEqual(is_star(code), board_of(code) == "star")


class TestNoLiteralStarPrefixFilters(unittest.TestCase):
    """三处历史过滤点不许再出现字面 `688` 前缀（那是漏掉 689 的根因）。"""

    FILES = ("app/models/daily_score.py", "app/portfolio/builder.py", "app/backtest/engine.py")

    def test_no_literal_688_prefix_filter(self):
        for rel in self.FILES:
            txt = (BASE / rel).read_text(encoding="utf-8")
            with self.subTest(file=rel):
                self.assertNotIn('startswith("688")', txt,
                                 f"{rel} 又出现了字面 688 过滤 —— 请改用 board.is_star")

    def test_pool_index_excludes_cdr(self):
        sql = (BASE / "app/models/pool_index.py").read_text(encoding="utf-8")
        self.assertIn("NOT LIKE '689%'", sql, "池子指数没有排除 689 CDR")


if __name__ == "__main__":
    unittest.main()
