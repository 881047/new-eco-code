"""`app/filelock.py` 的守卫测试（2026-09-15，外部独立审查 F1/F2）。

守两件**踩过/差点踩到**的事：
  ① **Windows 上 `os.kill(pid, 0)` 不是探活而是杀进程** —— CPython `os_kill_impl` 的
     MS_WINDOWS 分支对非 CTRL_* 信号直接 `OpenProcess(PROCESS_ALL_ACCESS)` +
     `TerminateProcess`。所以探活辅助函数在 Windows 上**必须返回 None（判不了）**，
     绝不许调 `os.kill`；否则"判断锁是否陈旧"会**误杀持锁进程**（生产事故）。
     这里用**源码形态断言**钉住：`os.kill` 必须出现在 `os.name == "nt"` 的早退之后。
  ② **清扫能力不许再丢**：`.stale.*` 一律清、`.tmp.*` 只清 1 小时前的（可能属于
     另一个正在建锁的进程）。旧 `scheduler.py` 有这一步，抽通用版时必须带上。
"""
import os
import sys
import time
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import filelock                                  # noqa: E402
from app.filelock import FileLock, _pid_alive             # noqa: E402


class TestNoWindowsKillTrap(unittest.TestCase):
    def test_pid_alive_never_calls_os_kill_on_windows(self):
        """Windows ⇒ 一律 None（判不了），**不调 os.kill**；POSIX ⇒ 仍按真语义探活。"""
        if os.name == "nt":
            self.assertIsNone(_pid_alive(os.getpid()),
                              "Windows 上必须返回 None（os.kill(pid,0) 会 TerminateProcess）")
            self.assertIsNone(_pid_alive(999_999_999),
                              "Windows 上不存在的 pid 也只许返回 None，不许去 kill")
        else:
            self.assertTrue(_pid_alive(os.getpid()), "POSIX: 自己的 pid 应判为活着")

    def test_os_kill_is_guarded_by_platform_check_in_source(self):
        """源码形态守卫：`os.kill` 必须出现在 `os.name == "nt"` 早退**之后**。

        行为测试在 Windows 上只能证明"这次没杀成"，证明不了"代码里没有那条路" ——
        而这条路的代价是误杀服务进程，值得用形态断言钉死。

        ⚠️ 用 **AST 结构**而不是文本顺序：文本 find 会先命中 docstring / 其它函数里的
        同款字符串（本条测试第一版就因此假红过两次）。结构断言问的是真问题 ——
        "`os.kill` 是否在 `if os.name == \"nt\": return` **这条早退之后**"。
        """
        import ast
        src = Path(filelock.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next((n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "_pid_alive"), None)
        self.assertIsNotNone(fn, "找不到 _pid_alive")

        def _is_nt_check(node) -> bool:
            return (isinstance(node, ast.Compare)
                    and isinstance(node.left, ast.Attribute) and node.left.attr == "name"
                    and isinstance(node.left.value, ast.Name) and node.left.value.id == "os"
                    and isinstance(node.comparators[0], ast.Constant)
                    and node.comparators[0].value == "nt")

        guard_lineno = None
        for node in ast.walk(fn):
            if isinstance(node, ast.If) and _is_nt_check(node.test) and node.lineno < 1000:
                returns_none = any(isinstance(s, ast.Return)
                                   and isinstance(s.value, ast.Constant) and s.value.value is None
                                   for s in node.body)
                if returns_none:
                    guard_lineno = node.lineno
                    break
        self.assertIsNotNone(guard_lineno,
                             '缺少 `if os.name == "nt": return None` 早退 ⇒ Windows 上会调 os.kill')

        kill_lines = [n.lineno for n in ast.walk(fn)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                      and n.func.attr == "kill"
                      and isinstance(n.func.value, ast.Name) and n.func.value.id == "os"]
        self.assertTrue(kill_lines, "os.kill 不见了（预期只出现在 POSIX 分支）")
        self.assertTrue(all(ln > guard_lineno for ln in kill_lines),
                        f"os.kill 出现在平台早退之前（kill@{kill_lines} vs guard@{guard_lineno}）"
                        " ⇒ Windows 上会误杀持锁进程")

    def test_unknown_ticks_keeps_lock_conservatively(self):
        """滴答数拿不到时，`_stale()` 必须保守认为**锁仍有效**（宁可等，不偷活锁/不误杀）。"""
        path = BASE / "data" / "weekly" / "_test_filelock_probe.lock"
        lock = FileLock(path)
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{os.getpid()}\n", encoding="utf-8")   # 只有 pid，没有滴答数
        if os.name == "nt":
            self.assertFalse(lock._stale(),
                             "Windows 判不了持锁者 ⇒ 不许当陈旧（否则会去搬锁并误杀）")
        else:
            self.assertFalse(lock._stale(), "POSIX 上自己的 pid 活着 ⇒ 锁有效")


class TestTerminatedUndestroyedHolder(unittest.TestCase):
    """2026-10-08 事故回归：持有者已终止但进程对象未销毁 ⇒ 锁必须判陈旧。

    当日实锤：服务实例 08:5x 无痕迹死亡，但拉起链（看门狗 `Popen` 未 wait、
    任务计划/wscript 包装链）还握着它的句柄 ⇒ 进程对象未销毁 ⇒
    OpenProcess/GetProcessTimes 照样成功、滴答数与锁里一致 ⇒ 旧判据把它当活锁
    ⇒ 看门狗拉起的新实例撞锁秒退，误发"自愈失败需要人工"。
    修复：`_stale()` 先问 `GetExitCodeProcess`（退出码 ≠ STILL_ACTIVE ⇒ 已终止，
    与对象是否销毁无关）。
    """

    def setUp(self):
        self.dir = BASE / "data" / "weekly" / "_test_filelock_strict"
        self.dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_terminated_but_undestroyed_holder_is_stale(self):
        """真复现：kill+wait 之后 Popen 句柄仍开着 ⇒ 对象未销毁 ⇒ 旧判据(滴答比对)
        恰好在这里失明；新判据(退出码)必须判 False/陈旧。"""
        if os.name != "nt":
            self.skipTest("Windows 专属：句柄未关闭的已终止进程")
        import subprocess
        from unittest import mock
        from app import timeutil
        lock = FileLock(self.dir / "service.lock")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            ticks = timeutil.proc_start_ticks(proc.pid)
            self.assertNotIn(ticks, (-1, None), "测试前提：活进程滴答数必须查得到")
            lock.path.write_text(f"{proc.pid}\n{ticks}", encoding="utf-8")
            proc.kill()
            proc.wait(timeout=10)     # 确定已终止；但 Popen 句柄仍开着 ⇒ 对象未销毁
            # 前提自证：对象未销毁 ⇒ 旧判据的 GetProcessTimes 依然"查得到滴答数"
            self.assertEqual(timeutil.proc_start_ticks(proc.pid), ticks,
                             "进程对象已被销毁，复现前提不成立（本用例失去意义）")
            self.assertIs(filelock._proc_alive_strict(proc.pid), False,
                          "退出码判活必须识破已终止进程，而非 True/None")
            self.assertTrue(lock._stale(),
                            "持有者已终止(对象未销毁)必须判陈旧，否则新实例永远撞锁")
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    def test_strict_alive_false_overrides_matching_ticks(self):
        """mock 回归：即使滴答数完全一致，alive_strict=False 也必须判陈旧。"""
        from unittest import mock
        path = self.dir / "mocked.lock"
        lock = FileLock(path)
        path.write_text("4242\n777", encoding="utf-8")
        with mock.patch.object(filelock, "_proc_alive_strict", return_value=False), \
             mock.patch.object(filelock, "_proc_start_ticks", return_value=777):
            self.assertTrue(lock._stale())

    def test_strict_alive_true_or_unknown_keeps_original_judgement(self):
        """alive_strict=True/None 不改判 —— 原有保守语义（宁可等锁）原样保留。"""
        from unittest import mock
        path = self.dir / "mocked2.lock"
        lock = FileLock(path)
        path.write_text("4242\n777", encoding="utf-8")
        for alive in (True, None):
            with mock.patch.object(filelock, "_proc_alive_strict", return_value=alive), \
                 mock.patch.object(filelock, "_proc_start_ticks", return_value=777):
                self.assertFalse(lock._stale(),
                                 f"alive_strict={alive!r} 时应走原逻辑：滴答一致 ⇒ 锁有效")


class TestGarbageSweep(unittest.TestCase):
    def setUp(self):
        self.dir = BASE / "data" / "weekly" / "_test_filelock_sweep"
        if self.dir.exists():
            import shutil
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True)
        self.lock = FileLock(self.dir / "ledger.json.lock")

    def tearDown(self):
        import shutil
        if self.dir.exists():
            shutil.rmtree(self.dir, ignore_errors=True)

    def test_sweep_removes_stale_and_old_tmp_but_keeps_fresh_tmp(self):
        stale = self.dir / "ledger.json.lock.stale.1234.5678"
        old_tmp = self.dir / "ledger.json.lock.tmp.9999"
        fresh_tmp = self.dir / f"ledger.json.lock.tmp.{os.getpid()}"
        for p in (stale, old_tmp, fresh_tmp):
            p.write_text("x", encoding="utf-8")
        old = time.time() - 7200                       # 2 小时前
        os.utime(old_tmp, (old, old))
        self.lock._cleanup_stale_locks()
        self.assertFalse(stale.exists(), ".stale.* 该清（已无锁语义）")
        self.assertFalse(old_tmp.exists(), "1 小时前的 .tmp.* 该清")
        self.assertTrue(fresh_tmp.exists(), "刚建的 .tmp.* 可能属于别人，不许碰")


class TestStaleTakeoverRace(unittest.TestCase):
    """2026-09-19 审查 A4：两个进程同时判陈旧时，**不许出现双持有者**。

    危害不是"抢锁失败"而是"抢成功的那个以为自己独占了" —— 于是 18:00 链与手动触发
    能同时写同一份账本，正是这把锁存在的理由。两条测试各自钉住一道加固：
      ① 搬走陈旧锁**之前**复读比对：期间被他人接管 ⇒ 对方那把是活锁，不许搬；
      ③ `os.link` 成功**之后**回读认领：锁被搬走 ⇒ 不许继续以为自己在锁内。
    交错用**真实文件 + 钩子**复现（不 mock 掉 acquire 本身），断言的是最终落盘状态。
    ⚠️ "对方的锁"用 foreign pid，其存活与否由 `_stale` 桩代言 —— 拿自己的 pid 冒充会让
    token 与对方内容相同，第③道加固根本分不出来（本测试第一版就因此假绿）。
    """

    DEAD = "999998\n1"                 # 死掉的持有者 ⇒ 真实 `_stale()` 判为陈旧
    LIVE = "999997\n424242"             # 另一个进程的活锁（跨平台无法证其存活 ⇒ 由桩代言）

    def setUp(self):
        self.dir = BASE / "data" / "weekly" / "_test_filelock_race"
        if self.dir.exists():
            import shutil
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True)
        self.path = self.dir / "ledger.json.lock"
        self.path.write_text(self.DEAD, encoding="utf-8")

    def tearDown(self):
        import shutil
        if self.dir.exists():
            shutil.rmtree(self.dir, ignore_errors=True)

    def _stale_stub(self, lock):
        """`LIVE` 内容 ⇒ 判为活着；其余走真实判据（陈旧锁那部分不许架空）。"""
        real = FileLock._stale

        def _s():
            return False if lock._content() == self.LIVE else real(lock)
        return _s

    def test_takeover_between_judgement_and_move_is_not_stolen(self):
        """① 判完"陈旧"之后、动手搬之前被他人接管 ⇒ 那把已是活锁，不许搬。"""
        lock = FileLock(self.path)
        flipped = []
        real = FileLock._stale

        def _s():
            if lock._content() == self.LIVE:
                return False                    # 对方进程活着（真实判据由桩代言）
            r = real(lock)
            if r and not flipped:               # 我们刚判完陈旧，此刻对手接管
                self.path.write_text(self.LIVE, encoding="utf-8")
                flipped.append(True)
            return r

        lock._stale = _s
        self.assertFalse(lock.acquire(retries=4),
                         "对方持有活锁 ⇒ 本轮必须认输，不许建第二把锁")
        self.assertTrue(flipped, "交错没构造成功（测试自身失效）")
        self.assertEqual(self.LIVE, self.path.read_text(encoding="utf-8").strip(),
                         "对方的活锁被搬走了 ⇒ A4 的复读比对没生效")

    def test_lock_snatched_after_link_is_not_claimed(self):
        """③ link 之后锁被搬走换成对方的 ⇒ acquire 必须 False，不许谎称持锁。"""
        lock = FileLock(self.path)
        lock._stale = self._stale_stub(lock)
        real_link = os.link
        snatched = []

        def _link_and_snatch(src, dst, *a, **kw):
            real_link(src, dst, *a, **kw)       # 锁已存在时这里抛 FileExistsError
            if not snatched:                    # 我们刚建好，对手把我们的搬走换成他的
                os.replace(dst, Path(str(dst) + ".stale.byother"))
                Path(dst).write_text(self.LIVE, encoding="utf-8")
                snatched.append(True)

        # 第 1 轮: link 抛 FileExistsError → 搬走陈旧锁 → 退避; 第 2 轮: link 成功 → 钩子
        # 搬走我们的并放上对方的 → 回读认领必须发现 token 不是自己的。
        filelock.os.link = _link_and_snatch
        try:
            self.assertFalse(lock.acquire(retries=3),
                             "锁已被搬走还返回 True ⇒ 双持有者，A4 的回读认领没生效")
        finally:
            filelock.os.link = real_link
        self.assertTrue(snatched, "交错没构造成功（测试自身失效）")
        self.assertEqual(self.LIVE, self.path.read_text(encoding="utf-8").strip(),
                         "不许把对方的锁抢回来")


if __name__ == "__main__":
    unittest.main()
