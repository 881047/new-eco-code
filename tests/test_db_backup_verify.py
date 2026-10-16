"""备份可读性校验（2026-09-13）

`db.backup_db()` 只保证**文件写出来了**；能不能读出来是另一回事，而这件事从来没人验过。
本库是全项目唯一的真相源，真出事那天才发现备份是坏的不可接受 ⇒ 周日档自动 quick_check。

守三条：① 好备份判 ok；② 坏文件（不是 SQLite）判 not ok 且**不抛异常**；
③ 不存在的路径判 not ok。另外锁死"只读"：校验**不许改动**被校验的文件（比对 mtime+大小）。
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import db  # noqa: E402


class TestVerifyBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="neweco_bkverify_"))

    def tearDown(self):
        for p in self.tmp.glob("*"):
            p.unlink()
        self.tmp.rmdir()

    def _good_db(self) -> Path:
        src = self.tmp / "src.db"
        con = sqlite3.connect(src)
        con.execute("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT)")
        con.executemany("INSERT INTO t VALUES (?,?)", [(i, f"v{i}") for i in range(50)])
        con.commit()
        con.close()
        return src

    def test_healthy_backup_passes(self):
        src = self._good_db()
        bk = self.tmp / "bk.db"
        s, d = sqlite3.connect(src), sqlite3.connect(bk)
        with d:
            s.backup(d)
        d.close()
        s.close()
        ok, detail = db.verify_backup(bk)
        self.assertTrue(ok, f"健康备份被判坏: {detail}")
        self.assertEqual(detail.lower(), "ok")

    def test_verify_does_not_modify_the_file(self):
        src = self._good_db()
        bk = self.tmp / "bk.db"
        s, d = sqlite3.connect(src), sqlite3.connect(bk)
        with d:
            s.backup(d)
        d.close()
        s.close()
        before = (bk.stat().st_size, bk.stat().st_mtime_ns)
        db.verify_backup(bk)
        after = (bk.stat().st_size, bk.stat().st_mtime_ns)
        self.assertEqual(before, after, "校验过程改动了备份文件 —— 只读口径被破坏")

    def test_garbage_file_fails_without_raising(self):
        bad = self.tmp / "bad.db"
        bad.write_bytes("这不是一个 SQLite 文件".encode("utf-8") * 100)
        ok, detail = db.verify_backup(bad)
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_missing_file_fails(self):
        ok, detail = db.verify_backup(self.tmp / "nope.db")
        self.assertFalse(ok)
        self.assertIn("不存在", detail)


if __name__ == "__main__":
    unittest.main()
