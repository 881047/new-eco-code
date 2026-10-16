"""baostock 采集链守护（**2026-10-13 重建版**）。

## 为什么有这个文件（重建背景，必读）

原实现是 `D:\\codex任务\\2026-09-15\\w\\work\\supervise.py` + 同目录的
`run_chain_hidden.vbs`。2026-10-13 夜间，一次按"目录名含 codex"判定的清理
把 `D:\\codex任务` 整目录删除 ⇒ 计划任务 `NEW ECO Baostock Chain`
（每 10 分钟）指向的启动器消失，任务自 21:36 起持续以退出码 1 失败
（`logs/baostock_chain_launcher.log` 最后一条成功记录停在 21:26:02）。

原文件从未入 git、D 盘无卷影副本 ⇒ **不可恢复**。本文件是**依据可考证据重建**，
不是原样恢复。重建所依据的证据：

1. `logs/baostock_chain_launcher.log`（628 KB，2026-09-17 ~ 2026-10-13）——
   完整保留了原实现的消息词汇表与时间戳格式，据此还原出判据与分支顺序；
2. `scripts/oneoff/backfill_baostock_witness.py` 的模块注释 ——
   明确写出"守护（supervise.py）启动子进程时**不设 cwd**（继承启动器的目录）"，
   以及"`--after` 默认等 `data/.baostock_min60_backfill.json` 的 `finished=true`"；
3. `data/.baostock_min60_backfill.json` / `.baostock_witness_backfill.json` ——
   两者都带顶层 `finished` 布尔字段（当前均为 `true`）；
4. `logs/backfill_baostock_{min60,witness}.stdout.log` 的存在 ——
   说明守护把各阶段的 stdout/stderr 重定向到了 `<stage>.stdout.log`
   （正是 `tests/test_baostock_backfill_guards.py` 记录的事故 #57：
   "隐藏启动又没重定向输出 ⇒ traceback 都没留下"）；
5. `docs/know/15_系统架构图.md`（"采集守护（work/supervise.py＋幂等启动器）"）
   与 `docs/know/07_事故台账.md`（"其 launcher 明写'绝不用 os.kill'"）。

## 行为（逐条对照日志还原，未自行发明判据）

    if 当前时刻落在 00:00–06:00 断网窗:
        log "<HH:MM> 在 00:00–06:00 窗外 ⇒ 什么都不做"; return
    for stage in (min60, witness):          # 严格串行，按序
        if stage 状态文件 finished == true:
            log "<stage> 已 finished ⇒ 看下一个"; continue
        if stage 的守护已在跑（pid 文件命中且 pid 存活）:
            log "<stage> 已有守护在跑（pid 文件命中）⇒ 不重复拉起"; return
        log "<stage> 无守护 ⇒ 拉起守护"
        拉起该阶段（脱离本进程）；return      # 一次只推进一个阶段
    log "链条全部 finished"

## 重建时**无法从证据确定、由本次实现选定**的部分（已知偏差，供复核）

- **pid 文件路径**：原实现只留下"pid 文件命中"这句日志，路径无从考证。
  本实现取 `logs/<stage>.guard.pid`（与 `logs/service_watchdog.state` 同目录）。
- **"守护"的实体**：本实现认为守护 = 该阶段脚本自身以脱离方式运行
  （证据：`<stage>.stdout.log` 的存在 + 阶段脚本本身已是可断点续跑的长任务）。
  若原实现另有一层 wrapper，则此处等价但不等同。
- **告警**：原实现是否发运维邮件无从考证；本实现**不发信**，只写日志
  （宁缺勿滥：先恢复采集能力，告警接线待人工确认后再加）。
- 原实现的退出码语义未留痕；本实现：0 = 正常（含什么都不做），2 = 拉起失败。

## 纪律

- **绝不用 `os.kill` / `taskkill` / `Stop-Process`**（07 号台账明载的原始约定）；
  陈旧 pid 文件只**删文件**，不碰进程。
- 子进程**不设 cwd**（与历史实现一致）⇒ 阶段脚本内部必须一律用绝对路径
  （见 `backfill_baostock_witness.py::DEFAULT_AFTER` 的注释）。
- 只依赖标准库（与 `service_watchdog.py` 同约束：自愈层要能在最脏的环境里跑）。
- 日志走 `NEWECO_LOG_DIR`（`tests/_isolation.py` 的红道）；生产不设该变量 ⇒ 行为不变。
- 时窗 `--windows 6-24`（用户 2026-09-15 明确允许**采集进程**放宽到此）；
  `00:00–06:00` 断网窗是硬底线，本文件在窗外直接什么都不做。

## 怎么启用（**由你决定**，本脚本自己不会改系统设置）

    schtasks /change /tn "NEW ECO Baostock Chain" ^
      /tr "wscript.exe //B \"D:\\NEW ECO\\scripts\\baostock_chain_hidden.vbs\""

手动跑一次（安全：当前两阶段均 finished ⇒ 只写一行"链条全部 finished"）：

    D:\\NEW ECO\\.venv\\Scripts\\python.exe -X utf8 D:\\NEW ECO\\scripts\\baostock_chain_supervise.py
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def _logs_dir() -> Path:
    """日志目录：优先 `NEWECO_LOG_DIR`（`tests/_isolation.py` 的红道），否则生产 `BASE/logs`。

    与 `service_watchdog.py::_logs_dir` 同一口径 —— 生产不会设该变量 ⇒ 行为不变。
    """
    return Path(os.environ.get("NEWECO_LOG_DIR") or (BASE / "logs"))


_LOGS = _logs_dir()
LOG = _LOGS / "baostock_chain_launcher.log"

#: 断网窗（硬底线）。窗外什么都不做 —— 日志原文即 "00:00–06:00 窗外 ⇒ 什么都不做"。
BLACKOUT_START, BLACKOUT_END = 0, 6

#: 正式口径时窗。阶段脚本自己会拒绝含 00:00–06:00 的时窗（见其 parse_windows）。
WINDOWS_SPEC = "6-24"

#: 链条定义：**严格串行**，顺序即依赖（witness 依赖 min60 的 finished）。
CHAIN = (
    {
        "name": "backfill_baostock_min60.py",
        "script": BASE / "scripts" / "oneoff" / "backfill_baostock_min60.py",
        "state": BASE / "data" / ".baostock_min60_backfill.json",
    },
    {
        "name": "backfill_baostock_witness.py",
        "script": BASE / "scripts" / "oneoff" / "backfill_baostock_witness.py",
        "state": BASE / "data" / ".baostock_witness_backfill.json",
    },
)


def log(msg: str) -> None:
    """时间戳格式 `%Y-%m-%dT%H:%M:%S` —— 逐字沿用原日志的格式（带 T，与
    `service_watchdog.py` 的空格分隔格式不同，故意保留以维持日志连续性）。"""
    line = f"{datetime.datetime.now():%Y-%m-%dT%H:%M:%S} {msg}"
    try:
        print(line)
    except Exception:
        pass                      # 计划任务是 wscript 隐藏启动: stdout 不可用时不许把守护带崩
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass                      # 日志写不了不该妨碍守护本身


def _in_blackout(now: datetime.datetime) -> bool:
    return BLACKOUT_START <= now.hour < BLACKOUT_END


def _finished(stage: dict) -> bool:
    """读阶段状态文件的顶层 `finished`。读不到/坏档 ⇒ False（fail-safe：宁可多跑一轮）。"""
    try:
        d = json.loads(stage["state"].read_text(encoding="utf-8"))
    except Exception:
        return False
    return bool(d.get("finished"))


def _pid_file(stage: dict) -> Path:
    return _LOGS / (stage["name"] + ".guard.pid")


def _pid_alive(pid: int) -> bool:
    """pid 是否存活（纯标准库：`tasklist` 查 PID，不引入 psutil）。

    只**查**，不杀 —— 与 07 号台账"绝不用 os.kill"的原始约定一致。
    """
    if pid <= 0:
        return False
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=10,
                           creationflags=flags, encoding="utf-8", errors="replace")
        return f'"{pid}"' in (r.stdout or "")
    except Exception:
        return False              # 查不了就当作"不在跑"，最坏是多拉一次（幂等脚本，可接受）


def _guard_running(stage: dict) -> bool:
    """pid 文件命中且 pid 存活 ⇒ 已有守护。陈旧 pid 文件只**删文件**，不碰进程。"""
    pf = _pid_file(stage)
    try:
        pid = int(pf.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if _pid_alive(pid):
        return True
    try:
        pf.unlink()
    except OSError:
        pass
    return False


def real_interpreter() -> str:
    """与 `start.bat` / `service_watchdog.py` 同口径：从 `.venv/pyvenv.cfg` 的 `home`
    解析真实解释器，解析不到才退回 venv 启动器。

    为什么不能直接用 `.venv\\Scripts\\python.exe`：见 `scripts/watchdog_task.cmd` 的注释 ——
    它是转发 stub，在无控制台上下文里会因"console write blocked"造成日志死锁（历史事故）。
    """
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


def _launch_guard(stage: dict) -> bool:
    """把该阶段脚本以**脱离本进程**的方式拉起，stdout/stderr 重定向到 `<stage>.stdout.log`。

    - `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`：守护退出不影响采集进程
      （与 `service_watchdog.py::restart` 同一手法）；
    - **不设 cwd**（与历史实现一致）⇒ 阶段脚本必须用绝对路径，这是它的既有纪律；
    - 打开 stdout 文件放在 `try` 内、失败退 `DEVNULL`：`logs/` 不可写时不许把守护带崩
      （07 号台账同族缺陷：`open` 写在 `try` 外会让异常穿出，自愈层在最需要时报不了）。
    """
    py = real_interpreter()
    env = {**os.environ,
           "PYTHONPATH": str(BASE / ".venv" / "Lib" / "site-packages"),
           "PYTHONIOENCODING": "utf-8"}
    stdout_path = _LOGS / (stage["name"] + ".stdout.log")
    try:
        out = stdout_path.open("a", encoding="utf-8", errors="ignore")
    except OSError:
        out = subprocess.DEVNULL
    flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    cmd = [py, "-u", "-X", "utf8", str(stage["script"]), "--windows", WINDOWS_SPEC]
    try:
        p = subprocess.Popen(cmd, env=env, stdout=out, stderr=subprocess.STDOUT,
                             creationflags=flags, close_fds=True)
    except OSError as e:
        log(f"{stage['name']} 拉起守护失败: {e}")
        return False
    try:
        _pid_file(stage).parent.mkdir(parents=True, exist_ok=True)
        _pid_file(stage).write_text(str(p.pid), encoding="utf-8")
    except OSError:
        pass                      # pid 文件写不了 → 下一轮会重复拉起；脚本幂等，可接受
    return True


def main() -> int:
    now = datetime.datetime.now()
    if _in_blackout(now):
        log(f"{now:%H:%M} 在 00:00–06:00 窗外 ⇒ 什么都不做")
        return 0
    for stage in CHAIN:
        if _finished(stage):
            log(f"{stage['name']} 已 finished ⇒ 看下一个")
            continue
        if _guard_running(stage):
            log(f"{stage['name']} 已有守护在跑（pid 文件命中）⇒ 不重复拉起")
            return 0
        log(f"{stage['name']} 无守护 ⇒ 拉起守护")
        return 0 if _launch_guard(stage) else 2
    log("链条全部 finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
