"""`scripts/oneoff/` 的卫生检查 (2026-09-12 审查 P2-22)

这个目录是**证据**, 不是代码垃圾堆: 台账(在仓库外)把「run / 表 / 模型目录 / 脚本 / 日志
→ 结论」一一钉死, 所以那一百来个脚本只能原地放着(2026-09-18 实点 101 个; 此处旧文案
写的 79、CLAUDE.md 旧写的 89 都是历史值 —— 下面的守卫按目录**双向收敛**, 数字漂了不红)。
但"原地放着"得有东西守着 —— 否则某天
一次"整理目录"就会让台账里的路径引用集体悬空, 而发现这件事的时刻, 是几个月后有人想复核
某条结论的时候(那时已经说不清"那个脚本去哪了")。

四条断言, 每条对应一种具体的坏结局:

1. **引用不悬空** —— app/ tests/ scripts/*.py 与仓库根 md 里提到的 oneoff 脚本必须真实
   存在。批量归档/改名会在这里当场炸, 并打印"谁在引用它"。
2. **冻结脚本在顶层** —— 被引用的脚本必须直接躺在 `scripts/oneoff/` 下, 不能出现在子目录
   里。它们的项目根是 `Path(__file__).resolve().parents[2]` 推出来的: 往下挪一格就指向
   `scripts/`, 读不到 `data/`, 还会**静默**造出一个假的 `scripts/data/`。
3. **没有裸名 import** —— 不许 `import exp_xxx` / `from exp_xxx import ...`: 那要求先把
   `scripts/oneoff/` 插进 `sys.path`, 于是几十个一次性脚本对整轮测试裸名可见。要 import
   就按**文件路径**加载(`tests/test_expert.py` 的 `_frozen_gate()` 是样板)。
4. **生产不 import 一次性脚本** —— `app/` 只准在注释/docstring 里提到它们。

另有一条"说明书不许过期": README 的索引必须覆盖目录里每个脚本。

运行: ./.venv/Scripts/python.exe -m unittest tests.test_oneoff_hygiene -v
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
ONE = ROOT / "scripts" / "oneoff"

#: 路径式引用: `scripts/oneoff/xxx.py`
_PAT_PATH = re.compile(r"oneoff/([A-Za-z0-9_.-]+\.(?:py|ps1))")
#: 裸名引用: `xxx.py`(只在名字确实存在于 oneoff 时才算数 —— 见 `_referenced`)
_PAT_BARE = re.compile(r"(?<![/\w-])([A-Za-z0-9_]+\.(?:py|ps1))")

#: 已知的**占位名**(不是脚本): app/logger.py 的 docstring 里拿它当地示例
#: (`python scripts/oneoff/xxx.py > log.txt`)。白名单是显式的 —— 白名单外的任何悬空引用
#: 都会测红, 这正是我们要的: "目录里少了个被引用的脚本"永远不是小事。
_PLACEHOLDERS = {
    "xxx.py": "app/logger.py docstring 里当示例写的占位名",
}

_SCAN_TEXT_SUFFIXES = (".py", ".md", ".ps1")


def _scripts() -> set:
    return {p.name for p in ONE.iterdir()
            if p.suffix in (".py", ".ps1") and p.name != "__init__.py"}


def _scan_files() -> list:
    """会被扫描"引用"的文件: app/ tests/ scripts/*.py 与仓库根 md"""
    return ([p for p in (ROOT / "app").rglob("*.py")]
            + [p for p in (ROOT / "tests").rglob("*.py")]
            + [p for p in (ROOT / "scripts").glob("*.py")]
            + [p for p in ROOT.glob("*.md")])


def _referenced(scope: list = None) -> dict:
    """{脚本名: [引用它的仓库相对路径]}。

    两种写法都算引用: 路径式(`scripts/oneoff/<名字>.py`)与裸名(`<名字>.py`)。裸名只在该
    名字确实存在于 oneoff 时才算 —— 否则满仓库的 `test_*.py` 会全被当成"引用了一次性脚本"。
    """
    have = _scripts()
    out: dict = {}
    for p in (scope if scope is not None else _scan_files()):
        if not p.exists() or p.resolve() == (ONE / "README.md").resolve():
            continue
        if p.suffix not in _SCAN_TEXT_SUFFIXES:
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore").replace("\\", "/")
        hits = set(_PAT_PATH.findall(txt)) | (set(_PAT_BARE.findall(txt)) & have)
        for name in hits:
            out.setdefault(name, []).append(p.relative_to(ROOT).as_posix())
    return out


class TestReferencesResolve(unittest.TestCase):
    def test_referenced_scripts_exist(self):
        """被引用的脚本必须还在原地 —— 悬空引用 = 台账的可复核入口断了"""
        dangling = {}
        for name, who in _referenced().items():
            if not (ONE / name).exists() and name not in _PLACEHOLDERS:
                dangling[name] = sorted(who)
        self.assertEqual(
            dangling, {},
            "引用了 scripts/oneoff/ 里**并不存在**的脚本(是被归档/改名/删掉了吗? "
            "这些文件是证据, 只能原地放着 —— 见 scripts/oneoff/README.md): "
            + "; ".join(f"{n} ← {', '.join(w[:3])}" for n, w in sorted(dangling.items())))

    def test_placeholders_are_still_placeholders(self):
        """白名单本身也要守: 里面每一条都必须是"确实不存在"的名字。

        哪天有人真建了一个 `xxx.py`, 那它就不再是占位符而是一个**没被核对过**的脚本 ——
        该把它从白名单里拿掉、纳入正常引用检查。
        """
        for name in _PLACEHOLDERS:
            self.assertFalse((ONE / name).exists(),
                             f"{name} 现在真的存在了 —— 请把它从 _PLACEHOLDERS 里删掉")

    def test_frozen_scripts_are_not_moved_into_subdirs(self):
        """冻结脚本不许进子目录: `parents[2]` 是按"正下方"写死的"""
        frozen = set(_referenced()) - set(_PLACEHOLDERS)
        moved = sorted(p.relative_to(ONE).as_posix() for p in ONE.rglob("*")
                       if p.is_file() and p.name in frozen and p.parent != ONE)
        self.assertEqual(
            moved, [],
            "被引用的脚本出现在 scripts/oneoff/ 的子目录里 —— 它用 "
            "Path(__file__).resolve().parents[2] 推项目根, 挪一格就指向 scripts/ "
            "(读不到 data/, 还会静默造出假的 scripts/data/): " + ", ".join(moved))

    def test_readme_indexes_every_script(self):
        """目录的说明书不许过期: 每个脚本都要在 README 索引里"""
        readme = ONE / "README.md"
        self.assertTrue(readme.exists(), "scripts/oneoff/README.md 不见了")
        txt = readme.read_text(encoding="utf-8")
        missing = sorted(n for n in _scripts() if f"`{n}`" not in txt)
        self.assertEqual(missing, [], f"README 索引漏了: {missing}")
        # 两条硬规矩必须写在最显眼处(README 存在的意义就是它们)
        self.assertIn("不得删除", txt)
        self.assertIn("不许批量搬家", txt)


class TestNoBareNameImports(unittest.TestCase):
    """要 import 冻结脚本, 就按**文件路径**加载 —— 不许把它们变成裸名模块"""

    @staticmethod
    def _import_hits(files: list) -> list:
        have = {p.stem for p in ONE.iterdir() if p.suffix == ".py"}
        pat = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w.]*)|import\s+([A-Za-z_][\w.]*))")
        hits = []
        for p in files:
            for i, ln in enumerate(p.read_text(encoding="utf-8",
                                               errors="ignore").splitlines(), 1):
                m = pat.match(ln)
                if not m:
                    continue
                mod = (m.group(1) or m.group(2)).split(".")[0]
                if mod in have:
                    hits.append(f"{p.relative_to(ROOT).as_posix()}:{i}: {ln.strip()}")
        return hits

    def test_tests_do_not_import_oneoff_by_bare_name(self):
        hits = self._import_hits([p for p in (ROOT / "tests").rglob("*.py")])
        self.assertEqual(hits, [], "测试里出现了一次性脚本的**裸名 import** —— 那要求先把 "
                                  "scripts/oneoff/ 插进 sys.path, 于是几十个脚本对整轮测试"
                                  "裸名可见; 请改用 tests/test_expert.py 的按路径加载: "
                                  + " | ".join(hits))

    def test_app_does_not_import_oneoff_at_all(self):
        """生产代码压根不该依赖一次性脚本(只在注释/docstring 里提到它们)"""
        hits = self._import_hits([p for p in (ROOT / "app").rglob("*.py")])
        self.assertEqual(hits, [], "生产代码 import 了一次性脚本(它在 app/ 之外、且没有"
                                  "任何生命周期保证): " + " | ".join(hits))

    def test_oneoff_dir_is_not_on_sys_path(self):
        """回归守卫: 谁也不许再把 `scripts/oneoff/` 插进 sys.path"""
        oneoff = str(ONE.resolve())
        onpath = [str(Path(p).resolve()) for p in sys.path if p]
        self.assertNotIn(oneoff, onpath,
                         "scripts/oneoff 被插进 sys.path 了 —— 这会让目录里几十个脚本"
                         "对整轮测试裸名可见, 且命中与否只取决于 import 顺序")


if __name__ == "__main__":
    unittest.main()
