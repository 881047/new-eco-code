# -*- coding: utf-8 -*-
"""互动易注意力追踪器单测 —— 全桩(不联网、不触生产库)。

覆盖: 行解析/归档幂等合并/覆盖率/拍板门 + run() 的桩网络集成
(同日重跑秒退幂等 / 失败票 fail-soft 继续喂下一票)。
夹具纪律: 先断言数据真的进去了(空库假阳性防御)。
"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

_spec = importlib.util.spec_from_file_location(
    "irm_tracker", BASE / "scripts" / "irm_attention_tracker.py")
trk = importlib.util.module_from_spec(_spec)
sys.modules["irm_tracker"] = trk
_spec.loader.exec_module(trk)


def _rows(code, n, answered_last=True):
    return [{"indexId": f"{code}-{i}", "stockCode": code,
             "pubDate": 1789000000000 + i * 86400000,
             "mainContent": f"问题{i}", "attachedContent": f"回答{i}" if (i < n - 1 or answered_last) else None,
             "pubClient": "网站"} for i in range(n)]


class TestParseAndMerge(unittest.TestCase):
    def test_parse_rows_normalizes(self):
        df = trk.parse_rows(_rows("000571.SZ", 3, answered_last=False), "000571.SZ")
        self.assertEqual(len(df), 3)
        self.assertFalse(df["is_answered"].iloc[-1], "回答内容为空 ⇒ 未回答")
        self.assertTrue(df["is_answered"].iloc[:-1].all())
        self.assertEqual(df["ask_ts"].iloc[0].tzinfo is not None, True,
                         "秒级时间戳须带时区(PIT)")
        self.assertEqual(set(df.columns),
                         {"index_id", "ts_code", "ask_ts", "is_answered",
                          "source", "question", "answer"})

    def test_parse_rows_drops_missing_index_id(self):
        rows = [{"stockCode": "X", "pubDate": 1789000000000, "mainContent": "无 id"}]
        self.assertEqual(len(trk.parse_rows(rows, "X")), 0)

    def test_merge_archive_dedups_by_index_id(self):
        a = trk.parse_rows(_rows("A", 2), "A")
        b = trk.parse_rows(_rows("A", 2), "A")            # 同 id 重采(滚动窗重叠)
        extra = trk.parse_rows(_rows("B", 1), "B")
        merged = trk.merge_archive(trk.merge_archive(pd.DataFrame(), a), pd.concat([b, extra]))
        self.assertEqual(len(merged), 3, "同 id 去重 + 新票入档")
        self.assertEqual(merged["ts_code"].nunique(), 2)

    def test_coverage_and_gate(self):
        snap = pd.DataFrame({"n_questions": [5, 0, 3], "fetch_status": ["ok"] * 3})
        self.assertAlmostEqual(trk.coverage_of(snap), 2 / 3)
        self.assertTrue(trk.coverage_of(pd.DataFrame(columns=["n_questions", "fetch_status"]))
                        != trk.coverage_of(pd.DataFrame(columns=["n_questions", "fetch_status"])))
        self.assertEqual(trk.gate_status(0, None, datetime(2026, 10, 2))[0], "WAITING")
        self.assertEqual(trk.gate_status(130, datetime(2026, 4, 2).date(),
                                         datetime(2026, 10, 2))[0], "READY")
        self.assertEqual(trk.gate_status(10, datetime(2026, 10, 1).date(),
                                         datetime(2026, 10, 2))[0], "WAITING")


class _RunBase(unittest.TestCase):
    """桩网络 + 临时目录 + 临时文件库的 run() 集成夹具。"""

    CODES = ["000001.SZ", "000002.SZ", "600000.SH"]

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        tmp = Path(self.td.name)
        patches = [
            mock.patch.object(trk, "DATA_DIR", tmp),
            mock.patch.object(trk, "ARCHIVE", tmp / "qa_archive.parquet"),
            mock.patch.object(trk, "DAILY", tmp / "daily_counts.parquet"),
            mock.patch.object(trk, "ORG_CACHE", tmp / "org_ids.json"),
            mock.patch.object(trk, "RATE_LIMIT_S", 0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # 临时文件库(非 :memory:): conn_ctx 每轮 run() 会 close 连接,
        # 多轮 run() 的测试需要数据跨连接存活
        self.dbpath = tmp / "test.db"
        conn = sqlite3.connect(self.dbpath)
        conn.execute("CREATE TABLE model_score (ts_code TEXT, trade_date TEXT, "
                     "score REAL, pred_ret REAL)")
        for i, c in enumerate(self.CODES):
            conn.execute("INSERT INTO model_score VALUES (?,?,?,?)", (c, "20260831", 99.0 - i, 0.01))
        conn.execute("INSERT INTO model_score VALUES ('000009.SZ','20260731',98.0,0.01)")
        conn.commit()
        conn.close()
        p = mock.patch.object(trk.app_db, "get_conn",
                              side_effect=lambda: sqlite3.connect(self.dbpath))
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.td.cleanup()

    def _stub_post(self, failing_code=None):
        calls = {"n_org": 0}

        def fake_post(session, url, data, timeout=20):
            if url.endswith("queryKeyboardInfo"):
                calls["n_org"] += 1
                return {"data": [{"secid": f"org{data['keyWord']}"}]}
            code = data["stockcode"]
            if failing_code and code in failing_code:
                raise RuntimeError("模拟网络失败")
            page = int(data["pageNum"])
            return {"rows": _rows(code, 2 if page == 1 else 0),
                    "totalPage": 1, "total": 2 if page == 1 else 0}

        return fake_post, calls


class TestRun(_RunBase):
    def test_first_run_collects_and_snapshots(self):
        fake, calls = self._stub_post()
        with mock.patch.object(trk, "_post", fake):
            res = trk.run(datetime(2026, 10, 2))
        self.assertEqual(res["n_ok"], 3, "3 票全采到")
        self.assertEqual(res["n_fail"], 0)
        daily = pd.read_parquet(trk.DAILY)
        self.assertEqual(len(daily), 3, "快照逐票一行")
        self.assertTrue((daily["fetch_date"] == "20261002").all())
        self.assertEqual(int(daily["n_questions"].max()), 2, "桩每票 2 问")
        arch = pd.read_parquet(trk.ARCHIVE)
        self.assertEqual(len(arch), 6, "归档 3 票 × 2 问")
        # 夹具防假阳性: 数据真进去了再断言行为
        self.assertEqual(arch["index_id"].nunique(), 6)

    def test_same_day_rerun_is_idempotent(self):
        fake, calls = self._stub_post()
        with mock.patch.object(trk, "_post", fake):
            trk.run(datetime(2026, 10, 2))
        before = pd.read_parquet(trk.DAILY)
        with mock.patch.object(trk, "_post", fake):
            res = trk.run(datetime(2026, 10, 2))
        self.assertEqual(res["n_ok"], 0, "同日重跑全部已采 ⇒ 秒退")
        after = pd.read_parquet(trk.DAILY)
        self.assertTrue(before.equals(after), "快照表零变化")

    def test_next_day_extends_snapshot(self):
        fake, calls = self._stub_post()
        with mock.patch.object(trk, "_post", fake):
            trk.run(datetime(2026, 10, 2))
            res = trk.run(datetime(2026, 10, 3))
        daily = pd.read_parquet(trk.DAILY)
        self.assertEqual(daily["fetch_date"].nunique(), 2)
        self.assertEqual(res["snapshot_days"], 2)
        self.assertEqual(res["gate"], "WAITING")

    def test_fail_soft_marks_failed_and_continues(self):
        fake, calls = self._stub_post(failing_code={"000002"})
        with mock.patch.object(trk, "_post", fake):
            res = trk.run(datetime(2026, 10, 2))
        self.assertEqual(res["n_fail"], 1)
        self.assertEqual(res["n_ok"], 2, "失败票不挡后续票")
        daily = pd.read_parquet(trk.DAILY)
        bad = daily[daily["ts_code"] == "000002.SZ"].iloc[0]
        self.assertTrue(str(bad["fetch_status"]).startswith("failed:"))
        self.assertEqual(int(bad["n_questions"]), 0)
        ok_cov = trk.coverage_of(daily[daily["fetch_status"] == "ok"])
        self.assertGreaterEqual(ok_cov, 0.5)

    def test_lock_held_skips(self):
        with mock.patch.object(trk.FileLock, "acquire", return_value=False):
            res = trk.run(datetime(2026, 10, 2))
        self.assertEqual(res.get("skipped"), "locked")
        self.assertFalse(trk.DAILY.exists(), "秒退不得写任何产物")


if __name__ == "__main__":
    unittest.main()
