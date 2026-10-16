"""`scheduler._stage_failed` 的回归测试（2026-09-14）

它决定**开机补跑要不要重跑整条链**：只要有一个被点名的阶段"最新一条记录是 error"，
`_catch_up` 就会重跑日频/月频全链。判错两个方向的代价都不小：
  · 本该重跑而没跑 ⇒ 数据落后、方案陈旧（静默空转）；
  · 不该重跑而重跑 ⇒ **每次开机重跑完整日链**（白烧 Tushare 配额、反复重建方案与挂单）。

历史教训（2026-09-05 修复，本文件钉的就是它）：原实现按"最近 N 天内**任意** error 行"匹配，
而成功重跑只**新增**一行 ok、旧的 error 行永不清除 ⇒ 一次瞬态失败之后 3 天内每次开机都重跑全链。
现在只认**每个阶段的最新一行**（`id = MAX(id) WHERE stage=p.stage`）。

运行: python -m pytest tests/test_scheduler_stage_failed.py -q
"""
import sqlite3
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import scheduler                              # noqa: E402
from app import timeutil                               # noqa: E402

DDL = ("CREATE TABLE pipeline_run (id INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT, "
       "status TEXT, started_at TEXT)")


def _db(rows) -> sqlite3.Connection:
    """rows: [(stage, status, started_at), …]（按给定顺序插入 ⇒ id 递增）。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(DDL)
    conn.executemany("INSERT INTO pipeline_run (stage, status, started_at) VALUES (?,?,?)", rows)
    conn.commit()
    return conn


RECENT = timeutil.days_ago(0)          # 今天（在东八区口径内）
OLD = timeutil.days_ago(10)            # 窗口（3 天）之外


class TestStageFailed(unittest.TestCase):
    def _run(self, rows, stages=("daily_score",), since_days=3) -> bool:
        conn = _db(rows)
        try:
            return scheduler._stage_failed(conn, tuple(stages), since_days=since_days)
        finally:
            conn.close()

    def test_latest_ok_wins_over_older_error(self):
        """**2026-09-05 修的那条**：旧 error + 新 ok ⇒ 必须判"不用补跑"。"""
        self.assertFalse(self._run([("daily_score", "error", RECENT),
                                    ("daily_score", "ok", RECENT)],
                                   stages=("daily_score",)),
                         "成功重跑之后仍按旧 error 重跑 ⇒ 每次开机白跑整条链")

    def test_latest_error_triggers_catch_up(self):
        self.assertTrue(self._run([("daily_score", "ok", RECENT),
                                   ("daily_score", "error", RECENT)]))

    def test_latest_aborted_also_triggers_catch_up(self):
        """'aborted'（`_stage` 的 finally 为强杀/中断落的行）必须和 error 一样触发补跑。

        2026-09-19 审查：原实现只认 'error' ⇒ 被强杀的阶段**既没有 error 行、也不会补跑**，
        缺口静默消失（连运维告警都接不到）。
        """
        self.assertTrue(self._run([("daily_score", "ok", RECENT),
                                   ("daily_score", "aborted", RECENT)]),
                        "aborted 不触发补跑 ⇒ 被强杀的阶段永远没人管")

    def test_aborted_superseded_by_later_ok_does_not_retrigger(self):
        """与 error 同一把尺：只看最新一行 ⇒ 已重跑成功的 aborted 不许反复触发整链。"""
        self.assertFalse(self._run([("daily_score", "aborted", RECENT),
                                    ("daily_score", "ok", RECENT)]))

    def test_error_outside_the_window_is_ignored(self):
        self.assertFalse(self._run([("daily_score", "error", OLD)]))

    def test_stage_without_any_row_is_not_failed(self):
        self.assertFalse(self._run([("other_stage", "error", RECENT)]))

    def test_any_requested_stage_failing_is_enough(self):
        rows = [("daily_score", "ok", RECENT), ("monitor", "error", RECENT)]
        self.assertTrue(self._run(rows, stages=("daily_score", "monitor")))
        self.assertFalse(self._run(rows, stages=("daily_score",)))


class TestStageSettlesOnInterrupt(unittest.TestCase):
    """`scheduler._stage` 的 finally（2026-09-19 审查）：**没走到收口的行不许永远留在 running**。

    原实现只有 try/except `Exception` ⇒ `KeyboardInterrupt`/`SystemExit`（看门狗强杀、
    Ctrl+C、关机）走不到 except，那一行就永远停在 'running'：
      · `_stage_watermark_ok` 只看"最新一行是不是 ok" ⇒ 卡在 running 的 factors/train
        每次开机整段重算（1197 万行）；
      · `_stage_failed` 只认 error ⇒ 该阶段**既不告警也不补跑**，缺口静默。
    写的是测试库（`tests/_isolation.py` 已把 `db.DB_PATH` 指到临时库），不碰生产台账。
    """

    def _latest(self, stage: str):
        from app import db
        with db.conn_ctx() as conn:
            return conn.execute(
                "SELECT status, ended_at FROM pipeline_run WHERE id="
                "(SELECT MAX(id) FROM pipeline_run WHERE stage=?)", (stage,)).fetchone()

    def test_base_exception_settles_as_aborted(self):
        from app import scheduler as sc
        with self.assertRaises(KeyboardInterrupt):
            sc._stage(lambda: (_ for _ in ()).throw(KeyboardInterrupt("强杀")),
                      "_test_stage_abort")
        status, ended = self._latest("_test_stage_abort")
        self.assertEqual(status, "aborted",
                         "被中断的阶段留在 running ⇒ 下游两个读者都被带偏")
        self.assertIsNotNone(ended, "aborted 也必须收口 ended_at，否则看不出卡了多久")

    def test_ordinary_exception_still_records_error(self):
        from app import scheduler as sc
        with self.assertRaises(ValueError):
            sc._stage(lambda: (_ for _ in ()).throw(ValueError("真失败")), "_test_stage_err")
        self.assertEqual(self._latest("_test_stage_err")[0], "error",
                         "finally 不许把 error 覆盖成 aborted（两种情况要分得开）")

    def test_success_records_ok(self):
        from app import scheduler as sc
        self.assertEqual(sc._stage(lambda: 7, "_test_stage_ok"), 7)
        self.assertEqual(self._latest("_test_stage_ok")[0], "ok")


if __name__ == "__main__":
    unittest.main()
