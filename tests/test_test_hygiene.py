"""测试卫生棘轮 —— "测试的测试"（2026-09-30 测试质量轮，用户指令"有问题的测试全部推倒重来"）。

## 为什么要有它（五亚型缺陷家族，09-30 盘点登记）

18 天事故台账里"基线/判定口径不对"的误判至少 9 起，横跨五个亚型；其中三个亚型的
缺陷会出现在**测试代码本身**，而静态可扫：

| 亚型 | 测试层的表现 | 本守卫的规则 |
|---|---|---|
| 哑判据（永不红/假绿） | `test_` 函数无任何断言——只能靠"恰好抛异常"变红，与"数据没有"同形 | R1 |
| 吞断言 | `try: assert ... except: pass`——断言失败被吞成绿 | R2 |
| 过宽判定 | `pytest.raises(Exception)`——任何异常都过，判据形同虚设 | R3 |

另两个亚型（方向反/样本量）无法静态扫，靠修复纪律覆盖：方向敏感量（MDD 等
"越负越好"）的改善方向必须配方向单测（2026-09-28 vol 缩放 runner 事故产物）；
不可逆动作（自动停买等）的判据必须带样本量守卫（2026-09-24 TURNOVER_MIN_DAYS 产物）。

## 白名单（隐式断言模式，逐条理由，删一条要给证据）

隐式断言 = 无显式 assert/raises，但失败会以异常形式变红的**合法**模式：
辅助函数内断言（golden 对比 `_check`）、循环内 `parse()`（300 棵树全须合法）、
"不抛"本身即语义（调度器缺席不崩）。白名单条目 = `文件名::测试名`，**新增测试
不允许进白名单**（新测试必须显式断言）。
"""
import ast
import re
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

TESTS = BASE / "tests"

#: R1 白名单——隐式断言模式（file::test_name → 理由）。只许删，不许加。
IMPLICIT_ASSERT_ALLOWLIST = {
    "test_backtest_golden.py::test_nav_and_metrics_match_snapshot":
        "断言在辅助 `_check`（golden 快照逐字段对比）内——golden 测试标准形态",
    "test_factory.py::test_generates_parseable_trees":
        "循环 300 次 `E.parse(s)`——生成树不合法即抛异常变红（隐式断言）",
    "test_factory.py::test_mutation_subtree_is_parseable":
        "变异子树 120 次 `E.parse(...)`——同上（隐式断言）",
    "test_review_fixes_0912.py::test_missing_scheduler_does_not_crash":
        "『不抛』本身就是被测语义（调度器缺席时 _fire 只记日志）",
}

_NAIVE_EXC = re.compile(r"raises\s*\(\s*Exception\s*\)|raises\s+Exception\b")


def _has_real_assert(fn: ast.AST) -> bool:
    """AST 级断言检测: ast.Assert / self.assertXxx / pytest.raises / fail。
    ⚠️ 首版审计用字符串 `"assert " in src` 漏掉全部 self.assertXxx——
    测量器自己犯了它要抓的错(2026-09-30)，故此函数必须是 AST 实现。"""
    for n in ast.walk(fn):
        if isinstance(n, ast.Assert):
            return True
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else (
                f.id if isinstance(f, ast.Name) else "")
            if name.startswith("assert") or name in ("raises", "fail"):
                return True
    return False


def scan() -> dict:
    bad = {"no_assert": [], "swallow": [], "broad_raises": [], "assert_true": []}
    for f in sorted(TESTS.glob("test_*.py")):
        src = f.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        rel = f.name
        for fn in ast.walk(tree):
            if not (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and fn.name.startswith("test")):
                continue
            qn = f"{rel}::{fn.name}"
            body_lines = src.splitlines()[fn.lineno - 1: fn.end_lineno]
            body_src = "\n".join(body_lines)
            if not _has_real_assert(fn) and qn not in IMPLICIT_ASSERT_ALLOWLIST:
                bad["no_assert"].append(qn)
            for node2 in ast.walk(fn):
                if isinstance(node2, ast.Try):
                    for h in node2.handlers:
                        if all(isinstance(s, (ast.Pass, ast.Continue, ast.Break))
                               for s in h.body):
                            try_body = "\n".join(
                                body_lines[node2.lineno - fn.lineno: h.lineno - fn.lineno])
                            if re.search(r"^\s*(?:self\.)?assert", try_body, re.M):
                                bad["swallow"].append(qn)
            if _NAIVE_EXC.search(body_src):
                bad["broad_raises"].append(qn)
            if re.search(r"^\s*assert\s+True\b", body_src, re.M):
                bad["assert_true"].append(qn)
    return bad


class TestTestHygiene(unittest.TestCase):
    """测试套件自身的质量棘轮：新增问题测试会在这里变红。"""

    def test_no_new_defect_patterns(self):
        bad = scan()
        for kind, lst in bad.items():
            self.assertEqual(lst, [], f"测试卫生({kind}): {lst}")

    def test_allowlist_only_shrinks(self):
        """白名单只许删不许加：以上 4 条是 2026-09-30 审计认定的合法隐式断言，
        每条带理由；新测试不得走白名单（写显式断言）。"""
        self.assertEqual(
            set(IMPLICIT_ASSERT_ALLOWLIST),
            {"test_backtest_golden.py::test_nav_and_metrics_match_snapshot",
             "test_factory.py::test_generates_parseable_trees",
             "test_factory.py::test_mutation_subtree_is_parseable",
             "test_review_fixes_0912.py::test_missing_scheduler_does_not_crash"})

    def test_ratchet_direction_is_verifiable(self):
        """棘轮自证（红蓝测试）：把扫描器喂一个带毒的假测试文件，必须抓到全部三类——
        守卫自己"永远绿"的话，它守的东西就是假的（假绿亚型的自我防御）。
        ⚠️ 毒样本字符串用拼接构造——字面写过宽 raises 写法会触发守卫自己的
        broad_raises 规则（守卫连 docstring/注释都扫，2026-09-30 首跑实测）。"""
        import tempfile
        poison = ("import unittest\n"
                  "class T(unittest.TestCase):\n"
                  "    def test_bare(self):\n"
                  "        x = 1\n"
                  "    def test_silent(self):\n"
                  "        try:\n"
                  "            assert 1 == 2\n"
                  "        except AssertionError:\n"
                  "            pass\n"
                  "    def test_wide(self):\n"
                  "        import pytest\n"
                  "        with pytest.raises" + "(Exception):\n"
                  "            raise ValueError\n")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "test_poison.py"
            p.write_text(poison, encoding="utf-8")
            import tests.test_test_hygiene as me
            with unittest.mock.patch.object(me, "TESTS", Path(td)):
                bad = me.scan()
        self.assertIn("test_poison.py::test_bare", bad["no_assert"],
                      "无断言测试没被抓到")
        self.assertIn("test_poison.py::test_silent", bad["swallow"])
        self.assertIn("test_poison.py::test_wide", bad["broad_raises"])


import unittest.mock  # noqa: E402  (放在底部避免顶部 import 噪声——此文件是元测试)
