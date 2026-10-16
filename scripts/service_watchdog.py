r"""服务看门狗（2026-09-13）：探活 → 不在就按 `start.bat` 的口径把服务拉起来。

## 为什么需要它（进程内看门狗的死角）

19:45 的执行档看门狗、健康度、告警邮件**都跑在同一个 uvicorn 进程里** ——
进程死了，它们一起死，**没有任何人会知道**。而"服务死了"意味着**接下来所有档都不会跑**
（最早的一次损失就是一整周没有仓位）。

## 设计

- **只做一件事**：探服务端口的 `/api/overview`；通了就静默退出（打印一行便于排查）；
- 不通 ⇒ 按 `start.bat` 同一口径拉起服务：解释器取自 `.venv/pyvenv.cfg` 的 `home`
  （换机/换盘可用），`PYTHONPATH` 指向 venv 的 site-packages，输出追加到 `logs/server.log`，
  进程**完全脱离**本脚本（`DETACHED_PROCESS`）⇒ 脚本退出不影响服务；
- **冷却**：5 分钟内只拉一次（避免"服务正在启动"被误判成"死了"而反复拉起）。
  冷却时间戳存 `logs/service_watchdog.state`。

## 2026-09-14 加固（07 #38，外部复核指出后逐条复核为真）

- **拉起 ≠ 起来**：`restart()` 只要 `Popen` 不抛就返回 True —— 子进程因端口被占/依赖坏掉
  **秒退**时，会发一封"**已自动拉起**"的成功邮件、并写 5 分钟冷却 ⇒ 永久起不来的终端可以
  "报成功"到永远。现在 `main()` 拉起后**宽限重探**（`VERIFY_SECONDS`）才判成功。
- **失败时不许哑掉**：原来 `(_LOGS/"server.log").open("a")` 写在 `try` 之外，
  `logs/` 被删时异常直接穿出 ⇒ 末尾的 `alert()` **永远执行不到**（自愈层在最需要报警时静默）。
  现在提成模块常量 `SERVER_LOG` 并把 `open` 放进 `try`（失败退 `DEVNULL`）。
- **端口被占不重复拉起**：uvicorn 在 Windows 用 `SO_REUSEADDR`，而本仓没有单实例锁 ⇒
  "探活失败(冷启动窗口/卡死) ⇒ 再拉一个"会**并存两套调度器、抢同一份账本**（可能重复下单）。
  现在先看端口有没有人在监听：有人监听就**不拉**，如实报失败并把处置办法写进告警正文。

## 怎么启用（**由你决定**，本脚本自己不会去改系统设置）

    schtasks /create /tn "NEW ECO Watchdog" /sc minute /mo 10 ^
      /tr "\"D:\NEW ECO\.venv\Scripts\python.exe\" -X utf8 \"D:\NEW ECO\scripts\service_watchdog.py\""

（Windows 任务计划，每 10 分钟一次；不需要管理员权限。删除用 `schtasks /delete /tn "NEW ECO Watchdog" /f`。）
也可以直接手动跑一次：`.venv\\Scripts\\python.exe -X utf8 scripts\\service_watchdog.py`
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
#: 日志目录（2026-09-21 修）：优先 `NEWECO_LOG_DIR`（`tests/_isolation.py` 的红道），
#: 否则生产 `BASE/"logs"`。
#: 原实现把下面 7 个日志路径**全部硬连** `BASE/"logs"`，不走红道 ⇒ 测试跑 watchdog 时
#: 会把"探活失败现场"（含测试自己的 `AssertionError: 测试不许起真子进程`）写进**生产**
#: `logs/watchdog_events.log` —— 与 07 号台账 #1 同族，2026-09-20 加取证钩子时又踩了一次。
#: 生产不会设 `NEWECO_LOG_DIR` ⇒ 行为逐字不变。
def _logs_dir() -> Path:
    return Path(os.environ.get("NEWECO_LOG_DIR") or (BASE / "logs"))


_LOGS = _logs_dir()


#: 服务端口（2026-09-22）。**刻意不 `from app import config`** —— 本脚本是自愈层，
#: 它的设计约束是"服务挂了时得能在最脏的环境里跑"（见 `port_occupied` 的 docstring：
#: 纯标准库）。若为读一个端口而依赖 `app.config` 能成功 import，就等于把自愈能力
#: 绑在它要救的那个东西上。⇒ 自己读**同一个环境变量**，默认值由
#: `tests/test_port_single_source.py` 钉住与 `app.config.DEFAULT_SERVER_PORT` 一致。
def _server_port() -> int:
    raw = (os.environ.get("NEWECO_PORT") or "").strip()
    if raw:
        try:
            p = int(raw)
            if 1 <= p <= 65535:
                return p
        except ValueError:
            pass
        print(f"[watchdog] NEWECO_PORT={raw!r} 非法 ⇒ 回落 8787")
    return 8787


PORT = _server_port()
PROBE = f"http://127.0.0.1:{PORT}/api/overview"
COOLDOWN_MIN = 5
LOG = _LOGS / "watchdog.log"
STATE = _LOGS / "service_watchdog.state"
ALERT_STATE = _LOGS / "service_watchdog.alert"
ALERT_MIN_GAP_MIN = 360          # 同类告警 6 小时内只发一条(服务长期起不来时不刷屏)
#: 「自愈成功」类告警的**同日合并**状态（2026-09-18 邮件优化）：内容记的是
#: `{"date": "YYYYMMDD", "n": 当天第几次自愈}` —— 同一天第 2、3 次自愈不再各发一封。
#: 为什么必须另有一个文件而不是复用 ALERT_STATE：失败类要**保留 6 小时闸**（拉不起来是
#: 持续事故，必须周期性催人），成功类要的是**一天一封**；同一把尺子量两件事必有一段失真。
HEAL_STATE = _LOGS / "service_heal.json"
#: 自愈台账保留多少个自然日（含今天）。超过就剪掉最旧的 —— 这个文件由**晚间日终总结**读，
#: 用来把"被合并掉的自愈"显示出来；只保留两周已经够回答"最近一周服务死了几次"。
HEAL_KEEP_DAYS = 14
ALERT_SCRIPT = BASE / "scripts" / "alert_service.py"
SERVER_LOG = _LOGS / "server.log"       # 2026-09-14: 提成常量(测试可注入; 见 07 #38)
EVENTS_LOG = _LOGS / "watchdog_events.log"   # 探活失败时的进程现场(2026-09-20 取证钩子)
VERIFY_SECONDS = 75.0            # 拉起后宽限重探秒数(0 = 关闭, 测试用)。2026-09-16 审查
                                 # SCH-08: 30s 在冷启动(首次 import+杀软扫描)>30s 时误报
                                 # "拉起失败需人工"; start.bat 自述冷启动 10-20s 是常态下限。
# 2026-09-22：`PORT` 已提到文件上方（由 `NEWECO_PORT` 环境变量派生，默认 8787）。
# 这里原来写死 `PORT = 8787`，位置在 `_server_port()` **之后** ⇒ 会把它覆盖掉。
# 删除，只留一处定义。


def log(msg: str) -> None:
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    try:
        print(line)
    except Exception:
        pass                        # 计划任务是无控制台环境: stdout 不可用时不许把探活带崩
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass                        # 日志写不了不该妨碍探活


def is_alive(timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(PROBE, timeout=timeout) as r:
            return 200 <= int(r.status) < 300
    except Exception:
        return False


def _in_cooldown(now: float = None) -> bool:
    now = now or time.time()
    try:
        last = float(STATE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return (now - last) < COOLDOWN_MIN * 60


def _mark_restart(now: float = None) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(str(now or time.time()), encoding="utf-8")
    except OSError:
        pass


def real_interpreter() -> str:
    """与 start.bat 同口径: 从 pyvenv.cfg 的 home 解析真实解释器, 解析不到才退回。
    2026-09-16 调度审查 SCH-07: 末级回退原为硬编码 D:\\Python313 —— start.bat 09-15 已把
    同场景回退改为 venv 自己的启动器(硬编码盘符在换机/迁移后指向过期安装), 这里对齐;
    venv 启动器永远解析到建 venv 的那个基础解释器。"""
    cfg = BASE / ".venv" / "pyvenv.cfg"
    try:
        for line in cfg.read_text(encoding="utf-8").splitlines():
            if line.strip().lower().startswith("home"):
                home = line.split("=", 1)[1].strip()
                p = Path(home) / "python.exe"
                if p.exists():
                    return str(p)
    except OSError:
        pass
    return str(BASE / ".venv" / "Scripts" / "python.exe")


def _alert_due(now: float = None) -> bool:
    """离上次**成功**告警是否已过 6 小时(读不到状态文件 = 该发)。

    只用于**拉起失败**类：拉不起来是持续事故，必须周期性催人，所以按时间闸。
    「自愈成功」类走 `HEAL_STATE` 的按天合并（见 `bump_heal`），不看这里。
    """
    now = now or time.time()
    try:
        last = float(ALERT_STATE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    return (now - last) >= ALERT_MIN_GAP_MIN * 60


def _mark_alert(now: float = None) -> None:
    try:
        ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
        ALERT_STATE.write_text(str(now or time.time()), encoding="utf-8")
    except OSError:
        pass


# ------------------------------------------------------------------ 自愈台账
# 2026-09-18 邮件优化：「自愈成功」从"6 小时一条"改成"同日一条"之后，**被合并掉的那几次
# 必须仍然看得见** —— 否则"降噪"就成了"隐瞒"，而服务一天死三次这件事恰恰是本轮最需要
# 让人知道的（邮件侧只能合并，不能修根因）。台账落在 `HEAL_STATE`，由晚间日终总结的
# 「今日运维」小节读（`app/notify.py::_ops_section`）。
def heal_read() -> dict:
    """读自愈台账；读不到/坏档 ⇒ 返回 `{"days": {}}`（fail-open：宁可重算，不许崩探活）。"""
    try:
        raw = json.loads(HEAL_STATE.read_text(encoding="utf-8"))
        days = raw.get("days") if isinstance(raw, dict) else None
        return {"days": days} if isinstance(days, dict) else {"days": {}}
    except Exception:
        return {"days": {}}


def _heal_write(book: dict) -> None:
    # 原子换名：这个文件被**另一个进程**（服务里的晚间邮件档）读，半截 JSON 会让那一节消失
    try:
        HEAL_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(HEAL_STATE) + ".tmp")
        tmp.write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, HEAL_STATE)
    except OSError:
        pass                        # 台账写不了不影响探活与拉起本身（它只是"给后人留个凭证"）


def bump_heal(ok: bool, today: str = None) -> dict:
    """记一次自愈尝试，返回今天这条台账 `{ok, fail, mailed}`。

    `mailed` = 今天是否已经为"成功类"发过信（同日合并的判据；只有**真的发出去**才置 1，
    发送失败时下一轮会再试 —— 与 `alert()` 的"失败不写时间戳"同一套语义）。
    """
    today = today or f"{datetime.datetime.now():%Y%m%d}"
    book = heal_read()
    days = book["days"]
    rec = dict(days.get(today) or {})
    rec.setdefault("ok", 0)
    rec.setdefault("fail", 0)
    rec.setdefault("mailed", 0)
    rec["ok" if ok else "fail"] += 1
    days[today] = rec
    if len(days) > HEAL_KEEP_DAYS:                 # 剪掉最旧的，按日期串排序即可
        for k in sorted(days)[:-HEAL_KEEP_DAYS]:
            days.pop(k, None)
    _heal_write(book)
    return rec


def mark_heal_mailed(today: str = None) -> None:
    """把今天的 `mailed` 置 1（成功类邮件确实发出去了才调）。"""
    today = today or f"{datetime.datetime.now():%Y%m%d}"
    book = heal_read()
    rec = dict(book["days"].get(today) or {"ok": 0, "fail": 0, "mailed": 0})
    rec["mailed"] = 1
    book["days"][today] = rec
    _heal_write(book)


def alert(title: str, body: str) -> bool:
    """把运维事件交给 `scripts/alert_service.py`（子进程，走生产邮件实现）。

    为什么用子进程：本脚本刻意只依赖标准库 —— 服务挂了的时候，它得能在**最脏的环境**里
    跑完探活与拉起，不该被 app 侧的依赖拖死。
    告警失败**只记日志**，绝不影响探活与拉起；而且失败时**不写**告警时间戳 ⇒ 下一轮
    (10 分钟后)会再试，直到真的发出去为止。
    """
    try:
        env = {**os.environ, "PYTHONPATH": str(BASE / ".venv" / "Lib" / "site-packages"),
               "PYTHONIOENCODING": "utf-8"}
        # 2026-09-13（用户实拍："时不时弹一个窗口又立刻消失"）：告警子进程是**控制台程序**，
        # 从无控制台的上下文（wscript 隐藏启动的计划任务）拉起时，Windows 会给它**新建一个控制台窗口**
        # ⇒ 每次告警都闪一下。加 CREATE_NO_WINDOW 后彻底无窗（不影响捕获输出）。
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.run([real_interpreter(), "-X", "utf8", str(ALERT_SCRIPT), title, body],
                           cwd=str(BASE), env=env, capture_output=True, timeout=120,
                           creationflags=no_window)
        if p.returncode != 0:
            log(f"告警未发出(退出码 {p.returncode}): {title}")
            return False
        log(f"告警已发出: {title}")
        return True
    except Exception as e:
        log(f"告警发送异常: {e}")
        return False


def port_occupied(host: str = "127.0.0.1", port: int = PORT, timeout: float = 1.0) -> bool:
    """服务端口上有没有人在监听（**只建 TCP 连接，不发 HTTP**）。

    用途：探活失败时区分"服务完全不在"与"端口被占"（正在启动 / 卡死 / 已有第二个实例）。
    后者**绝不能再生一个** —— uvicorn 在 Windows 用 `SO_REUSEADDR`，第二个实例会成功绑定，
    于是并存两套 APScheduler、抢同一份 `data/weekly/ledger.json`（07 #38）。
    纯标准库实现：本脚本的设计约束是"服务挂了时它得能在最脏的环境里跑"。
    """
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _await_alive(seconds: float, alive_fn=None) -> bool:
    """拉起后宽限重探：**起来才算成功**（07 #38）。`seconds<=0` 只探一次。"""
    alive_fn = alive_fn or is_alive
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if alive_fn():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(2.0, max(0.2, seconds)))


def restart() -> bool:
    """拉起服务（脱离本进程）。返回**是否已发出启动命令** ——
    是否真的起来由 `main()` 的宽限重探判定（07 #38：原实现把"命令发出去了"当成了"起来了"）。"""
    py = real_interpreter()
    env = {"PYTHONPATH": str(BASE / ".venv" / "Lib" / "site-packages"),
           "PYTHONIOENCODING": "utf-8"}
    full_env = {**os.environ, **env}
    try:
        out = SERVER_LOG.open("a", encoding="utf-8", errors="ignore")
    except OSError:
        # 2026-09-14（07 #38）：原来这一行在 try 之外 ⇒ logs/ 被删时异常穿出 main()，
        # 末尾的 alert() 永远执行不到 = 自愈层在最需要报警的时刻静默。日志写不了也照样拉。
        out = subprocess.DEVNULL
    # 2026-09-24（批 0.10，用户拍板"两条都现在做"）：加 CREATE_BREAKAWAY_FROM_JOB，
    # 使拉起的服务进程**脱离父作业对象**（计划任务设了「运行 10 分钟后停止任务」，
    # DETACHED_PROCESS 只脱离控制台、不脱离作业对象 ⇒ 可能被任务树带走）。
    # ⚠️ **因果未坐实**：真·自发死亡 ≈1.1 次/天且死因未明，散发特征与"每 10 分钟必死"不符；
    # 本改动属**猜测性处置**，收益未验证。登记于 07 号事故台账。
    # ⚠️ 若作业对象不允许 breakaway，`Popen` 会抛 OSError ⇒ **必须回退**不带该位重试，
    # 否则"修一个猜的因"反而把自愈彻底打断（比不修更坏）。
    base_flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        base_flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    # 2026-10-14（S13 取证闭环）：拉起改为经 `scripts/service_supervisor.py` 包装 ——
    # 包装进程 wait() 服务子进程并把**退出码**写 `logs/service_exit.log`。此前看门狗
    # 直启 ⇒ 服务被外部 TerminateProcess 杀掉时（2026-10-13/14 排查结论，见 07 #35）
    # 退出码无人记录，死因永远查不清。探活判据不受影响（is_alive 是纯 HTTP 探测）；
    # DETACHED / BREAKAWAY 语义不变，只是 Popen 的目标换成了包装器。
    cmd = [py, "-u", "-X", "utf8", str(BASE / "scripts" / "service_supervisor.py"),
           py, "-u", "-X", "utf8", "-m", "uvicorn", "app.main:app",
           "--host", "127.0.0.1", "--port", str(PORT)]
    for flags in (base_flags | breakaway, base_flags):
        try:
            subprocess.Popen(cmd, cwd=str(BASE), env=full_env,
                             stdout=out, stderr=subprocess.STDOUT,
                             creationflags=flags, close_fds=True)
            _mark_restart()
            log(f"服务不在 ⇒ 已发出拉起命令 ({py})"
                + ("[BREAKAWAY]" if flags & breakaway else "[回退:无 BREAKAWAY]"))
            return True
        except OSError as e:
            if not (flags & breakaway):
                log(f"拉起服务失败: {e}")
                return False
            log(f"BREAKAWAY 被拒（{e}）⇒ 回退普通 flags 重试")
    return False


def _death_scene_extra() -> list:
    """死亡现场的**两条新取证**（2026-09-22 加）：系统内存 + `server.log` 末尾。

    为什么加：09-14 与 09-22 的多次"服务不在"都**没有任何优雅关闭痕迹**
    （`server.log` 里没有 `Finished server process`、没有 Traceback、也没有 WER），
    ⇒ 只能靠现场旁证区分两种死法：**被外部杀/被任务树带走** vs **内存耗尽被系统杀**。
    本机每天自愈 3~8 次（累计 81 次），死因至今**未定 ⇒ 不猜**，只把现场加厚。

    同样只**留现场**：不改任何判据、不新增自愈行为，异常一律吞掉。
    """
    out = []
    try:                                   # 物理/提交内存（ctypes，不依赖外部命令）
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            gb = 1024 ** 3
            out.append(f"内存: 负载 {st.dwMemoryLoad}% | 可用物理 "
                       f"{st.ullAvailPhys / gb:.2f} GB / 共 {st.ullTotalPhys / gb:.2f} GB | "
                       f"可用提交 {st.ullAvailPageFile / gb:.2f} GB")
    except Exception as e:
        out.append(f"内存取证失败: {type(e).__name__}: {e}")
    try:                                   # uvicorn 最后的痕迹（有没有优雅关闭/异常）
        if SERVER_LOG.exists():
            tail = SERVER_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
            out.append("server.log 末 30 行:")
            out += [f"  | {t}" for t in tail]
        else:
            out.append(f"server.log 不存在: {SERVER_LOG}")
    except Exception as e:
        out.append(f"server.log 取证失败: {type(e).__name__}: {e}")
    return out


def snapshot_processes(path: Path = None) -> str:
    """拉起前抓一次进程现场（三补十一"未解事件"的取证钩子；写进 `logs/watchdog_events.log`）。

    09-14 那次服务死在 13:15~13:25，`server.log` 零痕迹、事件日志也没有 WER ⇒ 事后无法回答
    "当时端口上还有没有半死不活的 python.exe"。这一步只**留现场**、不做任何判定：
    拉起/不拉起的判据仍然只看 `is_alive()` + `port_occupied()` + 冷却，**没有新增自愈行为**。
    任何异常一律吞掉 —— 取证绝不允许把自愈链路带崩。
    """
    path = Path(path) if path else EVENTS_LOG
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"===== {stamp} 探活失败现场 ====="]
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=10, creationflags=flags,
                           encoding="utf-8", errors="replace")
        body = (r.stdout or "").strip()
        lines.append(f"tasklist rc={r.returncode}，python.exe {len(body.splitlines())} 行:")
        lines += body.splitlines()[:20] or ["  (无)"]
    except Exception as e:
        lines.append(f"tasklist 失败: {type(e).__name__}: {e}")
    try:
        lines.append(f"port_occupied({PORT}) = {port_occupied()}")
    except Exception as e:
        lines.append(f"端口探测失败: {type(e).__name__}: {e}")
    lines += _death_scene_extra()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n\n")
    except OSError:
        pass
    return "\n".join(lines)


def main() -> int:
    if is_alive():
        log("服务在跑（探活 200）")
        return 0
    if _in_cooldown():
        log(f"服务不可达，但 {COOLDOWN_MIN} 分钟冷却中 ⇒ 不重复拉起")
        return 1
    log("服务不可达 ⇒ 走自愈流程")
    snapshot_processes()                      # 先留现场再动手（三补十一；只写文件，不参与判定）
    note = ""
    if port_occupied():
        # 07 #38: 端口还有人监听(启动中/卡死/已有实例) ⇒ 不重复拉起, 否则并存两套调度器抢同一份账本
        ok = False
        note = (f"{PORT} 端口仍被占用，但 HTTP 探活失败 ⇒ 判定为「正在启动 / 卡死 / 已有第二个实例」，"
                "本次不重复拉起。人工处置：任务管理器里找残留的 python.exe（命令行含 uvicorn），"
                "必要时结束它再跑 start.bat；同时看 logs/server.log 尾部。")
        log("端口仍被占用且探活失败 ⇒ 不重复拉起（见 logs/server.log）")
    else:
        ok = restart()
        if ok and VERIFY_SECONDS > 0:
            ok = _await_alive(VERIFY_SECONDS)
            log("拉起后宽限内探活成功" if ok else
                f"拉起后 {VERIFY_SECONDS:.0f}s 仍探活失败（可能还在冷启动；看 logs/server.log）")
    # 2026-09-13: 自愈成功/失败都要**有人知道** —— 原来只写日志, 而日志没人看就等于没有。
    # 反面场景很硬: 服务因端口占用/依赖坏掉而反复拉不起来时, 终端会一直静默不交易。
    rec = bump_heal(ok)      # 每次自愈尝试都记一笔，**与发不发信无关**（被合并的也要留凭证）
    # 2026-09-18 邮件优化：成功类改「同日一条」，失败类保留 6 小时闸。
    # 实测 6 天里这类信发了 8 封，内容几乎相同 —— 重复的不是事故，是同一件事的回声。
    due = (not rec.get("mailed")) if ok else _alert_due()
    if due:
        ts = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
        if ok:
            title = "服务自愈: 已自动拉起"
            body = (f"{ts} 探活失败(服务不可达), 看门狗已自动把服务拉起来。\n"
                    f"解释器: {real_interpreter()}\n"
                    f"今天累计自愈 {rec.get('ok', 0)} 次"
                    f"（同日只在第 1 次发信，后续只记台账 logs/service_heal.json，"
                    f"晚间日终总结的「今日运维」小节统一看）。\n"
                    f"这条一般意味着服务曾经崩过或被杀掉, 请抽空看一眼 logs/server.log 的尾部。")
        else:
            title = "自愈失败: 服务拉不起来(需要人工)"
            body = (f"{ts} 探活失败, 且看门狗拉起失败 —— 终端此刻不会自己恢复。\n"
                    f"下一步: 看 logs/server.log 与 logs/watchdog_task.log; "
                    f"手动拉起用 start.bat。")
        if note:
            body = f"{body}\n\n{note}"
        if alert(title, body):
            if ok:
                mark_heal_mailed()   # 只记"当天已为成功类发过一封信"，台账计数照旧累加
            else:
                _mark_alert()
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
