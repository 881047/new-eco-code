"""P0-1 / P0-2 的守卫测试（2026-09-14 外部审查报告；两条都先复核为真再修）

**P0-1 失败必须能找到人**：全项目 `send_ops_alert` 只有 2 个调用点（备份校验、周频核对），
而 `_guarded` 抓到的日链/月链异常只写日志 + `pipeline_run.status='error'` ⇒ 无人值守时
"整月没出方案""整周零成交"可以完全静默地发生。

**P0-2 抢锁失败必须重排**：所有档共用一把 `_lock`，退避重排原来**只有月链**有；
09:32 建仓档 / 18:05 补成交档 / 19:45 判据④核对档被挤掉就是一天（甚至一周）没了。

全部用 mock：不发信（`conftest` 已把发送器换成记录器，这里再显式 patch `send_ops_alert`）、
不起调度器（`_scheduler` 换成 Mock）。
"""
import datetime
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import scheduler  # noqa: E402


class _AlertCase(unittest.TestCase):
    def setUp(self):
        scheduler._ALERTED.clear()
        p = mock.patch("app.notify.send_ops_alert", return_value=True)
        self.alert = p.start()
        self.addCleanup(p.stop)
        self.addCleanup(scheduler._ALERTED.clear)


class TestFailureAlerts(_AlertCase):
    def test_failing_task_alerts(self):
        ok = scheduler._guarded(lambda: (_ for _ in ()).throw(RuntimeError("日链炸了")),
                                "定时-每日更新")
        self.assertFalse(ok)
        self.assertEqual(self.alert.call_count, 1, "任务失败必须发一条运维告警")
        title, body = self.alert.call_args[0][0], self.alert.call_args[0][1]
        self.assertIn("定时-每日更新", title)
        self.assertIn("日链炸了", body)
        self.assertIn("pipeline_run", body)

    def test_same_day_does_not_spam(self):
        """同一天同一档只发一封（保鲜档一晚上 3 次触发 + 重排会打很多次）"""
        for _ in range(3):
            scheduler._guarded(lambda: (_ for _ in ()).throw(RuntimeError("x")), "定时-每日更新")
        self.assertEqual(self.alert.call_count, 1)

    def test_different_days_alert_again(self):
        """次日再失败必须重新告警（去重键里带日期）"""
        scheduler._guarded(lambda: (_ for _ in ()).throw(RuntimeError("x")), "定时-每日更新")
        scheduler._ALERTED.clear()          # 模拟跨天（键里带日期，这里直接清掉模拟新的一天）
        scheduler._guarded(lambda: (_ for _ in ()).throw(RuntimeError("x")), "定时-每日更新")
        self.assertEqual(self.alert.call_count, 2)

    def test_alert_failure_never_breaks_the_task(self):
        """告警发不出去绝不许把任务本身弄挂（也不能吃掉锁释放）"""
        self.alert.side_effect = RuntimeError("SMTP 挂了")
        ok = scheduler._guarded(lambda: (_ for _ in ()).throw(RuntimeError("x")), "定时-每日更新")
        self.assertFalse(ok)
        self.assertFalse(scheduler._lock.locked(), "锁必须已释放")

    def test_successful_task_sends_no_alert(self):
        self.assertTrue(scheduler._guarded(lambda: None, "定时-每日更新"))
        self.alert.assert_not_called()


class TestRetryOnBusy(_AlertCase):
    def _busy_call(self, stage, name, attempt=0, hold_lock=True):
        """在"锁被占"的状态下调用该档，返回 (调度器 mock, 记录跳过的 patch)"""
        sched = mock.Mock()
        with mock.patch.object(scheduler, "_scheduler", sched), \
                mock.patch.object(scheduler, "_record_skip") as p_skip:
            job = scheduler._retryable(lambda: None, stage, name, attempt)
            self.assertTrue(scheduler._lock.acquire(blocking=False))
            try:
                job()                        # 锁被自己占着 ⇒ 走到 on_busy
            finally:
                scheduler._lock.release()
        return sched, p_skip

    def test_busy_slot_records_skip_and_schedules_retry(self):
        sched, p_skip = self._busy_call("weekly_evening", "定时-周频袖套晚间")
        p_skip.assert_called_once()
        self.assertEqual(p_skip.call_args[0][0], "weekly_evening")
        sched.add_job.assert_called_once()
        kw = sched.add_job.call_args[1]
        self.assertEqual(kw["id"], "weekly_evening_retry_0")
        self.assertTrue(kw["replace_existing"])
        delay = scheduler._RETRY_MIN["weekly_evening"][0]
        self.assertAlmostEqual(
            kw["run_date"].replace(tzinfo=None),
            (datetime.datetime.now(scheduler._TZ) + datetime.timedelta(minutes=delay)
             ).replace(tzinfo=None), delta=datetime.timedelta(minutes=1))

    def test_retry_uses_next_interval_and_keeps_stage(self):
        sched, p_skip = self._busy_call("weekly_sleeve", "定时-周频袖套", attempt=1)
        kw = sched.add_job.call_args[1]
        self.assertEqual(kw["id"], "weekly_sleeve_retry_1")
        self.assertEqual(p_skip.call_args[0][0], "weekly_sleeve")
        self.assertAlmostEqual(
            kw["run_date"].replace(tzinfo=None),
            (datetime.datetime.now(scheduler._TZ) + datetime.timedelta(
                minutes=scheduler._RETRY_MIN["weekly_sleeve"][1])).replace(tzinfo=None),
            delta=datetime.timedelta(minutes=1))

    def test_exhausted_retries_alert_and_stop(self):
        stage = "weekly_health"
        sched, p_skip = self._busy_call(stage, "定时-周频健康",
                                       attempt=len(scheduler._RETRY_MIN[stage]))
        p_skip.assert_called_once()          # 台账仍留一行
        sched.add_job.assert_not_called()    # 不再重排
        self.assertEqual(self.alert.call_count, 1, "用尽重排必须告警（否则还是静默）")
        self.assertIn("定时-周频健康", self.alert.call_args[0][0])

    def test_all_retry_slots_are_known(self):
        """重排名单与档位一一对应（防止"以为加了重排、其实名字写错"）"""
        # 2026-09-19 审查 A2 补 daily(18:00 日链) 与 backup(唯一周备份) —— 这两档此前是
        # 裸 _guarded：抢锁失败连一行 pipeline_run 都不留。
        # 2026-09-28 补 daily_brief(08:30 今日待办) —— 它晚于 P0-2 早于重排表, 漏配时
        # 抢锁失败即"今日放弃"且无重排(唯一漏网的 _retryable 档)。
        self.assertEqual(set(scheduler._RETRY_MIN),
                         {"weekly_sleeve", "weekly_evening", "daily_summary", "weekly_health",
                          "daily", "backup", "daily_brief", "picks_email"})
        for stage, mins in scheduler._RETRY_MIN.items():
            with self.subTest(stage=stage):
                self.assertTrue(mins and all(m > 0 for m in mins))
                self.assertEqual(list(mins), sorted(mins), "重排间隔必须递增")

    def test_retryable_normal_path_actually_runs_the_slot(self):
        """**正常路径**（锁空闲）必须照常执行槽函数 —— 今晚 19:30/19:45 走的就是这条路。
        这条端到端断言补上"重排包装会不会把正常执行弄坏"这个唯一的空白。"""
        calls = []
        with mock.patch.object(scheduler, "_record_skip") as p_skip, \
                mock.patch.object(scheduler.db, "conn_ctx", side_effect=lambda: mock.MagicMock()):
            ok = scheduler._retryable(lambda: calls.append("ran") or {"ok": 1},
                                      "weekly_health", "定时-周频健康")()
        self.assertTrue(ok, "正常路径必须返回 True")
        self.assertEqual(calls, ["ran"], "槽函数必须被真的执行")
        p_skip.assert_not_called()
        self.assertEqual(self.alert.call_count, 0, "正常路径不该告警")

    def test_retry_job_still_wraps_the_same_slot_function(self):
        """重排不许另写一套执行口径：被执行的必须还是原槽函数本身（`__wrapped__` 恒等）"""
        import app.weekly.pipeline as wk
        seen = {}
        with mock.patch.object(scheduler, "_guarded",
                               side_effect=lambda fn, name, **kw:
                               seen.update(fn=fn, name=name) or True):
            scheduler._retryable(wk.run_evening_slot, "weekly_evening", "定时-周频袖套晚间")()
        self.assertIs(getattr(seen["fn"], "__wrapped__", seen["fn"]), wk.run_evening_slot,
                      "重排档必须复用同一个执行函数（与开机补跑同一条守卫）")


class TestNoUndedupedAlertPath(unittest.TestCase):
    """调度器里不许再有**绕过 `_ops_alert_once`** 的直发告警（2026-09-18 审查 Q-15）

    6 小时去重只存在于进程外看门狗（`scripts/service_watchdog.py:54`），而 `send_ops_alert`
    本体**不带任何去重**。原来备份校验失败与周频成交核对异常两处直调它 ⇒ 页脚那句
    "同类告警 6 小时内只发一条"对这两条路径是假的，连续坏的第二天会再收到同标题邮件。
    现在两处都走 `_ops_alert_once`（同 key 当天一次）。这条守卫防的是"以后有人图省事
    又写回直调" —— 那不会被任何现有用例抓到，因为直调照样发得出去、测试照样绿。
    """

    def test_send_ops_alert_only_called_from_the_dedup_helper(self):
        import ast
        src = (Path(scheduler.__file__)).read_text(encoding="utf-8")
        tree = ast.parse(src)
        owners = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for call in ast.walk(node):
                    if isinstance(call, ast.Call):
                        f = call.func
                        name = getattr(f, "id", None) or getattr(f, "attr", None)
                        if name == "send_ops_alert":
                            owners.append((node.name, call.lineno))
        stray = [f"{fn}:{ln}" for fn, ln in owners if fn != "_ops_alert_once"]
        self.assertEqual(stray, [],
                         f"直调 send_ops_alert 绕过了按天去重（Q-15）: {stray}")

    def test_footer_matches_the_real_dedup_semantics(self):
        """页脚要按**实际语义**说：调度器直发的告警不是看门狗发的，去重也不是 6 小时。

        断的是"渲染出来的邮件正文"，不是 notify.py 的源码 —— 源码里有解释这段历史的
        注释，grep 源码会把注释本身算进去（自测自证的坑）。运维读到的是邮件。
        """
        from app import notify
        cfg = {"email": {"enabled": True, "sender": "a@b.c", "auth_code": "x",
                         "smtp_server": "smtp.b.c", "smtp_port": 465,
                         "recipients": ["ops@b.c"]}}
        with mock.patch.object(notify.cfg_mod, "load_config", return_value=cfg), \
                mock.patch.object(notify, "_send_with_retry", return_value=True) as p_send:
            self.assertTrue(notify.send_ops_alert("标题", "正文一行"))
        msg = p_send.call_args.args[1]        # _send_with_retry(cfg, msg, tag)
        body = msg.get_payload(decode=True).decode("utf-8")
        self.assertNotIn("本条由进程外看门狗自动发出", body,
                         "页脚把来源写死成看门狗 ⇒ 调度器直发的告警跟着一起说谎")
        self.assertIn("调度器直发", body, "页脚要按来源分别说明去重口径")
        self.assertIn("正文一行", body, "正文本身仍要出现在邮件里（改页脚别把内容弄丢）")


if __name__ == "__main__":
    unittest.main()
