"""闪现窗口取证器（只读诊断；2026-09-13 07 号 #27/#29 用）

用户两次反馈同一句话："时不时弹出一个窗口并且立即消失"。这类问题的**唯一**可靠判据是
抓 `SetWinEventHook` 的 `EVENT_OBJECT_SHOW`，而不是猜哪条链路更可疑 —— 本项目已经吃过
一次亏：先修了 10 分钟看门狗计划任务（#27），用户仍然看到闪窗，第二个源其实是**登录自启**
那条链（#29）。

用法（跑 40 分钟可覆盖多个计划任务刻度）::

    D:\\Python313\\python.exe -X utf8 scripts\\diag_window_flash.py --seconds 2400

读数：
  * `visible=Y` 且进程是 cmd/conhost/WindowsTerminal ⇒ **真有窗口闪**，看 `pid` 后面的
    `进程 <- 父进程 <- 祖父进程` 定位是谁拉起来的；
  * `visible=N` ⇒ 窗口建了但从没 show（隐藏启动器就该长这样），用户看不见，不算闪窗；
  * 本机默认终端是 Windows Terminal，所以**控制台闪现表现为**
    `WindowsTerminal.exe class=CASCADIA_HOSTING_WINDOW_CLASS` + 一个
    `cmd.exe class=PseudoConsoleWindow` 子窗口 —— 不是老式 `ConsoleWindowClass`。

**必须做阳性对照**（否则"没抓到"可能只是钩子没生效）::

    Start-Process cmd.exe -ArgumentList '/c timeout /t 3'

应看到一行 `SHOW … WindowsTerminal.exe … visible=Y`；没有这行 ⇒ 先修钩子再谈结论。
"""
import argparse
import ctypes
import time
from ctypes import wintypes as w

u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

EV_OBJ_SHOW = 0x8002
EV_SYS_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
TH32CS_SNAPPROCESS = 0x00000002

# 只报这些进程（外加任何 visible 的顶层窗口）—— 否则 Chrome/Tooltip 的隐藏窗口会刷屏
WATCH = {"cmd.exe", "conhost.exe", "wscript.exe", "cscript.exe", "powershell.exe",
         "pwsh.exe", "python.exe", "pythonw.exe", "git.exe", "mshta.exe",
         "schtasks.exe", "timeout.exe"}

PROC = ctypes.WINFUNCTYPE(None, w.HANDLE, w.DWORD, w.HWND, w.LONG, w.LONG,
                          w.DWORD, w.DWORD)
u32.SetWinEventHook.restype = w.HANDLE
u32.SetWinEventHook.argtypes = [w.DWORD, w.DWORD, w.HMODULE, PROC,
                                w.DWORD, w.DWORD, w.DWORD]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD),
                ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
                ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


def proc_map() -> dict:
    """pid -> (ppid, exe 名)。只为把"谁拉起来的"写进日志。"""
    h = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    out, e = {}, PROCESSENTRY32W()
    e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    if k32.Process32FirstW(h, ctypes.byref(e)):
        while True:
            out[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile)
            if not k32.Process32NextW(h, ctypes.byref(e)):
                break
    k32.CloseHandle(h)
    return out


def image(pid: int) -> str:
    h = k32.OpenProcess(0x1000, False, pid)          # QUERY_LIMITED_INFORMATION
    if not h:
        return "?"
    buf, n = ctypes.create_unicode_buffer(1024), w.DWORD(1024)
    ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
    k32.CloseHandle(h)
    return buf.value.split("\\")[-1] if ok else "?"


def gtxt(hwnd, which: str) -> str:
    b = ctypes.create_unicode_buffer(512)
    (u32.GetWindowTextW if which == "t" else u32.GetClassNameW)(hwnd, b, 512)
    return b.value


def main() -> int:
    ap = argparse.ArgumentParser(description="闪现窗口取证（只读）")
    ap.add_argument("--seconds", type=int, default=600, help="监测时长（默认 600 秒）")
    args = ap.parse_args()
    stats = {"n": 0}

    def cb(hook, ev, hwnd, idobj, idchild, tid, t):
        try:
            if u32.GetAncestor(hwnd, 2) != hwnd:      # 只看顶层窗口
                return
            pid = w.DWORD(0)
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            img, cls = image(pid.value), gtxt(hwnd, "c")
            vis, alive = bool(u32.IsWindowVisible(hwnd)), bool(u32.IsWindow(hwnd))
            if not (vis or "Console" in cls or "Terminal" in cls or img in WATCH):
                return
            if ev == EV_SYS_FOREGROUND and not vis:
                return
            r = w.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(r))
            pm = proc_map()
            chain, p = img, pm.get(pid.value, (0, "?"))
            for _ in range(2):                        # 往上追两级父进程
                if not p[1] or p[1] in ("explorer.exe", "svchost.exe", "services.exe"):
                    break
                chain += " <- %s" % p[1]
                p = pm.get(p[0], (0, "?"))
            stats["n"] += 1
            print("%s %s pid=%d %s class=%s visible=%s alive=%s rect=%dx%d title=%r" % (
                time.strftime("%H:%M:%S"), "FG" if ev == EV_SYS_FOREGROUND else "SHOW",
                pid.value, chain, cls, "Y" if vis else "N", "Y" if alive else "N",
                r.right - r.left, r.bottom - r.top, gtxt(hwnd, "t")), flush=True)
        except Exception as exc:                      # 钩子回调里绝不能抛
            print("err %s" % exc, flush=True)

    # ⚠️ **回调必须保活**（2026-09-14 实测踩到并修，见 07 #43）：
    # 原写法 `SetWinEventHook(ev, ev, None, PROC(cb), ...)` 把 `PROC(cb)` 建了个临时对象，
    # 语句结束即可被 GC ⇒ Windows 之后回调的是**悬空函数指针** ⇒ 第一条窗口事件到达时
    # 进程**访问违例、无 traceback、无 stderr，直接消失**。后果极具迷惑性：
    # 日志只剩开头那行 —— "零窗口事件"与"取证器自己崩了"**无法区分**（#27/#29 的
    # "零闪窗"结论就是靠这个工具下的 ⇒ 那份证据作废，见 07 #43）。
    hook_fns, hooks = [], []
    for ev in (EV_OBJ_SHOW, EV_SYS_FOREGROUND):
        fn = PROC(cb)                     # 保活: 见上
        hook_fns.append(fn)
        hooks.append(u32.SetWinEventHook(ev, ev, None, fn, 0, 0, WINEVENT_OUTOFCONTEXT))
    print("监测中 %s（%d 秒）。阳性对照: Start-Process cmd.exe -ArgumentList '/c timeout /t 3'"
          % (time.strftime("%H:%M:%S"), args.seconds), flush=True)
    msg, end, last = w.MSG(), time.time() + args.seconds, time.time()
    while time.time() < end:
        while u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.02)
        if time.time() - last > 300:
            last = time.time()
            print("%s alive, events=%d" % (time.strftime("%H:%M:%S"), stats["n"]), flush=True)
    print("结束 %s events=%d" % (time.strftime("%H:%M:%S"), stats["n"]), flush=True)
    for h in hooks:
        try:
            u32.UnhookWinEvent(h)
        except Exception:  # noqa: BLE001,S110 - 收尾清理: 卸钩失败不影响已有结论
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
