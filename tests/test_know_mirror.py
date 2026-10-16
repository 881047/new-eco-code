r"""知识库镜像守卫（2026-09-13）

用户当天问过"知识库怎么不见了" —— 根因是它建在仓库外（`D:\\know`），
**git 里看不见、也没有历史**。现在 canonical 仍在 `D:\\know`（用户指定），
但仓库里有一份随提交走的**镜像** `docs/know/`。

本测试守三件事：
  1. 镜像存在且非空（"看不见"这类误解不该再发生）；
  2. 镜像与源**逐文件内容一致**（不一致就红，提示跑 `scripts/sync_know.py`）；
  3. 镜像里必须有那几份关键台账（纪律 / 口径 / 事故 / pin）—— 它们是"接手就能用"的最小集。

    ⚠️ 源在别的机器上不存在时**跳过**（仓库本身仍自带镜像，clone 下来也能读）。

2026-09-13 扩展：Codex skill（`D:\codex-home\skills\new-eco-know`，讲"哪件事读哪一份"）
同样建在仓库外、同样会丢 ⇒ 一起镜像并守一致性；镜像里的 `SKILL.md` 必须仍是**可被 Codex 加载的
frontmatter**（name/description），否则镜像只是一份好看的备份。
"""
import hashlib
import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SRC = Path(r"D:\know")
DST = BASE / "docs" / "know"
SKILL_SRC = Path(r"D:\codex-home\skills\new-eco-know")
SKILL_DST = BASE / "docs" / "codex-skill" / "new-eco-know"
SKILL_FILES = ("SKILL.md", "agents/openai.yaml")
KEY_FILES = ("README.md", "01_纪律与不变量.md", "02_口径与当前读数.md",
             "07_事故台账.md", "08_pin台账.md")
_src_ok = SRC.exists()
_skill_src_ok = SKILL_SRC.exists()

#: 镜像文件数下限（只在**本机没有源**时兜底，用于"clone 下来也要能自检"）；
#: 2026-09-14 外部审查 P2-11 之前写的是 8 —— 文件少一半照样绿，拦不住劣化。
MIN_MIRROR_MD = 20
#: skill 路由表条目下限（有源时另外要求"覆盖每一份知识库 md"）
MIN_SKILL_ROUTES = 20


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class TestMirrorExists(unittest.TestCase):
    def test_mirror_present_and_nonempty(self):
        self.assertTrue(DST.exists(), "仓库里没有 docs/know/ 镜像 —— 知识库又变成'看不见'了")
        # 2026-09-14 外部审查 P2-11：原来是"≥8 个 md"这种下限型阈值 —— 文件少掉一半照样绿，
        # 拦劣化能力等于没有。改成"与源同数"的硬断言（本机无源时由上面的 skipUnless 兜住）。
        n_dst = len(list(DST.glob("*.md")))
        if _src_ok:
            self.assertEqual(n_dst, len(list(SRC.glob("*.md"))),
                             "镜像与源的 md 数量不一致（漏同步或漏删）")
        else:
            self.assertGreaterEqual(n_dst, MIN_MIRROR_MD)

    def test_key_ledgers_present(self):
        names = {p.name for p in DST.glob("*.md")}
        missing = [n for n in KEY_FILES if n not in names]
        self.assertEqual(missing, [], f"镜像缺关键台账: {missing}")


@unittest.skipUnless(_src_ok, "本机没有 D:\\know 源（镜像仍在仓库里，可正常阅读）")
class TestMirrorInSync(unittest.TestCase):
    def test_every_source_file_mirrored_byte_identical(self):
        src = {p.name: p for p in SRC.glob("*.md")}
        dst = {p.name: p for p in DST.glob("*.md")}
        self.assertEqual(sorted(set(src) - set(dst)), [],
                         "镜像缺文件 —— 跑 scripts/sync_know.py")
        stale = [n for n in src if n in dst and _sha(src[n]) != _sha(dst[n])]
        self.assertEqual(stale, [], f"镜像与源不一致: {stale} —— 跑 scripts/sync_know.py")


class TestSkillMirror(unittest.TestCase):
    """Codex skill 的镜像：存在 + frontmatter 可加载 + （源在时）逐字节一致。"""

    def test_skill_mirror_present_and_loadable(self):
        for rel in SKILL_FILES:
            self.assertTrue((SKILL_DST / rel).is_file(), f"skill 镜像缺 {rel}")
        head = (SKILL_DST / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(head.startswith("---\n"), "镜像 SKILL.md 缺 frontmatter ⇒ Codex 加载不了")
        fm = head.split("---", 2)[1]
        self.assertIn("name: new-eco-know", fm)
        self.assertIn("description:", fm)

    def test_skill_routes_point_at_real_files(self):
        """路由表里点名的知识库文件必须真的存在 —— 死指针会让 skill 变成误导。"""
        body = (SKILL_DST / "SKILL.md").read_text(encoding="utf-8")
        routes = set(re.findall(r"\d\d_[^`|）。\s]+\.md", body))
        # 2026-09-14 外部审查 P2-11：下限型阈值 ⇒ 改成"覆盖全部知识库 md"的硬要求
        self.assertGreaterEqual(len(routes), MIN_SKILL_ROUTES,
                                "路由表太稀，说明 skill 被改坏了")
        if _src_ok:
            uncovered = sorted(p.name for p in SRC.glob("*.md")
                               if p.name not in routes and p.name != "README.md")
            self.assertEqual(uncovered, [],
                             f"这些知识库文件没被 skill 路由表覆盖（新增文件要登记路由）: {uncovered}")
        missing = sorted(r for r in routes if not (DST / r).is_file())
        self.assertEqual(missing, [], f"skill 指向不存在的知识库文件: {missing}")

    @unittest.skipUnless(_skill_src_ok, "本机没有 skill 源（镜像仍在仓库里，可手工恢复）")
    def test_skill_mirror_matches_source(self):
        for rel in SKILL_FILES:
            src, dst = SKILL_SRC / rel, SKILL_DST / rel
            self.assertTrue(src.is_file(), f"源 skill 缺 {rel}")
            self.assertTrue(dst.is_file(), f"镜像 skill 缺 {rel} —— 跑 scripts/sync_know.py")
            self.assertEqual(_sha(src), _sha(dst),
                             f"skill 镜像与源不一致: {rel} —— 跑 scripts/sync_know.py")


if __name__ == "__main__":
    unittest.main()
