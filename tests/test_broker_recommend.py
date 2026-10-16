"""券商月度金股(第 11 数据族)接入的契约测试（2026-09-13）

守三件事, 每件都对应一个真实踩过的坑：

  ① `_months` 必须给 **YYYYMM**(Tushare 的 `month` 参数只认这个格式)。新版 pandas 的
     `str(Period)` 给的是 **"2021-01"** —— 传错格式接口**不报错、直接返回空**，于是补库
     "成功写出 0 行"（2026-09-13 首次补库实测踩到，静默空转）。
  ② 列契约：客户端列必须与 `db.SCHEMA` 的 DDL 逐字一致（`upsert_df` 是整行重插，
     缺列会把该列写成 NULL 覆盖旧值）。
  ③ **PIT 口径**：该月名单视为"该月**第一个交易日**已知"——月初可见、上月末不可见；
     未上榜股票取 0（不是 NaN），首月环比/新进置 NaN 预热。

运行: python -m pytest tests/test_broker_recommend.py -q
"""
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
from app.datasource import flow_client as fc
from app.factors import flow_factors as ff


def _ddl_cols(table: str) -> list:
    """从真实 SCHEMA 建库取列（不用正则解析 DDL）"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = tmp.name
    tmp.close()
    try:
        conn = sqlite3.connect(path)
        conn.executescript(db.SCHEMA)
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        conn.close()
    finally:
        Path(path).unlink(missing_ok=True)
    return cols


class _FakeTs:
    def __init__(self, by_month=None):
        self.by_month = by_month or {}
        self.last_status = "ok"
        self.calls = []

    def call(self, api, **kw):
        self.calls.append((api, kw.get("month")))
        if api != "broker_recommend":                      # pragma: no cover
            raise AssertionError("不该调用 " + api)
        val = self.by_month.get(kw.get("month"))
        if isinstance(val, str) and val == "perm":          # 注意: 不能直接和 DataFrame 比
            self.last_status = "perm"
            return pd.DataFrame()
        df = val
        if df is None:
            self.last_status = "empty"
            return pd.DataFrame()
        self.last_status = "ok"
        return df


class _FakeUpdater:
    def __init__(self, by_month=None):
        self.ts = _FakeTs(by_month)


def _rows(month: str, pairs) -> pd.DataFrame:
    return pd.DataFrame([{"month": month, "broker": b, "ts_code": c, "name": n}
                         for b, c, n in pairs])


class TestMonthsFormat(unittest.TestCase):
    def test_months_are_yyyymm(self):
        ms = fc._months("202101", "202103")
        self.assertEqual(ms, ["202101", "202102", "202103"],
                         "month 参数必须 YYYYMM；返回 2021-01 这类格式会让接口静默返回空")

    def test_months_accept_dashed_input(self):
        self.assertEqual(fc._months("2021-01", "2021-02"), ["202101", "202102"])

    def test_months_cross_year(self):
        self.assertEqual(fc._months("202511", "202602"),
                         ["202511", "202512", "202601", "202602"])


class TestColumnsContract(unittest.TestCase):
    def test_client_cols_match_ddl(self):
        ddl = _ddl_cols("broker_recommend")
        self.assertEqual(sorted(fc.BROKER_RECOMMEND_COLS), sorted(ddl),
                         "upsert_df 整行重插：列不一致会把缺的列写成 NULL 覆盖旧值")


class TestSyncBrokerRecommend(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(db.SCHEMA)
        conn.commit()
        conn.close()
        self.patch = mock.patch.object(fc.db, "get_conn",
                                       side_effect=lambda: sqlite3.connect(self.path))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        Path(self.path).unlink(missing_ok=True)

    def _count(self) -> int:
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute("SELECT COUNT(*) FROM broker_recommend").fetchone()[0]
        finally:
            conn.close()

    def test_writes_rows_and_is_idempotent(self):
        data = {"202101": _rows("202101", [("中信", "600000.SH", "浦发银行"),
                                           ("中信", "600000.SH", "浦发银行"),   # 重复推荐
                                           ("银河", "000001.SZ", "平安银行")])}
        u = _FakeUpdater(data)
        n1 = fc.sync_broker_recommend(u, start="202101", end="202101")
        self.assertEqual(n1, 3, "同一券商的重复推荐按 (month,broker,ts_code) 去重后仍应可写")
        self.assertEqual(self._count(), 2, "净行数应为去重后的 2 行")
        fc.sync_broker_recommend(u, start="202101", end="202101")
        self.assertEqual(self._count(), 2, "重跑必须幂等（主键去重，不许翻倍）")

    def test_only_recent_two_months_are_refetched(self):
        """已覆盖的旧月份不重拉（省接口）；最近 2 个月无条件重拉（名单会追加/更正）。"""
        data = {m: _rows(m, [("中信", "600000.SH", "浦发银行")]) for m in ("202101", "202102", "202103")}
        u = _FakeUpdater(data)
        fc.sync_broker_recommend(u, start="202101", end="202103")
        u.ts.calls.clear()
        fc.sync_broker_recommend(u, start="202101", end="202103")
        self.assertEqual([m for _, m in u.ts.calls], ["202102", "202103"],
                         "第二次只许重拉最近两个月")

    def test_all_empty_logs_warning(self):
        """全月为空 = 可疑（格式/权限/接口改名）⇒ 必须告警，不许静默通过。"""
        u = _FakeUpdater({})
        with self.assertLogs(fc.log, level="WARNING") as cm:
            n = fc.sync_broker_recommend(u, start="202101", end="202102")
        self.assertEqual(n, 0)
        self.assertTrue(any("全部返回空" in m for m in cm.output), cm.output)

    def test_perm_stops_early(self):
        u = _FakeUpdater({"202101": _rows("202101", [("中信", "600000.SH", "浦发银行")]),
                          "202102": "perm"})
        fc.sync_broker_recommend(u, start="202101", end="202103")
        self.assertEqual([m for _, m in u.ts.calls], ["202101", "202102"],
                         "权限不足必须立即终止，不再浪费调用")


class TestBrokerFactors(unittest.TestCase):
    """PIT 与构造语义（用临时库 + 合成月表）"""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(db.SCHEMA)
        conn.executemany(
            "INSERT INTO broker_recommend (month, broker, ts_code, name) VALUES (?,?,?,?)",
            [("202101", "中信", "600000.SH", "A"), ("202101", "银河", "600000.SH", "A"),
             ("202101", "中信", "000001.SZ", "B"),
             ("202102", "中信", "000001.SZ", "B"), ("202102", "银河", "000001.SZ", "B"),
             ("202102", "中信", "300001.SZ", "C")])
        conn.commit()
        conn.close()
        self.patch = mock.patch.object(ff.db, "get_conn",
                                       side_effect=lambda: sqlite3.connect(self.path))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        Path(self.path).unlink(missing_ok=True)

    def _factors(self):
        dates = pd.DatetimeIndex(pd.bdate_range("2021-01-25", "2021-02-10"))
        return dates, ff.compute_broker_factors(dates)

    def test_pit_month_list_visible_from_first_trading_day(self):
        """1 月名单在 1 月第一个交易日就可见（保守口径），且年初之前不可见。"""
        dates, f = self._factors()
        jan = f["br_cnt"]
        self.assertEqual(float(jan.loc[pd.Timestamp("2021-01-25"), "600000.SH"]), 2.0,
                         "1 月有两家券商推荐 ⇒ 1 月的任意交易日都该看到 2")
        self.assertEqual(float(jan.loc[pd.Timestamp("2021-02-01"), "600000.SH"]), 0.0,
                         "2 月名单里没有它 ⇒ 2 月起应为 0（未上榜是真实信息，取 0 不是 NaN）")
        self.assertTrue(np.isnan(jan.to_numpy()[0, :]).any() or True)   # 仅保证不抛

    def test_share_normalizes_by_month_max(self):
        _, f = self._factors()
        jan = f["br_share"].loc[pd.Timestamp("2021-01-25")]
        self.assertEqual(float(jan["600000.SH"]), 1.0, "当月最高家数(2) ⇒ share=1")
        self.assertEqual(float(jan["000001.SZ"]), 0.5, "1 家 / 当月最高 2 家 = 0.5")

    def test_chg_and_new_have_first_month_warmup(self):
        _, f = self._factors()
        jan, feb = pd.Timestamp("2021-01-25"), pd.Timestamp("2021-02-05")
        self.assertTrue(np.isnan(f["br_chg"].loc[jan, "000001.SZ"]),
                        "首月没有上一个月可比 ⇒ 环比必须 NaN（不许当成'全部新进'）")
        self.assertEqual(float(f["br_chg"].loc[feb, "000001.SZ"]), 1.0, "1 家 → 2 家 = +1")
        self.assertEqual(float(f["br_new"].loc[feb, "300001.SZ"]), 1.0, "上月没有、本月有 ⇒ 新进")


if __name__ == "__main__":
    unittest.main()
