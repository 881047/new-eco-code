"""2026-09-05 全项目审查修复单测: 行业列对齐 / 整手边界 / inf 校验 / 龙虎榜主键 / 空表守卫

运行: python -m unittest tests.test_review_fixes_0905 -v
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from app.backtest import engine
from app.portfolio import sizing
from app.factors import daily_engine
from app.factors import factor_engine
from app.datasource import lhb_client
from app.models import trainer
from app.web import api


class TestIndustryFor(unittest.TestCase):
    def test_row_index_candidates_map_correctly(self):
        # 审查事故场景: cand 带 read_sql 行号索引(非 RangeIndex 也非 ts_code),
        # 原 map(Series) 值与索引双错位 → lot_df 行业全 NaN; 修复后值与索引都对
        cand = pd.DataFrame({
            "ts_code": ["600000.SH", "000001.SZ", "002449.SZ"],
            "score": [95.0, 90.0, 85.0],
        }, index=[310394, 311163, 311433])
        ind = pd.DataFrame({
            "ts_code": ["600000.SH", "000001.SZ", "002449.SZ"],
            "industry": ["银行", "银行", "半导体"],
        })
        # 完整复刻 engine 的 lot_df 构造(此前"只改值不改索引"的修复在构造器对齐后依旧全 NaN)
        lot_df = pd.DataFrame({
            "score": cand["score"].values,
            "close": [10.0] * len(cand),
            "industry": engine._industry_for(cand, ind),
        }, index=cand["ts_code"])
        self.assertEqual(lot_df["industry"].tolist(), ["银行", "银行", "半导体"])
        self.assertEqual(lot_df["industry"].isna().sum(), 0)

    def test_missing_stock_is_nan(self):
        cand = pd.DataFrame({"ts_code": ["999999.SZ"]}, index=[7])
        ind = pd.DataFrame({"ts_code": ["600000.SH"], "industry": ["银行"]})
        got = engine._industry_for(cand, ind)
        self.assertTrue(pd.isna(got.iloc[0]))


class TestLotBoundary(unittest.TestCase):
    def test_exact_one_lot_boundary_kept(self):
        # 审查实证: e=100000, closes=[30,120,300,150,60,60], ind_cap=0.6 时
        # close=150 的股票精确权重 0.15 → 预算恰 15000=1 手, 浮点毛刺曾使它出局
        top = pd.DataFrame({
            "close": [30.0, 120.0, 300.0, 150.0, 60.0, 60.0],
            "industry": ["a", "b", "c", "d", "e", "f"],
            "score": [6, 5, 4, 3, 2, 1],
        })
        alloc = sizing.allocate_lots(top, 100000.0, 6, ind_cap=0.6)  # 2026-09-06 N10-21: slot_floor 死参数已删
        self.assertIn(3, alloc["shares"])          # close=150 的股票保留
        self.assertEqual(alloc["shares"][3], 100)  # 恰好 1 手
        # 总投入不超过资金(含 4 只股票的预算)
        self.assertLessEqual(alloc["cost"], 100000.0)


class TestSettingsFinite(unittest.TestCase):
    CFG = {"capital": 10000, "max_stocks": 12, "industry_max_weight": 0.25,
           "single_side_cost": 0.003, "risk_profile": "aggressive", "email": {}}

    def test_inf_capital_rejected(self):
        with mock.patch.object(api.cfg_mod, "load_config", return_value=dict(self.CFG)), \
                mock.patch.object(api.cfg_mod, "save_config") as p_save:
            from fastapi import HTTPException
            with self.assertRaises(HTTPException):
                api.post_settings({"capital": 1e999})  # json.loads("1e999") = inf
        p_save.assert_not_called()

    def test_nan_cost_rejected(self):
        with mock.patch.object(api.cfg_mod, "load_config", return_value=dict(self.CFG)), \
                mock.patch.object(api.cfg_mod, "save_config") as p_save:
            from fastapi import HTTPException
            with self.assertRaises(HTTPException):
                api.post_settings({"single_side_cost": float("nan")})
        p_save.assert_not_called()

    # 哨兵值: **真的会被注入**的一个控制码, 不是"看起来像打码"的字符串
    SENTINEL_CODE = "SENTINEL_AUTH_CODE_9f3a_do_not_write_me"

    def _cfg(self, auth_code=""):
        c = dict(self.CFG)
        c["email"] = {"enabled": False, "smtp_server": "smtp.example.com",
                      "smtp_port": 465, "sender": "a@b.c", "recipients": [],
                      "auth_code": auth_code}
        return c

    def test_env_auth_code_not_written_back_to_yaml(self):
        """第二轮复查 N4: 保存设置不得把授权码回写进受 git 跟踪的 config.yaml。

        2026-09-12 审查(测试质量项): **原断言恒真** —— 它拿保存值与字面量 `"mq**"` 比,
        而 `"mq**"` 是界面上给用户看的打码样式, 这条测试从没注入过它, 什么实现都能过。
        现在改成真的注入一个哨兵值(load_config 返回"文件里残留着明文码"的配置), 于是
        这条测试区分得开三种实现:
          (a) 正确 → yaml 里是空串, 哨兵被搬进 .env(授权码仍然生效, 只是离开了仓库);
          (b) 缺陷「直接回写」→ yaml 里出现哨兵 ⇒ 红;
          (c) 缺陷「直接丢弃」→ set_env_secret 没被调用 ⇒ 红(值悄悄失效同样是事故)。
        顺带钉住机制本身: 加载必须走 `inject_env=False`。
        """
        with mock.patch.object(api.cfg_mod, "load_config",
                               return_value=self._cfg(self.SENTINEL_CODE)) as p_load, \
                mock.patch.object(api.cfg_mod, "set_env_secret") as p_secret, \
                mock.patch.object(api.cfg_mod, "save_config") as p_save:
            api.post_settings({"capital": 10000})
        # 机制: 不回写靠的就是 inject_env=False(.env 的注入值压根不进这次内存里的 cfg)
        p_load.assert_called_once_with(inject_env=False)
        saved_cfg = p_save.call_args[0][0]
        self.assertIn("email", saved_cfg)
        self.assertEqual(saved_cfg["email"].get("auth_code") or "", "",
                         "授权码被回写进 config.yaml 了(该文件受 git 跟踪)")
        self.assertNotIn(self.SENTINEL_CODE, json.dumps(saved_cfg, ensure_ascii=False),
                         "哨兵出现在保存内容里 —— 授权码不得进 config.yaml")
        # 但值不能丢: 旧明文码应被搬进 .env
        p_secret.assert_called_once_with("EMAIL_AUTH_CODE", self.SENTINEL_CODE)

    def test_settings_save_does_not_touch_env_when_no_code_anywhere(self):
        """请求没带新码、文件里也没有旧码 ⇒ 不许去写 .env(别把空值覆盖进去)"""
        with mock.patch.object(api.cfg_mod, "load_config",
                               return_value=self._cfg("")) as p_load, \
                mock.patch.object(api.cfg_mod, "set_env_secret") as p_secret, \
                mock.patch.object(api.cfg_mod, "save_config") as p_save:
            api.post_settings({"capital": 10000})
        p_load.assert_called_once_with(inject_env=False)
        self.assertEqual(p_save.call_args[0][0]["email"].get("auth_code") or "", "")
        p_secret.assert_not_called()


class TestLhbPkGuard(unittest.TestCase):
    def test_stock_items_missing_pk_dropped(self):
        items = [
            {"thscode": "000001.SZ", "name": "A", "range_days": 1, "net_value": 1.0},
            {"thscode": None, "name": "B", "range_days": 1, "net_value": 2.0},  # 缺主键字段
            {"thscode": "000002.SZ", "name": "C", "range_days": None, "net_value": 3.0},
        ]
        df = lhb_client._parse_ths_stock_items(items, "20260905")
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["thscode"], "000001.SZ")

    def test_hm_items_missing_pk_dropped(self):
        hm = [{"name": "游资A", "buying": 1, "rows": [
            {"thscode": "000001.SZ", "name": "A", "net_value": 1.0},
            {"thscode": None, "name": "B", "net_value": 2.0},
        ]}, {"name": None, "buying": 2, "rows": [
            {"thscode": "000002.SZ", "name": "C", "net_value": 3.0},
        ]}]
        df = lhb_client._parse_ths_hm_items(hm, "20260905")
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["thscode"], "000001.SZ")


class TestDailyEngineEmptyGuard(unittest.TestCase):
    def test_empty_daily_table_returns_zero(self):
        # 审查场景: daily 空表时原 min(end_date, None) TypeError 使独立脚本 fatal
        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = (None,)
        with mock.patch.object(daily_engine.db, "get_conn", return_value=conn):
            self.assertEqual(daily_engine.compute_daily("20260905"), 0)


class TestMonthReturnCosts(unittest.TestCase):
    """第二轮复查 N2/N3: 重叠持仓不收费; 结转成本基准含佣金"""

    def test_overlap_no_transaction_costs(self):
        # 连续两月持有(重叠): 不换手=无交易 → r = sp/bp - 1, 不乘滑点/佣金
        r = engine._month_return(bp_adj=10.0, sp_adj=11.0, is_overlap=True, slip=0.001, cost=0.003)
        self.assertAlmostEqual(r, 0.1, places=9)

    def test_new_position_charges_both_sides(self):
        r = engine._month_return(bp_adj=10.0, sp_adj=11.0, is_overlap=False, slip=0.001, cost=0.003)
        expect = (11.0 * 0.999 * 0.997) / (10.0 * 1.001 * 1.003) - 1
        self.assertAlmostEqual(r, expect, places=9)

    def test_stuck_basis_includes_commission(self):
        b = engine._stuck_basis(10.0, slip=0.001, cost=0.003)
        self.assertAlmostEqual(b, 10.0 * 1.001 * 1.003, places=9)


class TestNameAtHist(unittest.TestCase):
    """第三轮复查 D1: 时点名称 PIT 过滤(纯函数)"""

    def test_st_period_uses_st_name(self):
        # 2019-01 被 ST、2021-05 摘帽: 2020 年评分日必须按 ST 名过滤
        rows = [("20190101", "20210520", "*ST测试"), ("20210521", "99999999", "测试股份")]
        self.assertEqual(trainer.name_at_hist(rows, "20200630"), "*ST测试")
        self.assertEqual(trainer.name_at_hist(rows, "20220101"), "测试股份")
        self.assertEqual(trainer.name_at_hist(rows, "20181231", "测试股份"), "测试股份")  # 改名前回退

    def test_no_records_falls_back(self):
        self.assertEqual(trainer.name_at_hist([], "20200630", "当前名"), "当前名")


class TestFactorNoLookahead(unittest.TestCase):
    """2026-09-05 诊断报告修复(测试盲区): 量价因子无未来函数回归 ——
    末日极端行情不得影响之前日期的因子值(全部 shift/rolling 只向后看)"""

    def _mk(self, close):
        n = len(close)
        idx = pd.date_range("2025-01-01", periods=n)
        return {
            "close": close.copy(),
            "adj": pd.DataFrame(1.0, index=idx, columns=["A"]),
            "high": close * 1.01,
            "low": close * 0.99,
            "bench": pd.Series(np.linspace(3000, 4000, n), index=idx),
            "turnover": pd.DataFrame(2.0, index=idx, columns=["A"]),
            "amount": pd.DataFrame(1e7, index=idx, columns=["A"]),
            "total_mv": pd.DataFrame(np.linspace(1e9, 2e9, n), index=idx, columns=["A"]),
            "pe_ttm": pd.DataFrame(np.linspace(20, 30, n), index=idx, columns=["A"]),
            "pb": pd.DataFrame(np.linspace(2, 3, n), index=idx, columns=["A"]),
            "dv_ttm": pd.DataFrame(np.linspace(0.01, 0.02, n), index=idx, columns=["A"]),
        }

    def test_last_day_spike_does_not_pollute_prior_factors(self):
        n = 300
        idx = pd.date_range("2025-01-01", periods=n)
        base = pd.DataFrame(np.linspace(10, 20, n), index=idx, columns=["A"])
        spiked = base.copy()
        spiked.iloc[-1, 0] = 1000.0  # 末日极端涨停
        f0 = factor_engine.compute_price_factors(self._mk(base))
        f1 = factor_engine.compute_price_factors(self._mk(spiked))
        for name in f0:
            ok = np.allclose(f0[name].iloc[-2].to_numpy(dtype=float),
                             f1[name].iloc[-2].to_numpy(dtype=float), equal_nan=True)
            self.assertTrue(ok, f"因子 {name} 倒数第二日被末日数据污染(未来函数)")


class TestStuckCarryForward(unittest.TestCase):
    """2026-09-05 诊断报告修复: 卖不出的持仓跨月盯市结转, 复牌补跌由组合承担"""

    def setUp(self):
        # 2026-09-28 第 8 次重钉(P1.5): 本类的持仓矩阵只给了 close/vol/pre_close
        # (未给 open), 且三个断言锁的是**收盘价**撮合下的结转/卖出价(如复牌日
        # close=5.0 ⇒ pnl=1000×(5−10)=−5000)。`FILL_AT_OPEN` 默认翻 True 后
        # 卖出价改用开盘价 ⇒ 变 −9000。这是**新口径的正确行为**, 不是 bug;
        # 但本类守卫的是旧口径下的"结转不放大/成本只计一次"这两条**会计性质**,
        # 与用哪个价成交无关 ⇒ 显式钉回 False, 保持守卫对象不变。
        from app.backtest import engine as _bt
        self._bt = _bt
        self._old_fill = _bt.FILL_AT_OPEN
        _bt.FILL_AT_OPEN = False

    def tearDown(self):
        self._bt.FILL_AT_OPEN = self._old_fill

    def _mk(self, close, vol, adj=None):
        idx = pd.DatetimeIndex([pd.Timestamp("2026-09-01") + pd.Timedelta(days=i)
                                for i in range(len(close))])
        m = {
            "close": pd.DataFrame(close, index=idx, columns=["A"]),
            "vol": pd.DataFrame(vol, index=idx, columns=["A"]),
            "open": pd.DataFrame([1.0] * len(close), index=idx, columns=["A"]),
            "pre_close": pd.DataFrame([1.0] * len(close), index=idx, columns=["A"]),
            "adj": pd.DataFrame(adj if adj is not None else np.ones((len(close), 1)),
                                index=idx, columns=["A"]),
        }
        return m, idx

    def test_suspended_then_resumed_crash_hits_portfolio(self):
        # 停牌(vol=0 无卖价)期间结转; 复牌可卖时变现 —— 收益按复牌价/上次盯市价计
        m, idx = self._mk(close=[10.0, np.nan, 5.0], vol=[1e6, 0, 1e6])
        stuck = {"A": {"shares": 1000, "bp_raw": 10000.0, "last_adj": 10.0, "last_raw": 10.0}}
        # pos1=0: 顺延 k=1 → dates[1] 停牌(vol=0) → k=2 → dates[2] 复牌 close 5.0 → 变现
        invested, pnl = engine._roll_stuck(stuck, m, idx, 0, set(), slip=0.0, cost=0.0)
        self.assertEqual(invested, 10000.0)
        self.assertAlmostEqual(pnl, -5000.0)  # 1000×(5-10), 复牌补跌由组合承担
        self.assertNotIn("A", stuck)

    def test_still_suspended_marked_to_market(self):
        # 停牌期间盯市: 月末最近收盘 5.0 vs 上次 10.0 → 计入当月盈亏, 仓位保留
        m, idx = self._mk(close=[10.0, np.nan, 5.0, np.nan], vol=[1e6, 0, 1e6, 0])
        stuck = {"A": {"shares": 1000, "bp_raw": 10000.0, "last_adj": 10.0, "last_raw": 10.0}}
        # pos1=2: 顺延 dates[3] 停牌 → 卖不出 → 盯市 close[2]=5.0
        invested, pnl = engine._roll_stuck(stuck, m, idx, 2, set(), slip=0.0, cost=0.0)
        self.assertEqual(invested, 10000.0)
        self.assertAlmostEqual(pnl, -5000.0)  # 1000×(5-10)
        self.assertIn("A", stuck)
        self.assertAlmostEqual(stuck["A"]["last_adj"], 5.0)

    def test_two_consecutive_suspension_months_base_is_prior_mark(self):
        # 第三轮复查 M1: 连续停牌多月, 每月盈亏基数=上月盯市市值(非固定原始成本) ——
        # 首月 10→5 记 -5000; 次月 5→4 应记 -1000(原实现按原始成本记 -2000, 放大 2 倍)
        m, idx = self._mk(close=[10.0, np.nan, 5.0, np.nan, 4.0, np.nan],
                          vol=[1e6, 0, 1e6, 0, 0, 0])
        stuck = {"A": {"shares": 1000, "bp_raw": 10000.0, "last_adj": 10.0, "last_raw": 10.0}}
        invested, pnl = engine._roll_stuck(stuck, m, idx, 2, set(), slip=0.0, cost=0.0)
        self.assertAlmostEqual(pnl, -5000.0)  # 首月 10→5
        self.assertAlmostEqual(stuck["A"]["last_adj"], 5.0)
        invested, pnl = engine._roll_stuck(stuck, m, idx, 4, set(), slip=0.0, cost=0.0)
        self.assertAlmostEqual(pnl, -1000.0)  # 次月 5→4, 基数是上月盯市市值
        self.assertAlmostEqual(stuck["A"]["last_adj"], 4.0)

    def test_resumed_sale_applies_cost_once(self):
        # 复牌可卖: 变现 = 股数×(卖价×(1-cost) - 上次盯市价), 并移除结转
        m, idx = self._mk(close=[10.0, np.nan, 8.0, 8.0], vol=[1e6, 0, 1e6, 1e6])
        stuck = {"A": {"shares": 1000, "bp_raw": 10000.0, "last_adj": 10.0, "last_raw": 10.0}}
        invested, pnl = engine._roll_stuck(stuck, m, idx, 1, set(), slip=0.0, cost=0.003)
        # pos1=1 卖价=close[2]=8.0: pnl = 1000×(8.0×0.997 - 10)
        self.assertAlmostEqual(pnl, 1000.0 * (8.0 * 0.997 - 10.0), places=4)
        self.assertNotIn("A", stuck)

    def test_adj_factor_not_magnified_on_sale(self):
        # 2026-09-07 口径回归锁: adj=1.686 时盈亏不得被复权因子放大 —— 旧实现
        # sh×(复权价差) 会记 -8430(×1.686 虚增), 正确=未复权市值×复权环比=-5000
        m, idx = self._mk(close=[10.0, np.nan, 5.0], vol=[1e6, 0, 1e6],
                          adj=[1.686, 1.686, 1.686])
        stuck = {"A": {"shares": 1000, "bp_raw": 10000.0, "last_adj": 16.86, "last_raw": 10.0}}
        invested, pnl = engine._roll_stuck(stuck, m, idx, 0, set(), slip=0.0, cost=0.0)
        self.assertEqual(invested, 10000.0)
        # 卖价=close[2]×1.686=8.43: 1000×10.0×(8.43/16.86-1) = -5000(不×1.686)
        self.assertAlmostEqual(pnl, -5000.0)
        self.assertNotIn("A", stuck)

    def test_adj_factor_not_magnified_on_mark(self):
        # 盯市滚动同口径: last_adj 存后复权价、last_raw 存未复权价, 两者同步滚动
        m, idx = self._mk(close=[10.0, np.nan, 5.0, np.nan], vol=[1e6, 0, 1e6, 0],
                          adj=[1.686] * 4)
        stuck = {"A": {"shares": 1000, "bp_raw": 10000.0, "last_adj": 16.86, "last_raw": 10.0}}
        invested, pnl = engine._roll_stuck(stuck, m, idx, 2, set(), slip=0.0, cost=0.0)
        self.assertAlmostEqual(pnl, -5000.0)  # 1000×10×(8.43/16.86-1)
        self.assertIn("A", stuck)
        self.assertAlmostEqual(stuck["A"]["last_adj"], 8.43)  # 后复权盯市滚动
        self.assertAlmostEqual(stuck["A"]["last_raw"], 5.0)   # 未复权本金基数同步滚动


if __name__ == "__main__":
    unittest.main()
