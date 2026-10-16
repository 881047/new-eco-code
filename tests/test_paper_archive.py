"""模拟盘归档(`app/paper/archive.py`)的守卫测试(2026-09-14)

起因两条, 都是真实事故:

1. **2026-09-10 12:03 的重置无留痕** —— 重置前的持仓/成交/净值只剩 4.6 GB 全库备份可翻;
2. **2026-09-14 第一版归档脚本写出一份假归档** —— 它把游标结果直接喂 `dict(...)`, 6 张表
   全部变成 `{"error": "cannot convert dictionary update sequence element #0 ..."}`, 文件
   只有 636 字节, 但**看起来像成功了**(没有自检就发现不了)。

所以本文件钉四件事: ①导出的是**真行**(列名/取值); ②归档**只读**(库文件字节不变);
③`verify()` 抓得住内容/行数/摘要/schema 四种坏法; ④CLI 退出码(0 可信 / 2 不可信)。
测试全部在临时库上跑, **不碰生产库**。
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.paper import archive as arc  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "scripts" / "oneoff" / "archive_paper.py"

_ACCOUNT = (1, 20000.0, "2026-09-10 12:03:18", None, 20000.0)
_POSITIONS = [("000571.SZ", "新大洲A", 400, 5.716, 7.613),
              ("300227.SZ", "光韵达", 200, 13.664, 5.5611)]
_NAV = [("20260911", 19894.42, 8357.88, 11535.0, 5034.5281)]
_INTEREST = [("20260911", 0.78)]


class _TempDB(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db_path = self.dir / "t.db"
        p = mock.patch.object(db, "DB_PATH", self.db_path)
        p.start()
        self.addCleanup(p.stop)
        db.init_db()
        self._seed()

    def _seed(self):
        conn = db.get_conn()
        conn.execute("INSERT INTO paper_account VALUES (?,?,?,?,?)", _ACCOUNT)
        conn.executemany("INSERT INTO paper_position VALUES (?,?,?,?,?)", _POSITIONS)
        conn.executemany("INSERT INTO paper_nav VALUES (?,?,?,?,?)", _NAV)
        conn.executemany("INSERT INTO paper_interest VALUES (?,?)", _INTEREST)
        conn.execute("INSERT INTO paper_order (order_date, ts_code, name, side, shares, reason,"
                     " status, created_at) VALUES ('20260831','000571.SZ','新大洲A','buy',400,"
                     "'月度方案买入','done','2026-09-10 12:03:18')")
        conn.execute("INSERT INTO paper_trade (exec_date, ts_code, name, side, shares, price,"
                     " cost, amount, reason) VALUES ('20260911','000571.SZ','新大洲A','buy',"
                     "400,5.716,5.02,2286.4,'月度方案买入')")
        conn.commit()
        conn.close()


class TestDump(_TempDB):
    def test_dump_exports_real_rows_not_error_objects(self):
        """第一版那个 bug 的回归守卫: 数值必须是真值, 不是 {"error": ...} 之类的假行"""
        pl = arc.dump(self.db_path)
        self.assertEqual(pl["schema"], arc.SCHEMA)
        self.assertEqual(pl["counts"], {"paper_account": 1, "paper_position": 2,
                                        "paper_order": 1, "paper_trade": 1,
                                        "paper_nav": 1, "paper_interest": 1})
        acc = pl["tables"]["paper_account"][0]
        self.assertEqual(acc["cash"], 20000.0)
        self.assertEqual(acc["initial_capital"], 20000.0)
        self.assertNotIn("error", acc)
        pos = pl["tables"]["paper_position"][0]
        self.assertEqual(pos["ts_code"], "000571.SZ")
        self.assertEqual(pos["entry_adj"], 7.613)
        self.assertEqual(pl["tables"]["paper_nav"][0]["nav"], 19894.42)

    def test_dump_is_read_only(self):
        """归档不许碰库: 导出前后**文件字节与 mtime 都不变**(mode=ro 的证明)"""
        before = self.db_path.read_bytes()
        mtime = self.db_path.stat().st_mtime
        arc.dump(self.db_path)
        self.assertEqual(self.db_path.read_bytes(), before, "归档写到了生产库")
        self.assertEqual(self.db_path.stat().st_mtime, mtime)

    def test_sha256_is_stable_across_dumps(self):
        """sha 只覆盖内容 ⇒ 两次导出(时间戳不同)算出同一个值(verify 才可能在别日复核)"""
        a, b = arc.dump(self.db_path), arc.dump(self.db_path)
        self.assertEqual(a["sha256"], b["sha256"])
        self.assertEqual(a["summary"], b["summary"])

    def test_summary_numbers_come_from_content(self):
        s = arc.dump(self.db_path)["summary"]
        self.assertEqual(s["positions"], 2)
        self.assertEqual(s["positions_cost"], round(400 * 5.716 + 200 * 13.664, 2))
        self.assertEqual(s["nav_last"], "20260911")
        self.assertEqual(s["nav_last_value"], 19894.42)
        self.assertEqual(s["interest_total"], 0.78)
        self.assertEqual(s["trades"], 1)


class TestVerify(_TempDB):
    def _written(self) -> Path:
        pl = arc.dump(self.db_path)
        return arc.write(self.dir / "backup" / "a.json", pl)

    def test_round_trip_passes(self):
        p = self._written()
        ok, detail = arc.verify(p)
        self.assertTrue(ok, detail)
        self.assertIn("OK", detail)

    def test_detects_content_tamper(self):
        """改了某一格内容 ⇒ sha256 对不上(必须报不可信, 而不是沉默)"""
        p = self._written()
        data = json.loads(p.read_text(encoding="utf-8"))
        data["tables"]["paper_account"][0]["cash"] = 999999.0
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        ok, detail = arc.verify(p)
        self.assertFalse(ok)
        self.assertIn("sha256", detail)

    def test_detects_count_mismatch(self):
        p = self._written()
        data = json.loads(p.read_text(encoding="utf-8"))
        data["counts"]["paper_position"] = 99
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        ok, detail = arc.verify(p)
        self.assertFalse(ok)
        self.assertIn("行数", detail)

    def test_detects_summary_no_longer_matches_content(self):
        """内容与摘要不自洽(删表但留着旧摘要)也要拦住"""
        p = self._written()
        data = json.loads(p.read_text(encoding="utf-8"))
        data["tables"]["paper_position"] = []
        data["counts"]["paper_position"] = 0
        data["sha256"] = arc.canonical_sha256(data["counts"], data["tables"])  # 连 sha 一起改
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        ok, detail = arc.verify(p)
        self.assertFalse(ok)
        self.assertIn("摘要", detail)

    def test_rejects_other_schema_and_missing_file(self):
        p = self.dir / "x.json"
        p.write_text('{"schema": "other/v1"}', encoding="utf-8")
        self.assertFalse(arc.verify(p)[0])
        self.assertFalse(arc.verify(self.dir / "nope.json")[0])

    def test_missing_tables_archive_as_zero_rows(self):
        """新装机/空库: 表不存在也按 0 行归档, 不抛(否则第一次重置就卡死在归档)"""
        conn = db.get_conn()
        for t in arc.TABLES:
            conn.execute("DROP TABLE %s" % t)
        conn.commit()
        conn.close()
        pl = arc.dump(self.db_path)
        self.assertEqual(set(pl["counts"].values()), {0})
        self.assertTrue(arc.verify(arc.write(self.dir / "z.json", pl))[0])


class TestCliAndResetWiring(_TempDB):
    def test_cli_verify_exit_codes(self):
        p = self._written = arc.write(self.dir / "cli.json", arc.dump(self.db_path))
        r = subprocess.run([sys.executable, "-X", "utf8", str(CLI), "--verify", str(p)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("通过", r.stdout)
        data = json.loads(p.read_text(encoding="utf-8"))
        data["tables"]["paper_account"][0]["cash"] = 1.0
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        r2 = subprocess.run([sys.executable, "-X", "utf8", str(CLI), "--verify", str(p)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(r2.returncode, 2, r2.stdout + r2.stderr)
        self.assertIn("不可信", r2.stdout)

    def test_cli_export_writes_a_verifiable_file(self):
        out = self.dir / "backup" / "export.json"
        r = subprocess.run([sys.executable, "-X", "utf8", str(CLI),
                            "--db", str(self.db_path), "--out", str(out)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(arc.verify(out)[0])
        self.assertIn("sha256", r.stdout)

    def test_reset_script_archives_before_wiping(self):
        """静态守卫: `reset_paper.py` 必须**先归档再清空**, 且归档失败要中止"""
        txt = (ROOT / "scripts" / "oneoff" / "reset_paper.py").read_text(encoding="utf-8")
        self.assertIn("require_confirm", txt)
        i_arch = txt.index("arc.dump(")
        i_wipe = txt.index("DELETE FROM %s")
        self.assertLess(i_arch, i_wipe, "归档必须在清空之前")
        self.assertIn("归档自检失败", txt, "归档自检不过必须中止重置(fail-closed)")
        self.assertIn("paper_reset", txt, "重置必须在 pipeline_run 留一行")


if __name__ == "__main__":
    unittest.main()
