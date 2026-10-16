r"""服务父进程（2026-10-14，S13 取证闭环）：包装 uvicorn，等它退出并记录**退出码**。

为什么：S13 的服务死亡是"无声消失"——`server.log` 无优雅关闭痕迹、无 WER 崩溃记录、
仓内无任何查杀脚本（2026-10-13/14 排查结论：**被外部 TerminateProcess**，证据链见
07 号事故台账 #35）。看门狗 `restart()` 直启时无人等待进程退出 ⇒ 退出码随进程一起
消失，死因永远查不清。退出码是区分死法的决定性证据：
  0            = 正常退出（有人优雅地停了它）
  1            = Python 层报错（server.log 里应有 Traceback）
  0xC0000005   = 原生访问违例崩溃（应伴随 WER 记录）
  0xC000013A   = 控制台被关闭 / CTRL_C
  其他小整数    = 被 TerminateProcess（具体值由杀手决定；taskkill /F 惯用 1）

本包装器只做 wait + 记录：不重试、不拉起（那是看门狗的事），不装信号处理器
（不改变 uvicorn 的任何行为）。uvicorn 的 stdout/stderr 仍进 `logs/server.log`
（与看门狗直启同一口径）。日志：`logs/service_exit.log`，一行一次退出。
"""
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
EXIT_LOG = BASE / "logs" / "service_exit.log"
SERVER_LOG = BASE / "logs" / "server.log"


def main() -> None:
    cmd = sys.argv[1:]
    if not cmd:
        return
    t0 = time.monotonic()
    try:
        out = SERVER_LOG.open("a", encoding="utf-8", errors="ignore")
    except OSError:
        out = subprocess.DEVNULL            # 与 service_watchdog.restart 同一兜底
    # 本包装器自身已被看门狗以 DETACHED_PROCESS 拉起（无控制台）；子进程继承该上下文，
    # CREATE_NO_WINDOW 仅作为双保险（对齐 tests/test_no_flashing_subprocess 的全仓纪律）。
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(cmd, cwd=str(BASE), env=os.environ.copy(),
                            stdout=out, stderr=subprocess.STDOUT,
                            creationflags=flags, close_fds=True)
    code = proc.wait()
    mins = (time.monotonic() - t0) / 60.0
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = (f"{ts} uvicorn 退出 code={code} (0x{code & 0xFFFFFFFF:08X}) "
            f"存活 {mins:.1f} 分钟\n")
    try:
        EXIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with EXIT_LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass                                # 进程已退出，取证写不了不影响任何东西


if __name__ == "__main__":
    main()
