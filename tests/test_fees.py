"""逐笔交易费用模型单测(2026-09-10)

背景: 用户核实券商为「佣金单笔最低 5 元」→ 原扁平 single_side_cost(0.3%)不再成立:
2 万/4~6 只每笔约 3000 元 = 单边 0.167%, 而 1500 元一笔 = 0.334% —— 最低佣金对
**小仓位的非线性惩罚**是本策略(长期小仓位组合)的关键项, 扁平费率会系统性美化结果。

覆盖: 佣金下限/比例段/临界点、印花税仅卖出且按日期(2023-08-28 减半)、过户费、
实际费率非线性、config 覆盖与 fail-safe、回测 _month_return 双边异费率与向后兼容、
模拟盘成交按真实费率计费(含无 config fees 段的缺省路径)。

运行: python -m unittest tests.test_fees -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backtest import engine as bt_engine
from app.paper import engine
from app.portfolio import fees
# 2026-09-12 审查(测试质量项): 原先 `from tests.test_paper_exec import _ExecBase` ——
# 跨测试文件 import **私有**基类, 对方一改名/挪目录, 这里就在 collection 期崩。
# 基座已挪进 tests/_paper_base.py, 两边都从那里取。
from tests._paper_base import _ExecBase

P = {"commission_rate": 0.00025, "min_commission": 5.0,
     "stamp_tax": 0.0005, "stamp_tax_before": 0.001, "transfer_fee": 0.00001}


class TestFeePrimitives(unittest.TestCase):
    def test_min_commission_floor(self):
        # 1500 元买入: 佣金取 5 元下限(万2.5 只算 0.375), + 过户费 0.015
        self.assertAlmostEqual(fees.trade_cost(1500, "buy", "20260910", P), 5.015, places=6)

    def test_commission_proportional_above_floor(self):
        # 10 万元买入: 佣金 25 元 > 下限, + 过户费 1 元
        self.assertAlmostEqual(fees.trade_cost(100000, "buy", "20260910", P), 26.0, places=6)

    def test_boundary_at_floor(self):
        # 成交额 5/0.00025 = 20000 元是佣金下限临界点
        self.assertAlmostEqual(fees.trade_cost(20000, "buy", "20260910", P),
                               5.0 + 0.2, places=6)
        self.assertAlmostEqual(fees.trade_cost(20001, "buy", "20260910", P),
                               20001 * 0.00025 + 0.20001, places=6)

    def test_stamp_tax_sell_only(self):
        buy = fees.trade_cost(30000, "buy", "20260910", P)
        sell = fees.trade_cost(30000, "sell", "20260910", P)
        self.assertAlmostEqual(sell - buy, 30000 * 0.0005, places=6)

    def test_stamp_tax_halved_on_20230828(self):
        self.assertAlmostEqual(fees.stamp_tax_rate("20230825", P), 0.001, places=9)
        self.assertAlmostEqual(fees.stamp_tax_rate("20230828", P), 0.0005, places=9)
        self.assertAlmostEqual(fees.stamp_tax_rate("2023-08-25", P), 0.001, places=9)

    def test_effective_rate_nonlinear(self):
        small = fees.effective_rate(1500, "buy", "20260910", P)
        large = fees.effective_rate(6000, "buy", "20260910", P)
        self.assertGreater(small, large)
        self.assertAlmostEqual(small, 5.015 / 1500, places=9)
        self.assertAlmostEqual(large, 5.06 / 6000, places=9)

    def test_zero_amount_no_cost(self):
        self.assertEqual(fees.trade_cost(0, "buy", "20260910", P), 0.0)
        self.assertEqual(fees.effective_rate(0, "sell", "20260910", P), 0.0)

    def test_params_override_partial(self):
        p = fees.fee_params({"fees": {"min_commission": 0, "commission_rate": 0.0001}})
        self.assertEqual(p["min_commission"], 0.0)
        self.assertAlmostEqual(p["commission_rate"], 0.0001)
        self.assertAlmostEqual(p["stamp_tax"], 0.0005)  # 缺键回退默认

    def test_params_failsafe_on_bad_values(self):
        # 非数值/负数不采纳(fail-safe: 设置页重置/换机不得静默改变成本口径)
        p = fees.fee_params({"fees": {"min_commission": "x", "commission_rate": -1}})
        self.assertEqual(p["min_commission"], 5.0)
        self.assertAlmostEqual(p["commission_rate"], 0.00025)
        p2 = fees.fee_params({"fees": None})
        self.assertEqual(p2["min_commission"], 5.0)

    def test_defaults_without_config(self):
        self.assertAlmostEqual(fees.trade_cost(3000, "buy", "20260910"), 5.03, places=6)


class TestEngineAsymmetricCost(unittest.TestCase):
    def test_side_rates_applied(self):
        # 卖出费率更高(含印花税)时收益应更低
        r_sym = bt_engine._month_return(10.0, 11.0, False, 0.001, 0.001)
        r_asym = bt_engine._month_return(10.0, 11.0, False, 0.001, 0.001, 0.0015)
        self.assertLess(r_asym, r_sym)

    def test_backward_compatible_default(self):
        # cost_s 省略 == 与 cost 相同(旧扁平口径调用方/既有单测不受影响)
        self.assertAlmostEqual(
            bt_engine._month_return(10.0, 11.0, False, 0.001, 0.003),
            bt_engine._month_return(10.0, 11.0, False, 0.001, 0.003, 0.003), places=12)

    def test_overlap_ignores_cost(self):
        self.assertAlmostEqual(
            bt_engine._month_return(10.0, 11.0, True, 0.001, 0.9, 0.9), 0.1, places=12)


class _RealFeesBase(_ExecBase):
    """_ExecBase 的桩 config 钉的是旧扁平 0.3%; 这里换回生产口径的逐笔真实费率"""

    def setUp(self):
        super().setUp()
        self.patch_cfg.stop()
        self.patch_cfg = mock.patch.object(
            engine.cfg_mod, "load_config",
            return_value={"single_side_cost": 0.003, "paper": {"slip": 0.001},
                          "capital": 10000, "fees": dict(P)})
        self.patch_cfg.start()


class TestPaperNoOverdraft(_RealFeesBase):
    """小额单最低佣金可能超过 0.3% 预算预留 → 现金不得转负(2026-09-10 费用模型补齐)"""

    def _cash(self):
        return self._one("SELECT cash FROM paper_account WHERE id=1")[0]

    def test_small_cash_defers_instead_of_overdraft(self):
        # 现金 1005: 0.3% 预留下"够买 100 股"(100×10.01×1.003=1004.00), 但真实费用
        # 5.01 → 需 1006.01, 成交则现金 -1.01。正确行为: 买不起 → 顺延, 不改账
        self._exec("UPDATE paper_account SET cash=1005.0 WHERE id=1")
        self._open_days(["20260910", "20260911"])
        self._order("20260910", "600000.SH", "buy", 100)
        self._daily("600000.SH", "20260911", 10.0, 9.9)
        self.assertEqual(engine.execute_pending("20260911"), [])
        self.assertAlmostEqual(self._cash(), 1005.0, places=2)
        self.assertEqual(self._status("600000.SH", "buy")[0], "pending")
        self.assertEqual(self._one("SELECT COUNT(*) FROM paper_position")[0], 0)

    def test_exact_cash_fills_to_zero(self):
        # 现金恰好 = 成交额 + 真实费用 → 足额成交, 现金归零不透支
        self._exec("UPDATE paper_account SET cash=1006.01 WHERE id=1")
        self._open_days(["20260910", "20260911"])
        self._order("20260910", "600000.SH", "buy", 100)
        self._daily("600000.SH", "20260911", 10.0, 9.9)
        trades = engine.execute_pending("20260911")
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(self._cash(), 0.0, places=2)


class TestPaperChargesRealFees(_RealFeesBase):
    """模拟盘成交通道: 成交额 1000 元买 → 佣金下限 5 元(旧扁平 0.3% 只收 3.00)"""

    def test_small_buy_charges_min_commission(self):
        self._open_days(["20260910", "20260911"])
        self._order("20260910", "600000.SH", "buy", 100)
        self._daily("600000.SH", "20260911", 10.0, 9.9)
        trades = engine.execute_pending("20260911")
        self.assertEqual(len(trades), 1)
        row = self._one("SELECT price, cost, amount FROM paper_trade WHERE ts_code='600000.SH'")
        # 成交价 = 开盘 10.0 × (1+0.001 滑点) = 10.01 → 成交额 1001 元
        self.assertAlmostEqual(row[0], 10.01, places=4)
        self.assertAlmostEqual(row[2], 1001.0, places=2)
        self.assertAlmostEqual(row[1], 5.01, places=2)

    def test_sell_charges_stamp_tax(self):
        self._open_days(["20260910", "20260911"])
        self._exec("INSERT INTO paper_position (ts_code, name, shares, avg_cost, entry_adj) "
                   "VALUES ('600000.SH','TEST',100,10.0,NULL)")
        self._order("20260910", "600000.SH", "sell", 100)
        self._daily("600000.SH", "20260911", 10.0, 9.9)
        trades = engine.execute_pending("20260911")
        self.assertEqual(len(trades), 1)
        row = self._one("SELECT price, cost, amount FROM paper_trade WHERE ts_code='600000.SH'")
        # 卖价 = 10.0 × (1-0.001) = 9.99 → 成交额 999 元: 5(佣金下限) + 0.4995(印花税) + 0.00999(过户)
        self.assertAlmostEqual(row[0], 9.99, places=4)
        self.assertAlmostEqual(row[1], 5.51, places=2)
        # 卖出的 amount 记的是净额(paper/engine.py: 扣费后), 与买入(毛额)口径不同
        self.assertAlmostEqual(row[2], 999.0 - 5.51, places=2)


class TestPaperFeeConfigOverride(_ExecBase):
    def test_config_min_commission_zero(self):
        # config 显式给 fees 段时按 config 计费(免 5 客户: 万1 无下限)
        self.patch_cfg.stop()
        self.patch_cfg = mock.patch.object(
            engine.cfg_mod, "load_config",
            return_value={"single_side_cost": 0.003, "paper": {"slip": 0.001},
                          "capital": 10000,
                          "fees": {"commission_rate": 0.0001, "min_commission": 0,
                                   "stamp_tax": 0.0, "stamp_tax_before": 0.0,
                                   "transfer_fee": 0.0}})
        self.patch_cfg.start()
        self._open_days(["20260910", "20260911"])
        self._order("20260910", "600000.SH", "buy", 100)
        self._daily("600000.SH", "20260911", 10.0, 9.9)
        engine.execute_pending("20260911")
        row = self._one("SELECT cost FROM paper_trade WHERE ts_code='600000.SH'")
        # 1001 × 0.0001 = 0.1001 佣金(无下限); 印花税/过户费率置 0 → 0.10
        self.assertAlmostEqual(row[0], 0.1, places=2)


if __name__ == "__main__":
    unittest.main()
