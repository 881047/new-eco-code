# -*- coding: utf-8 -*-
"""分数排名加权 sizing 缝单测（plans/rankweight-prereg-20261002.md，先红后绿）。

锁三件事:
1. rank_weighted=True 时 slot 按 (K+1−r)/Σ 归一排名权重分配(整手向下取整);
2. rank_weighted=False(缺省)与原等权路径**逐位一致**(pin 安全);
3. 超出 K=min(6, len(pool)) 的名字 slot=0 不买入。
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.portfolio.sizing import allocate_lots          # noqa: E402


def _pool(prices):
    return pd.DataFrame({
        "score": [100.0 - i for i in range(len(prices))],
        "close": prices,
        "industry": [f"ind{i}" for i in range(len(prices))],
    }, index=[f"{600000 + i}.SH" for i in range(len(prices))])


class TestRankWeighted(unittest.TestCase):
    def test_rank_weighted_allocates_by_rank(self):
        # 6 只各 10 元, 资金 21000: 权重 (7-r)/21 → 6/21,5/21,...,1/21 × 21000
        # = 6000,5000,4000,3000,2000,1000 → 各 600,500,400,300,200,100 股
        alloc = allocate_lots(_pool([10.0] * 6), 21000.0, 6, rank_weighted=True)
        self.assertEqual(alloc["shares"]["600000.SH"], 600)
        self.assertEqual(alloc["shares"]["600001.SH"], 500)
        self.assertEqual(alloc["shares"]["600004.SH"], 200)
        self.assertEqual(alloc["shares"]["600005.SH"], 100)

    def test_rank_weighted_below_6_names_normalizes(self):
        # 3 只: K=3, 权重 3/6,2/6,1/6 × 12000 = 6000,4000,2000 → 600,400,200 股
        alloc = allocate_lots(_pool([10.0] * 3), 12000.0, 6, rank_weighted=True)
        self.assertEqual(alloc["shares"]["600000.SH"], 600)
        self.assertEqual(alloc["shares"]["600001.SH"], 400)
        self.assertEqual(alloc["shares"]["600002.SH"], 200)

    def test_rank7_beyond_top6_not_bought(self):
        # 7 只: 第 7 名 slot=0 → 不买(等权缺省路径会买 max_stocks 封顶内)
        alloc = allocate_lots(_pool([10.0] * 7), 21000.0, 7, rank_weighted=True)
        self.assertNotIn("600006.SH", alloc["shares"])

    def test_default_path_unchanged(self):
        # 缺省(缺 kwarg)= 等权: slot=equity/6=3500 → affordable 350 → 主板整手 300 股
        # (= 原有生产行为, 整手向下取整; 等权的近似性由现金闲置承接)
        alloc = allocate_lots(_pool([10.0] * 6), 21000.0, 6)
        self.assertEqual(alloc["shares"]["600000.SH"], 300)
        self.assertEqual(alloc["shares"]["600005.SH"], 300)
        self.assertEqual(len(alloc["shares"]), 6)

    def test_expensive_stock_graceful(self):
        # rank1 50 元股: slot 6000 → affordable 120 → 主板整手 100 股;
        # rank6 1000 元股: slot 1000 → affordable 1 < 100 起买量 → 缺席(真实摩擦)
        alloc = allocate_lots(_pool([50.0, 10.0, 10.0, 10.0, 10.0, 1000.0]),
                              21000.0, 6, rank_weighted=True)
        self.assertEqual(alloc["shares"]["600000.SH"], 100)
        self.assertNotIn("600005.SH", alloc["shares"], "slot 1000 元买不起 1000 元股")


if __name__ == "__main__":
    unittest.main()
