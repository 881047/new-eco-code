"""14 号台账守卫的测试（2026-09-20 结清轮）—— 重点是"这条门能红"。

`scripts/audit_ledger_14.py` 管三件事：证据指针必须存在（且不许指向不可跟踪的 `logs/`）、
判决措辞必须落在"已做/不采纳/关闭/待跑"四词内、文档写的计数必须与代码/库现算一致。

为什么这个测试的大半篇幅在演"坏行"：本项目已经两次装上**永远不会红的闸**——
G2 撮合闸（买卖偏离混在一个序列里取均值 ⇒ 必然对消，见 14 号三补二十六）与
`test_web_weekly`（输入文件被 gitignore ⇒ CI 里永远 skip，见 §三补三十一）。
所以"守卫通过"本身不是证据，**"注入的坏行被抓住"才是**。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("audit_ledger_14", BASE / "scripts" / "audit_ledger_14.py")
mod = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(mod)

HDR = "| # | 事项 | 结论 | 证据指针 | 量级或代价 | 触发 / 复活条件 | 台账原位置 |\n"
SEP = "|---|---|---|---|---|---|---|\n"


def _row(verdict: str, pointer: str) -> str:
    return f"| Z1 | 用例 | {verdict} | {pointer} | 0 | never | 审核 |\n"


class TestLedger14Pointers(unittest.TestCase):
    def test_live_ledger_is_clean(self):
        """现行台账必须过三项检查（红了就说明又漂了，不是测试的错）。"""
        p = subprocess.run([sys.executable, "-X", "utf8",
                            str(BASE / "scripts" / "audit_ledger_14.py")],
                           capture_output=True, text=True, cwd=str(BASE))
        self.assertEqual(p.returncode, 0,
                         f"14 号台账守卫红：\n{p.stdout}\n{p.stderr}")

    def test_gate_is_falsifiable_end_to_end(self):
        """脚本自带的四条阴性对照必须全部抓到（任一漏网 ⇒ 该门是恒绿假门）。"""
        r = subprocess.run([sys.executable, "-X", "utf8", "-c",
                            "import importlib.util,json,sys;"
                            "spec=importlib.util.spec_from_file_location('a','scripts/audit_ledger_14.py');"
                            "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
                            "cur=m._current_sections(m.LEDGER.read_text('utf-8'));"
                            "print(json.dumps(m.check_negative_controls(cur, m.live_counts())))"],
                           capture_output=True, text=True, cwd=str(BASE))
        import json
        got = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual({k: v for k, v in got.items()},
                         {"抓虚构指针": True, "抓 logs 指针": True,
                          "抓越界措辞": True, "抓计数漂移": True},
                         f"有对照没抓住，说明守卫失灵：{got}")

    def test_missing_pointer_is_caught(self):
        cur = HDR + SEP + _row("待跑", "tests/test_definitely_not_a_real_file_xyz.py")
        bad, _ = mod.check_pointers(cur)
        self.assertTrue(any("test_definitely_not_a_real_file_xyz" in p for _, p, _ in bad),
                        "虚构指针没被抓到 ⇒ 指针检查是空的")

    def test_logs_pointer_is_caught(self):
        """`logs/` 被 .gitignore 忽略 ⇒ 只落 logs 的证据当年整批消失过（三补二十九·补）。"""
        cur = HDR + SEP + _row("关闭", "logs/ab_bench_tr_real_index.json")
        _, hits = mod.check_pointers(cur)
        self.assertTrue(hits and hits[0][1].startswith("logs/"), "指向 logs/ 没被抓到")

    def test_wildcard_and_deleted_with_retrieval_are_tolerated(self):
        """两条**合法**写法不许假红：通配写法、以及"已删 + 行内写明 `git show <sha>:<path>`"。"""
        cur = (HDR + SEP + _row("已做", "plans/mirror-* 这类通配不算指针")
               + _row("已做", "experiments/mirror/M0_report.md 已删（4a2bfd72），"
                              "git show 1dc2f5c3:experiments/mirror/M0_report.md 可复"))
        bad, logs = mod.check_pointers(cur)
        self.assertEqual(bad, [], f"合法写法被判假红：{bad}")
        self.assertEqual(logs, [])

    def test_out_of_vocabulary_verdict_is_caught(self):
        """本轮之前台账里有 8+ 处"留案结清/部分过门/待用户点头"这类越界措辞。"""
        cur = HDR + SEP + _row("留案结清", "tests/test_db_conn_hygiene.py")
        bad = mod.check_words(cur)
        self.assertTrue(bad and "留案结清" in bad[0][1], f"越界措辞没被抓到：{bad}")
        self.assertEqual(mod.check_words(HDR + SEP + _row("关闭", "tests/test_db_conn_hygiene.py")), [],
                         "四词内的正常行被判红 ⇒ 措辞检查过严")

    def test_count_drift_is_caught_both_directions(self):
        """文档写"N 键 / 键数 = N"两种写法都得与现算比对。"""
        live = dict(mod.live_counts())
        key = "现行 pin 键数"
        if live.get(key) is None:
            self.skipTest("读不到现行 pin 的 metrics（库不可用）")
        live[key] = 999
        for text in ("报告腿共 32 个键。\n", "指标 `metrics` 键数 = 32。\n"):
            self.assertTrue(mod.check_counts(text, live),
                            f"计数漂移未被抓到（写法：{text.strip()[:12]}）")


if __name__ == "__main__":
    unittest.main()
