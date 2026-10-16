"""调度器跨进程锁单测: 陈旧锁清理 / 异常不泄漏线程锁 (2026-09-04 审查修复)

运行: python -m unittest tests.test_scheduler_lock -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import scheduler


class _LockBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".lock", delete=False)
        tmp.close()
        Path(tmp.name).unlink()  # 锁文件应不存在
        self.path = Path(tmp.name)
        self.patcher = mock.patch.object(scheduler, "_LOCK_FILE", self.path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        if self.path.exists():
            self.path.unlink()


class TestLockStale(_LockBase):
    def _stale(self, content, ticks, alive=True):
        # alive 走 mock(默认 True=活着): 让本类用例只考滴答比对逻辑, 隔离真实的
        # OpenProcess/GetExitCodeProcess —— 退出码判死这条线由
        # test_terminated_undestroyed_holder_stale 单独钉住。
        self.path.write_text(content, encoding="utf-8")
        with mock.patch.object(scheduler, "_proc_alive_strict", return_value=alive), \
             mock.patch.object(scheduler, "_proc_start_ticks", return_value=ticks):
            return scheduler._lock_stale()

    def test_empty_file_stale(self):
        self.assertTrue(self._stale("", None))  # os.open/os.write 之间被杀 → 空文件

    def test_garbage_stale(self):
        self.assertTrue(self._stale("abc", None))
        self.assertTrue(self._stale("12x\n34", None))

    def test_dead_pid_stale(self):
        self.assertTrue(self._stale("99999\n123", -1))  # 进程不存在(-1)

    def test_unknown_ticks_assumed_held(self):
        # 2026-09-05 审查修复: 无法核对(如提权进程访问被拒)保守视为有效, 不偷活锁
        self.assertFalse(self._stale("99999\n123", None))

    def test_pid_reuse_stale(self):
        # pid 存活但创建时间不同 → 被其他进程复用
        self.assertTrue(self._stale("1234\n111", 222))

    def test_corrupt_tick_stale(self):
        self.assertTrue(self._stale("1234\nzzz", 222))

    def test_valid_held(self):
        self.assertFalse(self._stale("1234\n111", 111))

    def test_old_format_held(self):
        # 旧版仅 pid 的锁文件: 无法核对复用, 进程存活视为有效
        self.assertFalse(self._stale("1234", 222))

    def test_no_file_stale(self):
        self.assertTrue(scheduler._lock_stale())

    def test_terminated_undestroyed_holder_stale(self):
        # 2026-10-08 事故回归: 持有者已终止但进程对象未销毁(句柄未关)时, 滴答数
        # 仍查得到且一致 —— 退出码判死必须优先, 否则新实例永远撞锁
        # (当日看门狗拉起撞锁秒退、误发"需要人工"的直接原因)。
        self.assertTrue(self._stale("1234\n111", 111, alive=False))

    def test_alive_strict_unknown_keeps_conservative(self):
        # 判不了(None, 如提权进程)不改变原有保守语义: 滴答一致 ⇒ 锁有效
        self.assertFalse(self._stale("1234\n111", 111, alive=None))


class TestAcquireFileLock(_LockBase):
    def test_acquire_writes_pid_and_tick(self):
        with mock.patch.object(scheduler, "_proc_start_ticks", return_value=777):
            self.assertTrue(scheduler._acquire_file_lock())
        content = self.path.read_text(encoding="utf-8").strip()
        pid_s, _, tick_s = content.partition("\n")
        self.assertEqual(int(pid_s), scheduler.os.getpid())
        self.assertEqual(int(tick_s), 777)

    def test_second_acquire_fails_while_held(self):
        with mock.patch.object(scheduler, "_proc_start_ticks", return_value=777):
            self.assertTrue(scheduler._acquire_file_lock())
            with mock.patch.object(scheduler, "_lock_stale", return_value=False):
                self.assertFalse(scheduler._acquire_file_lock())

    def test_stale_lock_cleaned_and_reacquired(self):
        self.path.write_text("99999\n1", encoding="utf-8")  # 已死进程(-1)
        with mock.patch.object(scheduler, "_proc_start_ticks", return_value=-1):
            self.assertTrue(scheduler._acquire_file_lock())

    def test_replace_failure_gives_up(self):
        # 2026-09-05: 陈旧锁改为 os.replace 原子搬走, 搬不走(权限等)重试 5 次后放弃
        self.path.write_text("99999\n1", encoding="utf-8")
        with mock.patch.object(scheduler, "_proc_start_ticks", return_value=-1), \
                mock.patch.object(scheduler.os, "replace", side_effect=OSError("denied")):
            self.assertFalse(scheduler._acquire_file_lock())

    def test_release_only_own_token(self):
        # 2026-09-05 审查修复: 锁文件已换成他人令牌时不删(原无条件 unlink 误删他人新锁)
        with mock.patch.object(scheduler, "_proc_start_ticks", return_value=777):
            self.assertTrue(scheduler._acquire_file_lock())
        self.path.write_text("9999\n1", encoding="utf-8")
        with mock.patch.object(scheduler, "_proc_start_ticks", return_value=777):
            scheduler._release_file_lock()
        self.assertEqual(self.path.read_text(encoding="utf-8").strip(), "9999\n1")

    def test_release_own_token_removed(self):
        with mock.patch.object(scheduler, "_proc_start_ticks", return_value=777):
            self.assertTrue(scheduler._acquire_file_lock())
        with mock.patch.object(scheduler, "_proc_start_ticks", return_value=777):
            scheduler._release_file_lock()
        self.assertFalse(self.path.exists())


class TestGuarded(_LockBase):
    def test_thread_lock_released_on_acquire_exception(self):
        # 杀软/权限异常: 线程锁必须释放, 否则本进程所有后续任务静默停摆
        def boom():
            raise OSError("permission denied")
        with mock.patch.object(scheduler, "_acquire_file_lock", side_effect=boom):
            self.assertFalse(scheduler._guarded(lambda: None, "测试"))
        self.assertTrue(scheduler._lock.acquire(blocking=False))  # 锁已释放
        scheduler._lock.release()

    def test_foreign_lock_not_unlinked(self):
        released = []
        with mock.patch.object(scheduler, "_acquire_file_lock", return_value=False), \
                mock.patch.object(scheduler, "_release_file_lock",
                                  side_effect=lambda: released.append(1)):
            self.assertFalse(scheduler._guarded(lambda: None, "测试"))
        self.assertEqual(released, [])  # 未获取到文件锁, 不得释放(会误删他人锁)

    def test_fn_exception_releases_both_locks(self):
        released = []
        with mock.patch.object(scheduler, "_acquire_file_lock", return_value=True), \
                mock.patch.object(scheduler, "_release_file_lock",
                                  side_effect=lambda: released.append(1)):
            self.assertFalse(scheduler._guarded(lambda: (_ for _ in ()).throw(RuntimeError("x")), "测试"))
        self.assertEqual(released, [1])
        self.assertTrue(scheduler._lock.acquire(blocking=False))
        scheduler._lock.release()

    def test_success_releases_file_lock(self):
        released = []
        with mock.patch.object(scheduler, "_acquire_file_lock", return_value=True), \
                mock.patch.object(scheduler, "_release_file_lock",
                                  side_effect=lambda: released.append(1)):
            self.assertTrue(scheduler._guarded(lambda: None, "测试"))
        self.assertEqual(released, [1])

    def test_skip_when_thread_lock_busy(self):
        scheduler._lock.acquire()
        try:
            self.assertFalse(scheduler._guarded(lambda: None, "测试"))
        finally:
            scheduler._lock.release()


if __name__ == "__main__":
    unittest.main()
