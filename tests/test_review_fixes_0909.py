"""2026-09-09 审查修复回归测试(按优先级 ④①③⑤②)

④ regime 滚动分位分母口径  → tests/test_decision_core.py::TestRollingPctRank
① 财务因子披露日顺延一交易日
③ 退市样本标签不再丢弃
⑤ 上市锚定与上市初期无涨跌幅窗口径统一
② 全 NaN 特征样本不入训/不入推分截面(补测, 原优先级最低)
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.factors import factor_engine as fe


class TestShiftToNextTradingDay(unittest.TestCase):
    """① 披露日 → 其后第一个交易日(严格大于)"""

    CAL = ["20260129", "20260130", "20260202", "20260203"]

    def test_trading_day_shifts_forward(self):
        s = pd.Series(["20260130"])
        self.assertEqual(fe.shift_to_next_trading_day(s, self.CAL).iloc[0], "20260202")

    def test_non_trading_day_shifts_to_next(self):
        # 周六披露 → 周一可见(信息已公开, 非保守)
        s = pd.Series(["20260131"])
        self.assertEqual(fe.shift_to_next_trading_day(s, self.CAL).iloc[0], "20260202")

    def test_beyond_calendar_is_none(self):
        s = pd.Series(["20260301"])
        self.assertIsNone(fe.shift_to_next_trading_day(s, self.CAL).iloc[0])

    def test_index_preserved(self):
        s = pd.Series(["20260129", "20260130"], index=[7, 9])
        out = fe.shift_to_next_trading_day(s, self.CAL)
        self.assertEqual(list(out.index), [7, 9])
        self.assertEqual(out.loc[7], "20260130")


class TestFinaAnnDatePIT(unittest.TestCase):
    """① 披露日在评分日当天的报告不得进入该日截面(次日才可见)"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        conn = sqlite3.connect(self.path)
        conn.execute(
            "CREATE TABLE fina_indicator (ts_code TEXT, ann_date TEXT, end_date TEXT, "
            "roe REAL, netprofit_yoy REAL, or_yoy REAL, grossprofit_margin REAL, "
            "debt_to_assets REAL, eps REAL)")
        # A: 1/30(交易日)披露年报 → 2/2 才可见; B: 1/31(周六)披露 → 2/2 可见
        rows = [("A.SZ", "20260130", "20251231", 10.0, 1.0, 2.0, 3.0, 4.0, 0.5),
                ("B.SZ", "20260131", "20251231", 20.0, 1.0, 2.0, 3.0, 4.0, 0.5)]
        conn.executemany("INSERT INTO fina_indicator VALUES (?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        conn.close()
        self.patch = mock.patch.object(fe.db, "get_conn",
                                       side_effect=lambda: sqlite3.connect(self.path))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_month_end_announcement_not_visible_same_day(self):
        cal = ["20260130", "20260202", "20260227"]
        out = fe.compute_fina_factors(["20260130", "20260202", "20260227"], calendar=cal)
        roe = out["roe"]
        self.assertTrue(np.isnan(roe.loc["20260130", "A.SZ"]))   # 当日披露 → 当日不可见
        self.assertEqual(roe.loc["20260202", "A.SZ"], 10.0)      # 次一交易日可见
        self.assertEqual(roe.loc["20260202", "B.SZ"], 20.0)      # 周六披露 → 周一可见

    def test_forward_fill_keeps_old_value(self):
        cal = ["20260130", "20260202", "20260227"]
        out = fe.compute_fina_factors(["20260130", "20260227"], calendar=cal)
        roe = out["roe"]
        self.assertTrue(np.isnan(roe.loc["20260130", "A.SZ"]))
        self.assertEqual(roe.loc["20260227", "A.SZ"], 10.0)      # ffill 到后续月份


class TestForwardReturn(unittest.TestCase):
    """③ 退市/停牌样本标签按窗口内最后有效价计算, 不再静默丢弃"""

    def setUp(self):
        from app.models.trainer import forward_return
        self.fwd = forward_return
        self.ac = pd.DataFrame({
            "A": [1.0, 2.0, 3.0, 4.0, 5.0],            # 正常
            "B": [1.0, 2.0, np.nan, np.nan, np.nan],   # 窗口内退市
            "C": [1.0, np.nan, np.nan, 4.0, 5.0],      # 停牌后复牌
        }, index=pd.date_range("2026-01-01", periods=5))

    def test_normal_unchanged(self):
        r = self.fwd(self.ac, horizon=2)
        self.assertAlmostEqual(r.loc[self.ac.index[0], "A"], 3.0 / 1.0 - 1)
        self.assertAlmostEqual(r.loc[self.ac.index[1], "A"], 4.0 / 2.0 - 1)

    def test_delisted_uses_last_price(self):
        r = self.fwd(self.ac, horizon=2)
        # t0 退市: 持有到最后一个可成交价 2.0 → +100%(旧口径为 NaN 被丢弃)
        self.assertAlmostEqual(r.loc[self.ac.index[0], "B"], 2.0 / 1.0 - 1)
        # t1 退市: 2.0/2.0 - 1 = 0
        self.assertAlmostEqual(r.loc[self.ac.index[1], "B"], 0.0)

    def test_suspended_uses_last_price_before_window_end(self):
        r = self.fwd(self.ac, horizon=2)
        # t0 停牌贯穿窗口: 沿用停牌前价 1.0 → 0
        self.assertAlmostEqual(r.loc[self.ac.index[0], "C"], 0.0)

    def test_no_price_at_base_stays_nan(self):
        r = self.fwd(self.ac, horizon=2)
        self.assertTrue(np.isnan(r.loc[self.ac.index[1], "C"]))  # 基准日无价

    def test_tail_beyond_horizon_stays_nan(self):
        r = self.fwd(self.ac, horizon=2)
        self.assertTrue(np.isnan(r.loc[self.ac.index[4], "A"]))  # 窗口不足

    def test_ab_switch_reproduces_legacy(self):
        # delist_last_price=False = 修复前口径(A/B 对照臂)
        r = self.fwd(self.ac, horizon=2, delist_last_price=False)
        self.assertTrue(np.isnan(r.loc[self.ac.index[0], "B"]))   # 退市样本标签仍被丢弃
        self.assertTrue(np.isnan(r.loc[self.ac.index[0], "C"]))   # 停牌样本标签仍被丢弃
        self.assertAlmostEqual(r.loc[self.ac.index[0], "A"], 3.0 / 1.0 - 1)


_DS_SCHEMA = """
CREATE TABLE stock_basic (ts_code TEXT PRIMARY KEY, symbol TEXT, name TEXT, area TEXT,
    industry TEXT, market TEXT, list_date TEXT, delist_date TEXT);
CREATE TABLE daily (ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
    close REAL, pre_close REAL, change REAL, pct_chg REAL, vol REAL, amount REAL);
CREATE TABLE factor (ts_code TEXT, trade_date TEXT, factor TEXT, value REAL,
    PRIMARY KEY (ts_code, trade_date, factor)) WITHOUT ROWID;
CREATE TABLE daily_score (ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL,
    PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID;
CREATE TABLE daily_basic (ts_code TEXT, trade_date TEXT, total_mv REAL, circ_mv REAL);
"""


class TestDailyScoreListingAnchor(unittest.TestCase):
    """⑤ 日频推分补「上市满 60 交易日」锚定(与 trainer 同口径)"""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(_DS_SCHEMA)
        self.dates = [d.strftime("%Y%m%d")
                      for d in pd.date_range("2026-06-01", periods=62, freq="B")]
        self.T = self.dates[-1]
        # 锚定截止日 = ≤ 评分日的第 61 个交易日 = dates[1](与 trainer 的 dates[pos-60] 同日)
        self.cutoff = self.dates[1]
        for d in self.dates:                     # 62 个交易日的日历
            conn.execute("INSERT INTO daily (ts_code, trade_date, close, pre_close, vol, pct_chg,"
                         " amount) VALUES ('000001.SZ',?,10.0,10.0,1000,0.0,1000.0)", (d,))
        rows = [("000001.SZ", "19910403"),                      # 老股
                ("301999.SZ", self.dates[30]),                  # 次新(锚定后应剔除)
                ("301997.SZ", self.cutoff),                     # 恰在边界(保留)
                ("301998.SZ", self.dates[2]),                   # 边界后一天(剔除)
                ("301996.SZ", None),                            # list_date 缺失(保守: 保留)
                ("920419.BJ", "19910403")]                      # 北交(bse_enabled=false 剔除)
        for code, ld in rows:
            conn.execute("INSERT INTO stock_basic (ts_code, name, list_date) VALUES (?,?,?)",
                         (code, code, ld))
            conn.execute("INSERT INTO daily (ts_code, trade_date, close, pre_close, vol, pct_chg,"
                         " amount) VALUES (?,?,10.0,10.0,1000,0.5,1000.0)", (code, self.T))
            # P1-04 起市值过滤缺数据即报错(不再静默跳过整层), 夹具须给齐 daily_basic
            conn.execute("INSERT INTO daily_basic (ts_code, trade_date, total_mv) "
                         "VALUES (?,?,100000.0)", (code, self.T))
            conn.execute("INSERT INTO factor (ts_code, trade_date, factor, value) "
                         "VALUES (?,?,'mom_20',1.0)", (code, self.T))
        conn.commit()
        conn.close()
        self.patch_db = mock.patch.object(
            fe.db, "get_conn", side_effect=lambda: sqlite3.connect(self.path))
        self.patch_db.start()
        from app.models import daily_score as ds
        self.ds = ds
        model = lgb.train({"objective": "regression", "verbose": -1},
                          lgb.Dataset(pd.DataFrame({"mom_20": [1.0, 2.0, 3.0]}),
                                      pd.Series([1.0, 2.0, 3.0])), num_boost_round=5)
        self.model_path = Path(self.path).with_suffix(".model")
        model.save_model(str(self.model_path))

    def tearDown(self):
        self.patch_db.stop()
        os.unlink(self.path)
        os.unlink(self.model_path)

    def test_new_stock_excluded(self):
        with mock.patch.object(self.ds, "_model_files", lambda: [self.model_path]):
            self.ds.score_today(self.T)
        conn = sqlite3.connect(self.path)
        got = {r[0] for r in conn.execute("SELECT ts_code FROM daily_score")}
        conn.close()
        self.assertIn("000001.SZ", got)          # 老股保留
        self.assertNotIn("301999.SZ", got)       # 次新股被锚定剔除
        self.assertIn("301997.SZ", got)          # 边界日上市(= trainer cutoff)保留
        self.assertNotIn("301998.SZ", got)       # 边界后一天上市剔除
        self.assertIn("301996.SZ", got)          # list_date 缺失不额外收紧
        self.assertNotIn("920419.BJ", got)       # 北交与训练域对齐剔除(bse_enabled=false)


class TestStackAllNanSamples(unittest.TestCase):
    """② 全 NaN 特征样本不入训/不入推分截面

    机制: pandas 3.0 起 DataFrame.stack() 默认不再丢弃 NaN 行(pandas 2.x 默认丢弃),
    trainer._stack 因此返回「日期 × 全部股票」的笛卡尔积, 未上市/退市/停牌组合整行 NaN。
    """

    def _wide(self):
        idx = pd.to_datetime(["2026-01-30", "2026-02-27"])
        cols = pd.MultiIndex.from_tuples(
            [("A.SZ", "mom_20"), ("A.SZ", "rev_5"), ("B.SZ", "mom_20"), ("B.SZ", "rev_5")],
            names=["ts_code", "factor"])
        # B.SZ 在 2/27 无任何因子数据
        return pd.DataFrame([[1.0, 2.0, 3.0, 4.0],
                             [5.0, 6.0, np.nan, np.nan]], index=idx, columns=cols)

    def test_pandas3_stack_keeps_all_nan_rows(self):
        # 前提锁定: 若未来 pandas 恢复丢弃 NaN, 本测试失败 → 重新评估 ② 的兜底是否仍必要
        wide = self._wide()
        s = wide.xs("mom_20", axis=1, level="factor").stack()
        self.assertEqual(len(s), 4)          # 2 日期 × 2 股票(含 B.SZ/2-27 的 NaN)
        self.assertTrue(np.isnan(s.loc[("2026-02-27", "B.SZ")]))

    def test_stack_produces_all_nan_row_then_dropped(self):
        from app.models.trainer import _drop_all_nan_rows, _stack
        wide = self._wide()
        X = _stack(wide, ["mom_20", "rev_5"], wide.index)
        self.assertEqual(len(X), 4)                                  # 笛卡尔积
        self.assertEqual(int(X.isna().all(axis=1).sum()), 1)         # B.SZ/2-27 全 NaN
        X2 = _drop_all_nan_rows(X, "test")
        self.assertEqual(len(X2), 3)
        self.assertNotIn(("2026-02-27", "B.SZ"), X2.index)
        self.assertIn(("2026-01-30", "B.SZ"), X2.index)              # 有数据的行保留

    def test_drop_keeps_partial_nan_rows(self):
        # 只有部分因子缺失的行是合法样本(LightGBM 按缺失处理), 不得误剔
        from app.models.trainer import _drop_all_nan_rows
        df = pd.DataFrame({"a": [1.0, np.nan], "b": [np.nan, 2.0]}, index=["x", "y"])
        out = _drop_all_nan_rows(df, "test")
        self.assertEqual(list(out.index), ["x", "y"])

    def test_drop_empty_passthrough(self):
        from app.models.trainer import _drop_all_nan_rows
        df = pd.DataFrame({"a": []})
        self.assertTrue(_drop_all_nan_rows(df, "test").empty)


class TestTrainStart(unittest.TestCase):
    """2026-09-09 对照实验: 训练/评分月起点限制(科创板上市起 vs 全历史)

    机制: _apply_train_start 同时限制**训练窗口**与**评分月序列** —— 训练数据不早于
    train_start 的模型无法为更早的月份推分, 两个序列必须同源截断, 否则会出现
    "用含未来信息的模型给过去打分"的错配。
    """

    def _md(self):
        return pd.DatetimeIndex(pd.to_datetime(
            ["20140131", "20170131", "20190722", "20190731", "20190830", "20260831"]))

    def test_none_keeps_all(self):
        from app.models.trainer import _apply_train_start
        self.assertEqual(len(_apply_train_start(self._md(), None)), 6)
        self.assertEqual(len(_apply_train_start(self._md(), "")), 6)

    def test_filter_start_inclusive(self):
        from app.models.trainer import _apply_train_start
        out = _apply_train_start(self._md(), "20190722")
        self.assertEqual([d.strftime("%Y%m%d") for d in out],
                         ["20190722", "20190731", "20190830", "20260831"])

    def test_filter_accepts_ts_and_str(self):
        from app.models.trainer import _apply_train_start
        self.assertEqual(len(_apply_train_start(self._md(), "2019-07-31")), 3)
        self.assertEqual(len(_apply_train_start(self._md(), pd.Timestamp("2019-07-31"))), 3)

    def test_filter_beyond_range_empty(self):
        from app.models.trainer import _apply_train_start
        self.assertEqual(len(_apply_train_start(self._md(), "2027-01-01")), 0)


class TestTrainWindowSlice(unittest.TestCase):
    """2026-09-09 对照实验暴露: i=0 的负索引切片把未来喂进首月训练(前视)

    机制: 窗口 = md_all[i-36 : i-1]。i=0 时右端 -1 被 Python 当负索引,
    md_all[0:-1] = 除最后一期外的**全部**月份 —— 首月模型看到了未来 85 个月。
    生产 run 因 backtest_start 跳过 i<36 未触发, train_start 类实验必踩。
    """

    def _md(self, n):
        return pd.date_range("2019-07-31", periods=n, freq="ME")

    def test_i0_is_empty_not_all_but_last(self):
        from app.models.trainer import _train_window
        md = self._md(86)
        self.assertEqual(len(_train_window(md, 0, 36)), 0)   # 修复前 = 85(全历史含未来)
        self.assertEqual(len(_train_window(md, 1, 36)), 0)

    def test_never_contains_future_or_current_month(self):
        from app.models.trainer import _train_window
        md = self._md(86)
        for i in range(len(md)):
            w = _train_window(md, i, 36)
            if len(w) == 0:
                continue
            self.assertLess(w.max(), md[i])                  # 不含当月
            self.assertLess(w.max(), md[i - 1])              # 不含上月(末期 = i-2)
            self.assertLessEqual(len(w), 35)                 # 上限 = train_months-1

    def test_full_window_shape(self):
        from app.models.trainer import _train_window
        md = self._md(60)
        w = _train_window(md, 50, 36)
        self.assertEqual(len(w), 35)
        self.assertEqual(w[0], md[14])                       # i-36
        self.assertEqual(w[-1], md[48])                      # i-2

    def test_start_clamped_for_short_history(self):
        from app.models.trainer import _train_window
        md = self._md(20)
        w = _train_window(md, 13, 36)
        self.assertEqual(len(w), 12)
        self.assertEqual(w[0], md[0])


if __name__ == "__main__":
    unittest.main()
