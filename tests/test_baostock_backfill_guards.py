"""采集脚本的**恢复路径**守卫（2026-09-16 事故 #57）。

事故：一次 `10002007 网络接收错误` 走进重试分支后，`bs.logout()` 在坏 socket 上抛异常，
而它在 `except` 块里 ⇒ 没人接 ⇒ 采集进程当场死掉；隐藏启动又没重定向输出 ⇒ traceback 都没留下，
19:13 死到 20:22 才被我发现（白丢 1 小时）。

这里只钉一条**行为**：`relogin()` 在任何 vendor 异常下都**必须返回**、不许抛。
（跑完不真睡：把模块里的 `time.sleep` 打桩掉。）
"""
import ast
import importlib.util
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
SCRIPTS = {
    "min60": BASE / "scripts" / "oneoff" / "backfill_baostock_min60.py",
    "witness": BASE / "scripts" / "oneoff" / "backfill_baostock_witness.py",
}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _BsLogoutRaises:
    def logout(self):
        raise OSError("broken pipe")

    def login(self):
        raise OSError("no route to host")


class _BsLoginRefused:
    class _R:
        error_code, error_msg = "10001001", "登录失败"

    def logout(self):
        return None

    def login(self):
        return self._R()


class TestReloginNeverRaises(unittest.TestCase):
    def test_every_script_recovers_from_vendor_exceptions(self):
        with TemporaryDirectory() as td:
            logf = Path(td) / "x.log"
            for name, path in SCRIPTS.items():
                with self.subTest(script=name):
                    mod = _load(f"bs_{name}", path)
                    with mock.patch.object(mod.time, "sleep"):      # 不真睡
                        mod.relogin(_BsLogoutRaises(), logf)        # 不许抛
                        mod.relogin(_BsLoginRefused(), logf)        # 不许抛
                    self.assertIn("logout 失败", logf.read_text(encoding="utf-8"))
                    self.assertTrue(logf.read_text(encoding="utf-8").count("login 抛异常") >= 1)

    def test_recovery_paths_use_relogin_not_bare_calls(self):
        """AST 结构断言：`relogin` 之外最多只许有一处 `bs.logout()`（收尾那次）。

        用 AST 而不是数文本 —— 第一版按字符串数，`relogin` docstring 里引用的
        `bs.logout()` 也被算成调用，当场假红（与 07 号 #55 的坑同一个）。
        """
        def logout_calls(src: str) -> tuple:
            tree = ast.parse(src)
            total = [n for n in ast.walk(tree)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "logout"]
            inner = 0
            for fn in [n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == "relogin"]:
                inner = sum(1 for n in ast.walk(fn)
                            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                            and n.func.attr == "logout")
            return len(total), inner

        for name, path in SCRIPTS.items():
            with self.subTest(script=name):
                src = path.read_text(encoding="utf-8")
                self.assertIn("def relogin(", src)
                total, inner = logout_calls(src)
                self.assertEqual(inner, 1, "relogin 里应当恰好有一处 logout")
                self.assertLessEqual(total - inner, 1,
                                     "relogin 之外出现多处裸 logout：重试分支裸调 vendor = 会杀进程")


if __name__ == "__main__":
    unittest.main()
