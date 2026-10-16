"""周频袖套的**开机补跑**（2026-09-13）

缺口是真实的：袖套的三个档（09:32 开盘 / 18:05 晚间兜底 / 19:30 打分保鲜）**都跑在
调度器进程里**。服务若在 18:05 之后才起来（崩溃后被看门狗拉回、半夜掉线、重启），
当天两个成交档都已经错过 ⇒ 本周成交被拖到**下一个交易日**，而且成交价从"执行日开盘价"
悄悄变成"次日开盘价"。`_catch_up()` 里补一刀即可，**且必须复用晚间档同一个函数**，
否则就是第二套成交价口径（本仓库明令禁止）。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import scheduler  # noqa: E402


class _FakeConn:
    """_catch_up 只做 MAX()/EXISTS 型查询 ⇒ 返回空行即可走完全程。"""

    def execute(self, *a, **k):
        return self

    def fetchone(self):
        return (None,)

    def close(self):
        pass


class TestWeeklySleeveCatchUp(unittest.TestCase):
    def _run(self, now=None):
        calls = []
        import datetime
        now = now or datetime.datetime(2026, 9, 14, 19, 0)     # 缺省=晚间档之后(补跑应生效)

        def _fake_guarded(fn, name, **kw):
            calls.append((name, fn))
            return True

        with mock.patch.object(scheduler.db, "get_conn", return_value=_FakeConn()), \
                mock.patch.object(scheduler, "_guarded", side_effect=_fake_guarded), \
                mock.patch("app.weekly.pipeline.run_evening_slot") as wk:
            scheduler._catch_up(now=now)
        return calls, wk

    def test_catch_up_runs_weekly_evening_slot(self):
        calls, wk = self._run()
        names = [n for n, _ in calls]
        self.assertIn("开机补跑-周频袖套", names, "开机补跑漏了周频袖套 ⇒ 18:05 后重启会拖一天成交")

    def test_weekly_arm_reuses_the_evening_slot_function(self):
        """不许另写一套成交口径：被补跑**执行**的必须是 `run_evening_slot` 本身。

        2026-09-14：周频档外面包了一层 `_weekly_staged`（进 pipeline_run + 按结果分类），
        所以恒等检查改为看**内层函数**（包装器用 `__wrapped__` 暴露它）——语义不变：
        实际执行的就是 `app.weekly.pipeline.run_evening_slot`，没有第二套成交口径。
        """
        calls, wk = self._run()
        fn = dict(calls)["开机补跑-周频袖套"]
        self.assertIs(getattr(fn, "__wrapped__", fn), wk)

    def test_monthly_catch_ups_still_run(self):
        """补袖套不许把原有的补跑挤掉。"""
        calls, _ = self._run()
        names = [n for n, _ in calls]
        self.assertIn("开机补跑-开盘执行", names)

    def test_before_evening_window_does_not_run_weekly_slot(self):
        """**2026-09-14 新增（07 号 #34）**：盘前/盘中启动**不许**补跑周频晚间档 ——
        那时当日行情还没入库, 晚间档会"0 成交但照样落当日净值点", 把执行档看门狗短路
        （实测 2026-09-14 06:04:45 的 `成交 0 笔 ... NAV 1000000`）。"""
        import datetime
        calls, _ = self._run(now=datetime.datetime(2026, 9, 14, 6, 4))
        names = [n for n, _ in calls]
        self.assertNotIn("开机补跑-周频袖套", names, "盘前补跑又把看门狗短路了")
        self.assertIn("开机补跑-开盘执行", names, "只该拦周频档, 别把月频开盘档也拦了")

    def test_just_after_evening_window_runs_weekly_slot(self):
        """18:05 之后启动 ⇒ 仍然必须补跑（这条是原来要解决的问题：重启拖一天成交）。"""
        import datetime
        calls, _ = self._run(now=datetime.datetime(2026, 9, 14, 18, 6))
        self.assertIn("开机补跑-周频袖套", [n for n, _ in calls])


if __name__ == "__main__":
    unittest.main()
