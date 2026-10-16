"""周频档的 `pipeline_run` 台账守卫（2026-09-14，回应外部复核 D2/D2b/D3）。

背景：当天给四个周频档套了 `_stage` 想让"今天到底跑没跑"在库里可查，但外部复核指出三处
**台账本身会说谎/缺席**：

  D2  **按返回值分类**：周频档按设计不抛异常（返回 `{"error": …}` / `{"skipped": …}`）⇒ 直接套
      `_stage` 会把失败记成 `ok`（复核实测：出方案失败那行仍是 ok）；
  D2b **停用零写入**：`weekly.enabled=false` 时也会白写行，违背"sleeve 停用零副作用"的契约；
  D3  **抢锁失败留痕**：`_guarded` 抢不到锁就直接返回 ⇒ 连行都没有（月链早有 `_record_skip`）。

这里把三条都钉住；全部用 mock 的 `db`，**不碰生产库**。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import scheduler  # noqa: E402


class TestWeeklyStagedLedger(unittest.TestCase):
    def setUp(self):
        self.events = []

        def _start(conn, stage, run_date):
            self.events.append(("start", stage))
            return 7

        def _finish(conn, sid, status, rows=None, error=None):
            self.events.append(("finish", status, str(error) if error else None))

        self._p = [
            mock.patch("app.scheduler.db.conn_ctx", side_effect=lambda: mock.MagicMock()),
            mock.patch("app.scheduler.db.stage_start", side_effect=_start),
            mock.patch("app.scheduler.db.stage_finish", side_effect=_finish),
            mock.patch("app.scheduler._run_date", return_value="20260914"),
        ]
        for p in self._p:
            p.start()
            self.addCleanup(p.stop)

    def _run(self, result, enabled=True):
        cfg = {"weekly": {"enabled": enabled}}
        with mock.patch("app.config.load_config", return_value=cfg):
            return scheduler._weekly_staged(lambda: result, "weekly_x")()

    def test_error_result_is_recorded_as_error_not_ok(self):
        out = self._run({"error": "锚点 20260911 出方案失败: 打分存档未覆盖"})
        self.assertEqual(out["error"], "锚点 20260911 出方案失败: 打分存档未覆盖")
        self.assertEqual(self.events[-1][:2], ("finish", "error"), self.events)
        self.assertIn("打分存档", self.events[-1][2])

    def test_skipped_result_is_recorded_as_skipped(self):
        self._run({"skipped": "20260914 与上一开市日同属一个 ISO 周, 非执行日"})
        self.assertEqual(self.events[-1][:2], ("finish", "skipped"), self.events)

    def test_normal_result_is_ok(self):
        self._run({"anchor": "20260911", "trades": 3})
        self.assertEqual(self.events[-1][:2], ("finish", "ok"), self.events)

    def test_disabled_weekly_writes_no_stage_row(self):
        """D2b：`weekly.enabled=false` ⇒ 不写任何台账行（保持停用零副作用）。"""
        self._run({"skipped": "weekly.enabled=false"}, enabled=False)
        self.assertEqual(self.events, [], "停用时不许写 pipeline_run")

    def test_job_passes_on_busy_that_records_a_skipped_row(self):
        """D3：抢锁失败（`_guarded` 走 on_busy）时，台账里必须留一行 skipped。"""
        captured = {}
        with mock.patch("app.scheduler._guarded",
                        side_effect=lambda fn, name, **kw: captured.update(kw) or True):
            scheduler._weekly_job(lambda: None, "weekly_sleeve", "定时-周频袖套")()
        self.assertIn("on_busy", captured, "周频档必须把 on_busy 传给 _guarded")
        captured["on_busy"]("本进程已有任务在运行")
        self.assertEqual(self.events[-1][:2], ("finish", "skipped"), self.events)


if __name__ == "__main__":
    unittest.main()
