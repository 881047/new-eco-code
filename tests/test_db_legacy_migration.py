"""从**旧主键形状**的库升级到当前 schema 的那条路（2026-09-19 审查 A1）

`db._migrate_paper_pk` 把 `paper_order`/`paper_trade` 的旧复合主键重建成自增 `id`，
四步 DDL 包在一个事务里（2026-09-08 审查 P2 的产物：RENAME 与 CREATE 之间断电会把旧表
变成永久孤儿）。它自己 `BEGIN` —— 而 `_init_db` 在调到它之前刚执行过
`UPDATE paper_order SET created_at=…`，sqlite3 默认 isolation_level 下这条 DML 就开了一个
隐式事务（到 `_init_db` 末尾才 commit）。于是 `BEGIN` 必抛
`cannot start a transaction within a transaction` ⇒ 回滚再 raise ⇒ **`init_db()` 整个崩**，
服务起不来。

生产库早已带 `id` 列（函数开头提前 return）所以平时不发作，只在"升级旧库 / 从旧备份恢复"
这条 DR 路径上炸 —— 恰是最需要它能跑的那天。本测试就是那条路的演练。

另锁一条：迁移必须**保留全部数据**并给每行发 `id`（旧表数据丢不得，账本引用它）。
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import db  # noqa: E402

#: 旧版 paper_order 的形状（复合主键、无 id、无 created_at）—— 2026-09-04 之前的样子
_LEGACY_ORDER = """
CREATE TABLE paper_order (
    order_date TEXT, ts_code TEXT, name TEXT, side TEXT, shares INTEGER,
    reason TEXT, status TEXT DEFAULT 'pending', first_attempt TEXT,
    PRIMARY KEY (order_date, ts_code, side)
) WITHOUT ROWID;
"""
_LEGACY_TRADE = """
CREATE TABLE paper_trade (
    exec_date TEXT, ts_code TEXT, name TEXT, side TEXT, shares INTEGER,
    price REAL, cost REAL, amount REAL, reason TEXT,
    PRIMARY KEY (exec_date, ts_code, side)
) WITHOUT ROWID;
"""


class TestLegacyPaperMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="neweco_legacymig_"))
        self.path = self.tmp / "legacy.db"
        con = sqlite3.connect(self.path)
        con.executescript(_LEGACY_ORDER + _LEGACY_TRADE)
        con.executemany(
            "INSERT INTO paper_order (order_date, ts_code, name, side, shares, reason, status,"
            " first_attempt) VALUES (?,?,?,?,?,?,?,?)",
            [("20260831", "600000.SH", "浦发银行", "buy", 100, "月度方案买入", "pending", "20260901"),
             ("20260831", "000001.SZ", "平安银行", "sell", 200, "月度方案调出", "pending", "20260901")])
        con.execute("INSERT INTO paper_trade VALUES "
                    "('20260901','600519.SH','贵州茅台','buy',100,1700.0,1.2,170000.0,'月度方案买入')")
        con.commit()
        con.close()

    def tearDown(self):
        for p in sorted(self.tmp.glob("*")):
            try:
                p.unlink()
            except OSError:
                pass
        self.tmp.rmdir()

    def _migrate(self):
        """在旧库上跑一次真实的 `_init_db`（连接由本测试持有，语义与 `db.init_db` 一致）。"""
        con = sqlite3.connect(self.path)
        try:
            db._init_db(con)
        finally:
            con.close()

    def _cols(self, con, table):
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]

    def test_migration_survives_pending_dml(self):
        self._migrate()                                    # 修复前：这里直接抛 OperationalError
        con = sqlite3.connect(self.path)
        try:
            self.assertIn("id", self._cols(con, "paper_order"))
            self.assertIn("id", self._cols(con, "paper_trade"))
            self.assertIn("created_at", self._cols(con, "paper_order"))
            self.assertIsNone(                            # 中间态不许留下孤儿表
                con.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                            " AND name IN ('paper_order_old','paper_trade_old')").fetchone())
            self.assertEqual(
                2, con.execute("SELECT COUNT(*) FROM paper_order").fetchone()[0])
            self.assertEqual(                             # 数据逐行保留 + 发 id
                [("20260831", "000001.SZ", "sell", 200), ("20260831", "600000.SH", "buy", 100)],
                [tuple(r) for r in con.execute(
                    "SELECT order_date, ts_code, side, shares FROM paper_order ORDER BY id")])
            self.assertEqual([1], [r[0] for r in con.execute(
                "SELECT id FROM paper_trade")])
        finally:
            con.close()

    def test_idempotent_on_already_migrated_db(self):
        self._migrate()
        self._migrate()                                    # 第二次走提前 return，不许抛
        con = sqlite3.connect(self.path)
        try:
            self.assertEqual(2, con.execute("SELECT COUNT(*) FROM paper_order").fetchone()[0])
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
