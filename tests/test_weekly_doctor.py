"""开跑前体检的守卫（2026-09-13）

守三条:
  1. **绝不崩**: 任何一条检查抛异常都只能记成该条 ✗（体检自身挂了比不体检更糟）；
  2. 输出结构固定（name/ok/detail/fix），✗ 必须带可执行的修复指引；
  3. `next_exec` 必须与调度口径**逐字一致**（"下个执行档会不会动"是体检的核心结论）。
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db                            # noqa: E402
from app.weekly import doctor                 # noqa: E402

BASE = Path(__file__).resolve().parent.parent


class TestDoctorShape(unittest.TestCase):

    def test_corrupt_ledger_is_reported_by_name_with_the_real_recovery_source(self):
        """2026-09-14（与 07 #48 同族）：账本损坏时**必须**报"账本可读 ✗"并指向真实备份。

        改动前：`check_ledger` 的 ok 是字面量 True（永远绿），而 `Ledger.load` 的异常穿出后
        落到体检的通用分支 `(体检项异常)`，fix 文案指向 doctor 自己的日志 —— 真正该说
        "从 `.bak` / `data/weekly/backup/` 恢复"的那一刻，没有任何一条在说这句话。
        三处读账本的检查（可读性 / 风控状态 / 执行档看门狗）都要如实降级、都不许抛。
        红检：把 `ledger_or_error` 换回裸 `Ledger.load` ⇒ 本用例当场红。
        """
        p = BASE / "data" / "weekly" / "_test_doctor_corrupt.json"
        try:
            p.write_text('{"capital": 1000000.0, "cash": 1.0', encoding="utf-8")  # 半截 JSON
            for fn, name in ((doctor.check_ledger, "账本可读"),
                             (doctor.check_risk, "风控停买状态"),
                             (doctor.check_exec_ran, "执行档看门狗")):
                kw = {"ledger_path": p}
                if fn is doctor.check_exec_ran:
                    kw["today"] = "20260914"
                it = fn({}, **kw)
                self.assertFalse(it["ok"], f"{name} 在账本损坏时必须 ✗")
                self.assertIn("备份", it["fix"], f"{name} 的修复指引必须指向备份")
        finally:
            if p.exists():
                p.unlink()
    def test_never_raises_and_shape(self):
        items = doctor.checks()
        # 项数与**名单**都钉死（2026-09-20：原来只断言 >=7，于是"11 项/12 项/13 项"这种
        # 说法在 README 与知识库里各说各话而测试一直绿）。加/减一项 ⇒ 这里红 ⇒ 逼着同步文档。
        # 2026-09-22（D 案 §四）：+4 条「块级 IC」—— P0 四块每块至少一个读数进体检，
        # 整体 IC 还在正常带时某一块可能已经先塌了，没有分块读数就永远看不见。
        # 2026-09-24：+1 条「动态监控」—— dynamic.enabled=false 会让 run_monitor 第一行
        # 就 return，而 monitor stage 在 pipeline_run 里仍记 ok ⇒ 从运行台账看不出来。
        # 实踩：signal 表停在 20260904、静默停摆 20 天，直到人工比对接口日期才发现。
        # 2026-09-24（同日第二项）：+1 条「行情缓存写入」—— `_cache_save` 失败原来只有一行
        # `log.warning`，不进邮件/doctor/体检 ⇒ 09-24 19:34 的 `arrow.py_extension_type`
        # 失败沉睡了 2 小时才被人工审查发现。同属"有日志、无告警"的静默族。
        self.assertEqual([it["name"] for it in items], [
            "总开关 weekly.enabled", "动态监控", "下个执行档", "运行参数", "交易日历覆盖未来",
            "打分存档覆盖最新锚点", "模型新鲜度",
            "块级IC B1流动性/规模", "块级IC B2波动/彩票/β",
            "块级IC B3动量/趋势", "块级IC B4短窗反转",
            "方案文件", "账本可读", "风控停买状态",
            "执行档看门狗", "阶段台账收口", "邮件送达", "备份新鲜度", "磁盘余量",
            "行情缓存写入"],
            f"体检项清单变了: {[it['name'] for it in items]}")
        self.assertEqual(len(items), 20)
        for it in items:
            self.assertIn("name", it)
            self.assertIsInstance(it["ok"], bool)
            self.assertIsInstance(it["detail"], str)
            if not it["ok"]:
                self.assertTrue(it["fix"], f"✗ 的检查项必须给修复指引: {it['name']}")
        self.assertTrue(doctor.render(items))

    def test_switch_off_is_reported(self):
        self.assertFalse(doctor.check_switch({"weekly": {"enabled": False}})["ok"])
        self.assertTrue(doctor.check_switch({"weekly": {"enabled": True}})["ok"])

    def test_dynamic_off_is_reported(self):
        """2026-09-24：`dynamic.enabled=false` 必须 ✗。

        起因：该开关关闭时 `run_monitor` 第一行就 return，而 `monitor` stage 仍记 ok
        ⇒ 动态监控静默停摆 20 天（signal 表停在 20260904）无人察觉。
        ⚠️ 本用例只传 enabled=False（提前 return，不碰库）—— 避免单测连生产库取数。
        """
        it = doctor.check_dynamic({"dynamic": {"enabled": False}})
        self.assertFalse(it["ok"])
        self.assertIn("止损", it["detail"])
        self.assertTrue(it["fix"], "✗ 的检查项必须给修复指引")

    def test_halt_is_reported_as_not_ok(self):
        p = BASE / "data" / "weekly" / "_test_doctor.json"
        try:
            p.write_text(json.dumps({"capital": 1e6, "cash": 1e6, "positions": {},
                                     "nav_history": [], "trades": [], "inception": None,
                                     "last_mark": None,
                                     "halt": {"active": True, "since": "20261101",
                                              "reason": "健康度 dead"}}), encoding="utf-8")
            it = doctor.check_risk({}, ledger_path=p)
            self.assertFalse(it["ok"])
            self.assertIn("resume", it["fix"])
        finally:
            if p.exists():
                p.unlink()


class TestModelFreshness(unittest.TestCase):
    """P2 模型新鲜度的三条判据（2026-09-21 立；判据冻结见
    `plans/p1p2-model-provenance-20260921.md`，改阈值 = 改判据 ⇒ 重走预注册）。

    解决的是 05 号台账 §5.11 查出来的真实缺陷：生产模型"有多旧"原本**不可知**，
    才把组合层约 1.1σ 的放大误写成 3.0pp「漂移」。本项把模型年龄变成可查、可告警。

    红检方向：把情形①（无记录）改成报 ✓ ⇒ `test_no_record_is_not_ok` 当场红 ——
    "不可追溯"报成绿，等于把缺陷藏回去了。
    """

    def _run(self, asof, cur):
        class _Cur:
            def fetchone(self):
                return (cur,)
        class _Conn:
            def execute(self, *a, **k):
                return _Cur()
            def close(self):
                pass
        with mock.patch("app.db.get_conn", return_value=_Conn()), \
             mock.patch("app.db.get_meta", return_value=asof):
            return doctor.check_model_freshness({})

    def test_no_record_is_not_ok(self):
        it = self._run(None, "20260921")
        self.assertFalse(it["ok"], "无 data_asof ⇒ 模型年龄不可知，必须 ✗")
        self.assertIn("不可知", it["detail"])
        self.assertIn("10-01", it["fix"], "fix 必须给出明确的解除时点，否则是永远红的死项")

    def test_fresh_model_is_ok(self):
        it = self._run("20260911", "20260921")        # 滞后 10 天：模型正常地是新训的
        self.assertTrue(it["ok"])
        self.assertIn("滞后 10 天", it["detail"])

    def test_stale_model_is_not_ok(self):
        it = self._run("20260701", "20260921")        # 滞后 82 天 ⇒ 漏掉 ≥1 次月频重训
        self.assertFalse(it["ok"])
        self.assertIn("阈值 45", it["detail"])
        self.assertIn("train_model.py", it["fix"])

    def test_boundary_at_45_days(self):
        self.assertTrue(self._run("20260807", "20260921")["ok"])     # 恰好 45 天 ⇒ ✓
        self.assertFalse(self._run("20260806", "20260921")["ok"])    # 46 天 ⇒ ✗


class TestNextExecMatchesScheduler(unittest.TestCase):
    """`next_exec` = 调度的判据(与上一开市日不同 ISO 周)。"""

    @classmethod
    def setUpClass(cls):
        cls.conn = db.get_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_friday_to_monday(self):
        # 2026-09-11 是周五(锚点), 下一个执行日 = 09-14(周一), 用的锚点 = 09-11
        day, anchor = doctor.next_exec(self.conn, "20260911")
        self.assertEqual((day, anchor), ("20260914", "20260911"))

    def test_monday_is_an_exec_day(self):
        # 周一当天也算"下个执行档"= 今天, 锚点仍是上周五
        day, anchor = doctor.next_exec(self.conn, "20260914")
        self.assertEqual((day, anchor), ("20260914", "20260911"))

    def test_midweek_skips_to_next_monday(self):
        day, anchor = doctor.next_exec(self.conn, "20260915")   # 周二
        self.assertEqual(day, "20260921")
        self.assertEqual(anchor, "20260918")                    # 09-18 是周五


class TestPlanGateCrossCheck(unittest.TestCase):
    """"投出比例"必须与放行闸 G3 并排显示（2026-09-13）

    实测错配：G3 门槛 0.70 是按**回测口径**定的（全史平均投入率 0.769，账户已复利到千万），
    而纸面账户从 100 万起步、整手约束更硬 ⇒ 现役配置只投出 0.6968，**这道闸过不去**。
    单看"投出 0.6968"没人会觉得有问题 —— 并排才看得出来，且必须在**开跑前**看出来。
    """

    def _check(self, ratio):
        import tempfile
        from app.weekly import plan as wk_plan
        with tempfile.TemporaryDirectory(prefix="neweco_plan_") as d:
            p = Path(d) / "plan_20260911.json"
            p.write_text(json.dumps({"rows": [{"ts_code": "600000.SH", "shares": 100}],
                                     "invested_ratio": ratio}), encoding="utf-8")
            with mock.patch.object(wk_plan, "PLAN_DIR", Path(d)):
                return doctor.check_plan({}, "20260911")

    def test_below_gate_is_flagged(self):
        item = self._check(0.6968)
        self.assertTrue(item["ok"], "低于门槛是配置选择, 不是体检失败")
        self.assertIn("G3", item["detail"])
        self.assertIn("0.6968", item["detail"])

    def test_above_gate_has_no_flag(self):
        item = self._check(0.7153)
        self.assertNotIn("G3", item["detail"])
        self.assertIn("0.7153", item["detail"])

    def test_gate_threshold_comes_from_protocol(self):
        """门槛值只许有一个来源（protocol.MIN_INVESTED），体检里不许硬编码。"""
        from app.weekly import protocol
        self.assertEqual(protocol.MIN_INVESTED, 0.70)
        self.assertIn("0.7", self._check(0.60)["detail"])


class TestExecWatchdog(unittest.TestCase):
    """执行档看门狗：执行日没落账 = "根本没交易" 类事故，必须能被检出来。"""

    def setUp(self):
        self.path = BASE / "data" / "weekly" / "_test_watchdog.json"
        if self.path.exists():
            self.path.unlink()

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def test_not_an_exec_day_is_not_applicable(self):
        it = doctor.check_exec_ran({}, "20260915", ledger_path=self.path)   # 周二
        self.assertTrue(it["ok"])
        # 两个分支: 非开市日→"不适用"; 开市日但同周→"非执行日"
        self.assertTrue("不适用" in it["detail"] or "非执行日" in it["detail"], it["detail"])

    def test_exec_day_without_nav_point_alerts(self):
        # ⚠️ 必须显式给 now：2026-09-14 早修了"早上跑体检假警报"的缺陷 —— 09:32 之前 / 09:32~18:15
        # 两段都**不算失败**（晚间档是设计内的兜底），只有 **18:15 之后**仍无净值点才判 ✗。
        import datetime
        it = doctor.check_exec_ran({}, "20260914", ledger_path=self.path,
                                   now=datetime.datetime(2026, 9, 14, 19, 0))   # 周一=执行日, 晚间档已过
        self.assertFalse(it["ok"])
        self.assertIn("没有", it["detail"])
        self.assertIn("exec --date", it["fix"])

    def test_exec_day_before_window_is_not_an_alarm(self):
        """09:32 之前跑体检：应当 OK（尚未到窗口），而不是 ✗（2026-09-14 06:02 实测假警报）"""
        import datetime
        it = doctor.check_exec_ran({}, "20260914", ledger_path=self.path,
                                   now=datetime.datetime(2026, 9, 14, 6, 2))
        self.assertTrue(it["ok"], it)
        self.assertIn("尚未到", it["detail"])

    def test_exec_day_after_open_slot_waits_for_evening(self):
        """09:32 已过但 18:15 未到：等晚间兜底档（同价口径），不算失败"""
        import datetime
        it = doctor.check_exec_ran({}, "20260914", ledger_path=self.path,
                                   now=datetime.datetime(2026, 9, 14, 10, 30))
        self.assertTrue(it["ok"], it)
        self.assertIn("晚间兜底", it["detail"])

    def test_exec_day_with_nav_point_passes(self):
        import datetime
        self.path.write_text(json.dumps({
            "capital": 1e6, "cash": 6e4, "trades": [],
            "positions": {"600000.SH": {"shares": 100, "cost": 10.0, "bought": "20260914"}},
            "inception": "20260914", "last_mark": "20260914",
            "nav_history": [{"date": "20260914", "nav": 9.3e5, "invested": 9.3e5}],
            "last_plan": {"anchor": "20260911", "applied_on": "20260914",
                          "n_rows": 98, "n_trades": 94}}),
            encoding="utf-8")
        it = doctor.check_exec_ran({}, "20260914", ledger_path=self.path,
                                   now=datetime.datetime(2026, 9, 14, 10, 30))
        self.assertTrue(it["ok"], it)
        self.assertIn("已落", it["detail"])

    def test_empty_marker_before_evening_waits(self):
        """**2026-09-14 新增（07 号 #34）**：0 成交 / 0 持仓的空占位净值点 ≠ 已执行 ——
        18:15 之前只能说"等晚间兜底", 不能说 ✓ 已执行。"""
        import datetime
        self.path.write_text(json.dumps({
            "capital": 1e6, "cash": 1e6, "positions": {}, "trades": [],
            "inception": "20260914", "last_mark": "20260914",
            "nav_history": [{"date": "20260914", "nav": 1e6, "invested": 0.0}],
            "last_plan": {"anchor": "20260911", "applied_on": "20260914",
                          "n_rows": 98, "n_trades": 0}}),
            encoding="utf-8")
        it = doctor.check_exec_ran({}, "20260914", ledger_path=self.path,
                                   now=datetime.datetime(2026, 9, 14, 6, 5))
        self.assertTrue(it["ok"], it)
        self.assertIn("空占位", it["detail"])

    def test_empty_marker_after_evening_alerts(self):
        """18:15 之后仍是 0 成交 / 0 持仓 ⇒ 判 ✗（整周空仓这种最坏情况必须有人知道）。"""
        import datetime
        self.path.write_text(json.dumps({
            "capital": 1e6, "cash": 1e6, "positions": {}, "trades": [],
            "inception": "20260914", "last_mark": "20260914",
            "nav_history": [{"date": "20260914", "nav": 1e6, "invested": 0.0}],
            "last_plan": {"anchor": "20260911", "applied_on": "20260914",
                          "n_rows": 98, "n_trades": 0}}),
            encoding="utf-8")
        it = doctor.check_exec_ran({}, "20260914", ledger_path=self.path,
                                   now=datetime.datetime(2026, 9, 14, 19, 0))
        self.assertFalse(it["ok"], it)
        self.assertIn("空占位", it["detail"])


class TestParamsSnapshot(unittest.TestCase):
    """运行参数快照：配置漂移（改了忘改回去）必须在体检里一眼看见。"""

    def test_overrides_are_reported(self):
        cfg = {"weekly": {"capital": 500_000, "equity_weight": 1.0, "max_lot_cost_mult": 2.0,
                          "email": False, "risk_halt": {"enabled": False}},
               "regime_equity_weight": {"aggressive": {"attack": 0.873}},
               "fees": {"commission_rate": 0.0003}}
        p = doctor.params(cfg)
        self.assertEqual(p["capital"], 500_000)
        self.assertEqual(p["equity_weight"], 1.0)
        self.assertEqual(p["equity_weight_source"], "weekly 覆盖")
        self.assertEqual((p["top_n"], p["band"]), (100, 5.0))     # 50 万档 = top100/带5×
        self.assertEqual(p["max_lot_cost_mult"], 2.0)
        self.assertFalse(p["email"])
        self.assertFalse(p["risk_halt"])
        self.assertAlmostEqual(p["commission_rate"], 0.0003, places=6)
        self.assertTrue(doctor.check_params(cfg)["ok"])           # 参数项是"信息"，不该报 ✗

    def test_inherits_monthly_weight_by_default(self):
        cfg = {"regime_equity_weight": {"aggressive": {"attack": 0.873}}}
        p = doctor.params(cfg)
        self.assertEqual(p["equity_weight"], 0.873)
        self.assertEqual(p["equity_weight_source"], "继承月频")
        self.assertTrue(p["risk_halt"])                            # 风控停买默认开


class TestBackupFreshness(unittest.TestCase):
    """备份失效是"看不见的失效" —— 没人会每天去翻 data/backup/。"""

    def setUp(self):
        # 2026-09-19 审查: 原来落在 `tests/_tmp_backup_check/`（**仓库树内**的固定路径）。
        # 三宗罪：① 跑测试会把仓库变脏（该路径不在 .gitignore，中断一次就留个 untracked 目录，
        # 而"仓库干净"是本项目的一条审计项）；② 固定路径 ⇒ pytest-xdist 并发时两个 worker
        # 互相 rmtree；③ 只读检出（CI 挂载只读）直接崩。全套 40+ 处临时目录都是 tempfile，
        # 只有这里例外 —— 改回与全库一致。
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="neweco_backup_check_"))
        (self.tmp / "backup").mkdir(parents=True)
        (self.tmp / "weekly").mkdir(parents=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk(self, name, days_ago=0):
        p = self.tmp / "backup" / name
        p.write_bytes(b"x")
        import os
        import time
        ts = time.time() - days_ago * 86400
        os.utime(p, (ts, ts))
        return p

    def test_fresh_backup_passes(self):
        self._mk("neweco_20260913.db")
        with mock.patch.object(doctor, "DATA", self.tmp):
            it = doctor.check_backup({})
        self.assertTrue(it["ok"])
        self.assertIn("neweco_20260913.db", it["detail"])

    def test_missing_backup_fails(self):
        with mock.patch.object(doctor, "DATA", self.tmp):
            it = doctor.check_backup({})
        self.assertFalse(it["ok"])
        self.assertIn("无", it["detail"])
        self.assertTrue(it["fix"])

    def test_stale_backup_fails(self):
        self._mk("neweco_20260901.db", days_ago=12)
        with mock.patch.object(doctor, "DATA", self.tmp):
            it = doctor.check_backup({})
        self.assertFalse(it["ok"])
        self.assertIn("12 天前", it["detail"])

    def test_ledger_without_snapshot_fails(self):
        self._mk("neweco_20260913.db")
        (self.tmp / "weekly" / "ledger.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(doctor, "DATA", self.tmp):
            it = doctor.check_backup({})
        self.assertFalse(it["ok"], "有账本却没快照 ⇒ 必须报 ✗")


class TestStageLedgerCheck(unittest.TestCase):
    """体检项「阶段台账收口」（2026-09-19 审查）：只抓**当前悬空**的行。

    强杀/断电会让 `pipeline_run` 里某一行永远停在 'running'，而 `_stage_failed` 只认 'error'
    ⇒ 那个阶段既不告警也不补跑。判据必须精准：已被同阶段后续行取代的历史悬挂行（生产库里
    就有一条 20260911 的 daily_sync）是取证噪声，报 ✗ 只会制造"永远红的一项"。
    写的是测试库（`_isolation` 已把 DB_PATH 指到临时库），用例结束清掉自己插的行。
    """

    STAGES = ("_tst_doc_dangling", "_tst_doc_superseded", "_tst_doc_today")

    def setUp(self):
        import datetime
        from app import timeutil
        self.today = timeutil.date8()
        self.yday = (timeutil.today() - datetime.timedelta(days=1)).strftime("%Y%m%d")

    def tearDown(self):
        with db.conn_ctx() as conn:
            conn.execute(f"DELETE FROM pipeline_run WHERE stage IN ({','.join('?' * 3)})",
                         self.STAGES)
            conn.commit()

    def _put(self, stage, status, run_date):
        with db.conn_ctx() as conn:
            conn.execute("INSERT INTO pipeline_run (started_at, stage, run_date, status) "
                         "VALUES (?,?,?,?)",
                         (f"{run_date[:4]}-{run_date[4:6]}-{run_date[6:]} 18:00:00",
                          stage, run_date, status))
            conn.commit()

    def _check(self, stage):
        return doctor.check_stage_ledger({}, today=self.today, stages=(stage,))

    def test_dangling_row_fails_and_names_the_stage_with_a_fix(self):
        self._put("_tst_doc_dangling", "running", self.yday)
        it = self._check("_tst_doc_dangling")
        self.assertFalse(it["ok"], "往日起跑且最新一行仍是 running ⇒ 必须 ✗")
        self.assertIn("_tst_doc_dangling", it["detail"])
        self.assertIn(self.yday, it["detail"])
        self.assertTrue(it["fix"], "✗ 必须给可执行的修复指引")

    def test_superseded_row_is_noise_not_a_failure(self):
        self._put("_tst_doc_superseded", "running", self.yday)
        self._put("_tst_doc_superseded", "ok", self.today)
        self.assertTrue(self._check("_tst_doc_superseded")["ok"],
                        "该阶段后来跑成功了 ⇒ 历史悬挂行不该再报 ✗")

    def test_today_running_is_still_in_flight(self):
        self._put("_tst_doc_today", "running", self.today)
        self.assertTrue(self._check("_tst_doc_today")["ok"],
                        "今天起跑的可能还在跑，不算悬空")


class TestPlanFileTimeGate(unittest.TestCase):
    """「方案文件」体检项的**时间闸**（2026-09-24）。

    缺陷：原实现只要 `plan_<anchor>.json` 不存在就报 ✗，而**方案是执行日 09:32 档现算的**
    ⇒ 每个非执行日的体检都挂一条假 ✗（`fix` 文案自己都写着"09:32 档会现算"）。
    实测：2026-09-24 体检 `16/20`，其中一条 ✗ 就是它。

    为什么必须修：体检一有假 ✗，"有 ✗ 就要查"这条纪律就失效（报警疲劳）。

    红检：把 `check_plan` 里的时间闸去掉（回到"不存在就 ✗"）⇒
    `test_non_exec_day_is_not_a_failure` 当场红。
    """

    def setUp(self):
        import tempfile
        from app.weekly import plan as wk_plan
        self.wk_plan = wk_plan
        self.tmp = Path(tempfile.mkdtemp(prefix="neweco_plan_gate_"))
        self._p = mock.patch.object(wk_plan, "PLAN_DIR", self.tmp)
        self._p.start()
        self.addCleanup(self._p.stop)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _at(self, hh, mm):
        import datetime
        return datetime.datetime(2026, 9, 24, hh, mm)

    def test_non_exec_day_is_not_a_failure(self):
        """非执行日（today < exec_day）⇒ ✓，不是 ✗。"""
        it = doctor.check_plan({}, "20260924", today="20260924", exec_day="20260928")
        self.assertTrue(it["ok"], f"非执行日不该报 ✗：{it['detail']}")
        self.assertIn("非执行日", it["detail"])
        self.assertIn("20260928", it["detail"])

    def test_exec_day_before_open_slot_is_not_a_failure(self):
        """执行日但未到 09:32 ⇒ ✓（方案还没到该生成的时候）。"""
        it = doctor.check_plan({}, "20260924", today="20260928", exec_day="20260928",
                               now=self._at(8, 0))
        self.assertTrue(it["ok"], f"09:32 之前不该报 ✗：{it['detail']}")

    def test_exec_day_after_open_slot_fails(self):
        """执行日且已过 09:32 仍无方案 ⇒ ✗（这才是真的欠方案）。"""
        it = doctor.check_plan({}, "20260924", today="20260928", exec_day="20260928",
                               now=self._at(10, 0))
        self.assertFalse(it["ok"], "过了窗口还没有方案 ⇒ 必须 ✗")
        self.assertTrue(it["fix"])

    def test_overdue_after_exec_day_fails(self):
        """执行日已过（today > exec_day）⇒ ✗。"""
        it = doctor.check_plan({}, "20260924", today="20260929", exec_day="20260928")
        self.assertFalse(it["ok"], "执行日已过还没方案 ⇒ 必须 ✗")

    def test_existing_plan_is_reported_normally(self):
        """方案存在时按原逻辑报（行数 / 投出比例），不受时间闸影响。"""
        (self.tmp / "plan_20260924.json").write_text(
            json.dumps({"rows": [1, 2, 3], "invested_ratio": 0.87}), encoding="utf-8")
        it = doctor.check_plan({}, "20260924", today="20260924", exec_day="20260928")
        self.assertTrue(it["ok"])
        self.assertIn("3 只", it["detail"])

    def test_empty_plan_still_fails(self):
        """方案存在但 0 行 ⇒ 仍 ✗（时间闸只管"该不该存在"，不管内容好坏）。"""
        (self.tmp / "plan_20260924.json").write_text(
            json.dumps({"rows": [], "invested_ratio": 0.0}), encoding="utf-8")
        it = doctor.check_plan({}, "20260924", today="20260924", exec_day="20260928")
        self.assertFalse(it["ok"])

    def test_backward_compatible_without_dates(self):
        """不传 today/exec_day 时退回旧行为（不存在就 ✗）—— 供直接调用方使用。"""
        it = doctor.check_plan({}, "20260924")
        self.assertFalse(it["ok"])


class TestMarketCacheItem(unittest.TestCase):
    """行情缓存写入的**可观测出口**（2026-09-24）。

    背景：`factor_engine._cache_save` 的失败原来只有一行 `log.warning` —— 不进邮件、
    不进 doctor、不进体检 ⇒ 2026-09-24 19:34 那行 `arrow.py_extension_type` 失败
    **沉睡了 2 小时**才被人工审查发现。体检是"没人看日志时唯一会说话的地方"。

    红检：把 `checks()` 里那行 `check_market_cache` 删掉 ⇒ `TestDoctorShape` 的
    项数断言当场红（19≠20）；把 `_alert_cache_write_failed` 的 `_write_cache_status`
    删掉 ⇒ `test_failed_write_is_not_ok` 红。
    """

    def setUp(self):
        import tempfile
        from app.factors import factor_engine as fe
        self.fe = fe
        self.tmp = Path(tempfile.mkdtemp(prefix="neweco_mktcache_item_"))
        self._p = mock.patch.object(fe, "MARKET_CACHE_DIR", self.tmp)
        self._p.start()
        self.addCleanup(self._p.stop)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_status_file_is_not_a_failure(self):
        """没有状态文件 = 还没发生过大面板写入（新部署 / 刚清缓存）⇒ **不许假红**。"""
        self.assertFalse(self.fe._cache_status_path().exists())
        it = doctor.check_market_cache({})
        self.assertTrue(it["ok"], "缺状态文件不该判 ✗（否则每天早上一片假红）")

    def test_failed_write_is_not_ok_and_names_the_error(self):
        self.fe._write_cache_status(
            ok=False, key="deadbeefcafe1234",
            error="No type extension with name arrow.py_extension_type found")
        it = doctor.check_market_cache({})
        self.assertFalse(it["ok"], "写入失败必须 ✗（这正是原来没人看得见的那一类）")
        self.assertIn("arrow.py_extension_type", it["detail"])
        self.assertIn("deadbeefcafe1234"[:16], it["detail"])
        self.assertTrue(it["fix"], "✗ 必须给可执行的修复指引")

    def test_successful_write_is_ok(self):
        self.fe._write_cache_status(ok=True, key="abc123", error=None)
        it = doctor.check_market_cache({})
        self.assertTrue(it["ok"])
        self.assertIn("成功", it["detail"])

    def test_corrupt_status_file_is_not_ok(self):
        self.fe._cache_status_path().write_text("{not json", encoding="utf-8")
        it = doctor.check_market_cache({})
        self.assertFalse(it["ok"])
        self.assertIn("不可读", it["detail"])


if __name__ == "__main__":
    unittest.main()
