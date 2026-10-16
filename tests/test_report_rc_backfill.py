"""`report_rc`(分析师研报预测, 第 12 数据族)补库脚本的契约测试（2026-09-13）

第 12 族的补库是本项目**最贵的一次数据获取**（接口 1 次/分钟 + 10 次/小时 ⇒ 全史约 9 小时），
所以"断点续跑"必须是对的：跑一半崩掉、重启后从头再来 = 白烧整夜配额。这里守四件事：

  ① 列契约：脚本的目标列必须与 `db.SCHEMA` 的 DDL 逐字一致（`upsert_df` 整行重插）；
  ② 分页：一页撞到 5000 行上限 ⇒ 记 `next_offset` 继续；返回 < 5000 ⇒ 该月完成；
  ③ 断点：检查点里已完成月份**不许重拉**，未完成月份必须**从记录的 offset 续跑**；
  ④ `_sqlite_safe`：`create_time` 是 datetime，sqlite3 绑不了 Timestamp ⇒ 必须转字符串。

运行: python -m pytest tests/test_report_rc_backfill.py -q
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import db                                                 # noqa: E402

SCRIPT = BASE / "scripts" / "oneoff" / "backfill_report_rc.py"


def _load_mod():
    """按**文件路径**加载（oneoff 卫生测试禁止把冻结脚本当裸模块 import）"""
    spec = importlib.util.spec_from_file_location("_backfill_report_rc", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rows(n: int, month: str, offset: int) -> pd.DataFrame:
    """造 n 行合法数据（主键四列必须唯一，否则 INSERT OR REPLACE 会吃掉重复行）"""
    recs = []
    for i in range(n):
        k = offset + i
        recs.append({"ts_code": f"{k:06d}.SZ", "name": "X", "report_date": f"{month}15",
                     "report_title": f"t{k}", "report_type": "个股研报", "classify": "一般",
                     "org_name": "某券商", "author_name": "某人", "quarter": "2026Q2",
                     "op_rt": 1.0, "op_pr": 2.0, "tp": 3.0, "np": 4.0, "eps": 0.5,
                     "pe": 20.0, "rd": 1.0, "roe": 0.1, "ev_ebitda": 8.0, "rating": "买入",
                     "max_price": 30.0, "min_price": 20.0, "imp_dg": "",
                     "create_time": pd.Timestamp("2026-07-15 09:00:00")})
    return pd.DataFrame(recs)


class _FakePro:
    """按 (start_date 的月份, offset) 返回分页数据；并把调用记下来供断言"""

    def __init__(self, pages: dict):
        self.pages = pages          # {"2026-01": {0: df(5000), 5000: df(1200)}, ...}
        self.calls = []

    def query(self, api, **kw):
        assert api == "report_rc"
        m = str(kw["start_date"])[:4] + "-" + str(kw["start_date"])[4:6]
        self.calls.append((m, int(kw["offset"])))
        return self.pages.get(m, {}).get(int(kw["offset"]), pd.DataFrame(columns=_load_mod().REPORT_RC_COLS))


class TestReportRcBackfill(unittest.TestCase):
    def setUp(self):
        # 护栏要求显式确认；测试里显式给出（证明"默认拒绝、显式放行"这条链是通的）
        self._old_confirm = os.environ.get("ONEOFF_CONFIRM")
        os.environ["ONEOFF_CONFIRM"] = "ALL"
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.db_path)
        conn.executescript(db.SCHEMA)
        conn.commit()
        conn.close()
        self.state = Path(tempfile.mkdtemp(prefix="rc_state_")) / "state.json"
        self.mod = _load_mod()
        self.mod.MIN_INTERVAL = 0.0            # 测试不睡
        self.mod.STATE = self.state
        self.mod.START_MONTH = "2026-01"
        self._patches = [
            mock.patch.object(self.mod.db, "get_conn",
                              side_effect=lambda: sqlite3.connect(self.db_path)),
            mock.patch.object(self.mod.db, "conn_ctx", self._conn_ctx()),
            mock.patch("tushare.pro_api", side_effect=lambda token: self.pro),
            # 固定"今天"⇒ 月份集合确定（默认会随真实日期漂移，断言就不稳）
            mock.patch("pandas.Timestamp.today",
                       return_value=pd.Timestamp("2026-02-15 10:00:00")),
        ]
        for p in self._patches:
            p.start()

    def _conn_ctx(self):
        @contextmanager
        def _cm():
            c = sqlite3.connect(self.db_path)
            try:
                yield c
            finally:
                c.close()
        return _cm

    def tearDown(self):
        for p in self._patches:
            p.stop()
        if self._old_confirm is None:
            os.environ.pop("ONEOFF_CONFIRM", None)
        else:
            os.environ["ONEOFF_CONFIRM"] = self._old_confirm
        Path(self.db_path).unlink(missing_ok=True)
        self.state.unlink(missing_ok=True)
        # 2026-09-24 补：`self.state` 是 `mkdtemp(prefix="rc_state_")` **里面的** state.json
        # ⇒ 只 unlink 那个文件，**父目录永远留着**。本机 `%TEMP%` 因此累积了 1336 个
        # `rc_state_*`（07 号 #64 同族）。要删的是目录，不是文件。
        import shutil
        shutil.rmtree(self.state.parent, ignore_errors=True)

    def _count(self) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM report_rc").fetchone()[0]
        finally:
            conn.close()

    def test_cols_match_ddl(self):
        conn = sqlite3.connect(self.db_path)
        try:
            ddl = [r[1] for r in conn.execute("PRAGMA table_info(report_rc)")]
        finally:
            conn.close()
        self.assertEqual(sorted(self.mod.REPORT_RC_COLS), sorted(ddl),
                         "upsert_df 整行重插：脚本列与 DDL 不一致会静默写坏列")

    def test_pagination_and_completion(self):
        """一月两页(5000+1200) + 二月一页(300) ⇒ 878/1200 行落库、检查点记两个完成月"""
        self.pro = _FakePro({"2026-01": {0: _rows(5000, "2026-01", 0),
                                         5000: _rows(1200, "2026-01", 5000)},
                             "2026-02": {0: _rows(300, "2026-02", 0)}})
        rc = self.mod.main()
        self.assertEqual(rc, 0)
        self.assertEqual(self.pro.calls, [("2026-01", 0), ("2026-01", 5000), ("2026-02", 0)],
                         "撞上限必须翻页，且不许重复请求")
        self.assertEqual(self._count(), 6500)
        st = self.mod._load_state()
        self.assertEqual(st["done"], ["2026-01", "2026-02"])
        self.assertEqual(st.get("next_offset") or {}, {})
        self.assertEqual(st.get("empty") or [], [], "这两个月都有数据 ⇒ 不该进 empty 名单")

    def test_resume_skips_done_and_continues_from_offset(self):
        """检查点：已完成月不重拉；未完成月从记录的 offset 续跑"""
        self.state.write_text(
            '{"last_call_ts": 0, "next_offset": {"2026-02": 5000}, "done": ["2026-01"]}',
            encoding="utf-8")
        self.pro = _FakePro({"2026-02": {5000: _rows(40, "2026-02", 5000)}})
        self.mod.main()
        self.assertEqual(self.pro.calls, [("2026-02", 5000)],
                         "已完成的 2026-01 不许再拉；2026-02 必须从 offset=5000 续")
        self.assertIn("2026-02", self.mod._load_state()["done"])

    def test_empty_month_is_recorded_not_silently_ok(self):
        """整月空页 ⇒ 进 `empty` 名单留痕（真实数据里每月都有研报，空页意味着有问题）"""
        self.pro = _FakePro({})
        self.mod.main()
        st = self.mod._load_state()
        self.assertEqual(st.get("empty"), ["2026-01", "2026-02"])
        self.assertEqual(st["done"], ["2026-01", "2026-02"], "仍要标完成，否则每轮都重拉")

    def test_sqlite_safe_converts_timestamp(self):
        df = pd.DataFrame({"a": [pd.Timestamp("2026-07-15 09:00:00"), None],
                           "b": [1.5, None]})
        out = self.mod._sqlite_safe(df)
        self.assertEqual(out["a"].iloc[0], "2026-07-15 09:00:00")
        self.assertTrue(pd.isna(out["a"].iloc[1]))
        conn = sqlite3.connect(self.db_path)
        try:                                   # 真绑一次：不转的话这里会抛 binding 错误
            conn.execute("CREATE TABLE t (a TEXT, b REAL)")
            conn.executemany("INSERT INTO t VALUES (?,?)",
                             [tuple(r) for r in out.where(out.notna(), None).itertuples(index=False, name=None)])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
