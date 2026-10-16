"""决策主干纯函数与边界单测 (2026-09-06 第十轮 N10-2/N10-8)

补上此前测试体系的盲区: 名称 PIT 链(NameLookup)、builder 时点名过滤、结转退市尾部、
paper 限幅时点名、regime 三态映射、neutralize 常数截面保护。全部 tempfile 隔离。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as app_db
from app.backtest import engine as bt_engine
from app.factors import factor_engine
from app.models import regime as regime_mod
from app.models.names import NameLookup
from app.paper import engine as paper_engine
from app.portfolio import builder

_SCHEMA_NAMES = """
CREATE TABLE stock_basic (ts_code TEXT, name TEXT, industry TEXT);
CREATE TABLE namechange (ts_code TEXT, start_date TEXT, end_date TEXT, name TEXT);
"""


class TestNameLookupPit(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(_SCHEMA_NAMES)
        conn.execute("INSERT INTO stock_basic VALUES ('600000.SH','测试股份','i0')")
        # 2019-01 戴帽 *ST、2021-05 摘帽
        conn.execute("INSERT INTO namechange VALUES "
                     "('600000.SH','20190101','20210520','*ST测试'),"
                     "('600000.SH','20210521','99999999','测试股份')")
        conn.commit()
        conn.close()
        self.patch = mock.patch.object(app_db, "get_conn",
                                       side_effect=lambda: sqlite3.connect(self.path))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        os.unlink(self.path)

    def test_is_st_follows_pit_name_chain(self):
        nl = NameLookup()
        self.assertTrue(nl.is_st("600000.SH", "20200630"))   # 戴帽期
        self.assertFalse(nl.is_st("600000.SH", "20220101"))  # 摘帽后
        self.assertFalse(nl.is_st("600000.SH", "20181231"))  # 改名前回退当前名(正常)

    def test_missing_tables_degrade_to_no_st(self):
        # 新装机/测试库无 namechange/stock_basic: 降级为空, 不抛错、不误判 ST
        conn = sqlite3.connect(self.path)
        conn.execute("DROP TABLE namechange")
        conn.execute("DROP TABLE stock_basic")
        conn.commit()
        conn.close()
        nl = NameLookup()
        self.assertFalse(nl.is_st("600000.SH", "20200630"))
        self.assertEqual(nl.name_at("600000.SH", "20200630"), "")


class TestBuilderPitFilter(unittest.TestCase):
    """N10-4: builder 候选剔除按方案锚定日时点名称(原用当前名称, 补建历史方案偏差)"""

    _SCHEMA = _SCHEMA_NAMES + """
    CREATE TABLE model_score (ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL);
    CREATE TABLE daily (ts_code TEXT, trade_date TEXT, close REAL, pre_close REAL,
                        vol REAL, pct_chg REAL);
    CREATE TABLE regime (trade_date TEXT, state TEXT);
    CREATE TABLE repo_rate (trade_date TEXT, overnight REAL, r7d REAL, r14d REAL);
    CREATE TABLE plan (plan_date TEXT, plan_type TEXT, ts_code TEXT, name TEXT,
                       weight REAL, shares INTEGER, industry TEXT, reason TEXT);
    """

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(self._SCHEMA)
        conn.execute("INSERT INTO stock_basic VALUES "
                     "('600000.SH','测试股份','i0'),('600001.SH','对照股份','i1')")
        conn.execute("INSERT INTO namechange VALUES "
                     "('600000.SH','20260901','99999999','*ST测试')")
        conn.execute("INSERT INTO model_score VALUES "
                     "('600000.SH','20260831',99.0,0.01),('600001.SH','20260831',95.0,0.01),"
                     "('600000.SH','20260930',99.0,0.01),('600001.SH','20260930',95.0,0.01)")
        conn.execute("INSERT INTO daily VALUES "
                     "('600000.SH','20260831',10.0,10.0,1e6,0.5),('600001.SH','20260831',10.0,10.0,1e6,0.5)")
        conn.execute("INSERT INTO regime VALUES ('20260831','defense')")
        conn.execute("INSERT INTO repo_rate VALUES ('20260831',1.5,1.6,1.7)")
        conn.commit()
        conn.close()
        self.patch_db = mock.patch.object(app_db, "get_conn",
                                          side_effect=lambda: sqlite3.connect(self.path))
        self.patch_db.start()
        self.patch_cfg = mock.patch.object(
            builder.cfg_mod, "load_config",
            return_value={"capital": 10000, "risk_profile": "aggressive",
                          "max_stocks": 6, "state_stocks": {"attack": 6, "neutral": 5, "defense": 4},
                          "stock_candidate_top": 0, "industry_max_weight": 0.25,
                          "single_side_cost": 0.003,
                          "regime_equity_weight": {"aggressive": {"attack": 1.0, "neutral": 0.9,
                                                                  "defense": 0.8}}})
        self.patch_cfg.start()

    def tearDown(self):
        self.patch_db.stop()
        self.patch_cfg.stop()
        os.unlink(self.path)

    def _run(self, plan_date):
        captured = {}

        def fake_alloc(top, equity_cash, max_stocks, **kw):
            captured["codes"] = list(top.index)
            return {"shares": {}, "weights": {}, "cost": 0.0, "n_eff": 0, "cash_left": equity_cash}
        with mock.patch.object(builder, "allocate_lots", side_effect=fake_alloc), \
             mock.patch.object(builder.funds, "score_etfs", return_value=pd.DataFrame()), \
             mock.patch.object(builder.funds, "otc_fund_picks", return_value={}), \
             mock.patch.object(builder.repo, "repo_advice",
                               return_value={"desc": "x", "rate": 1.5}):
            builder.build_plan(plan_date)
        return captured

    def test_st_renamed_after_plan_date_is_kept(self):
        # 方案锚定 0831: 0901 才戴帽 → 时点名仍正常, 高分股保留
        captured = self._run("20260831")
        self.assertIn("600000.SH", captured["codes"])

    def test_star_excluded_from_selection(self):
        # 2026-09-08 用户拍板: 训练全市场、选股排除科创板(star_enabled=False 默认)
        conn = sqlite3.connect(self.path)
        conn.execute("INSERT INTO model_score VALUES ('688111.SH','20260831',99.9,0.01)")
        conn.execute("INSERT INTO stock_basic (ts_code, name, industry) VALUES ('688111.SH','科创股','i9')")
        conn.execute("INSERT INTO daily (ts_code, trade_date, close, pre_close, vol, pct_chg) "
                     "VALUES ('688111.SH','20260831',10.0,10.0,1e6,0.5)")
        conn.commit()
        conn.close()
        captured = self._run("20260831")
        self.assertNotIn("688111.SH", captured["codes"])

    def test_st_renamed_before_plan_date_is_filtered(self):
        # 方案锚定 0930: 已戴帽 → 被 PIT 过滤, 仅剩对照股
        captured = self._run("20260930")
        self.assertNotIn("600000.SH", captured["codes"])
        self.assertIn("600001.SH", captured["codes"])


class TestStuckDelistTail(unittest.TestCase):
    """N10-2 故障注入: 退市股(摘牌后无日线)结转按最后收盘盯市, 复牌无望时尾部由组合承担"""

    def _mk(self, close, vol):
        idx = pd.DatetimeIndex([pd.Timestamp("2026-09-01") + pd.Timedelta(days=i)
                                for i in range(len(close))])
        m = {
            "close": pd.DataFrame(close, index=idx, columns=["A"]),
            "vol": pd.DataFrame(vol, index=idx, columns=["A"]),
            "open": pd.DataFrame([1.0] * len(close), index=idx, columns=["A"]),
            "pre_close": pd.DataFrame([1.0] * len(close), index=idx, columns=["A"]),
            "adj": pd.DataFrame(np.ones((len(close), 1)), index=idx, columns=["A"]),
        }
        return m, idx

    def test_delisted_carries_last_close_mtm(self):
        # 前 3 日有行情(10→9.5), 之后摘牌无日线: 结转继续按最后收盘 9.5 盯市
        close = [10.0, 9.5, 9.0, np.nan, np.nan, np.nan]
        vol = [1e6] * 3 + [0] * 3
        m, idx = self._mk(close, vol)
        stuck = {"A": {"shares": 100, "bp_raw": 1000.0, "last_adj": 9.5, "last_raw": 9.5}}
        invested, pnl = bt_engine._roll_stuck(stuck, m, idx, 2, set(), slip=0.0, cost=0.0)
        self.assertEqual(invested, 1000.0)
        self.assertAlmostEqual(pnl, 100 * (9.0 - 9.5), places=6)  # 最后一日盯市盈亏
        self.assertIn("A", stuck)  # 卖不出: 仍在结转账本
        self.assertAlmostEqual(stuck["A"]["last_adj"], 9.0, places=6)


class TestPaperExecPricePitName(unittest.TestCase):
    """N10-4: paper 限幅按成交日时点名称(name_fn), 不再只用建单冻结名"""

    _SCHEMA = """
    CREATE TABLE daily (ts_code TEXT, trade_date TEXT, open REAL, pre_close REAL, vol REAL);
    """

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(self._SCHEMA)
        # 开盘 +6%(10.0→10.6): 5% 档封板买不进, 10% 档可成交。
        # ⚠️ 日期取 **20260601**(主板 ST 改革 20260706 之前) —— 用 20260706 之后就落在
        # 改革后(ST 也是 10%), +6% 两档都可成交, 这个用例就没有分辨力了。
        # 改革后的行为由 test_main_st_reform_relaxes_paper_band 单独覆盖。
        conn.execute("INSERT INTO daily VALUES ('600000.SH','20260601',10.6,10.0,1e6)")
        conn.commit()
        conn.close()
        self.conn = sqlite3.connect(self.path)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def test_pit_name_fn_overrides_frozen_name(self):
        # 建单冻结名正常, 成交日时点名已是 ST → 按 5% 档挡住 +6% 开盘
        p = paper_engine._exec_price(self.conn, "600000.SH", "20260601", "buy",
                                     slip=0.0, name="正常名",
                                     name_fn=lambda c, d: "*ST成交日")
        self.assertIsNone(p)
        # 成交日时点名摘帽 → 10% 档, 可成交
        p2 = paper_engine._exec_price(self.conn, "600000.SH", "20260601", "buy",
                                      slip=0.0, name="正常名",
                                      name_fn=lambda c, d: "正常名")
        self.assertAlmostEqual(p2, 10.6, places=6)

    def test_no_name_fn_falls_back_to_frozen_name(self):
        p = paper_engine._exec_price(self.conn, "600000.SH", "20260601", "buy",
                                     slip=0.0, name="*ST冻结名")
        self.assertIsNone(p)  # 旧口径: 冻结名含 ST → 5% 档

    def test_main_st_reform_relaxes_paper_band(self):
        """2026-07-06 主板 ST 由 5% 放宽到 10% —— 同一只、同一开盘价, 成交日落在
        改革前 vs 改革后, 执行价必须**不同**(改革前买不进, 改革后可成交)。"""
        self.conn.execute("INSERT INTO daily VALUES ('600000.SH','20260706',10.6,10.0,1e6)")
        self.conn.commit()
        self.assertIsNone(paper_engine._exec_price(
            self.conn, "600000.SH", "20260601", "buy", slip=0.0, name="*ST冻结名"))
        p = paper_engine._exec_price(self.conn, "600000.SH", "20260706", "buy",
                                     slip=0.0, name="*ST冻结名")
        self.assertAlmostEqual(p, 10.6, places=6)


class TestRegimeStateRules(unittest.TestCase):
    """N10-8: 三态映射纯函数"""

    def test_attack_neutral_defense_thresholds(self):
        idx = pd.Index(["d1", "d2", "d3", "d4", "d5"])
        score = pd.Series([6, 4, 4, 1, 2], index=idx, dtype=float)
        dd = pd.Series([0.0, 0.0, 0.0, -0.25, 0.0], index=idx, dtype=float)
        vol = pd.Series([0.2, 0.2, 0.4, 0.2, 0.2], index=idx, dtype=float)
        st = regime_mod._apply_state_rules(score, dd, vol, score_max=6)
        self.assertEqual(st["d1"], "attack")   # score 6 >= score_max-1=5
        self.assertEqual(st["d2"], "neutral")  # score 4, 无风控触发
        self.assertEqual(st["d3"], "defense")  # 波动 > 35%
        self.assertEqual(st["d4"], "defense")  # 回撤 < -20%
        self.assertEqual(st["d5"], "defense")  # score <= 2


class TestRollingPctRank(unittest.TestCase):
    """审查项 ④: 滚动分位分母口径 —— 窗口内 NaN 不得被当成「不在低位」拉低分位"""

    def test_no_nan_matches_legacy(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        pr = regime_mod._rolling_pct_rank(s, 5)
        self.assertAlmostEqual(pr.iloc[4], (4 + 0.5) / 5)  # 4 个更小 + 1 个相等(半权)
        self.assertTrue(pr.iloc[:4].isna().all())          # 窗口未满为 NaN

    def test_leading_nan_uses_valid_count(self):
        # PE 序列上线前: 窗口 [NaN, NaN, 1, 2, 3] —— 分母应为 3 而非 5
        s = pd.Series([np.nan, np.nan, 1.0, 2.0, 3.0])
        pr = regime_mod._rolling_pct_rank(s, 5)
        self.assertAlmostEqual(pr.iloc[4], (2 + 0.5) / 3)  # 旧口径会算出 0.5(低估)

    def test_inner_nan_excluded(self):
        s = pd.Series([1.0, np.nan, 3.0, np.nan, 5.0])
        pr = regime_mod._rolling_pct_rank(s, 5)
        self.assertAlmostEqual(pr.iloc[4], (2 + 0.5) / 3)

    def test_last_nan_returns_nan(self):
        # 窗口末值自身缺失 → 无法排序, 返回 NaN(贡献 0, 不误加 ±1 分)
        s = pd.Series([1.0, 2.0, 3.0, np.nan, np.nan])
        pr = regime_mod._rolling_pct_rank(s, 5)
        self.assertTrue(np.isnan(pr.iloc[4]))

    def test_all_nan_window(self):
        s = pd.Series([np.nan] * 5)
        pr = regime_mod._rolling_pct_rank(s, 5)
        self.assertTrue(pr.isna().all())

    def test_window_equals_length(self):
        # 2026-09-07 边界修复回归: n == w 时最早 1 个点应算出
        s = pd.Series([1.0, 2.0, 3.0])
        pr = regime_mod._rolling_pct_rank(s, 3)
        self.assertFalse(np.isnan(pr.iloc[2]))
        self.assertAlmostEqual(pr.iloc[2], (2 + 0.5) / 3)


class TestNeutralize(unittest.TestCase):
    """N10-8: neutralize 常数截面除零保护与行业中性化"""

    def test_constant_column_protected(self):
        # 某一截面整行常数(所有股票同值): 行业中性化后 std=0 → 除零保护为 NaN 不崩溃
        idx = pd.date_range("2025-01-01", periods=5)
        df = pd.DataFrame({"A": [3.0, 1.0, 1.0, 1.0, 1.0],
                           "B": [3.0, 2.0, 3.0, 4.0, 5.0],
                           "C": [3.0, 2.0, 3.0, 4.0, 5.0],
                           "D": [3.0, 2.0, 3.0, 4.0, 5.0]}, index=idx)
        ind = pd.Series(["i0", "i0", "i1", "i1"], index=["A", "B", "C", "D"])
        out = factor_engine.neutralize(df, ind)
        self.assertTrue(out.iloc[0].isna().all())   # 首日整行常数 → NaN, 不崩溃
        self.assertTrue(out.iloc[1:].notna().all().all())

    def test_industry_demeaned(self):
        idx = pd.date_range("2025-01-01", periods=20)
        rng = np.random.default_rng(7)
        df = pd.DataFrame(rng.normal(size=(20, 6)), index=idx,
                          columns=[f"C{i}" for i in range(6)])
        ind = pd.Series(["x", "x", "x", "y", "y", "y"], index=df.columns)
        out = factor_engine.neutralize(df, ind)
        for col in df.columns:
            for date in idx:
                # 同行业均值应接近 0(中性化后)
                grp = out.loc[date, ind[ind == ind[col]].index]
                self.assertAlmostEqual(grp.mean(), 0.0, places=9)


class TestBuilderSizeLiquidityFilter(unittest.TestCase):
    """R11-3: 市值/流动性硬过滤 —— 微盘/低流动候选被剔出, 过滤参数同 trainer/builder"""

    _SCHEMA = _SCHEMA_NAMES + """
    CREATE TABLE model_score (ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL);
    CREATE TABLE daily (ts_code TEXT, trade_date TEXT, close REAL, pre_close REAL,
                        vol REAL, pct_chg REAL, amount REAL);
    CREATE TABLE daily_basic (ts_code TEXT, trade_date TEXT, total_mv REAL);
    CREATE TABLE regime (trade_date TEXT, state TEXT);
    CREATE TABLE repo_rate (trade_date TEXT, overnight REAL, r7d REAL, r14d REAL);
    CREATE TABLE plan (plan_date TEXT, plan_type TEXT, ts_code TEXT, name TEXT,
                       weight REAL, shares INTEGER, industry TEXT, reason TEXT);
    """

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(self._SCHEMA)
        # 大票 A: 市值 50亿(5e5 万元)、日额 3e4 千元; 微盘 B: 市值 10亿、日额 5e3 千元
        conn.execute("INSERT INTO stock_basic VALUES "
                     "('600000.SH','大票','i0'),('600001.SH','微盘','i1')")
        conn.execute("INSERT INTO daily_basic VALUES "
                     "('600000.SH','20260831',5e5),('600001.SH','20260831',1e5)")
        conn.execute("INSERT INTO model_score VALUES "
                     "('600000.SH','20260831',95.0,0.01),('600001.SH','20260831',99.0,0.01)")
        for d in ("20260804", "20260831"):
            conn.execute("INSERT INTO daily VALUES "
                         "('600000.SH',?,10.0,10.0,1e6,0.5,3e4),('600001.SH',?,10.0,10.0,1e6,0.5,5e3)",
                         (d, d))
        conn.execute("INSERT INTO regime VALUES ('20260831','defense')")
        conn.execute("INSERT INTO repo_rate VALUES ('20260831',1.5,1.6,1.7)")
        conn.commit()
        conn.close()
        self.patch_db = mock.patch.object(app_db, "get_conn",
                                          side_effect=lambda: sqlite3.connect(self.path))
        self.patch_db.start()
        self.patch_cfg = mock.patch.object(
            builder.cfg_mod, "load_config",
            return_value={"capital": 10000, "risk_profile": "aggressive",
                          "max_stocks": 6, "state_stocks": {"attack": 6, "neutral": 5, "defense": 4},
                          "stock_candidate_top": 0, "industry_max_weight": 0.25,
                          "stock_size_pctl": 0.2, "stock_min_amt20": 20000,
                          "single_side_cost": 0.003,
                          "regime_equity_weight": {"aggressive": {"attack": 1.0, "neutral": 0.9,
                                                                  "defense": 0.8}}})
        self.patch_cfg.start()

    def tearDown(self):
        self.patch_db.stop()
        self.patch_cfg.stop()
        os.unlink(self.path)

    def test_microcap_filtered_out_despite_higher_score(self):
        captured = {}

        def fake_alloc(top, equity_cash, max_stocks, **kw):
            captured["codes"] = list(top.index)
            return {"shares": {"600000.SH": 100}, "weights": {"600000.SH": 1.0},
                    "cost": 1000.0, "n_eff": 1, "cash_left": equity_cash - 1000}
        with mock.patch.object(builder, "allocate_lots", side_effect=fake_alloc), \
             mock.patch.object(builder.funds, "score_etfs", return_value=pd.DataFrame()), \
             mock.patch.object(builder.funds, "otc_fund_picks", return_value={}), \
             mock.patch.object(builder.repo, "repo_advice",
                               return_value={"desc": "x", "rate": 1.5}):
            builder.build_plan("20260831")
        # 微盘 B 分数更高(99)但市值/流动性双不达标 → 被剔; 只有大票 A 进分配
        self.assertNotIn("600001.SH", captured["codes"])
        self.assertIn("600000.SH", captured["codes"])


class TestMonthPnlStopLoss(unittest.TestCase):
    """R15-1(2026-09-06): 回测日频止损分支单测 —— 触损/不触损/释放现金计息。
    该分支的 A/B 曾把年化打到 -25.76%/回撤 -96.31%, 必须锁死口径"""

    def _mk(self, close, open_=None):
        n = len(close)
        idx = pd.DatetimeIndex([pd.Timestamp("2026-09-01") + pd.Timedelta(days=i)
                                for i in range(n)])
        # 2026-09-28(P1.5 换 pin): `open` 原被随意写死成 1.0 —— 在收盘价口径下没人读
        # 它, 翻 `FILL_AT_OPEN=True` 后月末卖出改用开盘价 ⇒ 卖出价变 1.0, pnl 从
        # -50 变 -900。合成数据不应凭空制造日内跳空: 默认 open = close(无日内波动),
        # 需要测日内差异的用例显式传 open_。
        m = {
            "close": pd.DataFrame(close, index=idx, columns=["A"]),
            "vol": pd.DataFrame([1e6] * n, index=idx, columns=["A"]),
            "open": pd.DataFrame(close if open_ is None else open_,
                                 index=idx, columns=["A"]),
            "pre_close": pd.DataFrame([1.0] * n, index=idx, columns=["A"]),
            "adj": pd.DataFrame(np.ones((n, 1)), index=idx, columns=["A"]),
        }
        return m, idx

    def test_stop_hits_sells_at_loss_and_credits_cash(self):
        # 买入日(索引1)价10, 第3日 8.2(-18%) 触损 → 当日收盘卖出; 剩余期现金计息>0
        # pos1=5(卖出执行日=6, 其后还有 7 保证 _exec_side 不越界)
        m, idx = self._mk([10.0, 10.0, 10.0, 8.2, 8.0, 8.0, 8.0, 8.0])
        alloc = {"shares": {"A": 100}}
        execs = {"A": (10.0, 10.0, 1)}  # bp_adj, bp_raw, 买入日索引
        invested, pnl, credit = bt_engine._month_pnl(
            alloc, execs, None, {}, m, idx, 5, set(), slip=0.0, cost=0.0,
            stop_loss=-0.15, cash_r=0.01, hold_days=20)
        self.assertEqual(invested, 1000.0)
        self.assertAlmostEqual(pnl, 100 * (8.2 - 10.0), places=6)  # 止损兑现 -18%
        self.assertGreater(credit, 0.0)  # 释放现金剩余期计息

    def test_no_stop_within_threshold(self):
        # 月内最大跌幅 -10%: 不触发, 走月末正常卖出路径, 无计息补偿
        m, idx = self._mk([10.0, 10.0, 9.5, 9.0, 9.2, 9.4, 9.5, 9.6])
        alloc = {"shares": {"A": 100}}
        execs = {"A": (10.0, 10.0, 1)}
        invested, pnl, credit = bt_engine._month_pnl(
            alloc, execs, None, {}, m, idx, 5, set(), slip=0.0, cost=0.0,
            stop_loss=-0.15, cash_r=0.01, hold_days=20)
        self.assertAlmostEqual(pnl, 100 * 10.0 * (9.5 / 10.0 - 1), places=6)
        self.assertEqual(credit, 0.0)

    def test_stop_none_disables_layer(self):
        # stop_loss=None: 月内 -18% 也不割(旧行为), 月末按收盘卖出
        m, idx = self._mk([10.0, 10.0, 10.0, 8.2, 8.0, 8.0, 8.0, 8.0])
        alloc = {"shares": {"A": 100}}
        execs = {"A": (10.0, 10.0, 1)}
        invested, pnl, credit = bt_engine._month_pnl(
            alloc, execs, None, {}, m, idx, 5, set(), slip=0.0, cost=0.0,
            stop_loss=None, cash_r=0.01, hold_days=20)
        self.assertAlmostEqual(pnl, 100 * 10.0 * (8.0 / 10.0 - 1), places=6)
        self.assertEqual(credit, 0.0)


if __name__ == "__main__":
    unittest.main()
