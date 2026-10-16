"""周频**健康档里的账本按日快照**（2026-09-14）

为什么单测它：`data/weekly/ledger.json` 是袖套**唯一的成交记录**（丢了无法重建），而它**不在**
`neweco.db` 里 ⇒ 周日那条 `db.backup_db()` 保护不到它。原实现只在周日备份档顺带快照一次：
  · 周中新写的成交**一周内没有副本**；
  · `doctor.check_backup` 的"账本快照"那一项在首个周日之前一直红（2026-09-14 实测 10/11）。
修法：在**每个工作日的 19:45 健康档**里补一次（那时日链与两档成交都已写完）。

守两条：① 健康档真的调了 `backup_snapshots`；② 快照失败**不许**影响健康读数（该档还有
风控停买/看门狗/成交核对等动作，任何一个都不能被一次快照失败带走）。

运行: python -m pytest tests/test_weekly_slot_snapshot.py -q
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfg_mod          # noqa: E402
from app import scheduler                  # noqa: E402


class TestHealthSlotDailySnapshot(unittest.TestCase):
    def _run(self, snapshot_side_effect=None):
        calls = []
        side = snapshot_side_effect or (lambda: calls.append("snap") or "x.json")
        with mock.patch.object(cfg_mod, "load_config",
                               return_value={"weekly": {"enabled": True}}), \
                mock.patch("app.weekly.health.current",
                           return_value={"level": "healthy"}), \
                mock.patch("app.weekly.risk.apply", return_value={}), \
                mock.patch("app.weekly.risk.decide", return_value={}), \
                mock.patch("app.weekly.ledger.Ledger.load", return_value=mock.Mock()), \
                mock.patch("app.weekly.ledger.backup_snapshots", side_effect=side), \
                mock.patch("app.weekly.doctor.check_exec_ran",
                           return_value={"ok": True, "detail": "非执行日"}), \
                mock.patch("app.weekly.reconcile.report", return_value={"available": False}), \
                mock.patch("app.weekly.attrib.refresh", return_value=None):
            res = scheduler._weekly_health_slot()
        return calls, res

    def test_health_slot_takes_a_daily_ledger_snapshot(self):
        calls, res = self._run()
        self.assertEqual(calls, ["snap"], "健康档必须每个工作日给账本留一份快照（07 #36）")
        self.assertEqual(res.get("level"), "healthy")

    def test_snapshot_failure_does_not_break_the_health_slot(self):
        def _boom():
            raise OSError("磁盘满")

        calls, res = self._run(snapshot_side_effect=_boom)
        self.assertEqual(res.get("level"), "healthy",
                         "一次快照失败不许把健康读数/风控动作带走")


if __name__ == "__main__":
    unittest.main()
