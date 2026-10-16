"""`enable_utf8_stdio` 的回归测试 (2026-09-12)。

背景(实测事故, 不是假想): A/B 第 2 轮 `ab_const_n_5v6_r2.py` 六个回测臂全部跑完、
结果都已落库, 却在最后打印判决表时崩掉 ——

    UnicodeEncodeError: 'gbk' codec can't encode character '\\u2713'

因为脚本是被 `> log.txt` 重定向跑的(没有 `-X utf8`), Windows 下 Python 用 locale
(cp936)编码被重定向的输出。**崩的是最便宜的收尾段落, 昂贵的那半已经完成且不可见。**
修法见 `app/logger.py: enable_utf8_stdio`, 由 `setup_logger()` 调用。

这里用**子进程 + `PYTHONIOENCODING=cp936` + 重定向**复现真实现场(而不是 mock 编码),
并同时跑一个**不调 setup_logger 的对照** —— 否则这个测试可能只是因为环境本来就是
UTF-8 而"通过"(本项目已多次栽在"看着正常其实缺一条腿"的测试上)。
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

_SNIPPET = (
    "import sys; sys.path.insert(0, {root!r}); "
    "from app.logger import setup_logger; setup_logger(); "
    "print('OK \\u2713 \\u21d2 \\u2550')"
)
_UNGUARDED = "print('OK \\u2713 \\u21d2 \\u2550')"


class TestUtf8Stdio(unittest.TestCase):

    def _run(self, code: str):
        """把 code 以 cp936 编码的输出重定向到文件跑一遍, 返回 (returncode, 文本)。"""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.txt"
            env = dict(os.environ, PYTHONIOENCODING="cp936")
            env.pop("PYTHONUTF8", None)          # -X utf8 的等价环境变量, 必须去掉
            with open(out, "wb") as fh:
                proc = subprocess.run(
                    [sys.executable, "-c", code], stdout=fh, stderr=fh,
                    cwd=str(_ROOT), env=env, timeout=120)
            return proc.returncode, out.read_bytes().decode("utf-8", "replace")

    def test_setup_logger_makes_redirected_output_survive_non_ascii(self):
        rc, text = self._run(_SNIPPET.format(root=str(_ROOT)))
        self.assertEqual(rc, 0, "setup_logger() 之后仍打不出非 ASCII:\n" + text)
        self.assertIn("✓", text, "输出里没有 ✓, 内容: " + repr(text))

    def test_without_setup_logger_it_actually_crashes(self):
        """对照: 不调 setup_logger 时必须崩 —— 证明上面那条不是因为环境本来就是 UTF-8。"""
        rc, text = self._run(_UNGUARDED)
        self.assertNotEqual(rc, 0, "对照组居然没崩, 说明本测试无法证明守卫有效: " + repr(text))
        self.assertIn("UnicodeEncodeError", text, "崩的原因不是编码: " + repr(text))


if __name__ == "__main__":
    unittest.main()
