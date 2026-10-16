"""全项目统一的时钟 (2026-09-12 审查 P2-18)

**存在的理由**: 同一份运行记录里原本混着三个钟 ——
  · `db.stage_start/set_status`、`notify._log_send` 写 SQLite 的 `datetime('now','localtime')`
    (跟**机器**时区走)
  · `scheduler._run_date()` 用 `ZoneInfo("Asia/Shanghai")`(跟**交易所/东八区**走)
  · `framework._trace` / `build_ctx` 用裸 `datetime.now()` / `date.today()`(naive, 也跟机器走)
本机上三者恰好都是东八区, 所以**今天没有任何可见影响**; 但换一台非东八区的机器部署,
`run_date` 与 `started_at` 会跨日错位 —— 而"最近 N 天失败重跑"的窗口判定正是读
`started_at`(见 scheduler._stage_failed), 交错一天就意味着该补的没补、或重复整链重跑。

取舍: A 股的所有"今天/昨天"语义都以东八区为准(交易所在上海), 机器时区是部署细节。
故统一到**东八区**, 且返回 **naive** 值(不带 tzinfo)—— 因为入库/比较用的都是
"YYYY-MM-DD HH:MM:SS" 字符串, SQLite 的 datetime() 也是 naive, 带 tzinfo 反而会在
比较时被隐式转换, 引入新的不一致。

用法: 一律 `from app import timeutil` + `timeutil.now()`(模块属性查找),
**不要** `from app.timeutil import now` —— 后者会让测试无法用 mock.patch.object 换钟。
"""
import datetime
import os
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")

# SQLite datetime() 的输出格式, 保持逐字符一致(历史行与比较都按这个格式)
_SQLITE_FMT = "%Y-%m-%d %H:%M:%S"


def now() -> datetime.datetime:
    """当前东八区时间(naive: 墙上时间与本机时区无关)"""
    return datetime.datetime.now(TZ).replace(tzinfo=None)


def today() -> datetime.date:
    return now().date()


def stamp() -> str:
    """入库用时间戳, 与 SQLite `datetime('now','localtime')` 同格式"""
    return now().strftime(_SQLITE_FMT)


def date8() -> str:
    """YYYYMMDD(与 run_date / trade_date 同格式)"""
    return now().strftime("%Y%m%d")


def days_ago(n: int) -> str:
    """N 天前的时间戳(替代 SQL 里的 `datetime('now','localtime','-N days')`)"""
    return (now() - datetime.timedelta(days=n)).strftime(_SQLITE_FMT)


def proc_start_ticks(pid: int):
    """进程创建时间(Windows FILETIME 滴答数)——锁陈旧判定"pid 是否被复用"的唯一判据。

    返回: **滴答数**=正常; **-1**=进程不存在(OpenProcess 错误码 87);
    **None**=无法确定(提权进程会让低权限进程 OpenProcess 被拒; 非 Windows 平台亦如此)。

    ⚠️ 单一实现(2026-09-20 审查 D-06 / P1-2): 原先 `app/filelock.py` 与 `app/scheduler.py`
    **各写一份几乎逐行相同的实现**, 且 filelock 那份多一道 `os.name != "nt"` 平台检查。
    两处若只改一处 ⇒ 对同一个 pid 给出相反的"是否已被复用"结论 ⇒
    **文件锁与调度锁的并发互斥静默失效**(可能双跑管线)。故收敛到本函数。

    调用方请**保留自己的模块级名字**再转调本函数(如
    `def _proc_start_ticks(pid): return timeutil.proc_start_ticks(pid)`),
    不要在本模块之外 `from app.timeutil import proc_start_ticks` —— 与本文档 §用法同一条
    纪律: 直接 from-import 会让测试无法用 `mock.patch.object` 换掉它。
    """
    if os.name != "nt":
        return None                      # 非 Windows 不碰 ctypes.windll(它不存在)
    try:
        import ctypes
        from ctypes import wintypes
        ct = wintypes.FILETIME()
        et = wintypes.FILETIME()
        kt = wintypes.FILETIME()
        ut = wintypes.FILETIME()
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            err = ctypes.windll.kernel32.GetLastError()
            return -1 if err == 87 else None
        try:
            ok = ctypes.windll.kernel32.GetProcessTimes(
                h, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut))
            if not ok:
                return None
            return (ct.dwHighDateTime << 32) | ct.dwLowDateTime
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return None


_STILL_ACTIVE = 259   # Windows 哨兵: 进程尚未退出时 GetExitCodeProcess 返回它


def proc_alive_strict(pid: int):
    """进程是否**仍在运行**(Windows: OpenProcess + GetExitCodeProcess)。

    返回: **True**=仍在运行; **False**=已终止(即使进程对象因句柄未关闭尚未销毁);
    **-1**=进程不存在(OpenProcess 错误码 87); **None**=无法确定(提权进程让
    OpenProcess 被拒; 非 Windows 平台亦如此)。

    为什么 `proc_start_ticks` 的滴答数比对不够: 它只能防"pid 被复用"。进程**已终止**
    但进程对象尚未销毁时(任何一方仍持有句柄 —— 拉起者没 wait 的 Popen、任务计划
    服务、wscript/cmd 包装链), OpenProcess 与 GetProcessTimes 照样成功、滴答数与
    锁里记录一致 ⇒ 滴答比对把死进程当活锁 ⇒ 新实例永远拿不到锁。2026-10-08 09:01
    实锤: 服务实例 08:5x 无痕迹死亡后, 看门狗拉起的新实例撞锁秒退, 误发"自愈失败
    需要人工"。GetExitCodeProcess 对已终止进程返回**真实退出码**(≠ STILL_ACTIVE)
    —— 这是唯一不依赖"对象已销毁"的可靠补判。

    调用方请**保留自己的模块级名字**再转调(与 `proc_start_ticks` 同一条纪律):
    `def _proc_alive_strict(pid): return timeutil.proc_alive_strict(pid)`。
    """
    if os.name != "nt":
        return None                      # 非 Windows 不碰 ctypes.windll(它不存在)
    try:
        import ctypes
        from ctypes import wintypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            err = ctypes.windll.kernel32.GetLastError()
            return -1 if err == 87 else None
        try:
            code = wintypes.DWORD()
            if not ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                return None
            return code.value == _STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return None
