"""2026-09-04 深度审查 P0 修复单测: 月度截面完结月过滤 / 回测撮合口径 / ETF 复权动量

运行: python -m unittest tests.test_p0_fixes -v
"""
import contextlib
import datetime
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from app import db
from app import timeutil
from app.factors.factor_engine import month_end_dates
from app.backtest.engine import _exec_side
from app.portfolio import builder, funds, repo


# ---------- P0-2: month_end_dates 完结月过滤 ----------
class TestMonthEndDates(unittest.TestCase):
    def _dates(self, *d):
        return pd.DatetimeIndex([pd.Timestamp(x) for x in d])

    def test_without_today_keeps_all_month_max(self):
        dates = self._dates("2026-08-28", "2026-08-31", "2026-09-01", "2026-09-03")
        out = month_end_dates(dates)
        self.assertEqual(list(out), list(self._dates("2026-08-31", "2026-09-03")))

    def test_today_filters_unfinished_month(self):
        # 9/1 的行情已入库, 但 9 月未完结: 截面只到 8/31
        dates = self._dates("2026-08-28", "2026-08-31", "2026-09-01", "2026-09-03")
        out = month_end_dates(dates, today=datetime.date(2026, 9, 4))
        self.assertEqual(list(out), list(self._dates("2026-08-31")))

    def test_today_on_month_first_day(self):
        # 每月 1 日晚跑月度流程的典型场景
        dates = self._dates("2026-08-31", "2026-09-01")
        out = month_end_dates(dates, today=datetime.date(2026, 9, 1))
        self.assertEqual(list(out), list(self._dates("2026-08-31")))

    def test_today_next_month_includes_finished_month(self):
        # 10/1 跑: 9 月已完结, 9/30 截面可用
        dates = self._dates("2026-09-28", "2026-09-30")
        out = month_end_dates(dates, today=datetime.date(2026, 10, 1))
        self.assertEqual(list(out), list(self._dates("2026-09-30")))


# ---------- P0-2: builder 方案锚定日不取月中脏截面 ----------
_SCHEMA = """
CREATE TABLE stock_basic (ts_code TEXT PRIMARY KEY, symbol TEXT, name TEXT, area TEXT,
    industry TEXT, market TEXT, list_date TEXT, delist_date TEXT);
CREATE TABLE daily (ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
    close REAL, pre_close REAL, change REAL, pct_chg REAL, vol REAL, amount REAL);
CREATE TABLE model_score (ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL,
    PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID;
CREATE TABLE plan (plan_date TEXT, plan_type TEXT, ts_code TEXT, name TEXT,
    weight REAL, shares INTEGER, industry TEXT, reason TEXT,
    PRIMARY KEY (plan_date, plan_type, ts_code)) WITHOUT ROWID;
CREATE TABLE regime (trade_date TEXT PRIMARY KEY, state TEXT, score REAL,
    drawdown REAL, vol REAL);
CREATE TABLE repo_rate (trade_date TEXT PRIMARY KEY, overnight REAL, r7d REAL, r14d REAL);
CREATE TABLE fund_basic (ts_code TEXT PRIMARY KEY, name TEXT, management TEXT,
    fund_type TEXT, market TEXT, invest_type TEXT, list_date TEXT, delist_date TEXT,
    benchmark TEXT, m_fee REAL, c_fee REAL);
CREATE TABLE fund_daily (ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
    close REAL, pre_close REAL, pct_chg REAL, vol REAL, amount REAL);
CREATE TABLE fund_adj (ts_code TEXT, trade_date TEXT, adj_factor REAL);
CREATE TABLE fund_rank (snap_date TEXT, code TEXT, name TEXT, fund_type TEXT,
    ret_1y REAL, ret_3y REAL);
CREATE TABLE trade_cal (cal_date TEXT PRIMARY KEY, is_open INTEGER);
"""


class _BuilderBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()
        self.patch_db = mock.patch.object(
            db, "get_conn", side_effect=lambda: sqlite3.connect(self.path))
        self.patch_db.start()

    def tearDown(self):
        self.patch_db.stop()
        os.unlink(self.path)

    def _c(self):
        return sqlite3.connect(self.path)

    def _exec(self, sql, args=()):
        conn = self._c()
        conn.execute(sql, args)
        conn.commit()
        conn.close()

    def _one(self, sql, args=()):
        conn = self._c()
        row = conn.execute(sql, args).fetchone()
        conn.close()
        return row

    def _cfg(self):
        return {"capital": 10000, "max_stocks": 5, "industry_max_weight": 0.25,
                "risk_profile": "aggressive", "single_side_cost": 0.003,
                "regime_equity_weight": {"aggressive": {"attack": 1.0, "neutral": 0.9,
                                                        "defense": 0.8}},
                "email": {}}


class TestBuilderPlanDate(_BuilderBase):
    def test_plan_anchors_on_completed_month_end(self):
        # 月末评分 0831 与月中脏评分 0903 并存: 默认 build_plan 必须锚定 0831
        self._exec("INSERT INTO model_score VALUES ('600000.SH','20260831',95.0,0.02)")
        self._exec("INSERT INTO model_score VALUES ('600000.SH','20260903',99.0,0.03)")
        self._exec("INSERT INTO stock_basic (ts_code, name, industry) "
                   "VALUES ('600000.SH','浦发银行','银行')")
        self._exec("INSERT INTO regime VALUES ('20260831','attack',1.0,NULL,NULL)")
        self._exec("INSERT INTO daily (ts_code, trade_date, close, vol, pct_chg) "
                   "VALUES ('600000.SH','20260831',10.0,1000,0.5)")
        self._exec("INSERT INTO repo_rate VALUES ('20260831',1.5,1.6,1.7)")
        with contextlib.ExitStack() as st:
            # 2026-09-29 收编 timeutil(东八区): builder 的"已完结月"判定改走
            # timeutil.today() —— 假钟注入点从 builder.datetime 挪到 timeutil.today
            # (app.timeutil 模块对象全进程共享, builder 内是模块属性查找, patch 生效)。
            st.enter_context(mock.patch.object(timeutil, "today",
                                               lambda: datetime.date(2026, 9, 4)))
            st.enter_context(mock.patch.object(builder.cfg_mod, "load_config",
                                               return_value=self._cfg()))
            st.enter_context(mock.patch.object(builder.funds, "score_etfs",
                                               return_value=pd.DataFrame()))
            st.enter_context(mock.patch.object(builder.funds, "otc_fund_picks",
                                               return_value={}))
            st.enter_context(mock.patch.object(builder.repo, "repo_advice",
                                               return_value={"desc": "x", "rate": 1.5}))
            summary = builder.build_plan()  # 不传 plan_date: 走 MAX(model_score) 分支
        self.assertEqual(summary["plan_date"], "20260831")
        self.assertIsNone(self._one("SELECT 1 FROM plan WHERE plan_date='20260903'"))
        row = self._one("SELECT COUNT(*) FROM plan WHERE plan_date='20260831'")
        self.assertGreater(row[0], 0)


class TestClockSingleSource(unittest.TestCase):
    """2026-09-29 收编 timeutil(东八区) 的回归钉：方案锚定(build_plan)/逆回购日历
    (repo_advice)/研报与股东新鲜度(analyst_ready/holder_ready)这三处**计算输入位点**
    的"今天"必须走统一时钟 —— 裸 date.today() 在非东八区机器上会让月初锚定、日历窗
    起点、新鲜度判定各漂一天。`test_clock_hygiene` 只扫 datasource 层（设计如此），
    这里补 portfolio/factors 侧的位点；判据形态与它一致（跳过注释行）。
    """

    def test_no_naive_clock_in_builder_repo_flow_factors(self):
        naive = re.compile(
            r"\bdate\s*\.\s*today\s*\(\s*\)|\bdatetime\s*\.\s*today\s*\(\s*\)")
        base = Path(__file__).resolve().parent.parent
        for rel in ("app/portfolio/builder.py", "app/portfolio/repo.py",
                    "app/factors/flow_factors.py"):
            with self.subTest(file=rel):
                src = (base / rel).read_text(encoding="utf-8")
                code = "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())
                self.assertIsNone(naive.search(code), f"{rel} 还有裸时钟")
                self.assertIn("timeutil", src, f"{rel} 的'今天'必须走 timeutil")


# ---------- P0-6: 回测撮合与模拟盘口径一致 ----------
def _mk(dates, close, vol, open_, pre, adj=None):
    idx = pd.DatetimeIndex(dates)
    cols = ["A"]
    m = {
        "close": pd.DataFrame(close, index=idx, columns=cols),
        "vol": pd.DataFrame(vol, index=idx, columns=cols),
        "open": pd.DataFrame(open_, index=idx, columns=cols),
        "pre_close": pd.DataFrame(pre, index=idx, columns=cols),
        "adj": pd.DataFrame(adj if adj is not None else np.ones((len(dates), 1)),
                            index=idx, columns=cols),
    }
    return m, idx


class TestTrainDeterminism(unittest.TestCase):
    """P1-4 回归: 固定 seed + deterministic → 两次训练结果一致"""

    def test_same_data_same_model(self):
        import lightgbm as lgb
        from app.models.trainer import PARAMS
        rng = np.random.default_rng(3)
        X = pd.DataFrame({f"f{i}": rng.normal(size=600) for i in range(4)})
        y = 1.5 * X["f0"] - 0.7 * X["f1"] + rng.normal(scale=0.5, size=600)
        preds = []
        for _ in range(2):
            model = lgb.train(PARAMS, lgb.Dataset(X, y), num_boost_round=30)
            preds.append(model.predict(X))
        np.testing.assert_array_almost_equal(preds[0], preds[1], decimal=12)


class TestBacktestExec(unittest.TestCase):
    def setUp(self):
        self.days = [pd.Timestamp("2026-09-01") + pd.Timedelta(days=i) for i in range(12)]
        # 2026-09-28 第 8 次重钉(P1.5): 本类锁的是**收盘价**撮合口径下的行为
        # (2026-09-05 审查修复的原始口径 —— "回测按收盘价成交, 可交易性判定也按
        # 收盘价")。`FILL_AT_OPEN` 自本次重钉起**默认 True**, 若不显式钉回 False,
        # 这 4 个锁会在换 pin 当天静默改测另一套口径 —— 那不是"更新基线", 是把
        # 守卫测的东西换掉了。开盘价口径另由 `TestOpenFillExec` 覆盖。
        from app.backtest import engine as _bt
        self._bt = _bt
        self._old_fill = _bt.FILL_AT_OPEN
        _bt.FILL_AT_OPEN = False

    def tearDown(self):
        self._bt.FILL_AT_OPEN = self._old_fill

    def test_t_board_executable_next_day(self):
        # T字板: 开盘 10.2(未涨停) 盘中曾开板, 收盘 10.8(+8%, 未封死涨停)。
        # 2026-09-05 审查修复: 回测按收盘价成交, 可交易性判定也按收盘价 —— 收盘未封板
        # 即可成交; 原"开盘判定+收盘成交"口径错配会把封死涨停的收盘价当成成交价
        m, idx = _mk(self.days,
                     close=[10.8] * 12, vol=[1e6] * 12,
                     open_=[10.2] * 12, pre=[10.0] * 12)
        bp, raw, _ = _exec_side(m, idx, 0, "A", "buy")
        self.assertAlmostEqual(bp, 10.8, places=6)  # 当天收盘价成交

    def test_oneword_limit_up_skips(self):
        # 一字涨停(开盘即涨停价): 顺延; 次日开板后成交
        m, idx = _mk(self.days,
                     close=[11.0, 10.8] + [10.8] * 10,
                     vol=[1e6] * 12,
                     open_=[11.0, 10.5] + [10.5] * 10, pre=[10.0] * 12)
        bp, raw, _ = _exec_side(m, idx, 0, "A", "buy")
        self.assertAlmostEqual(bp, 10.8, places=6)  # 第二天成交

    def test_sell_oneword_limit_down_skips(self):
        m, idx = _mk(self.days,
                     close=[9.0, 9.5] + [9.5] * 10,
                     vol=[1e6] * 12,
                     open_=[9.0, 9.5] + [9.5] * 10, pre=[10.0] * 12)
        sp, _, _ = _exec_side(m, idx, 0, "A", "sell")
        self.assertAlmostEqual(sp, 9.5, places=6)  # 一字跌停顺延一天后卖出

    def test_five_day_limit_up_then_void(self):
        # 连续 6 个交易日一字涨停: 顺延满 5 天, 订单作废(与模拟盘 expired 一致)
        m, idx = _mk(self.days,
                     close=[11.0] * 12, vol=[1e6] * 12,
                     open_=[11.0] * 12, pre=[10.0] * 12)
        bp, raw, _ = _exec_side(m, idx, 0, "A", "buy")
        self.assertIsNone(bp)

    def test_suspension_then_void(self):
        # 停牌(vol=0)满 5 个交易日: 作废, 不再按最后收盘价强制成交(旧口径会虚高)
        m, idx = _mk(self.days,
                     close=[10.0] * 12, vol=[0] * 6 + [1e6] * 6,
                     open_=[10.0] * 12, pre=[10.0] * 12)
        bp, raw, _ = _exec_side(m, idx, 0, "A", "buy")
        self.assertIsNone(bp)

    def test_limit_band_by_pit_callable(self):
        # 第七轮 N7-6: st_codes 可调用时按执行日时点名称判定限幅 —— 首日戴帽(5% 档,
        # 收盘 +6% 超 5% 涨停价 10.5 → 买不进顺延), 次日摘帽(10% 档, +2% 可成交)
        m, idx = _mk(self.days,
                     close=[10.6, 10.2] + [10.2] * 10,
                     vol=[1e6] * 12,
                     open_=[10.0] * 12, pre=[10.0] * 12)
        is_st = lambda code, d: d == "20260901"  # 仅首日戴帽
        bp, raw, _ = _exec_side(m, idx, 0, "A", "buy", is_st)
        self.assertAlmostEqual(bp, 10.2, places=6)

    def test_limit_band_set_backward_compat(self):
        # 旧口径兼容: 传集合时仍按集合成员判 5% 档(现有调用方/测试不破坏)
        m, idx = _mk(self.days,
                     close=[10.6, 10.2] + [10.2] * 10,
                     vol=[1e6] * 12,
                     open_=[10.0] * 12, pre=[10.0] * 12)
        bp, raw, _ = _exec_side(m, idx, 0, "A", "buy", {"A"})
        self.assertAlmostEqual(bp, 10.2, places=6)


# ---------- P0-7: ETF 动量用复权价 ----------
class TestScoreEtfsAdjusted(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()
        self.patch_db = mock.patch.object(
            db, "get_conn", side_effect=lambda: sqlite3.connect(self.path))
        self.patch_db.start()
        # 130 个工作日, 最后 20 日为"除权后"区间
        ds = pd.bdate_range("2023-06-01", periods=130)
        self.dates = ds.strftime("%Y%m%d").tolist()
        self.event = self.dates[-20]
        # A: 除权日分红, 未复权价 1.0→0.95, adj 1.0→1.05263(复权后≈1.0, 动量应为 0)
        # B: 无除权, 价格 1.0 上下微幅波动(对照组)
        conn = self._c()
        conn.execute("INSERT INTO fund_basic (ts_code, name, fund_type, invest_type, "
                     "market, list_date) VALUES ('510000.SH','除权ETF','债券型','','E','20200101')")
        conn.execute("INSERT INTO fund_basic (ts_code, name, fund_type, invest_type, "
                     "market, list_date) VALUES ('510001.SH','对照ETF','债券型','','E','20200101')")
        for i, d in enumerate(self.dates):
            if d >= self.event:
                conn.execute("INSERT INTO fund_daily (ts_code, trade_date, close, amount) "
                             "VALUES ('510000.SH',?,?,20000)", (d, 0.95 + (i % 2) * 0.001))
                conn.execute("INSERT INTO fund_adj (ts_code, trade_date, adj_factor) "
                             "VALUES ('510000.SH',?,1.05263)", (d,))
            else:
                conn.execute("INSERT INTO fund_daily (ts_code, trade_date, close, amount) "
                             "VALUES ('510000.SH',?,?,20000)", (d, 1.0 + (i % 2) * 0.001))
                conn.execute("INSERT INTO fund_adj (ts_code, trade_date, adj_factor) "
                             "VALUES ('510000.SH',?,1.0)", (d,))
            conn.execute("INSERT INTO fund_daily (ts_code, trade_date, close, amount) "
                         "VALUES ('510001.SH',?,?,20000)", (d, 1.0 + (i % 2) * 0.001))
            conn.execute("INSERT INTO fund_adj (ts_code, trade_date, adj_factor) "
                         "VALUES ('510001.SH',?,1.0)", (d,))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.patch_db.stop()
        os.unlink(self.path)

    def _c(self):
        return sqlite3.connect(self.path)

    def test_dividend_fund_momentum_not_depressed(self):
        out = funds.score_etfs(top_n=10)
        self.assertFalse(out.empty)
        a = out[out["ts_code"] == "510000.SH"].iloc[0]
        # 复权后动量≈0(±0.2%); 旧口径(未复权)会把分红当 -5% 假跌
        self.assertAlmostEqual(a["mom60"], 0.0, delta=0.002)
        self.assertAlmostEqual(a["mom120"], 0.0, delta=0.002)
        b = out[out["ts_code"] == "510001.SH"].iloc[0]
        self.assertAlmostEqual(b["mom60"], 0.0, delta=0.002)


class TestRepoAdvice(_BuilderBase):
    """P2-5 回归: 节前 GC001 吃满假期利息、文案 1000 元起、数据日参数"""

    def test_holiday_gap_advises_gc001_not_lock14(self):
        # 9/30 后休市 9 天: GC001 隔夜即可吃满假期利息(按实际占用天数计息), 不再锁14天
        for d in ("20260930", "20261009", "20261010"):
            self._exec("INSERT INTO trade_cal VALUES (?,1)", (d,))
        self._exec("INSERT INTO repo_rate VALUES ('20260930',2.0,2.1,2.2)")
        r = repo.repo_advice(10000, today="20260930")
        self.assertEqual(r["term"], 1)
        self.assertIn("GC001 隔夜", r["desc"])

    def test_daily_rolling_text_1000_yuan(self):
        # 日常滚动文案: 沪深均 1000 元起(2022-10 后无"沪市10万起")
        for d in ("20261009", "20261012", "20261013", "20261014", "20261015"):
            self._exec("INSERT INTO trade_cal VALUES (?,1)", (d,))
        r = repo.repo_advice(10000, today="20261012")
        self.assertEqual(r["term"], 1)
        self.assertIn("1000元起", r["desc"])
        self.assertNotIn("10万", r["desc"])

    def test_uses_data_date_not_system_today(self):
        # 传入方案数据日: 期限建议按该日日历推算(历史补跑口径)
        for d in ("20260331", "20260401", "20260402"):
            self._exec("INSERT INTO trade_cal VALUES (?,1)", (d,))
        r = repo.repo_advice(10000, today="20260331")
        self.assertEqual(r["term"], 14)  # 季末 → 锁14天


if __name__ == "__main__":
    unittest.main()
