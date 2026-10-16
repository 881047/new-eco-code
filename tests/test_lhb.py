"""龙虎榜数据层/因子单测: python -m unittest tests.test_lhb -v"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from app.datasource import lhb_client as lc
from app.factors import factor_engine as fe


def _mk_dates(n=30):
    return pd.bdate_range("2024-01-01", periods=n)


class TestDedupeStock(unittest.TestCase):
    def test_prefers_same_day_row_over_multi_day(self):
        df = pd.DataFrame({
            "ts_code": ["000001.SZ", "000001.SZ"],
            "name": ["平安银行", "平安银行"],
            "net_ratio": [-1.2, 3.5],
            "reason": ["日涨幅偏离值达到7%的前5只证券",
                       "连续三个交易日内，涨幅偏离值累计达到20%的证券"],
        })
        out = lc._dedupe_stock(df)
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["net_ratio"], -1.2)  # 保留当日榜

    def test_single_row_untouched(self):
        df = pd.DataFrame({
            "ts_code": ["600519.SH"], "name": ["贵州茅台"],
            "net_ratio": [1.0], "reason": ["日振幅值达到15%的前5只证券"],
        })
        self.assertEqual(len(lc._dedupe_stock(df)), 1)


class TestNormTsCode(unittest.TestCase):
    """2026-09-28 审查: 920(北交所新段)判定必须在 "9"→沪B 之前 —— 原顺序使
    920 分支永不可达, 920xxx 被错配成 .SH 后在股票池交集里被整行静默丢弃。"""

    def test_bse_920_segment(self):
        self.assertEqual(lc._norm_ts_code("920001"), "920001.BJ")
        self.assertEqual(lc._norm_ts_code("920238"), "920238.BJ")

    def test_sh_b_share_900_still_sh(self):
        # 900 段 = 沪 B 股, 维持 .SH(与 event_client 首位口径一致)
        self.assertEqual(lc._norm_ts_code("900901"), "900901.SH")

    def test_main_segments_unchanged(self):
        self.assertEqual(lc._norm_ts_code("600519"), "600519.SH")
        self.assertEqual(lc._norm_ts_code("000001"), "000001.SZ")
        self.assertEqual(lc._norm_ts_code("300750"), "300750.SZ")
        self.assertEqual(lc._norm_ts_code("830799"), "830799.BJ")
        self.assertEqual(lc._norm_ts_code("430047"), "430047.BJ")
        self.assertEqual(lc._norm_ts_code("168001"), "168001.UN")  # 非股票段 → 未知


class TestParseAkDetail(unittest.TestCase):
    """两种时期的列名变体都必须解析出关键字段"""

    def test_new_column_names(self):
        raw = pd.DataFrame({
            "代码": ["000017", "300364"], "名称": ["深中华A", "中文在线"],
            "收盘价": [11.75, 28.31], "涨跌幅": [0.0, 3.7757],
            "龙虎榜净买额": [0.0, 1.7e8], "龙虎榜买入额": [7.7e9, 2.9e8],
            "龙虎榜卖出额": [7.7e9, 1.2e8], "龙虎榜成交额": [7.7e9, 4.1e8],
            "市场总成交额": [7.7e9, 6.3e9], "净买额占总成交比": [0.0, 2.76],
            "成交额占总成交比": [100.0, 6.5], "换手率": [24.2, 33.3],
            "流通市值": [5.5e9, 1.8e10],
            "上榜原因": ["严重异常期间日收盘价格涨幅偏离值累计达到100%的证券",
                         "日换手率达到30%的前5只证券"],
            "上榜后1日": [None, None],
        })
        out = lc._parse_ak_detail(raw, "20260902")
        self.assertEqual(len(out), 2)
        # 多日榜(严重异常开头)排后, 当日榜排前
        self.assertEqual(out.iloc[0]["ts_code"], "300364.SZ")
        self.assertAlmostEqual(out.iloc[0]["net_ratio"], 2.76)
        self.assertNotIn("上榜后1日", out.columns)  # 未来数据不得入库

    def test_old_column_names(self):
        raw = pd.DataFrame({
            "代码": ["000033"], "名称": ["新都退"],
            "收盘价": [4.70], "涨跌幅": [-9.96],
            "龙虎榜净买额": [-1410.0], "龙虎榜买入额": [7520.0],
            "龙虎榜卖出额": [8930.0], "龙虎榜成交额": [16450.0],
            "市场总成交额": [8930.0], "净买额占总成交额比": [-15.79],
            "成交额占总成交额比": [184.21], "换手率": [0.0004],
            "流通市值": [2.0e9], "上榜原因": ["退市整理期"],
        })
        out = lc._parse_ak_detail(raw, "20170601")
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out.iloc[0]["net_ratio"], -15.79)

    def test_bj_kept_and_convertible_dropped(self):
        # 2026-09-08 全市场扩展: 北交所(8开头)保留; 可转债(12xxxx)仍剔除
        raw = pd.DataFrame({
            "代码": ["600519", "127106", "830799"], "名称": ["贵州茅台", "伟隆转债", "艾融软件"],
            "收盘价": [100, 100, 10], "涨跌幅": [1, 1, 1],
            "龙虎榜净买额": [1, 1, 1], "龙虎榜买入额": [1, 1, 1],
            "龙虎榜卖出额": [1, 1, 1], "龙虎榜成交额": [1, 1, 1],
            "市场总成交额": [1, 1, 1], "净买额占总成交比": [1, 1, 1],
            "成交额占总成交比": [1, 1, 1], "换手率": [1, 1, 1],
            "流通市值": [1, 1, 1], "上榜原因": ["x", "x", "x"],
        })
        out = lc._parse_ak_detail(raw, "20240101")
        self.assertEqual(list(out["ts_code"]), ["600519.SH", "830799.BJ"])


class TestLhbFactors(unittest.TestCase):
    """构造稀疏 lhb/lhb_org 数据, 验证滚动和与 NaN 语义"""

    def _build_db(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE lhb (trade_date TEXT, ts_code TEXT, name TEXT,"
                     "close REAL, pct_chg REAL, net_buy REAL, buy_value REAL, sell_value REAL,"
                     "lhb_amount REAL, market_amount REAL, net_ratio REAL, amount_ratio REAL,"
                     "turnover REAL, float_mv REAL, reason TEXT,"
                     "PRIMARY KEY (trade_date, ts_code)) WITHOUT ROWID")
        conn.execute("CREATE TABLE lhb_org (trade_date TEXT, ts_code TEXT, name TEXT,"
                     "org_buy_num REAL, org_sell_num REAL, org_buy_value REAL,"
                     "org_sell_value REAL, org_net_value REAL, org_net_ratio REAL,"
                     "reason TEXT, PRIMARY KEY (trade_date, ts_code)) WITHOUT ROWID")
        return conn

    def test_rolling_sum_and_nan(self):
        conn = self._build_db()
        dates = [d.strftime("%Y%m%d") for d in _mk_dates(10)]
        for i, d in enumerate(dates):
            conn.execute(
                "INSERT INTO lhb VALUES (?,?,?,NULL,NULL,NULL,NULL,NULL,NULL,NULL,?,NULL,NULL,NULL,'x')",
                (d, "A.SH", "A", float(i + 1)))
        idx = pd.to_datetime(dates, format="%Y%m%d")
        with mock.patch.object(fe.db, "get_conn", return_value=conn), \
                mock.patch.object(fe.db, "table_exists", return_value=True):
            out = fe.compute_lhb_factors(idx)
        # lhb_cnt20: 每天上榜 1 次, 第 10 天滚动和 = 10
        self.assertAlmostEqual(out["lhb_cnt20"].iloc[-1]["A.SH"], 10.0)
        # lhb_net20: 1+2+...+10 = 55
        self.assertAlmostEqual(out["lhb_net20"].iloc[-1]["A.SH"], 55.0)
        # 未上榜股票为 NaN
        self.assertTrue(np.isnan(out["lhb_net20"].iloc[-1].get("B.SH", np.nan)))

    def test_neutralize_nan_propagates(self):
        df = pd.DataFrame({"a": [1.0, 2.0], "b": [np.nan, 3.0]}, index=pd.to_datetime(["2024-01-01", "2024-01-02"]))
        ind = pd.Series({"a": "银行", "b": "银行"})
        out = fe.neutralize(df, ind)
        self.assertTrue(np.isnan(out.loc["2024-01-01", "b"]))  # NaN 保持 NaN


class TestThsParse(unittest.TestCase):
    def test_stock_items(self):
        items = [{
            "thscode": "002536.SZ", "name": "飞龙股份",
            "concept_list": [{"name": "芯片概念"}, {"name": "汽车电子"}],
            "change": 0.09994, "net_value": 1.78e9, "hot_rank": 2,
            "org_net_value": -7.2e7, "limit_reason": "日涨幅偏离值达到7%的前5只证券",
            "range_days": 1,
        }]
        df = lc._parse_ths_stock_items(items, "20260902")
        self.assertEqual(df.iloc[0]["thscode"], "002536.SZ")
        import json
        concepts = json.loads(df.iloc[0]["concepts"])
        self.assertEqual(concepts, ["芯片概念", "汽车电子"])

    def test_hot_money_items(self):
        items = [{"name": "T王", "buying": 2.3e8,
                  "rows": [{"thscode": "300164.SZ", "name": "通源石油",
                            "net_value": 5.0e7, "hot_money_item_net_value": 3.0e7}]}]
        df = lc._parse_ths_hm_items(items, "20260902")
        self.assertEqual(df.iloc[0]["hm_name"], "T王")
        self.assertEqual(df.iloc[0]["net_value"], 3.0e7)  # 优先游资个股净买入
        self.assertEqual(df.iloc[0]["buying"], 2.3e8)

    def test_hm_fallback_net_value(self):
        items = [{"name": "X", "buying": 1.0,
                  "rows": [{"thscode": "000001.SZ", "name": "平安银行",
                            "net_value": 9.9e6, "hot_money_item_net_value": None}]}]
        df = lc._parse_ths_hm_items(items, "20260902")
        self.assertEqual(df.iloc[0]["net_value"], 9.9e6)


if __name__ == "__main__":
    unittest.main()
