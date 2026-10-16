"""**子进程不许在桌面闪窗**（2026-09-13，用户实拍"时不时弹窗又消失"之后的守卫）

为什么需要：本项目有两类"无控制台"上下文 —— 看门狗（计划任务，Interactive only）与
服务进程（由看门狗用 `DETACHED_PROCESS` 拉起，本身没有控制台）。在这两种上下文里，
**启动任何控制台程序**（cmd/git/python.exe）Windows 都会给它**新建一个控制台窗口** ⇒ 闪一下。

守三条：
  ① `plan._code_version()` 调 `git` 必须带 `CREATE_NO_WINDOW`（出方案时会被调用）；
  ② 看门狗 `restart()` 必须用 `DETACHED_PROCESS`（否则服务自己带个黑窗）；
  ③ **静态扫描**：`app/**` 里所有 `subprocess.run/Popen/call/check_output` 调用都必须在
     调用表达式内显式写 `creationflags`（生产代码一律无窗；带不带隐藏标志由调用点决定）。

运行: python -m unittest tests.test_no_flashing_subprocess -v
"""
import re
import sys
import importlib.util
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.weekly import plan as wk_plan          # noqa: E402


def _call_text(src: str, start: int) -> str:
    """从 `subprocess.xxx(` 的 '(' 起做括号配对，返回整个调用表达式文本。"""
    i = src.index("(", start)
    depth, j = 0, i
    while j < len(src):
        if src[j] == "(":
            depth += 1
        elif src[j] == ")":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
        j += 1
    return src[i:i + 400]


class TestPlanCodeVersion(unittest.TestCase):
    def test_git_call_hides_console_window(self):
        fake = mock.Mock(stdout="abc1234\n")
        with mock.patch.object(wk_plan.subprocess, "run", return_value=fake) as m:
            self.assertEqual(wk_plan._code_version(), "abc1234")
        flags = m.call_args.kwargs.get("creationflags", 0)
        self.assertTrue(flags & getattr(wk_plan.subprocess, "CREATE_NO_WINDOW", 1),
                        "git 从无控制台的服务里拉起会新建窗口闪一下 ⇒ 必须带 CREATE_NO_WINDOW")


class TestWatchdogRestart(unittest.TestCase):
    """⚠️ 本类调**真的** `wd.restart()`（只把 Popen 打桩）⇒ 它会把 "服务不在 ⇒ 已拉起"
    写进 `wd.LOG`，并打开 `logs/server.log`（追加句柄）。2026-09-14 外部复核实测：生产
    `logs/watchdog.log` 里已有 **72 行**这种测试产物 ⇒ 这里必须把两个落点都指向测试文件。"""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "svc_watchdog_flash", BASE / "scripts" / "service_watchdog.py")
        self.wd = importlib.util.module_from_spec(spec)
        sys.modules["svc_watchdog_flash"] = self.wd
        spec.loader.exec_module(self.wd)
        logs = BASE / "logs"
        self.log = logs / "_test_flash.log"
        self.state = logs / "_test_flash.state"
        self.alert_state = logs / "_test_flash.alert"
        self.heal_state = logs / "_test_flash.heal.json"
        self.server_log = logs / "_test_flash_server.log"
        self.events_log = logs / "_test_flash.events.log"
        for p in (self.log, self.state, self.alert_state, self.heal_state, self.server_log,
                  self.events_log):
            p.unlink(missing_ok=True)
        tmp = Path(self._tmpdir())
        self._tmp = tmp          # 2026-09-24 补：留给 tearDown 删（原来只存局部变量）
        (tmp / "logs").mkdir(parents=True, exist_ok=True)     # restart() 会打开 BASE/logs/server.log
        self._st = ExitStack()
        # ⚠️ 把 BASE 整体指到临时目录，这样 `restart()` 里所有由 BASE 派生的路径都不会碰生产
        # 文件（Popen 已打桩 ⇒ 环境变量里的解释器路径无所谓）。
        # 2026-09-22 更正：这里原写「`logs/server.log` 是**硬编码**打开的（不是常量）」——
        # 自 2026-09-14（07 #38）起它已提成常量 `SERVER_LOG`、`open` 也移进了 try，
        # 2026-09-21 又改为走 `NEWECO_LOG_DIR` 红道。旧注释会误导后来者以为 patch 常量没用，
        # 从而漏掉落点（本文件 tearDown 漏 `events_log` 就是这么来的）。
        self._st.enter_context(mock.patch.object(self.wd, "BASE", tmp))
        self._st.enter_context(mock.patch.object(self.wd, "LOG", self.log))
        self._st.enter_context(mock.patch.object(self.wd, "STATE", self.state))
        self._st.enter_context(mock.patch.object(self.wd, "ALERT_STATE", self.alert_state))
        # 自愈台账（同日合并/计数）也是**模块级常量** ⇒ patch BASE 不会带走它，必须单独指
        self._st.enter_context(mock.patch.object(self.wd, "HEAL_STATE", self.heal_state))
        # 探活失败现场(2026-09-20 新增落点)同样是模块级常量 ⇒ 必须单独指走
        self._st.enter_context(mock.patch.object(self.wd, "EVENTS_LOG", self.events_log))

    @staticmethod
    def _tmpdir() -> str:
        import tempfile
        return tempfile.mkdtemp(prefix="neweco_flash_")

    def tearDown(self):
        self._st.close()
        # 2026-09-22 补：`events_log` 在 setUp 里已建并 patch，但清理元组漏了它 ⇒
        # 每次跑测试都在生产 `logs/_test_flash.events.log` 留一份（07 号 #1 同族第四次踩）。
        # 纪律：setUp 里 patch 了几个常量，tearDown 就必须清理几个。
        for p in (self.log, self.state, self.alert_state, self.heal_state, self.server_log,
                  self.events_log):
            p.unlink(missing_ok=True)
        # 2026-09-24 补：上面删的是**文件**，而 `BASE` 被 patch 到的那个临时**目录**
        # 从没删过 ⇒ 本机 `%TEMP%` 累积了 248 个 `neweco_flash_*`（07 号 #64 同族）。
        # ⚠️ 同一条纪律的第二半：**patch 到哪，就要把那一层也收干净**。
        import shutil
        tmp = getattr(self, "_tmp", None)
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_restart_detaches_from_console(self):
        wd = self.wd
        with mock.patch.object(wd, "real_interpreter", return_value=r"D:\python\python.exe"), \
             mock.patch.object(wd.subprocess, "Popen") as m, \
             mock.patch.object(wd, "_mark_restart"):
            wd.restart()
        flags = m.call_args.kwargs.get("creationflags", 0)
        self.assertTrue(flags & getattr(wd.subprocess, "DETACHED_PROCESS", 1),
                        "服务必须脱离控制台启动（DETACHED_PROCESS），否则自己带一个黑窗")


class TestAppSubprocessCallsDeclareFlags(unittest.TestCase):
    PAT = re.compile(r"subprocess\.(run|Popen|call|check_output)\s*\(")

    def test_every_call_declares_creationflags(self):
        bad = []
        for p in sorted((BASE / "app").rglob("*.py")):
            src = p.read_text(encoding="utf-8", errors="ignore")
            for m in self.PAT.finditer(src):
                call = _call_text(src, m.start())
                if "creationflags" not in call:
                    ln = src[:m.start()].count("\n") + 1
                    bad.append(f"{p.relative_to(BASE)}:{ln}")
        self.assertEqual(bad, [], f"这些生产代码里的子进程没声明 creationflags（会闪窗）: {bad}")


if __name__ == "__main__":
    unittest.main()
