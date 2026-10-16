"""`app.main._is_service_process()` —— 崩溃留痕"只在服务进程装配"的守卫（2026-09-22 补）

## 为什么必须单独测这个函数

`app/main.py` 在**导入期**做一件事：

```python
if _is_service_process():
    _arm_crash_traces()
```

`_arm_crash_traces()` 会在 `data/crash/` 写一份 `started/atexit`、开 `faulthandler`、注册 `atexit`。
它在导入期**无条件**执行的年代，任何 import `app.main` 的短命进程（pytest、CLI 脚本）都会留一份
噪声 —— 实测一天 **20 份**，而 `_CRASH_KEEP=20` 正好把它们全留着 ⇒ **真崩溃的留痕被挤掉/淹没**。
这个目录存在的唯一理由就是 09-14 那次"死因未明"（`server.log` 零痕迹）：
**留痕目录被噪声占满 = 这个机制在最需要它的时候失效。**

⇒ 判据收成一句话：`sys.argv` 里出现 `uvicorn` 才装配。这个函数就是那条判据本身，
它错了不会有任何测试红（只是悄悄地又污染 `data/crash/`），所以必须有自己的用例。

## 写法上的红线：**不许用 `importlib.reload`**

本模块导入 `app.main` 时 `_is_service_process()` 已经跑过一次了。若为了"测 True 分支"去
`importlib.reload(app.main)` 且此时 `sys.argv` 含 `uvicorn` ⇒ **会真的执行 `_arm_crash_traces()`**，
当场往 `data/crash/` 写一份 —— 正好复现本函数要防的那个缺陷。
正确做法：函数在**调用时**读 `sys.argv`，所以 `mock.patch.object(sys, "argv", ...)` 就够了。
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.main import _is_service_process                        # noqa: E402

#: 崩溃留痕目录 —— 用来断言"本进程没有往这里写"
_CRASH_DIR = BASE / "data" / "crash"


def _owned_by(path: Path, pid: str) -> bool:
    """这份留痕是不是 `pid` 那个进程写的。

    `_arm_crash_traces()` 的首行固定是 `started pid=<pid> <时间> cmd=<命令行>`
    （2026-09-21 起加了 `cmd=`，更早的只有 pid+时间）。按这一行判归属，
    比"文件总数变没变"可靠 —— 服务看门狗拉起服务时也会写一份，那是别人的。
    """
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            return f.readline().startswith(f"started pid={pid} ")
    except OSError:
        return False


class TestIsServiceProcess(unittest.TestCase):
    """判据：命令行里出现 `uvicorn`（不区分大小写）才算"作为服务在跑"。"""

    @staticmethod
    def _run(argv):
        with mock.patch.object(sys, "argv", argv):
            return _is_service_process()

    def test_uvicorn_module_invocation_is_service(self):
        """`start.bat:46` 与 `scripts/service_watchdog.py:295` 两条启动路径都长这样。"""
        self.assertTrue(self._run(["python", "-m", "uvicorn", "app.main:app"]))

    def test_uvicorn_with_port_is_service(self):
        self.assertTrue(self._run(["python", "-m", "uvicorn", "app.main:app", "--port", "8787"]))

    def test_case_insensitive(self):
        """判据用的是 `str(a).lower()` ⇒ 大小写都要认（Windows 路径/CLI 不规范时兜底）。"""
        self.assertTrue(self._run(["python", "-m", "UVICORN", "app.main:app"]))
        self.assertTrue(self._run(["C:\\Python\\Scripts\\Uvicorn.exe", "app.main:app"]))

    def test_pytest_is_not_service(self):
        """**本测试自己**的进程形态：pytest 会 import `app.main`，绝不能装配。"""
        self.assertFalse(self._run(["pytest", "-q", "tests/"]))
        self.assertFalse(self._run([str(BASE / ".venv" / "Scripts" / "pytest.exe"), "-q"]))

    def test_cli_scripts_are_not_service(self):
        """`run_weekly.py` / `compute_factors.py` 这类 CLI 也 import `app.main`。"""
        for argv in (["python", "scripts/run_weekly.py", "doctor"],
                     ["python", "scripts/compute_factors.py"],
                     ["python", "scripts/audit_engineering.py"]):
            with self.subTest(argv=argv):
                self.assertFalse(self._run(argv))

    def test_empty_argv_is_not_service(self):
        self.assertFalse(self._run([]))

    def test_non_string_arg_does_not_raise(self):
        """`str(a)` 是为了这个：`sys.argv` 被外部塞非字符串时不许抛。"""
        self.assertTrue(self._run([12345, "uvicorn"]))
        self.assertFalse(self._run([None, 3.14]))

    def test_no_crash_trace_written_by_this_module(self):
        """阴性对照：本进程**不许**在 `data/crash/` 留下任何留痕。

        守的是"别用 reload 去测 True 分支"那条红线 —— 真有人 reload 了，这里会红，
        而不是悄悄污染目录（正是本函数要防的缺陷）。

        ⚠️ 判据刻意**不看文件总数**，而看"有没有**本进程 pid** 的留痕"：
        服务看门狗每 10 分钟探活、必要时会拉起服务并写一份 `service_*.log`，
        那个文件属于**另一个进程**、完全合法。若按"总数不变"断言，测试就会随
        看门狗的时间点**偶发假红**（把别人的正常留痕算成自己的）。按 pid 断言则与
        并发无关 —— 我们只关心"我这一跑有没有写"。
        """
        me = str(os.getpid())
        before = {p for p in _CRASH_DIR.glob("*") if _owned_by(p, me)} \
            if _CRASH_DIR.exists() else set()
        self._run(["python", "-m", "uvicorn", "app.main:app"])          # 只调函数，不 reload
        after = {p for p in _CRASH_DIR.glob("*") if _owned_by(p, me)} \
            if _CRASH_DIR.exists() else set()
        self.assertEqual(before, after,
                         f"本进程(pid={me}) 在 data/crash/ 留了痕: "
                         f"{sorted(p.name for p in after - before)} —— "
                         "若这里红了，多半是有人在测试里 importlib.reload 了 app.main")


if __name__ == "__main__":
    unittest.main()
