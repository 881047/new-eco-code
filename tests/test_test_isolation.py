"""测试隔离守卫（2026-09-13）：测试的日志不许落进生产 `logs/app.log`。

背景见 `tests/conftest.py` 的 docstring —— 2026-09-13 那次审计 `--tests` 把
"实时行情获取失败: boom / LightGBM 训练失败 / daily 表为空" 混写了生产 app.log，
而 app.log 是事故取证的第一手台账（07 号台账 #1 的同类复发）。

这里钉两条**外部事实**，而不是只断言"conftest 里设过环境变量"（那是自述，不是证据）：
  ① 测试进程看到的 `LOG_DIR` 不是生产 `logs/`；
  ② 真写一条日志后，探针出现在临时目录、**本次跑测试期间也不新增到生产 app.log**。

⚠️ 2026-09-14 口径修正（外部审查 §9.4）：②的判据从"app.log 里搜不到探针"改成
"**本次新增的字节里**搜不到探针"。原因是历史污染 —— 2026-09-14 22:03 / 22:27 两次
按旧文档跑 `scripts/ci_run_tests.py`（unittest 路径，不加载 conftest）留下 2 行探针，
而 app.log 是证据台账，**不删除历史行**（删证据比留噪声更糟）。用"文件原长度之后的
增量"判断，既保住守卫的抓真能力（真污染时增量里一定有探针），又不会被既成事实卡死：
判据是**没有新污染**，不是"历史必须干净"。

为什么按字节 offset 而不是"整文件读"：app.log 有 5MB 轮转 + 生产服务在写，整文件读
既慢又可能读到别人写的中间状态；只读增量是最小充分证据。
"""
import logging
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _isolation
from app import notify
from app import config as cfg_mod
from app import db as db_mod
from app.logger import setup_logger

BASE = Path(__file__).resolve().parent.parent
PROD_APP_LOG = BASE / "logs" / "app.log"
PROBE = "NE-TEST-LOG-PROBE-8f3c1d"


class TestLogIsolation(unittest.TestCase):
    def test_log_dir_is_not_production(self):
        self.assertNotEqual(Path(cfg_mod.LOG_DIR).resolve(), (BASE / "logs").resolve(),
                            "测试会话的 LOG_DIR 指回了生产 logs/ ⇒ 又会往 app.log 写")

    def test_probe_lands_in_temp_not_production(self):
        setup_logger()
        # 先记下生产日志水位：只对"本用例造成的增量"负责（见模块 docstring ⚠️）
        before = PROD_APP_LOG.stat().st_size if PROD_APP_LOG.exists() else 0
        logging.getLogger("app.weekly.pipeline").warning(PROBE)
        for h in logging.getLogger().handlers:
            h.flush()
        tmp_files = list(Path(cfg_mod.LOG_DIR).glob("*.log"))
        self.assertTrue(tmp_files, f"临时日志目录里没有日志文件: {cfg_mod.LOG_DIR}")
        self.assertTrue(any(PROBE in p.read_text(encoding="utf-8", errors="replace")
                            for p in tmp_files),
                        "探针没写进临时日志 ⇒ handler 还挂在别处")
        if PROD_APP_LOG.exists():
            with PROD_APP_LOG.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(before)
                delta = f.read()
            self.assertNotIn(PROBE, delta,
                             "探针写进了生产 logs/app.log（本次新增字节）⇒ 测试污染了证据台账")


class TestOpsAlertPathCannotMailOut(unittest.TestCase):
    """**端到端**守卫：调度器的告警重发路径也不许真发信（2026-09-14 实测缺口）。

    为什么这条必须有：隔离原来只换 `notify._send_with_retry`，而 `scheduler._ops_alert_once`
    在首选发送器抛错时会**再调一次 `notify.send_ops_alert`**。2026-09-14 22:26:54 按旧文档跑
    `scripts/ci_run_tests.py`（unittest 路径 ⇒ conftest 不加载）时，SMTP 被测试打挂 ⇒ 走到
    这个分支 ⇒ **真的发出一封**「【NEW ECO·运维】任务失败: 测试」（`notify_log` id=18 有据；
    id=17 是 22:03 同类）。日志污染还有归档可查，邮件污染是**直接进用户收件箱**的假告警。

    判据（外部事实，不是"我们 patch 过了"）：告警路径跑完后**临时库里的 notify_log 没增行**
    —— 发信与落库是同一段代码，落库不增行就说明真没发出去；顺带保证生产库只被读、不被写。
    """

    def test_ops_alert_fallback_does_not_send_mail_or_write_notify_log(self):
        # 走**真实入口**（scheduler 的 except 分支调的就是 notify.send_ops_alert），
        # 只在传输层放个探针观察"到底有没有试图外呼"。
        # ⚠️ 故意用**空 email 配置**：即使隔离失效，真实现也会在配好 SMTP 之前返回 False ——
        #    这条测试的失败模式是"别的守卫红"，**不是"又发了一封假告警"**。
        tmp = Path(tempfile.mkdtemp(prefix="neweco_notify_guard_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        called = []
        with mock.patch.object(db_mod, "DB_PATH", tmp / "guard.db"), \
                mock.patch("smtplib.SMTP_SSL",
                           side_effect=lambda *a, **k: called.append(a)):
            conn = sqlite3.connect(str(tmp / "guard.db"))
            conn.execute("CREATE TABLE notify_log (created_at TEXT, tag TEXT, ok INTEGER, error TEXT)")
            conn.commit()
            rows_before = conn.execute("SELECT COUNT(*) FROM notify_log").fetchone()[0]
            conn.close()
            with mock.patch.object(notify.cfg_mod, "load_config", return_value={"email": {}}):
                self.assertFalse(notify.send_ops_alert("测试标题", "测试正文"))
            conn = sqlite3.connect(str(tmp / "guard.db"))
            rows_after = conn.execute("SELECT COUNT(*) FROM notify_log").fetchone()[0]
            conn.close()
        self.assertEqual(called, [], "告警入口真的去建 SMTP 连接了")
        self.assertEqual(rows_after, rows_before,
                         "告警路径写出了 notify_log ⇒ 真发信了（见本类 docstring）")


class TestIsolationModuleIsSingleSource(unittest.TestCase):
    """隔离实现只许有一份（2026-09-14 外部审查 P0-1 的**结构性**守卫）。

    为什么单独立一条：这次事故的根因不是"谁忘了写隔离"，而是**隔离有两个入口、只实现了一个**
    —— conftest（pytest）有、`scripts/ci_run_tests.py`（unittest）没有。所以除了断言
    "没污染"（上面两条），还要断言"**两个入口调的是同一份实现**"，否则下次有人改一处、
    另一处静默失效，同样的事会再来第三次。
    """

    def test_conftest_reuses_isolation_module(self):
        src = (BASE / "tests" / "conftest.py").read_text(encoding="utf-8")
        self.assertIn("import _isolation", src,
                      "conftest 没复用 tests/_isolation.py ⇒ 隔离又分叉了")
        self.assertNotIn("tempfile.mkdtemp", src,
                         "conftest 自己又造了一份临时日志目录 ⇒ 两套实现必然漂移")

    def test_ci_entry_establishes_isolation_before_discovery(self):
        src = (BASE / "scripts" / "ci_run_tests.py").read_text(encoding="utf-8")
        self.assertIn("_isolation.setup()", src,
                      "CI 入口(ci_run_tests.py)没建立隔离 ⇒ 按文档跑它会污染 app.log")
        self.assertIn("_isolation.assert_active()", src,
                      "CI 入口没有「隔离已生效」的自证 ⇒ 带病往下跑")
        # setup 必须排在 discover 之前：测试模块在收集阶段就 import app（test_local_guard）
        self.assertLess(src.index("_isolation.setup()"), src.index("loader.discover"),
                        "隔离建立在 discover 之后 ⇒ 收集阶段的 import 已经拿到生产路径")


class TestReadOnlyMountIsReal(unittest.TestCase):
    """`prod_ro` 必须**真的**只读（2026-09-18 审查 Q-09）

    `_isolation._seed_reference_data` 的主连接原来漏了 `uri=True`。SQLite 只有在
    **主连接**开了 URI 标志之后才解析 `ATTACH 'file:…?mode=ro'`；漏了就必然抛
    OperationalError，被那句 `except` 吞掉，落到 fallback `ATTACH (str(src))` ——
    默认**读写**挂上 11G 生产库。于是"prod_ro 只读"这个保护从来没生效过一次：
    它看起来存在（代码里明明写着 mode=ro），实际是摆设。

    现有守卫只测"逻辑上没写生产库"（对 prod_ro 全是 SELECT），**没测挂载模式**，
    所以这个摆设一直是绿的。这里补两条：机制在位 + 保护真能挡写。
    """

    def test_main_connection_enables_uri(self):
        src = (BASE / "tests" / "_isolation.py").read_text(encoding="utf-8")
        i = src.index("def _seed_reference_data")
        body = src[i:src.index("def ", i + 10)]
        self.assertIn("uri=True", body,
                      "主连接没开 uri=True ⇒ mode=ro 的 ATTACH 必然失败并 fallback 成读写挂载")
        self.assertNotIn("sqlite3.connect(str(db_file))", body,
                         "回归到非 URI 主连接了（只读保护重新变成摆设）")

    def test_readonly_uri_attach_actually_refuses_writes(self):
        """把隔离用的那一手原样演一遍，并**真的试着写**：写必须被拒。

        只断言"代码里写了 mode=ro"是自述，不是证据 —— 保护是否生效要实测。
        """
        tmp = Path(tempfile.mkdtemp(prefix="ne_ro_"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        src = tmp / "src.db"
        s = sqlite3.connect(str(src))
        s.execute("CREATE TABLE t(a)")
        s.execute("INSERT INTO t VALUES (1)")
        s.commit()
        s.close()
        conn = sqlite3.connect(f"file:{tmp / 'main.db'}", uri=True)
        conn.execute("ATTACH DATABASE ? AS prod_ro", (f"file:{src}?mode=ro",))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM prod_ro.t").fetchone()[0], 1,
                         "只读挂载也得能读（否则上面的 SELECT 就是白测）")
        with self.assertRaises(sqlite3.OperationalError) as cm:
            conn.execute("INSERT INTO prod_ro.t VALUES (2)")
        self.assertIn("readonly", str(cm.exception).lower(),
                      "拒绝的原因必须是「只读」，不是碰巧的路径错误")
        conn.close()


class TestSchedulerLockIsIsolated(unittest.TestCase):
    """调度器锁也要隔离（2026-09-18 审查修，与 Q-09 同族：保护得由**路径**保证）

    `scheduler._LOCK_FILE` 指向生产 `data/.task.lock`。`test_scheduler_ops_alert.py`
    端到端跑 `_retryable` 的正常路径时会去抢那把**真锁** —— 服务只要正处在定时档
    （18:00 / 18:05 / 19:30 / 19:45 + misfire 追补），锁被服务进程持有，这 5 条用例
    就集体红，报的却是"正常路径必须返回 True"，看着像代码坏了。
    实测：18:00 档在跑时复现，隔离后与活体服务共存也能稳定绿。
    """

    def test_lock_path_is_not_the_production_one(self):
        from app import scheduler
        prod = cfg_mod.DATA_DIR / ".task.lock"
        self.assertNotEqual(Path(scheduler._LOCK_FILE).resolve(), Path(prod).resolve(),
                            "测试进程的调度锁指向生产 data/ ⇒ 与在跑的服务抢同一把锁 ⇒ 假红")
        self.assertIn("task", str(scheduler._LOCK_FILE).lower(),
                      "锁被彻底改名/去掉了 ⇒ 抢锁路径本身没在测了")


if __name__ == "__main__":
    unittest.main()
