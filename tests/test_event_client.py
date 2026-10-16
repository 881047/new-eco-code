"""事件族归一化纯函数单测（2026-09-13，关键路径测试第三批）

为什么先测这组：F26/F27/F35 判"事件族周频 0/10 通过"的**数据基础**就是这三个
`normalize_*`（业绩预告 / 限售解禁 / 董监高变动）。它们错了，结论就站在错的数据上；
而它们是纯函数 ⇒ 最便宜的守卫，不需要数据库、不需要网络。

守四类不变量：
  1. **列契约**：输出列必须与 `db.SCHEMA` 里三张表一一对应 —— `upsert_df` 是
     `INSERT OR REPLACE` 整行重插，缺列会把旧值覆盖成 NULL（见 db.upsert_df 的契约注）；
  2. **筛选与去重**：预告只留两个净利润口径、公告日必须是**真实日历日**；解禁只留正股数；
  3. **方向语义**：董监高股数带符号 —— 全非负时才用变动比例的符号
     （写成 `shares * sign(shares)` 会把减持乘成正的，是实测踩过的坑）；
  4. **PK 列非空**：缺失的字符串列必须落成 `未标注`（NULL 会让整块 chunk 报 IntegrityError）。

运行: python -m unittest tests.test_event_client -v
"""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from app import db
from app.datasource import event_client as ec

MAPPING = {"000001": "000001.SZ", "600000": "600000.SH"}


def _forecast_raw(rows: list) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["股票代码", "公告日期", "预测指标", "预告类型",
                                       "业绩变动幅度", "预测数值"])


class TestColumnContract(unittest.TestCase):
    """归一化输出必须与建表 DDL 的列一一对应（多了少了都会静默写坏数据）。"""

    @classmethod
    def setUpClass(cls):
        cls.conn = sqlite3.connect(":memory:")
        cls.conn.executescript(db.SCHEMA)
        cls.cols = {t: [r[1] for r in cls.conn.execute(f"PRAGMA table_info({t})")]
                    for t in ("forecast", "restricted_release", "insider_trade")}

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_forecast_columns_match_ddl(self):
        df = ec.normalize_forecast(_forecast_raw(
            [["000001", "2024-04-30", "归属于上市公司股东的净利润", "预增", 50.0, 1e8]]),
            "20240630", MAPPING)
        self.assertEqual(sorted(df.columns), sorted(self.cols["forecast"]))

    def test_restricted_columns_match_ddl(self):
        raw = pd.DataFrame([["000001", "2024-06-30", "首发原股东限售股份", 1000.0]],
                           columns=["股票代码", "解禁时间", "限售股类型", "解禁数量"])
        df = ec.normalize_restricted(raw, MAPPING)
        self.assertEqual(sorted(df.columns), sorted(self.cols["restricted_release"]))

    def test_insider_columns_match_ddl(self):
        raw = pd.DataFrame([["000001", "2024-06-28", "张三", "竞价交易", -1000.0,
                             -0.001, 10.0, -10000.0]],
                           columns=["代码", "日期", "董监高人员姓名", "变动原因",
                                    "变动股数", "变动比例", "成交均价", "变动金额"])
        df = ec.normalize_insider(raw, MAPPING)
        self.assertEqual(sorted(df.columns), sorted(self.cols["insider_trade"]))


class TestCodeMapping(unittest.TestCase):
    def test_known_code_uses_mapping(self):
        self.assertEqual(ec._to_ts_code(1, MAPPING), "000001.SZ")       # 000001 → 补零后命中
        self.assertEqual(ec._to_ts_code("600000", MAPPING), "600000.SH")

    def test_unknown_code_infers_suffix_by_first_digit(self):
        cases = {("600001", ".SH"), ("900901", ".SH"), ("300001", ".SZ"), ("200001", ".SZ"),
                 ("430001", ".BJ"), ("830001", ".BJ"), ("000002", ".SZ")}
        for code, suffix in cases:
            self.assertEqual(ec._to_ts_code(code, {}), code + suffix)


class TestQuarterEnds(unittest.TestCase):
    def test_boundaries_are_inclusive(self):
        self.assertEqual(ec._quarter_ends("20240101", "20241231"),
                         ["20240331", "20240630", "20240930", "20241231"])
        self.assertEqual(ec._quarter_ends("20240331", "20240331"), ["20240331"])
        self.assertEqual(ec._quarter_ends("20240401", "20240629"), [])


class TestNormalizeForecast(unittest.TestCase):
    def test_metric_whitelist_and_date_normalization(self):
        df = ec.normalize_forecast(_forecast_raw([
            ["000001", "2024-04-30", "归属于上市公司股东的净利润", "预增", 50.0, 1e8],
            ["600000", "2024-04-29", "扣除非经常性损益后的净利润", "略增", 10.0, 2e8],
            ["000001", "2024-04-30", "营业收入", "预增", 30.0, 3e8],          # 非白名单 ⇒ 丢
        ]), "20240630", MAPPING)
        self.assertEqual(len(df), 2)
        self.assertEqual(list(df["ann_date"]), ["20240430", "20240429"])
        self.assertTrue((df["end_date"] == "20240630").all())

    def test_invalid_calendar_date_is_dropped(self):
        """2026-09-13 修：只查长度会放过 20241301/20240230（同 07 号 #19 那类脏日期）。"""
        df = ec.normalize_forecast(_forecast_raw([
            ["000001", "2024-04-30", "归属于上市公司股东的净利润", "预增", 50.0, 1e8],
            ["000001", "2024-13-01", "归属于上市公司股东的净利润", "预增", 50.0, 1e8],
            ["600000", "2024-02-30", "归属于上市公司股东的净利润", "预增", 50.0, 1e8],
            ["600000", "nan", "归属于上市公司股东的净利润", "预增", 50.0, 1e8],
        ]), "20240630", MAPPING)
        self.assertEqual(list(df["ann_date"]), ["20240430"])

    def test_dedup_keeps_last_and_missing_value_column_tolerated(self):
        raw = _forecast_raw([
            ["000001", "2024-04-30", "归属于上市公司股东的净利润", "预增", 10.0, 1e8],
            ["000001", "2024-04-30", "归属于上市公司股东的净利润", "预增", 60.0, 9e8],
        ]).drop(columns=["预测数值"])            # 上游缺列时用 raw.get ⇒ 该列全 NaN, 不该炸
        df = ec.normalize_forecast(raw, "20240630", MAPPING)
        self.assertEqual(len(df), 1)
        self.assertEqual(float(df["chg_mid"].iloc[0]), 60.0)
        self.assertTrue(pd.isna(df["profit_mid"].iloc[0]))

    def test_empty_input_returns_empty(self):
        self.assertTrue(ec.normalize_forecast(None, "20240630", MAPPING).empty)
        self.assertTrue(ec.normalize_forecast(pd.DataFrame(), "20240630", MAPPING).empty)


class TestNormalizeRestricted(unittest.TestCase):
    @staticmethod
    def _raw(rows):
        return pd.DataFrame(rows, columns=["股票代码", "解禁时间", "限售股类型", "解禁数量"])

    def test_only_positive_shares_with_valid_date_survive(self):
        df = ec.normalize_restricted(self._raw([
            ["000001", "2024-06-30", "首发原股东限售股份", 1000.0],
            ["000001", "2024-06-30", "首发原股东限售股份", 0.0],        # 零 ⇒ 丢
            ["600000", "2024-06-30", "定向增发机构配售股份", -5.0],      # 负 ⇒ 丢
            ["600000", "2024-06-30", "定向增发机构配售股份", None],      # 缺失 ⇒ 丢
            ["600000", "不是日期", "定向增发机构配售股份", 100.0],        # 脏日期 ⇒ 丢
        ]), MAPPING)
        self.assertEqual(len(df), 1)
        self.assertEqual(df["release_date"].iloc[0], "20240630")

    def test_dedup_on_pk(self):
        df = ec.normalize_restricted(self._raw([
            ["000001", "2024-06-30", "首发原股东限售股份", 1000.0],
            ["000001", "2024-06-30", "首发原股东限售股份", 2000.0],
        ]), MAPPING)
        self.assertEqual(len(df), 1)
        self.assertEqual(float(df["release_shares"].iloc[0]), 2000.0)


class TestNormalizeInsider(unittest.TestCase):
    @staticmethod
    def _raw(rows):
        return pd.DataFrame(rows, columns=["代码", "日期", "董监高人员姓名", "变动原因",
                                           "变动股数", "变动比例", "成交均价", "变动金额"])

    def test_negative_shares_keep_their_sign(self):
        """全列含负号 ⇒ 直接用股数符号（不能乘 sign(shares)，那会把减持变成增持）。"""
        df = ec.normalize_insider(self._raw([
            ["000001", "2024-06-28", "张三", "竞价交易", -1000.0, None, 10.0, -10000.0],
            ["000001", "2024-06-27", "李四", "竞价交易", 500.0, None, 10.0, 5000.0],
        ]), MAPPING)
        got = dict(zip(df["person"], df["delta_shares"]))
        self.assertEqual(got["张三"], -1000.0)
        self.assertEqual(got["李四"], 500.0)

    def test_all_nonnegative_shares_use_ratio_sign(self):
        """东财有时只给绝对值 ⇒ 用变动比例的符号定方向。"""
        df = ec.normalize_insider(self._raw([
            ["000001", "2024-06-28", "张三", "竞价交易", 1000.0, -0.001, 10.0, 10000.0],
            ["000001", "2024-06-27", "李四", "竞价交易", 800.0, 0.002, 10.0, 8000.0],
        ]), MAPPING)
        got = dict(zip(df["person"], df["delta_shares"]))
        self.assertEqual(got["张三"], -1000.0)
        self.assertEqual(got["李四"], 800.0)

    def test_unknown_direction_nan_ratio_is_dropped(self):
        """股数全非负 + 比例缺失 ⇒ delta 变 NaN ⇒ 该行被剔除（不默认成增持）。"""
        df = ec.normalize_insider(self._raw([
            ["000001", "2024-06-28", "张三", "竞价交易", 1000.0, None, 10.0, 10000.0],
        ]), MAPPING)
        self.assertTrue(df.empty)

    def test_zero_ratio_is_currently_treated_as_increase(self):
        """**钉住现状，不是认可**：比例恰好 0（或上游只给绝对值且比例取整为 0）时,
        `ratio.where(ratio != 0, 1.0)` 把符号定为 +1 ⇒ 记成增持。

        口径影响（2026-09-13 实测）：生产表 171,344 行里 **1,426 行**是
        `delta_shares > 0 且 ratio = 0` —— 方向**未证实**。这批行进了 F27 的输入,
        所以"改成丢弃"= 改事件口径 + 需重跑 F27, 属预注册范围（登记在 14 号待决事项）,
        不能顺手改。本测试的作用是：谁改了这个默认值，谁就会看到这条断言变红。
        """
        df = ec.normalize_insider(self._raw([
            ["000001", "2024-06-27", "李四", "竞价交易", 500.0, 0.0, 10.0, 5000.0],
        ]), MAPPING)
        self.assertEqual(len(df), 1)
        self.assertEqual(float(df["delta_shares"].iloc[0]), 500.0)

    def test_missing_pk_strings_become_placeholder_not_null(self):
        """PK 列 NOT NULL：缺失落成 `未标注`，否则整块 chunk 报 IntegrityError（实测踩过）。"""
        df = ec.normalize_insider(self._raw([
            ["000001", "2024-06-28", None, None, -1000.0, -0.001, 10.0, -10000.0],
        ]), MAPPING)
        self.assertEqual(len(df), 1)
        self.assertEqual(df["person"].iloc[0], "未标注")
        self.assertEqual(df["reason"].iloc[0], "未标注")
        self.assertTrue(df["delta_shares"].notna().all())


if __name__ == "__main__":
    unittest.main()
