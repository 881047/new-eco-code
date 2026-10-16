"""东财研报采集（第 12 族的主力源）的契约测试（2026-09-13）

换源原因：Tushare `report_rc` 实测 **10 次/天**（全史 8 天多）、AkShare 逐只封装 4.6 秒/只
（5900 只 ≈ 8 小时）；东财 `reportapi` 支持 **`code=*` + 日期区间**（一个月 19 页 ≈ 45 秒），
且字段更全（评级/上次评级/评级变动/三年 EPS·PE/目标价/infoCode）。

这里守四件事：
  ① **列契约**：脚本列 == `db.SCHEMA` 的 DDL（`upsert_df` 整行重插）；
  ② **年份标签**：EPS/PE 是"今年/明年/后年"⇒ 按位置存 `fy1..fy3`，把 `currentYear` 写进
     `fy_years`（否则明年重跑会对着错的年份读）；
  ③ 代码后缀映射（60/68→SH、00/30→SZ、83→BJ）、缺 `infoCode`/日期解析失败的行丢弃；
  ④ 月份切片与边界（拉数用的区间参数不能串月）。

运行: python -m pytest tests/test_research_report_backfill.py -q
"""
import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import db                                                   # noqa: E402

SCRIPT = BASE / "scripts" / "oneoff" / "backfill_research_report.py"


def _load_mod():
    spec = importlib.util.spec_from_file_location("_backfill_research_report", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _raw(n: int = 2) -> list:
    """模拟东财 API 的 data 数组（字段名与真实接口一致）"""
    recs = []
    for i in range(n):
        recs.append({"title": f"点评{i}", "stockName": "X",
                     "stockCode": "600000" if i % 2 == 0 else "300001",
                     "orgName": "国信证券股份有限公司", "orgSName": "国信证券",
                     "publishDate": f"2026-08-2{i} 00:00:00.000", "infoCode": f"AP{i}",
                     "predictThisYearEps": "1.47", "predictThisYearPe": "6.2",
                     "predictNextYearEps": "1.56", "predictNextYearPe": "5.9",
                     "predictNextTwoYearEps": "1.66", "predictNextTwoYearPe": "5.5",
                     "industryName": "银行Ⅱ", "emRatingName": "买入",
                     "lastEmRatingName": "增持", "ratingChange": "1",
                     "indvAimPriceT": "12.5", "indvAimPriceL": "9.0",
                     "reportType": "个股研报", "author": "张三",
                     "encodeUrl": f"H3_AP{i}_1"})
    return recs


class TestMapFrame(unittest.TestCase):
    def setUp(self):
        self.mod = _load_mod()

    def test_cols_match_ddl(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        path = tmp.name
        tmp.close()
        try:
            conn = sqlite3.connect(path)
            conn.executescript(db.SCHEMA)
            ddl = [r[1] for r in conn.execute("PRAGMA table_info(research_report_em)")]
            conn.close()
        finally:
            Path(path).unlink(missing_ok=True)
        self.assertEqual(sorted(self.mod.COLS), sorted(ddl),
                         "upsert_df 整行重插：脚本列与 DDL 不一致会静默写坏列")

    def test_maps_fields_and_labels_years(self):
        out = self.mod.map_records(_raw(), 2026)
        self.assertEqual(len(out), 2)
        self.assertEqual(out["fy_years"].iloc[0], "2026,2027,2028",
                         "年份标签必须落盘（今年/明年/后年的含义随年份漂）")
        self.assertAlmostEqual(float(out["fy1_eps"].iloc[0]), 1.47)
        self.assertAlmostEqual(float(out["fy3_pe"].iloc[0]), 5.5)
        self.assertEqual(out["report_date"].iloc[0], "20260820")
        self.assertEqual(out["rating"].iloc[0], "买入")
        self.assertEqual(out["rating_prev"].iloc[0], "增持")
        self.assertAlmostEqual(float(out["aim_price_t"].iloc[0]), 12.5)
        self.assertEqual(out["org_name"].iloc[0], "国信证券")

    def test_ts_code_suffix_and_bad_date(self):
        self.assertEqual(self.mod._to_ts_code("600000"), "600000.SH")
        self.assertEqual(self.mod._to_ts_code("000001"), "000001.SZ")
        self.assertEqual(self.mod._to_ts_code("300001"), "300001.SZ")
        self.assertEqual(self.mod._to_ts_code("830001"), "830001.BJ")
        bad = _raw(1)
        bad[0]["publishDate"] = "不是日期"
        self.assertEqual(len(self.mod.map_records(bad, 2026)), 0,
                         "解析不出日期 ⇒ 丢弃该行，不许瞎猜")
        bad2 = _raw(1)
        bad2[0]["infoCode"] = ""
        self.assertEqual(len(self.mod.map_records(bad2, 2026)), 0,
                         "缺 infoCode（主键）⇒ 丢弃")

    def test_empty_input(self):
        self.assertTrue(self.mod.map_records([], 2026).empty)
        self.assertTrue(self.mod.map_records(None, 2026).empty)

    def test_list_valued_author_is_flattened(self):
        """东财 `author` 有时返回**列表** ⇒ 必须拍平，否则 sqlite 绑不了、整夜任务白跑。"""
        raw = _raw(1)
        raw[0]["author"] = ["张三", "李四"]
        out = self.mod.map_records(raw, 2026)
        self.assertEqual(out["author"].iloc[0], "张三、李四")
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (author TEXT)")
        conn.executemany("INSERT INTO t VALUES (?)",
                         [tuple(r) for r in self.mod._sqlite_safe(out[["author"]])
                          .itertuples(index=False, name=None)])
        self.assertEqual(conn.execute("SELECT author FROM t").fetchone()[0], "张三、李四")
        conn.close()

    def test_sqlite_safe_binds(self):
        out = self.mod._sqlite_safe(self.mod.map_records(_raw(), 2026))
        conn = sqlite3.connect(":memory:")
        conn.execute(f"CREATE TABLE t ({', '.join(c + ' ' + ('TEXT' if c in ('ts_code','name','report_date','report_title','org_name','rating','industry','fy_years','pdf') else 'REAL') for c in self.mod.COLS)})")
        conn.executemany(f"INSERT INTO t VALUES ({','.join('?' * len(self.mod.COLS))})",
                         [tuple(r) for r in out.itertuples(index=False, name=None)])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 2)
        conn.close()

    def test_month_slicing_and_bounds(self):
        ms = self.mod._months("2026-07", "2026-09")
        self.assertEqual(ms, ["2026-07", "2026-08", "2026-09"])
        self.assertEqual(self.mod._month_bounds("2026-02"), ("2026-02-01", "2026-02-28"))
        self.assertEqual(self.mod._month_bounds("2024-02"), ("2024-02-01", "2024-02-29"),
                         "闰年边界不许串月")


if __name__ == "__main__":
    unittest.main()
