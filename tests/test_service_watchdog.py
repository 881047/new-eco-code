"""服务看门狗守卫（2026-09-13）

它存在的理由很硬：19:45 的执行档看门狗、健康度、告警邮件**都跑在同一个 uvicorn 进程里**，
进程死了它们一起死 ⇒ 必须有一个**进程外**的东西能把服务拉回来。

守四条：① 探活 200 才算活；② 冷却期内不重复拉起（避免"启动中"被误判成"死了"）；
③ 服务活着时**不许**写冷却状态、不许拉起；④ 拉起用 `pyvenv.cfg` 解析出的真实解释器。

外加一条（2026-09-13 实拍抓到）：**不许写生产日志**。原实现只 patch 了 `STATE`，`LOG` 仍指向
`logs/watchdog.log` —— 于是每跑一次测试就往真日志里写一行"服务在跑/走自愈流程"。
后果：真出事时翻日志，分不清哪些"已拉起"是自愈真的干过、哪些是测试写进去的
（`test_down_triggers_restart` 用测试日志里必须出现"走自愈流程"来锁死这一点）。
"""
import ast
import importlib.util
import json
import re
import sys
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

_spec = importlib.util.spec_from_file_location("svc_watchdog", BASE / "scripts" / "service_watchdog.py")
wd = importlib.util.module_from_spec(_spec)
sys.modules["svc_watchdog"] = wd
_spec.loader.exec_module(wd)

_guard = None


def setUpModule():
    """看门狗测试**一律不许起真子进程**（2026-09-13 实拍踩到的那类事故）。

    看门狗的两个出口都是"真动作"：`restart()` 真的拉起服务、`alert()` 真的发运维邮件。
    当天 `TestCooldownAndRestart.test_down_triggers_restart` 忘了 mock `alert`，于是**一次
    pytest 真的把一封「服务自愈: 已自动拉起」邮件发了出去**（`notify_log` 里查得到，id=8）。
    这和"测试往生产日志里写"是同一类事故：测试的副作用跑进了生产。这里把子进程入口整个封死 ——
    漏 mock 的测试会当场炸，而不是悄悄对外发信/拉服务。
    """
    global _guard
    _guard = mock.patch.multiple(wd.subprocess, run=mock.DEFAULT, Popen=mock.DEFAULT)
    mocks = _guard.start()
    for m in mocks.values():
        m.side_effect = AssertionError("测试不许起真子进程(会拉起服务/发邮件)")


def tearDownModule():
    if _guard is not None:
        _guard.stop()


class _IsolatedWatchdogCase(unittest.TestCase):
    """会**真的触发看门狗出口**（`alert` / `restart` / `main`）的测试类都继承这里。

    `STATE` / `LOG` / `ALERT_STATE` 一律指向 `logs/_test_watchdog.*` ⇒ **绝不写生产运维日志**。
    起因（2026-09-14 外部复核 O2）：`TestNoFlashingWindow` 调 `wd.alert()`（它会 `log()`），
    而原来没 patch `LOG` ⇒ 生产 `logs/watchdog.log` 里累积了 **51 行** "告警已发出: 标题"
    （测试里的字面标题）+ **72 行** "服务不在 ⇒ 已拉起"（另一个测试直呼真 `restart()`）。
    这与 07 号 #1/#2/#32 是同一类事故：**测试的副作用跑进生产**。
    """

    def setUp(self):
        logs = BASE / "logs"
        self.log = logs / "_test_watchdog.log"
        self.state = logs / "_test_watchdog.state"
        self.alert_state = logs / "_test_watchdog.alert"
        self.heal_state = logs / "_test_watchdog.heal.json"
        self.events_log = logs / "_test_watchdog.events.log"
        for p in (self.log, self.state, self.alert_state, self.heal_state, self.events_log):
            p.unlink(missing_ok=True)
        self._stack = ExitStack()
        self._stack.enter_context(mock.patch.object(wd, "STATE", self.state))
        self._stack.enter_context(mock.patch.object(wd, "LOG", self.log))
        self._stack.enter_context(mock.patch.object(wd, "ALERT_STATE", self.alert_state))
        # 自愈台账是 2026-09-18 新加的**第三个落点**，同样是模块级常量 ⇒ 必须一起指走
        self._stack.enter_context(mock.patch.object(wd, "HEAL_STATE", self.heal_state))
        # 探活失败现场是 2026-09-20 加的**第四个落点**，同样漏过 ⇒ 生产
        # `logs/watchdog_events.log` 尾部出现过测试自己的
        # "AssertionError: 测试不许起真子进程"（07 号 #1 同族，第三次踩）。
        self._stack.enter_context(mock.patch.object(wd, "EVENTS_LOG", self.events_log))

    def tearDown(self):
        self._stack.close()
        for p in (self.log, self.state, self.alert_state, self.heal_state, self.events_log):
            p.unlink(missing_ok=True)

    def _ctx(self):
        return self._stack          # 需要额外 patch 的（如 alert）自己继续 enter_context


class TestNoFlashingWindow(_IsolatedWatchdogCase):
    """2026-09-13 用户实拍："时不时弹出一个窗口并且立即消失"。

    根因：计划任务是 **Interactive only**，而它直接执行 `watchdog_task.cmd`（批处理）
    ⇒ **每 10 分钟在桌面上闪一个控制台窗口**；另外 `alert()` 拉起的告警子进程是控制台程序，
    从无控制台的上下文启动时 Windows 会**新建**一个控制台窗口 ⇒ 每次告警再闪一下。

    守两条：
      ① 告警子进程必须带 `CREATE_NO_WINDOW`（否则隐藏启动也白搭）；
      ② 隐藏启动器必须是 ASCII-only 且真的用隐藏窗口参数（`Run(..., 0, ...)`）。

    ⚠️ **2026-09-14 修（外部复核 O2）**：本类调 `wd.alert()`，而 `alert()` 会 `log()` 到
    `wd.LOG` —— 原来没 patch ⇒ 每跑一次测试就往**生产** `logs/watchdog.log` 写一行
    "告警已发出: 标题"（实测已累积 51 行）。现在统一走 `_IsolatedWatchdogCase` 的隔离夹具。
    """

    def test_alert_subprocess_is_windowless(self):
        fake = mock.Mock(returncode=0)
        with mock.patch.object(wd.subprocess, "run", return_value=fake) as m:
            wd.alert("标题", "正文")
        kw = m.call_args.kwargs
        self.assertIn("creationflags", kw, "告警子进程没带 creationflags ⇒ 会闪控制台窗口")
        self.assertTrue(kw["creationflags"] & getattr(wd.subprocess, "CREATE_NO_WINDOW", 1),
                        "creationflags 里没有 CREATE_NO_WINDOW")

    def test_hidden_launcher_is_ascii_and_hidden(self):
        vbs = BASE / "scripts" / "watchdog_task_hidden.vbs"
        self.assertTrue(vbs.exists(), "缺隐藏启动器 scripts/watchdog_task_hidden.vbs")
        raw = vbs.read_bytes()
        raw.decode("ascii")                      # 非 ASCII 会抛错：计划任务代码页下会解析失败
        txt = raw.decode("ascii")
        self.assertIn("watchdog_task.cmd", txt, "启动器必须仍然调用 .cmd（它负责解析真解释器+写日志）")
        self.assertIn(", 0,", txt, "Run(..., 0, ...) 才是隐藏窗口；缺了就等于没修")

    def test_logon_launcher_is_ascii_and_hidden(self):
        """第二条同源闪窗（2026-09-13 07 #29）：登录自启那条链。

        启动文件夹里原来是 `start "NEW ECO" "…\\start.bat"` —— 直接跑批处理，
        于是**每次登录**都闪一个控制台窗口，开完看板就关。它与 07 #27 的
        10 分钟计划任务是**两个不同的源**，只修后者等于没修干净。
        """
        vbs = BASE / "scripts" / "start_hidden.vbs"
        self.assertTrue(vbs.exists(), "缺隐藏启动器 scripts/start_hidden.vbs")
        txt = vbs.read_bytes().decode("ascii")   # 非 ASCII 会抛错：WSH/代码页下会解析失败
        self.assertIn("start.bat", txt, "启动器必须调用 start.bat（解释器解析+日志都在它里面）")
        self.assertIn("--hidden", txt, "必须带 --hidden，否则隐藏窗口里的 pause 会永远卡住")
        self.assertIn(", 0,", txt, "Run(..., 0, ...) 才是隐藏窗口；缺了就等于没修")

    def test_logon_entry_is_a_shortcut_not_a_batch(self):
        """**2026-09-19 07 #60**：Startup 里的 `.bat` 壳本身就会闪窗。

        这条链修过两次仍在闪：Explorer 拉起启动文件夹里的批处理时，是 `cmd.exe` 开一个
        **可见**的控制台（实测：shell 启动的探针 .bat ⇒ `cmd.exe class=PseudoConsoleWindow
        visible=Y`），所以哪怕 .bat 里只写一行 `wscript //B start_hidden.vbs` 也照样每次登录
        闪一次 —— 07 #29 只改了 .bat 的**内容**，没改**外壳**。登录入口必须是
        `.lnk → wscript.exe`（GUI 子系统宿主，根本没有控制台）。
        """
        txt = (BASE / "scripts" / "create_shortcut.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("start_hidden.vbs", txt, "登录自启必须走隐藏启动器")
        self.assertIn("wscript.exe", txt, "宿主必须是 wscript.exe：控制台程序会被 Windows 新建窗口")
        self.assertNotIn("Set-Content", txt.split("$startup =", 1)[-1],
                         "安装器不许再把批处理写进启动文件夹（07 #60）")

    def test_start_bat_never_pauses_when_hidden(self):
        """隐藏模式下的 `pause` = 一个永远关不掉、用户还看不见的窗口。"""
        txt = (BASE / "start.bat").read_text(encoding="utf-8")
        bare = [ln.strip() for ln in txt.splitlines() if ln.strip().lower() == "pause"]
        self.assertEqual(bare, [], "start.bat 里有裸 pause ⇒ 隐藏自启会卡在看不见的窗口上")
        self.assertIn('if /i "%~1"=="--hidden"', txt, "缺少 --hidden 开关的识别")

    def test_start_bat_keeps_comments_ascii(self):
        """批处理里**注释行必须 ASCII**（2026-09-13 实测 A/B）。

        一条中文 `rem` 让 cmd 把**下一行**整行吞掉：`if defined NE_HIDDEN echo 登录自启
        -隐藏窗口 >> launch.log` 不再执行，日志里凭空少一行 —— 而窗口/服务一切正常，
        所以只有对着日志数行数才看得出来。多字节行解析漂移属静默失效，只能靠规则挡。
        用户可见的顶层 `echo …中文…` 不在本规则内（它们在 launch.log 里有多年正常记录）。
        """
        raw = (BASE / "start.bat").read_bytes()
        bad = [ln.decode("utf-8", "replace")[:70] for ln in raw.splitlines()
               if ln.strip().lower().startswith(b"rem") and any(b > 127 for b in ln)]
        self.assertEqual(bad, [], f"start.bat 的 rem 注释含非 ASCII：{bad} —— 会吞掉下一行")

    def test_hidden_branch_does_not_use_minimized_start(self):
        """**07 #44**：`--hidden` 分支不许走 `start /min` —— **最小化 ≠ 不可见**
        （`IsWindowVisible()` 仍为真 ⇒ 用户会看到一个不会自己消失的最小化控制台，
        实测：`cmd.exe class=ConsoleWindowClass visible=Y title='NEW ECO SERVER - 请勿关闭'`）。
        隐藏分支必须交给看门狗入口（它对服务用 `DETACHED_PROCESS`：根本没有控制台）。"""
        txt = (BASE / "start.bat").read_text(encoding="utf-8")
        self.assertIn("watchdog_task_hidden.vbs", txt,
                      "隐藏分支必须走隐藏拉起器（否则冷启动会留一个可见的最小化窗口）")
        i_goto = txt.index("if defined NE_HIDDEN goto start_hidden")
        i_min = txt.index('start "NEW ECO SERVER - 请勿关闭" /min')
        self.assertLess(i_goto, i_min,
                        "隐藏分支跳转必须排在 `start /min` **之前** ⇒ NE_HIDDEN 时根本执行不到它")


class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestProbe(unittest.TestCase):
    def test_alive_on_200(self):
        with mock.patch.object(wd.urllib.request, "urlopen", return_value=_Resp(200)):
            self.assertTrue(wd.is_alive())

    def test_dead_on_error(self):
        with mock.patch.object(wd.urllib.request, "urlopen", side_effect=OSError("refused")):
            self.assertFalse(wd.is_alive())

    def test_non_200_is_not_alive(self):
        with mock.patch.object(wd.urllib.request, "urlopen", return_value=_Resp(500)):
            self.assertFalse(wd.is_alive())


class TestCooldownAndRestart(unittest.TestCase):
    def setUp(self):
        self.state = BASE / "logs" / "_test_watchdog.state"
        self.log = BASE / "logs" / "_test_watchdog.log"
        self.alert_state = BASE / "logs" / "_test_watchdog.alert"
        self.heal_state = BASE / "logs" / "_test_watchdog.heal.json"
        self.events_log = BASE / "logs" / "_test_watchdog.events.log"
        # 2026-09-22 补：`events_log` 是 2026-09-20 新增的**第四个落点**，此前 setUp 创建并
        # patch 了它、却没进清理元组 ⇒ 生产 `logs/_test_watchdog.events.log` 每次跑测试都残留
        # （07 号 #1 同族第四次踩；元测试 TestNoProdLogWrites 原本拦不住 —— 它只检查类体内是否
        # 出现 "EVENTS_LOG" 字符串，不校验清理）。纪律：patch 几个常量就清理几个。
        for p in (self.state, self.log, self.alert_state, self.heal_state, self.events_log):
            if p.exists():
                p.unlink()

    def tearDown(self):
        for p in (self.state, self.log, self.alert_state, self.heal_state, self.events_log):
            if p.exists():
                p.unlink()

    def _ctx(self):
        """STATE / LOG / 告警时间戳 / 自愈台账 **都必须**指向测试文件（见模块 docstring 最后一段）。"""
        st = ExitStack()
        st.enter_context(mock.patch.object(wd, "STATE", self.state))
        st.enter_context(mock.patch.object(wd, "LOG", self.log))
        st.enter_context(mock.patch.object(wd, "ALERT_STATE", self.alert_state))
        st.enter_context(mock.patch.object(wd, "HEAL_STATE", self.heal_state))
        st.enter_context(mock.patch.object(wd, "EVENTS_LOG", self.events_log))
        st.enter_context(mock.patch.object(wd, "alert", return_value=True))
        st.enter_context(mock.patch.object(wd, "VERIFY_SECONDS", 0.0))       # 07 #38: 测试里不等宽限
        st.enter_context(mock.patch.object(wd, "port_occupied", return_value=False))
        return st

    def test_cooldown_blocks(self):
        with self._ctx():
            self.assertFalse(wd._in_cooldown())
            wd._mark_restart()
            self.assertTrue(wd._in_cooldown())
            self.assertFalse(wd._in_cooldown(now=time.time() + (wd.COOLDOWN_MIN + 1) * 60))

    def test_alive_does_not_restart_nor_write_state(self):
        with self._ctx(), \
                mock.patch.object(wd, "is_alive", return_value=True), \
                mock.patch.object(wd, "restart") as rst:
            self.assertEqual(wd.main(), 0)
            rst.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_down_triggers_restart(self):
        with self._ctx(), \
                mock.patch.object(wd, "is_alive", return_value=False), \
                mock.patch.object(wd, "restart", return_value=True) as rst, \
                mock.patch.object(wd, "alert", return_value=True) as p_alert:
            self.assertEqual(wd.main(), 0)
            rst.assert_called_once()
            p_alert.assert_called_once()
        # 这一行必须落在**测试日志**里: 若哪天真日志里出现"走自愈流程", 只能是自愈自己干的
        self.assertTrue(self.log.exists(), "日志没写到测试文件 ⇒ 多半又写进生产 logs/watchdog.log 了")
        self.assertIn("走自愈流程", self.log.read_text(encoding="utf-8"))

    def test_down_within_cooldown_does_not_restart(self):
        self.state.write_text(str(time.time()), encoding="utf-8")
        with self._ctx(), \
                mock.patch.object(wd, "is_alive", return_value=False), \
                mock.patch.object(wd, "restart") as rst:
            self.assertEqual(wd.main(), 1)
            rst.assert_not_called()

    def test_interpreter_resolves_from_pyvenv(self):
        py = wd.real_interpreter()
        self.assertTrue(str(py).lower().endswith("python.exe"))
        self.assertTrue(Path(py).exists(), f"解析出的解释器不存在: {py}")

    def test_restart_survives_unwritable_server_log(self):
        """**07 #38**：`logs/` 被删时原来 `server.log` 的 open 抛在 `try` 之外 ⇒ 异常穿出 `main()`，
        末尾的 `alert()` 永远执行不到（自愈层在最需要报警的时刻静默）。现在必须照拉不误、照样告警。"""
        with self._ctx(), \
                mock.patch.object(wd, "is_alive", return_value=False), \
                mock.patch.object(wd, "SERVER_LOG", Path(r"Z:\no_such_dir\server.log")), \
                mock.patch.object(wd, "VERIFY_SECONDS", 0.05), \
                mock.patch.object(wd, "real_interpreter", return_value=r"D:\python\python.exe"), \
                mock.patch.object(wd.subprocess, "Popen") as m_popen, \
                mock.patch.object(wd, "_mark_restart"):
            rc = wd.main()
        m_popen.assert_called_once()                 # 日志写不了也**照样拉**
        self.assertEqual(rc, 2)                      # 宽限=0 且探活 False ⇒ 如实报失败

    def test_restart_tries_breakaway_from_job(self):
        """批 0.10（用户拍板）：拉起首选带 `CREATE_BREAKAWAY_FROM_JOB`（疑被计划任务作业对象带走）。
        ⚠️ 本测试只锁「确实尝试了」，**不锁「确实有效」** —— 因果未坐实，见 07 号事故台账。"""
        brk = getattr(wd.subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        with self._ctx(), \
                mock.patch.object(wd, "real_interpreter", return_value=r"D:\python\python.exe"), \
                mock.patch.object(wd.subprocess, "Popen") as m_popen, \
                mock.patch.object(wd, "_mark_restart"):
            self.assertTrue(wd.restart())
        self.assertTrue(m_popen.call_args.kwargs["creationflags"] & brk,
                        "首次拉起没带 CREATE_BREAKAWAY_FROM_JOB")

    def test_restart_goes_through_exit_code_supervisor(self):
        """**07 #35（S13 取证闭环）**：拉起必须经 `service_supervisor.py` 包装 ——
        服务被外部 TerminateProcess 杀掉时（2026-10-13/14 排查结论），只有包装进程的
        `wait()` 能留下退出码。反面场景：退回直启 ⇒ 死因永远查不清。"""
        with self._ctx(), \
                mock.patch.object(wd, "real_interpreter", return_value=r"D:\python\python.exe"), \
                mock.patch.object(wd.subprocess, "Popen") as m_popen, \
                mock.patch.object(wd, "_mark_restart"):
            self.assertTrue(wd.restart())
        cmd = m_popen.call_args.args[0]
        self.assertIn("service_supervisor.py", " ".join(cmd),
                      "拉起没走退出码包装器")
        self.assertIn("uvicorn", " ".join(cmd), "包装器参数里必须仍含 uvicorn 启动命令")
        self.assertTrue((BASE / "scripts" / "service_supervisor.py").exists(),
                        "包装器脚本必须在仓内")

    def test_restart_falls_back_when_breakaway_rejected(self):
        """作业对象不允许 breakaway ⇒ `Popen` 抛 OSError ⇒ **必须回退**不带该位重试。
        反面场景很硬：不回退 = 修一个猜的因，反而把自愈彻底打断（比不修更坏）。"""
        brk = getattr(wd.subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        seen = []

        def _popen(*a, **kw):
            flags = kw.get("creationflags", 0)
            seen.append(flags)
            if flags & brk:
                raise OSError("拒绝访问（breakaway 被拒）")
            return mock.MagicMock()

        with self._ctx(), \
                mock.patch.object(wd, "real_interpreter", return_value=r"D:\python\python.exe"), \
                mock.patch.object(wd.subprocess, "Popen", side_effect=_popen), \
                mock.patch.object(wd, "_mark_restart"):
            self.assertTrue(wd.restart(), "BREAKAWAY 被拒后必须回退重试成功")
        self.assertEqual(len(seen), 2, f"应尝试两次（BREAKAWAY + 回退），实际 {len(seen)}")
        self.assertTrue(seen[0] & brk)
        self.assertFalse(seen[1] & brk, "回退那次仍带 BREAKAWAY ⇒ 没真正回退")

    def test_restart_returns_false_when_both_attempts_fail(self):
        """两档都失败 ⇒ 必须返回 False（不许把"命令发出去了"当成"起来了"）。"""
        with self._ctx(), \
                mock.patch.object(wd, "real_interpreter", return_value=r"D:\python\python.exe"), \
                mock.patch.object(wd.subprocess, "Popen", side_effect=OSError("端口占用")), \
                mock.patch.object(wd, "_mark_restart"):
            self.assertFalse(wd.restart())


class TestSelfHealAlert(unittest.TestCase):
    """自愈要**有人知道**（2026-09-13）：只写日志等于没人知道。

    反面场景很硬：服务因端口占用/依赖坏掉反复拉不起来时，终端会一直静默不交易。
    守五条：① 活着不发；② 拉起成功/失败各发一条，措辞不同；③ **成功类同日只一封**、
    失败类仍按 6 小时闸（2026-09-18 邮件优化）；④ 告警**没发出去**就不写状态 ⇒
    下一轮还会再试（宁可多试，不可静默）；⑤ 被合并掉的自愈**必须在台账里计数**（不许隐瞒）。
    """

    def setUp(self):
        self.state = BASE / "logs" / "_test_watchdog.state"
        self.log = BASE / "logs" / "_test_watchdog.log"
        self.alert_state = BASE / "logs" / "_test_watchdog.alert"
        # 自愈台账（同日合并 + 计数）必须一起打桩：它默认落在**生产 logs/**，
        # 不打桩的话跑一次测试就会往生产台账里写几条假自愈记录（2026-09-18 实测踩过）。
        self.heal_state = BASE / "logs" / "_test_watchdog.heal.json"
        self.events_log = BASE / "logs" / "_test_watchdog.events.log"
        # 2026-09-22 补：`events_log` 是 2026-09-20 新增的**第四个落点**，此前 setUp 创建并
        # patch 了它、却没进清理元组 ⇒ 生产 `logs/_test_watchdog.events.log` 每次跑测试都残留
        # （07 号 #1 同族第四次踩；元测试 TestNoProdLogWrites 原本拦不住 —— 它只检查类体内是否
        # 出现 "EVENTS_LOG" 字符串，不校验清理）。纪律：patch 几个常量就清理几个。
        for p in (self.state, self.log, self.alert_state, self.heal_state, self.events_log):
            if p.exists():
                p.unlink()

    def tearDown(self):
        for p in (self.state, self.log, self.alert_state, self.heal_state, self.events_log):
            if p.exists():
                p.unlink()

    def _ctx(self):
        st = ExitStack()
        st.enter_context(mock.patch.object(wd, "STATE", self.state))
        st.enter_context(mock.patch.object(wd, "LOG", self.log))
        st.enter_context(mock.patch.object(wd, "ALERT_STATE", self.alert_state))
        st.enter_context(mock.patch.object(wd, "HEAL_STATE", self.heal_state))
        st.enter_context(mock.patch.object(wd, "EVENTS_LOG", self.events_log))
        st.enter_context(mock.patch.object(wd, "VERIFY_SECONDS", 0.0))       # 07 #38
        st.enter_context(mock.patch.object(wd, "port_occupied", return_value=False))
        return st

    def _run(self, alive, restart_ok, alert_ok=True):
        with self._ctx(), \
                mock.patch.object(wd, "is_alive", return_value=alive), \
                mock.patch.object(wd, "restart", return_value=restart_ok), \
                mock.patch.object(wd, "alert", return_value=alert_ok) as p_alert:
            rc = wd.main()
        return rc, p_alert

    def test_alive_sends_no_alert(self):
        rc, p_alert = self._run(True, True)
        self.assertEqual(rc, 0)
        p_alert.assert_not_called()

    def test_restart_alert_marks_state(self):
        rc, p_alert = self._run(False, True)
        self.assertEqual(rc, 0)
        p_alert.assert_called_once()
        title, body = p_alert.call_args.args
        self.assertIn("已自动拉起", title)
        self.assertIn("看门狗已自动把服务拉起来", body)
        # 成功类走**按天**合并（2026-09-18），不再写 6 小时闸的 ALERT_STATE
        self.assertFalse(self.alert_state.exists(),
                         "成功类不该占 6 小时闸 —— 那会把随后真正的「拉起失败」告警一起挡掉")
        rec = json.loads(self.heal_state.read_text(encoding="utf-8"))["days"]
        today = time.strftime("%Y%m%d")
        self.assertEqual(rec[today]["mailed"], 1, "发出去了就要置 mailed，否则同日会连发")
        self.assertEqual(rec[today]["ok"], 1)

    def test_success_alerts_merge_within_the_same_day(self):
        """同日第二次自愈**不再发信**，但**台账必须继续累加**（合并 ≠ 隐瞒）。

        实测 6 天发了 8 封几乎相同的"已自动拉起" —— 重复的不是事故，是同一件事的回声；
        而"服务一天死三次"这件事本身不能因为合并就看不见，所以计数留在台账里，
        由 18:30 日终总结的「今日运维」小节汇总。
        """
        with self._ctx(), \
                mock.patch.object(wd, "is_alive", return_value=False), \
                mock.patch.object(wd, "restart", return_value=True), \
                mock.patch.object(wd, "alert", return_value=True) as p_alert:
            self.assertEqual(wd.main(), 0)
            self.assertEqual(wd.main(), 0)
            self.assertEqual(wd.main(), 0)
        self.assertEqual(p_alert.call_count, 1, "同日三次自愈只发第一封")
        rec = json.loads(self.heal_state.read_text(encoding="utf-8"))["days"]
        today = time.strftime("%Y%m%d")
        self.assertEqual(rec[today]["ok"], 3, "被合并掉的两次也要在台账里数得出来")

    def test_failed_alert_keeps_the_6h_gate(self):
        """失败类**不受**同日合并影响：拉不起来是持续事故，6 小时一条是刻意的骚扰级提醒。"""
        rc, p_alert = self._run(False, False)
        self.assertEqual(rc, 2)
        self.assertTrue(self.alert_state.exists(), "失败类仍写 ALERT_STATE(6 小时闸)")
        rec = json.loads(self.heal_state.read_text(encoding="utf-8"))["days"]
        today = time.strftime("%Y%m%d")
        self.assertEqual(rec[today]["fail"], 1)

    def test_failed_restart_alert_says_needs_human(self):
        rc, p_alert = self._run(False, False)
        self.assertEqual(rc, 2)
        title, _ = p_alert.call_args.args
        self.assertIn("自愈失败", title)
        self.assertIn("需要人工", title)

    # ---- 07 #38：拉起 ≠ 起来 / 端口被占不许再拉一个 --------------------------------
    def test_grace_probe_turns_an_unverified_spawn_into_a_failure(self):
        """命令发出去了、但宽限内探活仍失败 ⇒ **必须报失败**（原来只要 Popen 不抛就报"已自动拉起"，
        于是一个永远起不来的终端可以每小时发一封成功邮件）。"""
        with self._ctx(), \
                mock.patch.object(wd, "is_alive", return_value=False), \
                mock.patch.object(wd, "restart", return_value=True), \
                mock.patch.object(wd, "VERIFY_SECONDS", 0.05), \
                mock.patch.object(wd, "alert", return_value=True) as p_alert:
            rc = wd.main()
        self.assertEqual(rc, 2, "拉起后没起来 ⇒ 退出码必须是失败(2)")
        self.assertIn("自愈失败", p_alert.call_args.args[0])

    def test_grace_probe_confirms_success(self):
        """拉起后宽限内探活成功 ⇒ 才算成功（这也是"服务真的回来了"的唯一证据）。"""
        with self._ctx(), \
                mock.patch.object(wd, "is_alive", side_effect=[False, True]), \
                mock.patch.object(wd, "restart", return_value=True), \
                mock.patch.object(wd, "VERIFY_SECONDS", 1.0), \
                mock.patch.object(wd, "alert", return_value=True) as p_alert:
            rc = wd.main()
        self.assertEqual(rc, 0)
        self.assertIn("已自动拉起", p_alert.call_args.args[0])

    def test_occupied_port_blocks_a_duplicate_spawn(self):
        """**双实例防线**：端口还有人监听（启动中/卡死/已有实例）却探活失败时，
        绝不能再拉一个 —— uvicorn 在 Windows 用 SO_REUSEADDR，第二个实例会成功绑定，
        于是并存两套调度器、抢同一份账本（可能重复下单）。"""
        with self._ctx(), \
                mock.patch.object(wd, "is_alive", return_value=False), \
                mock.patch.object(wd, "port_occupied", return_value=True), \
                mock.patch.object(wd, "restart") as rst, \
                mock.patch.object(wd, "alert", return_value=True) as p_alert:
            rc = wd.main()
        self.assertEqual(rc, 2)
        rst.assert_not_called()
        self.assertIn("不重复拉起", p_alert.call_args.args[1])

    def test_port_occupied_probe_is_tcp_only(self):
        """`port_occupied` 只做 TCP 连接（不发 HTTP）—— 用真套接字自检一次。"""
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            self.assertTrue(wd.port_occupied(port=port, timeout=1.0))
        finally:
            srv.close()
        self.assertFalse(wd.port_occupied(port=port, timeout=0.5), "端口关了就该报 False")

    def test_failure_alert_is_throttled_within_6h(self):
        """**失败类**才吃 6 小时闸：拉不起来是持续事故，但要按小时催、不是按分钟刷。"""
        self.alert_state.write_text(str(time.time()), encoding="utf-8")
        rc, p_alert = self._run(False, False)
        self.assertEqual(rc, 2)
        p_alert.assert_not_called()

    def test_failure_alert_throttle_expires_after_6h(self):
        self.alert_state.write_text(str(time.time() - (wd.ALERT_MIN_GAP_MIN + 1) * 60),
                                    encoding="utf-8")
        rc, p_alert = self._run(False, False)
        p_alert.assert_called_once()

    def test_success_alert_ignores_the_6h_gate(self):
        """成功类**不看** 6 小时闸（2026-09-18）：它的合并单位是"自然日"。

        为什么两条闸必须分开：6 小时闸是为"服务一直起不来"设计的（每小时催一次），
        套在成功类上就变成了"早上自愈过一次 ⇒ 下午真死掉时那一封失败告警被挡 4 小时"。
        """
        self.alert_state.write_text(str(time.time()), encoding="utf-8")
        rc, p_alert = self._run(False, True)
        self.assertEqual(rc, 0)
        p_alert.assert_called_once()

    def test_failed_alert_does_not_mark_state_so_next_tick_retries(self):
        rc, p_alert = self._run(False, False, alert_ok=False)
        self.assertEqual(rc, 2)
        p_alert.assert_called_once()
        self.assertFalse(self.alert_state.exists(), "告警没发出去却写了时间戳 ⇒ 6 小时内不再试, 等于静默")


class TestTaskEntryCmd(unittest.TestCase):
    """计划任务入口 `scripts/watchdog_task.cmd` 的两条硬约束：**ASCII + CRLF**。

    2026-09-13 实测：apply_patch 写出来的是 LF 行尾，cmd.exe 把它解析成一串
    `'EALPY' is not recognized` —— 而这类错误**只在计划任务真正跑起来时**才暴露，
    平时改文件根本看不出来（编码不是根因：同一份内容 GBK/UTF-8 都触发，`start.bat` 一直是
    CRLF 所以从没踩到）。这里把它钉成守卫，避免哪天编辑器/工具再存一次 LF 就静默失效。
    """

    PATH = BASE / "scripts" / "watchdog_task.cmd"

    def test_is_ascii_only(self):
        raw = self.PATH.read_bytes()
        bad = [b for b in raw if b > 127]
        self.assertEqual(bad, [], f"{self.PATH.name} 含非 ASCII 字节 {bad[:8]} —— 这个文件必须纯 ASCII")

    def test_has_crlf_line_endings(self):
        raw = self.PATH.read_bytes()
        self.assertIn(b"\r\n", raw, "没有 CRLF ⇒ 行尾被改成 LF 了")
        self.assertIsNone(re.search(rb"(?<!\r)\n", raw), "存在裸 LF 行尾 —— cmd.exe 会解析错位")


class TestNoProdLogWrites(unittest.TestCase):
    """**元测试**：会触发看门狗出口（`alert` / `restart` / `main`）的测试类必须做隔离。

    起因（2026-09-14 外部复核 O2）：`TestNoFlashingWindow` 调 `wd.alert()`（它内部 `log()`），
    `test_no_flashing_subprocess.py` 调真 `restart()` —— 两个都没 patch `LOG` ⇒ 生产
    `logs/watchdog.log` 里累积了 51 行 "告警已发出: 标题" + 72 行 "服务不在 ⇒ 已拉起"。
    这类"测试副作用跑进生产"在本项目已复发四次（07 #1 / #2 / #21 / #32），所以这里不再靠人盯：
    静态扫这两个测试文件，**要求**每个触达出口的测试类要么继承 `_IsolatedWatchdogCase`，
    要么在自己的正文里 patch 了 `LOG`。
    """

    TRIGGERS = ("wd.alert(", "wd.restart(", "wd.main(", "self.wd.restart(", "self.wd.main(")
    FILES = ("test_service_watchdog.py", "test_no_flashing_subprocess.py")
    #: 看门狗的**所有**落点：日志、冷却时间戳、告警时间戳、自愈台账（2026-09-18 新增）、
    #: 探活失败现场（**2026-09-20 新增**）。
    #: 元测试按"全都要 patch"要求 ⇒ 以后再加落点，必须同时加到这里，否则红。
    #: ⚠️ 2026-09-21：`EVENTS_LOG` 当初加进来时**漏了同步这里** ⇒ 生产
    #: `logs/watchdog_events.log` 尾部混进测试自己的 "AssertionError: 测试不许起真子进程"
    #: （07 号 #1 同族，第三次踩同一个坑）⇒ 补进清单。
    SIDE_EFFECTS = ("LOG", "STATE", "ALERT_STATE", "HEAL_STATE", "EVENTS_LOG")

    def test_every_class_touching_entrypoints_isolates_the_log(self):
        bad = []
        for fname in self.FILES:
            p = BASE / "tests" / fname
            src = p.read_text(encoding="utf-8")
            tree = ast.parse(src)
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                body = ast.get_source_segment(src, node) or ""
                if not any(t in body for t in self.TRIGGERS):
                    continue
                bases = [getattr(b, "id", getattr(b, "attr", "")) for b in node.bases]
                if "_IsolatedWatchdogCase" in bases:
                    continue
                missing = [s for s in self.SIDE_EFFECTS
                           if f'"{s}"' not in body]
                if missing:
                    bad.append(f"{fname}::{node.name} 缺 {missing}")
        self.assertEqual(bad, [],
                         "这些测试类会触达看门狗出口却没隔离全部落点 ⇒ 会写进生产 logs/: "
                         f"{bad}")

    def test_every_sink_created_in_setup_is_cleaned_in_teardown(self):
        """第二道闸：光"patch 了"不够，**tearDown 必须清理**。

        起因（2026-09-22）：`EVENTS_LOG` 在 `TestCooldownAndRestart` / `TestSelfHealAlert` /
        `TestWatchdogRestart` 三个类里 setUp 都建了、`_ctx()` 也都 patch 了，但**tearDown 的清理
        元组漏了它** ⇒ 生产 `logs/_test_watchdog.events.log` 实测残留 2579B（07 号 #1 同族
        **第四次**踩）。上面那个元测试只检查"类正文里出现过该常量名"，拦不住"patch 了但没清理"。

        判据不写死常量名单（那样下次加落点还会再漏一次 —— 已经漏过三次了），
        改成从代码里**现推**：setUp 里凡是赋值给「`logs/` 下的路径」的属性，tearDown 必须引用到。
        ⇒ 以后新加落点，只要 setUp 建了文件，不清理就红。
        """
        bad = []
        for fname in self.FILES:
            p = BASE / "tests" / fname
            src = p.read_text(encoding="utf-8")
            tree = ast.parse(src)
            by_name = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                body = ast.get_source_segment(src, node) or ""
                if not any(t in body for t in self.TRIGGERS):
                    continue
                bases = [getattr(b, "id", getattr(b, "attr", "")) for b in node.bases]
                # setUp/tearDown 可能在基类（_IsolatedWatchdogCase）里
                owner = by_name.get("_IsolatedWatchdogCase") if "_IsolatedWatchdogCase" in bases else node
                fns = {n.name: n for n in owner.body if isinstance(n, ast.FunctionDef)}
                up, down = fns.get("setUp"), fns.get("tearDown")
                if up is None:
                    continue
                # setUp 里「值指向 logs/ 下文件」的属性 = 会落进生产 logs/ 的落点
                sinks = [n.targets[0].attr
                         for n in ast.walk(up)
                         if isinstance(n, ast.Assign)
                         and len(n.targets) == 1
                         and isinstance(n.targets[0], ast.Attribute)
                         and "logs" in (ast.get_source_segment(src, n.value) or "")]
                if not sinks:
                    continue
                down_src = ast.get_source_segment(src, down) if down else ""
                missing = sorted({s for s in sinks if f"self.{s}" not in down_src})
                if missing:
                    where = f"{fname}::{node.name}"
                    if owner is not node:
                        where += f"（清理在基类 {owner.name}）"
                    bad.append(f"{where} 的 tearDown 没清理 {missing}")
        self.assertEqual(bad, [],
                         "setUp 在 logs/ 下建了文件、tearDown 却没清 ⇒ 每次跑测试都往生产 "
                         f"logs/ 留残留: {bad}")


class TestDeathSceneForensics(unittest.TestCase):
    """2026-09-22 新增的两条取证：**内存** + `server.log` 末 30 行。

    起因：服务每天自愈 3~8 次（累计 81 次），而 `server.log` 里**没有任何**
    `Finished server process` / Traceback / WER ⇒ 死因未明，**不许猜**（14 号同类先例）。
    只能把现场加厚，用旁证区分「被外部杀 / 被任务树带走」与「内存耗尽被系统杀」。

    这两条是**取证**，不是判据 ⇒ 铁律：**永不抛、永不改判据、永不新增自愈行为**。
    （不测 `snapshot_processes` 整体，因为它要真跑 `tasklist` —— 本模块有子进程禁令。）
    """

    def test_extra_returns_memory_line_and_never_raises(self):
        out = wd._death_scene_extra()
        self.assertIsInstance(out, list)
        self.assertTrue(all(isinstance(x, str) for x in out))
        self.assertTrue(any(("内存:" in x) or ("取证失败" in x) for x in out),
                        f"内存取证行缺失: {out[:2]}")

    def test_extra_survives_missing_server_log(self):
        bogus = Path(BASE) / "logs" / "_no_such_server_log_4test.log"
        with mock.patch.object(wd, "SERVER_LOG", bogus):
            out = wd._death_scene_extra()          # 不许抛
        self.assertTrue(any(("不存在" in x) or ("取证失败" in x) for x in out),
                        f"server.log 缺失时应当如实标注而非静默: {out[-2:]}")


if __name__ == "__main__":
    unittest.main()
