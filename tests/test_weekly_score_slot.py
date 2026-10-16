"""打分保鲜档 `run_score_slot` + 锚点判定的守卫测试（2026-09-13）

为什么单测它：19:30 那条档决定"**要不要重训**"。没有它 / 判错它，下个锚点出方案时会因
"存档未覆盖该锚点"而失败 ⇒ **终端静默不交易**（本项目真发生过这类静默缺口）。
另外 `is_week_last_open` 的注释记着一个实测坑：不能用"和今天比 ISO 周"来判锚点，
否则周六跑时会误判成上周五 —— 这里用临时交易日历把那两种情况都钉住。

守七条：
  1. `weekly.enabled=false` ⇒ 跳过（不读日历）；
  2. 日历/行情都没有 ⇒ `日历/行情不足`（不重训）；
  3. 锚点已在存档里 ⇒ 跳过（**不浪费 6~8 分钟重训**）；
  4. 存档缺该锚点 ⇒ 触发重训（train_fn 注入）；
  5. 完全没有存档（FileNotFoundError）⇒ 也走重训；
  6. 重训抛异常 ⇒ 返回 `{"error": ...}`，**绝不向上抛**；
  7. `newest_anchor` / `is_week_last_open` 的口径：**周五当天**算已完成的锚点；
     同周内的周一到周四都不算。

运行: python -m unittest tests.test_weekly_score_slot -v
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.weekly import pipeline as wk_pipe        # noqa: E402
from app.weekly import scores as wk_scores        # noqa: E402

_SCHEMA = """
CREATE TABLE trade_cal (cal_date TEXT PRIMARY KEY, is_open INTEGER);
CREATE TABLE daily (ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
    close REAL, pre_close REAL, change REAL, pct_chg REAL, vol REAL, amount REAL,
    PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID;
"""
# 两周完整交易日（缺任一天都会让"本周最后一个开市日"判错 —— 夹具必须像真日历）
OPEN_DAYS = ["20260907", "20260908", "20260909", "20260910", "20260911",
             "20260914", "20260915", "20260916", "20260917", "20260918"]
FRIDAY, TUESDAY = "20260911", "20260908"


class TestAnchorHelpers(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(_SCHEMA)
        conn.executemany("INSERT INTO trade_cal VALUES (?,1)", [(d,) for d in OPEN_DAYS])
        conn.executemany("INSERT INTO daily (ts_code, trade_date, close) VALUES ('600000.SH',?,10.0)",
                         [(d,) for d in OPEN_DAYS])
        conn.commit()
        conn.close()
        self.conn = sqlite3.connect(self.path)
        # addCleanup 是 LIFO：先注册 unlink ⇒ 最后执行（那时连接已关，Windows 才删得掉）
        self.addCleanup(lambda: Path(self.path).unlink(missing_ok=True))
        self.addCleanup(self.conn.close)

    def test_friday_is_the_week_last_open_day(self):
        self.assertTrue(wk_pipe.is_week_last_open(self.conn, FRIDAY))

    def test_midweek_day_is_not_the_week_last_open_day(self):
        self.assertFalse(wk_pipe.is_week_last_open(self.conn, TUESDAY))

    def test_newest_anchor_skips_earlier_days_of_this_week(self):
        self.assertEqual(wk_pipe.newest_anchor(self.conn, "20260914"), FRIDAY)
        # 周五当晚跑（当天就是锚点）：也要认出它，而不是退回上周
        self.assertEqual(wk_pipe.newest_anchor(self.conn, FRIDAY), FRIDAY)


class TestRunScoreSlot(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(_SCHEMA)
        conn.executemany("INSERT INTO trade_cal VALUES (?,1)", [(d,) for d in OPEN_DAYS])
        conn.executemany("INSERT INTO daily (ts_code, trade_date, close) VALUES ('600000.SH',?,10.0)",
                         [(d,) for d in OPEN_DAYS])
        conn.commit()
        conn.close()
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(lambda: Path(self.path).unlink(missing_ok=True))
        self.addCleanup(self.conn.close)
        self.cfg = {"weekly": {"enabled": True}}

    def _latest(self, anchors):
        return (None, pd.DatetimeIndex(anchors), None, None, Path("scores_x.npz"))

    def test_disabled_skips(self):
        out = wk_pipe.run_score_slot(today="20260914", cfg={"weekly": {"enabled": False}})
        self.assertEqual(out, {"skipped": "weekly.enabled=false"})

    def test_no_calendar_or_market_data_skips(self):
        empty = sqlite3.connect(":memory:")
        empty.executescript(_SCHEMA)
        out = wk_pipe.run_score_slot(today="20260914", cfg=self.cfg, conn=empty,
                                     train_fn=lambda: self.fail("不该重训"))
        self.assertIn("日历/行情不足", out["skipped"])
        empty.close()

    def test_already_covered_skips_without_training(self):
        with mock.patch.object(wk_scores, "latest", return_value=self._latest([FRIDAY])):
            out = wk_pipe.run_score_slot(today="20260914", cfg=self.cfg, conn=self.conn,
                                         train_fn=lambda: self.fail("已覆盖却重训了"))
        self.assertIn("打分已覆盖锚点", out["skipped"])
        self.assertEqual(out["anchor"], FRIDAY)

    def test_missing_anchor_triggers_retrain(self):
        with mock.patch.object(wk_scores, "latest", return_value=self._latest(["20260904"])):
            out = wk_pipe.run_score_slot(today="20260914", cfg=self.cfg, conn=self.conn,
                                         train_fn=lambda: "/tmp/scores_new.npz")
        self.assertTrue(out["retrained"])
        self.assertEqual(out["anchor"], FRIDAY)
        self.assertIn("scores_new", out["path"])

    def test_no_archive_at_all_also_retrains(self):
        with mock.patch.object(wk_scores, "latest", side_effect=FileNotFoundError("无存档")):
            out = wk_pipe.run_score_slot(today="20260914", cfg=self.cfg, conn=self.conn,
                                         train_fn=lambda: "/tmp/scores_first.npz")
        self.assertTrue(out["retrained"])

    def test_training_failure_is_reported_not_raised(self):
        def _boom():
            raise RuntimeError("LightGBM 训练失败")

        with mock.patch.object(wk_scores, "latest", side_effect=FileNotFoundError("无存档")):
            out = wk_pipe.run_score_slot(today="20260914", cfg=self.cfg, conn=self.conn,
                                         train_fn=_boom)
        self.assertIn("error", out)
        self.assertIn("LightGBM", out["error"])


if __name__ == "__main__":
    unittest.main()
