"""事件类数据源单测（业绩预告，2026-09-13）

守四件事，都是**静默数据损坏**类的坑：
  1. 列契约：写库的目标列必须与 `db.SCHEMA` 的 DDL **逐字一致**（`upsert_df` 是整行重插，
     少一列就把该列写成 NULL 覆盖旧值 —— 与 `test_flow_sources` 同一条理由）；
  2. 代码映射：东财返回的是 6 位纯数字，必须映射成带交易所后缀的 `ts_code`；
  3. 报告期枚举：季度末的生成与边界（含 start/end 当天）；
  4. **PIT 窗口**：因子面板只能用 `ann_date ≤ 锚点` 的记录，且超过保鲜期的记录不参与 ——
     这条错了就是前视，回测会凭空变好。

运行: ./.venv/Scripts/python.exe -X utf8 -m pytest tests/test_event_sources.py -q
"""
import sqlite3
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from app import db
from app.datasource import event_client as ec

# 因子构造（保鲜期/窗口/优先级）属于**研究口径**, 冻结在预注册脚本里;
# 这里按"研究/生产各管一段"的方式加载它, 只测 PIT 窗口这一条硬要求。
_F26 = Path(__file__).resolve().parent.parent / "experiments" / "05_label" \
    / "F26_forecast_weekly_ic.py"
_spec = importlib.util.spec_from_file_location("f26_for_pit_test", _F26)
f26 = importlib.util.module_from_spec(_spec)
sys.modules["f26_for_pit_test"] = f26
_spec.loader.exec_module(f26)


def _ddl_cols(table: str) -> list:
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


class TestForecastContract(unittest.TestCase):
    def test_written_columns_match_ddl(self):
        raw = pd.DataFrame({"股票代码": ["600000"], "公告日期": ["2026-01-20"],
                            "预测指标": ["归属于上市公司股东的净利润"], "预告类型": ["预增"],
                            "业绩变动幅度": [50.0], "预测数值": [1e8]})
        df = ec.normalize_forecast(raw, "20251231", {})
        self.assertEqual(sorted(df.columns), sorted(_ddl_cols("forecast")))

    def test_code_mapping_and_fallback(self):
        raw = pd.DataFrame({"股票代码": ["600000", "000001", "920188", "123456"],
                            "公告日期": ["2026-01-20"] * 4,
                            "预测指标": ["归属于上市公司股东的净利润"] * 4,
                            "预告类型": ["预增"] * 4,
                            "业绩变动幅度": [50.0] * 4, "预测数值": [1e8] * 4})
        df = ec.normalize_forecast(raw, "20251231", {"600000": "600000.SH"})
        self.assertEqual(set(df["ts_code"]),
                         {"600000.SH", "000001.SZ", "920188.SH", "123456.SH"})

    def test_keeps_only_main_metrics_and_drops_bad_dates(self):
        raw = pd.DataFrame({"股票代码": ["600000"] * 3, "公告日期": ["2026-01-20", "2026-01-20", "bad"],
                            "预测指标": ["归属于上市公司股东的净利润", "每股收益", "归属于上市公司股东的净利润"],
                            "预告类型": ["预增"] * 3, "业绩变动幅度": [50.0] * 3,
                            "预测数值": [1e8] * 3})
        df = ec.normalize_forecast(raw, "20251231", {})
        self.assertEqual(len(df), 1)
        self.assertEqual(df["ann_date"].iloc[0], "20260120")

    def test_quarter_ends_boundaries(self):
        self.assertEqual(ec._quarter_ends("20130101", "20131231"),
                         ["20130331", "20130630", "20130930", "20131231"])
        self.assertEqual(ec._quarter_ends("20130401", "20130630"), ["20130630"])
        self.assertEqual(ec._quarter_ends("20131231", "20131231"), ["20131231"])


class TestForecastIsPointInTime(unittest.TestCase):
    """最要命的一条：锚点之后才公告的预告, 绝不允许出现在该锚点的面板里。"""

    RAW = pd.DataFrame({
        "ts_code": ["600000.SH", "600000.SH", "000001.SZ"],
        "end_date": ["20250331", "20250630", "20250331"],
        "ann_date": ["20250120", "20250715", "20230601"],       # 第三条远超保鲜期
        "metric": ["归属于上市公司股东的净利润"] * 3,
        "fc_type": ["预增", "首亏", "略增"],
        "chg_mid": [50.0, -30.0, 20.0],
    })

    def _panels(self, anchors):
        cols = ["600000.SH", "000001.SZ"]
        with mock.patch("pandas.read_sql", return_value=self.RAW):
            return f26.build_panels(None, pd.to_datetime(anchors), cols)

    def test_future_announcement_is_invisible(self):
        before = self._panels(["2025-06-30"])["fc_type"].iloc[0, 0]      # 7/15 才公告
        after = self._panels(["2025-08-01"])["fc_type"].iloc[0, 0]
        self.assertTrue(np.isfinite(before) and before != -2.0, "7/15 的预告不能出现在 6/30")
        self.assertEqual(after, -2.0, "8/1 应该看到 7/15 的首亏预告")

    def test_stale_records_expire(self):
        p = self._panels(["2026-09-11"])
        self.assertTrue(np.isnan(p["fc_type"].iloc[0, 1]), "2023-06 的预告早过保鲜期")

    def test_cover_flag_only_marks_recent(self):
        # 全市场 0/1: 2025-01-20 的预告在 2025-03-31 的 90 天窗内 ⇒ 1.0;
        # 不在窗内 ⇒ 0.0(不是 NaN —— 留 NaN 会让该列零方差、被静默跳过)
        p = self._panels(["2025-03-31", "2025-06-30", "2026-09-11"])
        self.assertEqual(p["fc_cover90"].iloc[0, 0], 1.0)
        self.assertEqual(p["fc_type"].iloc[0, 0], 2.0)
        self.assertEqual(p["fc_cover90"].iloc[1, 0], 0.0)
        self.assertEqual(p["fc_cover90"].iloc[2, 0], 0.0)


class TestReleaseAndInsiderContracts(unittest.TestCase):
    """解禁 / 董监高：列契约 + 只留日程与股数（不落任何未来字段）。"""

    def test_release_columns_match_ddl(self):
        raw = pd.DataFrame({"股票代码": ["600000"], "解禁时间": ["2026-05-20"],
                            "限售股类型": ["首发原股东限售股份"], "解禁数量": [1e6],
                            "实际解禁数量": [9e5], "实际解禁市值": [1e8],
                            "占解禁前流通市值比例": [0.03],
                            "解禁前一交易日收盘价": [10.0],
                            "解禁前20日涨跌幅": [-5.0], "解禁后20日涨跌幅": [7.0]})
        df = ec.normalize_restricted(raw, {})
        self.assertEqual(sorted(df.columns), sorted(_ddl_cols("restricted_release")))
        self.assertEqual(df["release_date"].iloc[0], "20260520")
        self.assertEqual(df["release_shares"].iloc[0], 1e6)
        # 未来涨跌幅这类字段**不得**出现在入库形状里
        for bad in ("解禁后20日涨跌幅", "实际解禁市值", "占解禁前流通市值比例"):
            self.assertNotIn(bad, df.columns)

    def test_release_drops_nonpositive_and_bad_rows(self):
        raw = pd.DataFrame({"股票代码": ["600000", "600000", "000001"],
                            "解禁时间": ["2026-05-20", "bad", "2026-06-01"],
                            "限售股类型": ["A", "B", "C"],
                            "解禁数量": [1e6, 2e6, 0.0]})
        df = ec.normalize_restricted(raw, {})
        self.assertEqual(len(df), 1)
        self.assertEqual(df["ts_code"].iloc[0], "600000.SH")

    def test_insider_columns_and_sign(self):
        raw = pd.DataFrame({"日期": ["2026-05-20", "2026-05-21"],
                            "代码": ["600000", "000001"],
                            "变动人": ["张三", "李四"],
                            "变动股数": [-50000.0, 30000.0],
                            "成交均价": [10.0, 8.0],
                            "变动金额": [-500000.0, 240000.0],
                            "变动原因": ["竞价交易", "竞价交易"],
                            "变动比例": [-0.001, 0.002],
                            "变动后持股数": [1e5, 2e5],
                            "持股种类": ["A股", "A股"],
                            "董监高人员姓名": ["张三", "李四"],
                            "职务": ["董事", "监事"],
                            "变动人与董监高的关系": ["本人", "本人"],
                            "开始时持有": [1.5e5, 1.7e5],
                            "结束后持有": [1e5, 2e5]})
        df = ec.normalize_insider(raw, {})
        self.assertEqual(sorted(df.columns), sorted(_ddl_cols("insider_trade")))
        self.assertLess(df.loc[df["ts_code"] == "600000.SH", "delta_shares"].iloc[0], 0,
                        "减持必须保持负号")
        self.assertGreater(df.loc[df["ts_code"] == "000001.SZ", "delta_shares"].iloc[0], 0)
        self.assertEqual(df["change_date"].tolist(), ["20260520", "20260521"])

    def test_insider_absolute_shares_uses_ratio_sign(self):
        """东财有时只给"变动股数"的绝对值 ⇒ 方向必须取自"变动比例"的符号。"""
        raw = pd.DataFrame({"日期": ["2026-05-20", "2026-05-21"],
                            "代码": ["600000", "000001"],
                            "变动人": ["张三", "李四"],
                            "变动股数": [50000.0, 30000.0],      # 全为非负 ⇒ 无方向
                            "成交均价": [10.0, 8.0],
                            "变动金额": [500000.0, 240000.0],
                            "变动原因": ["竞价交易", "竞价交易"],
                            "变动比例": [-0.001, 0.002],
                            "变动后持股数": [1e5, 2e5], "持股种类": ["A股", "A股"],
                            "董监高人员姓名": ["张三", "李四"], "职务": ["董事", "监事"],
                            "变动人与董监高的关系": ["本人", "本人"],
                            "开始时持有": [1.5e5, 1.7e5], "结束后持有": [1e5, 2e5]})
        df = ec.normalize_insider(raw, {}).set_index("ts_code")
        self.assertLess(df.loc["600000.SH", "delta_shares"], 0)
        self.assertGreater(df.loc["000001.SZ", "delta_shares"], 0)


class TestRepurchaseAndSeoContracts(unittest.TestCase):
    """第 9、10 族（回购 / 增发）：列契约 + **只存计划侧字段**（累计已回购是前视）。"""

    def test_repurchase_columns_match_ddl_and_drop_current_values(self):
        raw = pd.DataFrame({"股票代码": ["000034"], "回购起始时间": ["2026-08-17"],
                            "计划回购金额区间-下限": [3.1e7], "计划回购金额区间-上限": [3.1e7],
                            "实施进度": ["实施中"],
                            # 下面两个是**当前快照的累计值** ⇒ 绝不允许入库(前视)
                            "已回购股份数量": [1378200.0], "已回购金额": [3.1e7],
                            "最新公告日期": ["2026-09-10"]})
        df = ec.normalize_repurchase(raw, {})
        self.assertEqual(sorted(df.columns), sorted(_ddl_cols("repurchase")))
        self.assertEqual(df["start_date"].iloc[0], "20260817")
        self.assertAlmostEqual(df["amount_cap"].iloc[0], 3.1e7)
        for bad in ("已回购金额", "已回购股份数量", "最新公告日期"):
            self.assertNotIn(bad, df.columns)
        # 2026-09-16 数据线审查 DATA-08: progress(实施进度)同性质前视, 一并移除
        self.assertNotIn("progress", df.columns)

    def test_seo_columns_match_ddl(self):
        raw = pd.DataFrame({"股票代码": ["688347"], "发行方式": ["定向增发"],
                            "发行总数": [190768392.0], "发行价格": [43.34],
                            "发行日期": ["2026-09-03"], "增发上市日期": ["2026-09-09"]})
        df = ec.normalize_seo(raw, {})
        self.assertEqual(sorted(df.columns), sorted(_ddl_cols("seo_issue")))
        self.assertEqual(df["issue_date"].iloc[0], "20260903")
        self.assertEqual(df["method"].iloc[0], "定向增发")


if __name__ == "__main__":
    unittest.main()
