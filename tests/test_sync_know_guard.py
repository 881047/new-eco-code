"""知识库同步的**安全闸**（2026-09-14，外部复核 O6 指出后修）

为什么：`data` 侧的知识库镜像（`docs/know/**`）是**唯一带版本历史**的副本（canonical `D:\\know`
没有 git）。而 `sync_know.py` 原来唯一的守卫是"源目录存在"，`_files()` 又是**非递归** glob ⇒
`D:\\know` 被清空、或 `.md` 被挪进子目录时，源会变成 0 个文件 ⇒ 镜像里每个文件都被判成 `extra`
**逐个 unlink** ⇒ 一条命令删掉整库。恢复虽然能 `git checkout`，但这类操作不该"看起来正常"。

守四条：
  ① 源 0 个匹配文件 ⇒ 拒绝（返回 3），镜像**原封不动**；
  ② 源 < 镜像的 50%（且镜像 ≥4 个）⇒ 拒绝（返回 3）；
  ③ `--force` 是唯一的放行方式；
  ④ 正常同步（源 ≥ 镜像一半）照旧工作，包括删掉镜像里真正多余的旧文件。

运行: python -m pytest tests/test_sync_know_guard.py -q
"""
import importlib.util
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

_spec = importlib.util.spec_from_file_location("sync_know_guard", BASE / "scripts" / "sync_know.py")
sk = importlib.util.module_from_spec(_spec)
sys.modules["sync_know_guard"] = sk
_spec.loader.exec_module(sk)


class TestSyncKnowGuard(unittest.TestCase):
    def _setup(self, td: str, n_src: int, n_dst: int):
        src, mirror = Path(td) / "src", Path(td) / "mirror"
        src.mkdir(parents=True, exist_ok=True)
        mirror.mkdir(parents=True, exist_ok=True)
        for i in range(n_src):
            (src / f"f{i:02d}.md").write_text(f"src{i}", encoding="utf-8")
        for i in range(n_dst):
            (mirror / f"f{i:02d}.md").write_text(f"old{i}", encoding="utf-8")
        self._orig = sk.MIRRORS
        # 2026-10-15 对齐 8006aca9：MIRRORS 已扩为 5 元组（+required），夹具同步
        sk.MIRRORS = (("测试库", src, mirror, ("*.md",), True),)
        self.addCleanup(lambda: setattr(sk, "MIRRORS", self._orig))
        return src, mirror

    def test_empty_source_is_refused_and_mirror_survives(self):
        with TemporaryDirectory() as td:
            _src, mirror = self._setup(td, n_src=0, n_dst=5)
            rc = sk.main([])
            self.assertEqual(rc, 3, "源 0 个文件时必须拒绝同步")
            self.assertEqual(len(list(mirror.glob("*.md"))), 5, "镜像被删了 —— 守卫没起作用")

    def test_heavy_shrink_is_refused_without_force(self):
        with TemporaryDirectory() as td:
            _src, mirror = self._setup(td, n_src=2, n_dst=10)
            rc = sk.main([])
            self.assertEqual(rc, 3, "源只有镜像的 20% 必须拒绝")
            self.assertEqual(len(list(mirror.glob("*.md"))), 10)
            self.assertEqual(sk.main(["--force"]), 0, "--force 应当放行")
            self.assertEqual(len(list(mirror.glob("*.md"))), 2, "--force 后按源同步")

    def test_normal_sync_still_removes_true_extras(self):
        with TemporaryDirectory() as td:
            src, mirror = self._setup(td, n_src=4, n_dst=5)
            (src / "f99.md").write_text("new", encoding="utf-8")
            rc = sk.main([])
            self.assertEqual(rc, 0)
            self.assertEqual(sorted(p.name for p in mirror.glob("*.md")),
                             ["f00.md", "f01.md", "f02.md", "f03.md", "f99.md"])
            self.assertEqual((mirror / "f99.md").read_text(encoding="utf-8"), "new")


if __name__ == "__main__":
    unittest.main()
