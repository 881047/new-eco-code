"""十大流通股东（第 13 数据族）补库的契约测试（2026-09-13）

守四件事：
  ① 列契约：脚本列 == `db.SCHEMA` 的 DDL（`upsert_df` 整行重插，缺列会写 NULL 覆盖旧值）；
  ② **PIT 两列都在**：`ann_date`（公告日，因子必须用它）与 `end_date`（报告期）都要存；
  ③ 分页：一页撞 6000 行上限 ⇒ 记 `next_offset` 继续；返回 < 6000 ⇒ 该期完成；
  ④ 幂等 + 断点：重跑不翻倍；检查点里已完成的报告期不重拉，未完成的从 offset 续。

运行: python -m pytest tests/test_top10_holder_backfill.py -q
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

from app import db                                                   # noqa: E402

SCRIPT = BASE / "scripts" / "oneoff" / "backfill_top10_holder.py"


def _load_mod():
    spec = importlib.util.spec_from_file_location("_backfill_top10", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rows(n: int, period: str, offset: int) -> pd.DataFrame:
    return pd.DataFrame([{
        "ts_code": f"{offset + i:06d}.SZ", "ann_date": "20260901", "end_date": period,
        "holder_name": f"股东{offset + i}", "hold_amount": 1.0e7, "hold_ratio": 1.0,
        "hold_float_ratio": 2.0, "hold_change": 0.0, "holder_type": "证券投资基金",
    } for i in range(n)])


class _FakeTs:
    def __init__(self, pages):
        self.pages = pages          # {period: {offset: df}}
        self.last_status = "ok"
        self.calls = []

    def call(self, api, **kw):
        assert api == "top10_floatholders"
        p, off = kw["period"], int(kw["offset"])
        self.calls.append((p, off))
        return self.pages.get(p, {}).get(off, pd.DataFrame())


class TestTop10Backfill(unittest.TestCase):
    def setUp(self):
        self._old_confirm = os.environ.get("ONEOFF_CONFIRM")
        os.environ["ONEOFF_CONFIRM"] = "ALL"
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.db_path)
        conn.executescript(db.SCHEMA)
        conn.commit()
        conn.close()
        self.state = Path(tempfile.mkdtemp(prefix="t10_state_")) / "state.json"
        self.mod = _load_mod()
        self.mod.STATE = self.state
        self.mod.START_PERIOD = "2026-03-31"
        self._patches = [
            mock.patch.object(self.mod.db, "get_conn",
                              side_effect=lambda: sqlite3.connect(self.db_path)),
            mock.patch.object(self.mod.db, "conn_ctx", self._conn_ctx()),
            mock.patch("app.datasource.tushare_client.TushareClient",
                       side_effect=lambda: self.ts),
            # main() 里有 argparse ⇒ 必须给它干净的 argv（否则会读到 pytest 自己的参数）
            mock.patch.object(sys, "argv", ["backfill_top10_holder.py"]),
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
        # 2026-09-24 补：同 `test_report_rc_backfill` —— `self.state` 是
        # `mkdtemp(prefix="t10_state_")` 里面的 state.json，只 unlink 文件会让
        # **父目录永远留着**（本机已累积 1012 个）。07 号 #64 同族。
        import shutil
        shutil.rmtree(self.state.parent, ignore_errors=True)

    def _count(self) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM top10_holder").fetchone()[0]
        finally:
            conn.close()

    def test_cols_match_ddl(self):
        conn = sqlite3.connect(self.db_path)
        try:
            ddl = [r[1] for r in conn.execute("PRAGMA table_info(top10_holder)")]
        finally:
            conn.close()
        self.assertEqual(sorted(self.mod.COLS), sorted(ddl))
        self.assertIn("ann_date", ddl, "PIT 必须存公告日")
        self.assertIn("end_date", ddl, "报告期也要存（但因子不许用它对齐）")

    def test_quarters(self):
        qs = self.mod._quarters("2020-09-30", "2021-06-30")
        self.assertEqual(qs, ["20200930", "20201231", "20210331", "20210630"])

    def test_paging_done_and_Idempotent(self):
        self.ts = _FakeTs({"20260331": {0: _rows(6000, "20260331", 0),
                                        6000: _rows(50, "20260331", 6000)},
                           "20260630": {0: _rows(20, "20260630", 0)}})
        self.assertEqual(self.mod.main(), 0)
        self.assertEqual([o for _, o in self.ts.calls if _ == "20260331"], [0, 6000])
        self.assertEqual(self._count(), 6070)
        st = self.mod._load_state()
        # 报告期集合随"今天"滚动（20260331/20260630/20260930），断言只钉关键事实
        self.assertIn("20260331", st["done"])
        self.assertIn("20260630", st["done"])
        with mock.patch("app.datasource.tushare_client.TushareClient",
                        side_effect=lambda: _FakeTs({})):      # 再跑一次：都已完成 ⇒ 不再调用
            self.mod.main()
        self.assertEqual(self._count(), 6070, "重跑必须幂等")

    def test_resume_from_checkpoint(self):
        self.state.write_text('{"done": ["20260331"], "next_offset": {"20260630": 6000}, "rows": 6000}',
                              encoding="utf-8")
        self.ts = _FakeTs({"20260630": {6000: _rows(30, "20260630", 6000)}})
        self.mod.main()
        self.assertEqual(self.ts.calls[0], ("20260630", 6000),
                         "20260630 必须从 offset=6000 续")
        self.assertNotIn(("20260331", 0), self.ts.calls,
                         "早于'最近两期'窗口的已完成报告期不许重拉")


if __name__ == "__main__":
    unittest.main()
