"""备份保留策略单测 (2026-09-12 体检报告跟进) —— `db._prune_backups` 此前零直接测试。

它已经有过一次**静默失效**: 2026-09-06 第十六轮 R16-2 发现 `rsplit("_")` 对
`neweco_20260904_pre_ens` 取到后缀 "ens", 字符串比较恒大于 cutoff ⇒ 该文件永远
删不掉, 而且不报错、日志也不打。同类"看着正常其实缺一条腿"的缺陷在本项目出现过
多次(见 `regime._pool_state_frame` 预热期), 所以这里把四条规则钉在测试上:

  1. 窗口内(最近 keep_days 天)全留
  2. 窗口外, 但属于「该月最新 1 份」→ 留
  3. 窗口外, 且不是该月最新 → 删
  4. 提取不到日期的非标准命名 → 不动(人工处理)
  5. **边车文件(-shm/-wal)随本体一起删** —— 2026-09-12 新修: 原先只 unlink .db,
     于是每清一份备份就永久留两个孤儿边车(SQLite 以 WAL 模式被打开过一次就会生成)

测试全部在临时目录里跑, 不碰 data/backup/ 真备份。
"""
import datetime
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as db_mod


class TestPruneBackups(unittest.TestCase):
    KEEP_DAYS = 7

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    # ---- 造文件 ---------------------------------------------------------
    def _mk(self, name: str, sidecars: bool = True):
        p = self.dir / name
        p.write_bytes(b"")
        if sidecars:
            (self.dir / (name + "-shm")).write_bytes(b"")
            (self.dir / (name + "-wal")).write_bytes(b"")
        return p

    @staticmethod
    def _days_ago(n: int) -> str:
        return (datetime.date.today() - datetime.timedelta(days=n)).strftime("%Y%m%d")

    # ---- 用例 -----------------------------------------------------------
    def test_window_month_latest_and_sidecars(self):
        recent = self._mk("neweco_%s.db" % self._days_ago(2))       # 规则 1: 窗口内
        old_latest = self._mk("neweco_20200120.db")                # 规则 2: 该月最新
        old_stale = self._mk("neweco_20200105.db")                 # 规则 3: 该月非最新
        odd = self._mk("neweco_something.db")                      # 规则 4: 无日期

        db_mod._prune_backups(self.dir, keep_days=self.KEEP_DAYS)

        self.assertTrue(recent.exists(), "窗口内的备份被删了")
        self.assertTrue(old_latest.exists(), "该月最新一份被删了(月度保留没生效)")
        self.assertFalse(old_stale.exists(), "窗口外且非月最新的备份没被删掉")
        self.assertTrue(odd.exists(), "非标准命名被自动删了(应留人工处理)")

        # 规则 5: 被删的本体, 边车必须跟着走; 保留下来的本体, 边车必须还在
        for side in ("-shm", "-wal"):
            self.assertFalse((self.dir / ("neweco_20200105.db" + side)).exists(),
                             "被删备份留下了孤儿边车 %s —— 会永久累积" % side)
            self.assertTrue((self.dir / ("neweco_20200120.db" + side)).exists(),
                            "保留下来的备份边车被误删 %s" % side)
            self.assertTrue((self.dir / ("neweco_%s.db%s"
                                         % (self._days_ago(2), side))).exists(),
                            "窗口内备份的边车被误删 %s" % side)

    def test_month_latest_named_with_suffix_is_not_immortal(self):
        """R16-2 回归: `neweco_YYYYMMDD_pre_ens` 这类带后缀的命名必须可选可删。

        原 bug 的症状是「永远删不掉且不报错」—— 所以这里断言的是**它真的消失了**,
        而不是「函数没抛异常」。同时 month_latest 是按完整文件名排序选的,
        `_pre_ens` 后缀字典序大于纯数字名, 会顶掉同月的正常备份 —— 那是**有意的**
        (该月最新的一份, 不管叫什么, 都留), 这里把它一并钉住。
        """
        self._mk("neweco_20200105.db")
        pre = self._mk("neweco_20200120_pre_ens.db")

        db_mod._prune_backups(self.dir, keep_days=self.KEEP_DAYS)

        self.assertTrue(pre.exists(), "带后缀的该月最新备份被删了")
        self.assertFalse((self.dir / "neweco_20200105.db").exists(),
                         "同月更旧的备份没被删 —— R16-2 的 rsplit bug 复发了")

    def test_empty_dir_is_noop(self):
        """推倒重写(2026-09-30 测试卫生轮): 原版只验证"不抛异常"——对 _prune_backups
        的行为零断言, 是五个亚型缺陷盘点(test_test_hygiene 审计)里唯一的真弱测试。
        新判据: 空目录是 noop 的**语义** = 不创建文件、不改目录内容、可重复调用(幂等)。"""
        db_mod._prune_backups(self.dir, keep_days=self.KEEP_DAYS)   # 不抛异常(隐式断言 1)
        self.assertEqual(list(self.dir.iterdir()), [], "空目录跑了清理器不该凭空多出文件")
        # 幂等: 再跑一次仍不抛、仍无文件
        db_mod._prune_backups(self.dir, keep_days=self.KEEP_DAYS)
        self.assertEqual(list(self.dir.iterdir()), [], "二次清理不得留下任何中间产物")
        # 空目录与"全在窗口内"的目录走同一条代码路径 —— 删除集合为空不等于跳过执行
        self._mk("neweco_%s.db" % self._days_ago(1))
        db_mod._prune_backups(self.dir, keep_days=self.KEEP_DAYS)
        self.assertTrue((self.dir / ("neweco_%s.db" % self._days_ago(1))).exists(),
                        "窗口内唯一备份被误删")


if __name__ == "__main__":
    unittest.main()
