"""代码卫生的**硬规则守卫**（2026-09-14，来自一次全仓过度设计审计）

审计（`ponytail-audit` 口径，见 KB 12 号）当场量了四类经典反模式：

| 反模式 | 实测 | 结论 |
|---|---|---|
| 裸 `except:`（不带异常类型） | **0** | 保持 0 —— 它会把 KeyboardInterrupt/SystemExit 一起吞掉 |
| `print()` 出现在**库路径**（`app/**`，非 `__main__`） | **0** | 保持 0 —— 日志走 `logging`，print 会污染服务日志/邮件 |
| `TODO/FIXME/XXX/HACK` 标记 | **0** | 保持 0 —— 待办进 KB 台账，不进代码注释 |
| `except …: pass`（静默吞异常） | 13 处 | **逐个核对全部合法**（缓存读写、临时文件清理、日志自愈、锁文件清理）⇒ 不设阈值，改由评审管 |

为什么要有这个文件：这三条是"零容忍"型规则，而它们的**回归成本极低**（顺手写一个裸 except 谁都会），
靠人盯迟早破防。测试把它们钉死；真有正当理由时，改这里并写明理由（与 `test_db_conn_hygiene.py`
的棘轮同一思路：只许更严，不许更松）。

运行: python -m pytest tests/test_code_hygiene_ratchet.py -q
"""
import ast
import re
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

APP = BASE / "app"
SCRIPTS = BASE / "scripts"


def _files():
    return sorted(APP.rglob("*.py"))


class TestCodeHygiene(unittest.TestCase):
    def test_no_bare_except(self):
        bad = []
        for p in _files():
            try:
                tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and node.type is None:
                    bad.append(f"{p.relative_to(BASE).as_posix()}:{node.lineno}")
        self.assertEqual(bad, [], f"裸 except（要写具体异常类型或 Exception）: {bad}")

    def test_no_print_outside_main_guard(self):
        """库路径里的 print 会把内容混进服务 stdout（日志/邮件都读不到），统一用 logging。"""
        bad = []
        for p in _files():
            src = p.read_text(encoding="utf-8", errors="ignore")
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            main_lines = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.If):        # if __name__ == "__main__":
                    t = node.test
                    if (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
                            and t.left.id == "__name__"):
                        for sub in ast.walk(node):
                            if hasattr(sub, "lineno"):
                                main_lines.add(sub.lineno)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "print" and node.lineno not in main_lines):
                    bad.append(f"{p.relative_to(BASE).as_posix()}:{node.lineno}")
        self.assertEqual(bad, [], f"库路径里的 print(): {bad}")

    def test_no_unused_imports(self):
        """未使用的 import —— 2026-09-20 审查清理 13 处后钉死，防再堆积。

        用 AST + 正则而**不是** pyflakes：CI 与本机 .venv 都不装静态分析工具
        （requirements-lock.txt 里没有），靠外部依赖就守不住 —— 必须零依赖。

        范围 `app/**` + `scripts/*.py`（不含 `scripts/oneoff/`：那是受保护证据存档，
        由 test_oneoff_hygiene.py 单独守）。

        三类豁免（都是有意为之，不是漏网）：
          ① 带 `# noqa: F401` 的 re-export —— 如 trainer.py 的 name_at_hist，
             tests/test_review_fixes_0905.py 正是经 trainer.name_at_hist() 调用，
             删则 4 条断言失败；
          ② `from __future__ import ...` —— 影响语法，不直接使用；
          ③ `__init__.py` —— 包 re-export 是正常模式。

        判定用"除导入行外是否还有出现"：宁可漏报也不误报（守卫的目的是防堆积，
        不是当 linter 用）。
        """
        targets = sorted(APP.rglob("*.py")) + sorted(SCRIPTS.glob("*.py"))
        bad = []
        for p in targets:
            if p.name == "__init__.py":
                continue
            src = p.read_text(encoding="utf-8", errors="ignore")
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            lines = src.splitlines()
            for node in ast.walk(tree):
                pairs = []
                if isinstance(node, ast.Import):
                    for a in node.names:
                        if a.name == "__future__":
                            continue
                        pairs.append((a.asname or a.name.split(".")[0], node.lineno))
                elif isinstance(node, ast.ImportFrom):
                    if node.module == "__future__":
                        continue
                    for a in node.names:
                        if a.name == "*":
                            continue
                        pairs.append((a.asname or a.name, node.lineno))
                for name, lineno in pairs:
                    imp_line = lines[lineno - 1] if lineno <= len(lines) else ""
                    if "noqa" in imp_line and "F401" in imp_line:
                        continue
                    used = any(lineno != i
                               and re.search(r"\b" + re.escape(name) + r"\b", line)
                               for i, line in enumerate(lines, 1))
                    if not used:
                        bad.append("%s:%d %s" % (p.relative_to(BASE).as_posix(),
                                                 lineno, name))
        self.assertEqual(bad, [], "未使用的 import: %s" % bad)

    def test_no_todo_markers_in_code(self):
        """待办进 KB 台账（14 号），代码里只留 `ponytail:` 这类可检索的显式债标（当前也没有）。"""
        pat = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
        bad = []
        for p in _files():
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if pat.search(line):
                    bad.append(f"{p.relative_to(BASE).as_posix()}:{i}")
        self.assertEqual(bad, [], f"TODO/FIXME 标记: {bad}")


if __name__ == "__main__":
    unittest.main()
