"""整手分配函数单测: 贪心逐只分配 (2026-09-05 第二轮复查 N5 重写)"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from app.portfolio.sizing import allocate_lots


def _mk(closes, scores=None, industries=None):
    scores = scores if scores is not None else list(range(len(closes), 0, -1))
    df = pd.DataFrame({
        "score": scores,
        "close": closes,
        "industry": industries if industries is not None
        else [f"i{i % 3}" for i in range(len(closes))],
    }, index=[f"c{i}" for i in range(len(closes))])
    return df


class TestAllocateLots(unittest.TestCase):
    def test_lot_multiples_and_within_budget(self):
        top = _mk([3.0, 5.0, 8.0, 10.0, 15.0, 20.0, 30.0, 40.0, 50.0, 60.0, 80.0, 100.0])
        r = allocate_lots(top, equity_cash=8000, max_stocks=12)
        self.assertGreater(r["n_eff"], 0)
        self.assertLessEqual(r["cost"], 8000)
        for code, sh in r["shares"].items():
            self.assertEqual(sh % 100, 0, f"{code} 股数非整手: {sh}")
            self.assertGreater(sh, 0)
        self.assertAlmostEqual(sum(r["weights"].values()), 1.0, places=2)

    def test_greedy_skips_unaffordable_and_continues(self):
        # 贪心: 买不起一手的跳过, 预算自然留给后面的股票(不再按中位价砍只数)
        top = _mk([80.0, 5.0, 5.0], scores=[3, 2, 1])
        r = allocate_lots(top, equity_cash=6000, max_stocks=3)
        self.assertNotIn("c0", r["shares"])  # 80 元买不起一手 → 跳过
        self.assertIn("c1", r["shares"])
        self.assertIn("c2", r["shares"])
        self.assertEqual(r["n_eff"], 2)

    def test_slot_cap_per_stock(self):
        # 每槽预算 = 资金/max_stocks: 单只投入不超过一槽
        top = _mk([2.0] * 6)
        r = allocate_lots(top, equity_cash=6000, max_stocks=6)
        for code, sh in r["shares"].items():
            self.assertLessEqual(sh * 2.0, 6000 / 6 + 1e-6)

    def test_unaffordable_all_drops(self):
        r = allocate_lots(_mk([900.0]), equity_cash=500, max_stocks=12)
        self.assertEqual(r["n_eff"], 0)
        self.assertEqual(r["shares"], {})
        self.assertEqual(r["cash_left"], 500)

    def test_industry_cap_skips_full_industry(self):
        # 同行业 4 只, 每只 1 槽 1000 元 → 1 手; eff_cap=max(0.25, 2/4)=0.5
        # → 前 2 只合计 0.5 后, 第 3/4 只被行业上限跳过
        top = _mk([10.0] * 4, scores=[4, 3, 2, 1], industries=["a", "a", "a", "a"])
        r = allocate_lots(top, equity_cash=4000, max_stocks=4, ind_cap=0.25)
        self.assertEqual(r["n_eff"], 2)
        eff_cap = max(0.25, 2.0 / 4)
        for w in r["weights"].values():
            self.assertLessEqual(w, eff_cap + 0.02)

    def test_cost_rate_reduces_lots(self):
        top = _mk([10.0])
        r0 = allocate_lots(top, equity_cash=1000, max_stocks=1, cost_rate=0.0)
        r1 = allocate_lots(top, equity_cash=1000, max_stocks=1, cost_rate=0.003)
        self.assertEqual(r0["shares"]["c0"], 100)
        # 含成本: 1000 // (10×100×1.003) = 0 → 买不起(成交端会因费用超预算)
        self.assertEqual(r1["n_eff"], 0)

    def test_range_index_input_yields_ts_code_keys(self):
        # 第三轮复查 M3: 输入带 ts_code 列但 RangeIndex(如 builder 的 read_sql 结果)时,
        # shares 的 key 必须是 ts_code 字符串而非整数行号
        df = pd.DataFrame({
            "score": [3, 2, 1],
            "close": [2.0, 2.0, 2.0],
            "industry": ["a", "b", "a"],
            "ts_code": ["600000.SH", "000001.SZ", "002449.SZ"],
        })
        r = allocate_lots(df, equity_cash=6000, max_stocks=3)
        self.assertTrue(r["shares"])
        for k in r["shares"]:
            self.assertIsInstance(k, str)
            self.assertTrue(k.endswith((".SH", ".SZ")))

    def test_zero_budget(self):
        r = allocate_lots(_mk([10.0]), equity_cash=0, max_stocks=12)
        self.assertEqual(r["n_eff"], 0)
        self.assertEqual(r["cost"], 0.0)

    def test_down_fill_after_unaffordable(self):
        # 第五轮复查 P5-1(2026-09-05): 候选池大于目标只数时, 买不起的头部票跳过,
        # 向下补位到买得起的票(原 head(max_stocks) 截断会整月空仓)
        top = _mk([30.0, 25.0, 20.0, 15.0, 10.0, 5.0], scores=[6, 5, 4, 3, 2, 1])
        r = allocate_lots(top, equity_cash=4000, max_stocks=4)
        self.assertEqual(r["n_eff"], 2)
        self.assertIn("c4", r["shares"])  # 10 元 × 100 = 1000 = 一槽
        self.assertIn("c5", r["shares"])  # 5 元 × 100 = 500
        self.assertNotIn("c0", r["shares"])

    def test_down_fill_after_industry_block(self):
        # 行业上限挡掉后向下补位到其他行业(8/31 场景: top4 全同行业只买 2 只,
        # 补位后第 3/4 只应来自其他行业)
        top = _mk([10.0] * 6, scores=[6, 5, 4, 3, 2, 1],
                  industries=["a", "a", "a", "b", "b", "b"])
        r = allocate_lots(top, equity_cash=4000, max_stocks=4, ind_cap=0.25)
        # a 行业 2 只(50% 上限) + b 行业补位 2 只 = 4 只
        self.assertEqual(r["n_eff"], 4)
        self.assertIn("c3", r["shares"])
        self.assertIn("c4", r["shares"])
        eff_cap = max(0.25, 2.0 / 4)
        ind_w = {}
        for code, sh in r["shares"].items():
            i = int(code[1:])
            ind_w[top.loc[code, "industry"]] = ind_w.get(top.loc[code, "industry"], 0.0) + sh * 10.0 / 4000
        for w in ind_w.values():
            self.assertLessEqual(w, eff_cap + 0.02)

    def test_pool_capped_at_max_stocks(self):
        # 池子大于目标只数且全部买得起: 仍封顶 max_stocks 只
        top = _mk([10.0] * 8, scores=list(range(8, 0, -1)))
        r = allocate_lots(top, equity_cash=4000, max_stocks=3)
        self.assertEqual(r["n_eff"], 3)
        self.assertEqual(list(r["shares"].keys()), ["c0", "c1", "c2"])

    def test_floor_stocks_controls_eff_cap(self):
        # 2026-09-06 第十三轮 R13-2: 行业上限下界参数化护栏 —— floor=2(历史现状)
        # 同行业可容 2 只; floor=1 时第 2 只同行业即顶破 25% 上限被跳
        top = _mk([10.0] * 4, scores=[4, 3, 2, 1], industries=["a", "a", "a", "a"])
        r2 = allocate_lots(top, equity_cash=4000, max_stocks=4, ind_cap=0.25, floor_stocks=2.0)
        self.assertEqual(r2["n_eff"], 2)  # eff_cap=0.5: 容 2 只
        r1 = allocate_lots(top, equity_cash=4000, max_stocks=4, ind_cap=0.25, floor_stocks=1.0)
        self.assertEqual(r1["n_eff"], 1)  # eff_cap=0.25: 第 2 只同行业被跳
        r0 = allocate_lots(top, equity_cash=4000, max_stocks=4, ind_cap=0.25)
        self.assertEqual(r0["n_eff"], 2)  # 缺省回退 floor=2

    def test_stress_industry_cap_never_violated(self):
        # 随机压力: 任意行业组合下, 已投行业权重不得超过 eff_cap
        import random
        random.seed(42)
        for _ in range(300):
            n = random.randint(2, 12)
            max_s = random.randint(2, n)
            eq = random.randint(3000, 50000)
            closes = [random.choice([2, 5, 8, 12, 20, 40]) for _ in range(n)]
            n_ind = random.randint(1, min(n, 5))
            inds = [f"i{random.randint(0, n_ind - 1)}" for _ in range(n)]
            top = _mk(closes, industries=inds)
            r = allocate_lots(top, equity_cash=eq, max_stocks=max_s, ind_cap=0.25)
            eff_cap = max(0.25, 2.0 / max_s)
            ind_w = {}
            for code, sh in r["shares"].items():
                i = int(code[1:])
                ind = inds[i]
                ind_w[ind] = ind_w.get(ind, 0.0) + sh * closes[i] / eq
            for w in ind_w.values():
                self.assertLessEqual(w, eff_cap + 0.02, f"行业权重 {w} 超上限 {eff_cap}")


if __name__ == "__main__":
    unittest.main()
