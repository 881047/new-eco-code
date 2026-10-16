"""定时任务: 每日盘后更新 + 月度全流程 + 开机/错过自动补跑"""
import datetime
import logging
import os
import random
import threading
import time
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo

# 2026-09-06 第十轮 N10-9: cron 用 Asia/Shanghai, 日期判定统一同一时区 ——
# 服务器 naive 本地时间在本机(东八区)与 cron 一致, 显式 helper 消除换时区部署时的错位
_TZ = ZoneInfo("Asia/Shanghai")

from apscheduler.schedulers.background import BackgroundScheduler

from app import config as cfg_mod
from app import db
from app import timeutil
from app.accounts_mail import run_accounts_mail_slot as accounts_mail_slot
from app.config import DATA_DIR

log = logging.getLogger(__name__)

_scheduler = None
_lock = threading.Lock()
_LOCK_FILE = DATA_DIR / ".task.lock"

#: 运维告警去重（2026-09-14 外部审查 P0-1）：`{key: 日期}`，同一档同一天只发一封。
#: 为什么要去重：保鲜档一晚上有 3 次触发、退避重排还会再打几次 —— 不去重会把人淹掉，
#: 然后"告警"就变成了噪声（那比不告警更糟）。
#:
#: ⚠️ **不要给这里加 `with _lock:`**（2026-09-15 外部审查 P1-1 的建议，已复核并否决）：
#: 本函数的两个调用点**都在 `_lock` 持有期间** —— `_guarded` 的 `except` 分支（见 256 行）
#: 与 `_retryable._on_busy`（抢锁失败回调，见 379 行）。`_lock` 是**非重入**的
#: `threading.Lock()`，同一线程二次获取会**永久死锁**（整个调度器停摆），比它想修的
#: "并发重复告警"严重得多。get→set 天然串行，故当前实现不需要额外加锁。
#: （唯一遗留：字典跨天不清理，每个 key 只占一行，量级可忽略。）
_ALERTED: dict = {}


def _ops_alert_once(key: str, title: str, body: str) -> bool:
    """发一条运维告警（同 key 同一天只发一次）。

    **为什么做在调度器里**：外部审查 P0-1 实测 —— 全项目 `send_ops_alert` 只有 2 个调用点
    （备份校验失败、周频成交核对异常），而 `_guarded` 抓到的日链/月链异常**只写日志**。
    后果：无人值守时"整月没出方案""整周零成交"可以完全静默地发生 —— 而这正是项目自己在
    周频侧专门做看门狗要防的事故类型。失败必须能自己找到人。

    发不出去**绝不影响主流程**（`send_ops_alert` 自己吞异常；这里再兜一层）。
    """
    day = _run_date()
    if _ALERTED.get(key) == day:
        log.info("运维告警 %s 今日已发过, 跳过重复告警", key)
        return False
    try:
        from app.notify import send_ops_alert
        ok = bool(send_ops_alert(title, body))
    except Exception:
        # 2026-09-16 调度审查 SCH-02: 原先**先写标记再发送** —— SMTP 抖动导致发送失败时
        # 标记不回滚, 当天该档后续失败全部被去重键吞掉(与看门狗"失败不写时间戳下轮再试"
        # 的方向相反)。改为**只在发送成功后写标记**; 失败/未配置时下个失败档位重试
        # (未配置路径在 send_ops_alert 里早退, 成本可忽略)。
        log.exception("运维告警发送失败(不影响主流程): %s", title)
        return False
    if ok:
        _ALERTED[key] = day
    else:
        log.warning("运维告警未发出(未配置/SMTP 失败), 不写去重标记 ⇒ 下个失败档位重试: %s", title)
    return ok


def _proc_start_ticks(pid: int):
    """进程创建时间(Windows FILETIME 滴答数)
    返回: 滴答数=正常; -1=进程不存在(OpenProcess 错误码 87); None=无法确定
    (2026-09-05 审查修复: 提权进程会让低权限进程访问被拒, 原 None 一律按"陈旧"处理
    会把活锁误判可偷)

    2026-09-20 审查 D-06 / P1-2: 实现已收敛到 `timeutil.proc_start_ticks`（**单一实现**）——
    原 filelock.py 另有一份几乎逐行相同的副本（且那份多一道非 Windows 平台检查）。
    此处**保留模块级名字**再转调，既消重又不破坏
    `tests/test_scheduler_lock.py` 里 9 处 `mock.patch.object(scheduler, "_proc_start_ticks")`。"""
    return timeutil.proc_start_ticks(pid)


def _proc_alive_strict(pid: int):
    """转调 `timeutil.proc_alive_strict` —— 与 `_proc_start_ticks` 同一条纪律（单一实现 +
    模块级名字可 mock）。2026-10-08: `_lock_stale` 用它识破"持有者已终止但进程对象
    未销毁"的假活锁（当日看门狗拉起撞锁误报的直接原因，详见该函数 docstring）。"""
    return timeutil.proc_alive_strict(pid)


def _lock_stale() -> bool:
    """锁文件是否陈旧(2026-09-04 修复): 空/乱码、持有者进程已退出、或 pid 已被复用 → True。
    锁文件内容格式: pid\\n创建时间滴答数; 旧版仅 pid 的文件无法核对复用, 进程存活即视为有效。
    2026-09-05: 无法核对(ticks is None, 如提权进程)按有效处理 —— 宁可锁滞留也不偷活锁。"""
    try:
        text = _LOCK_FILE.read_text(encoding="utf-8").strip()
        pid_s, _, tick_s = text.partition("\n")
        pid = int(pid_s)
        if pid <= 0:
            return True
        # 2026-10-08 与 filelock._stale 同步: 先问退出码 —— 持有者已终止但进程对象
        # 未销毁(句柄未关)时滴答比对会把死锁当活锁; -1/None 交回原逻辑。
        if _proc_alive_strict(pid) is False:
            return True
        ticks = _proc_start_ticks(pid)
        if ticks == -1:
            return True  # 进程不存在(被强杀/正常退出未清理)
        if ticks is None:
            return False  # 无法核对: 保守视为有效(审查 TOCTOU 修复)
        if tick_s.strip():
            try:
                if int(tick_s) != ticks:
                    return True  # pid 已被其他进程复用(创建时间不同)
            except ValueError:
                return True  # 内容损坏
        return False
    except Exception:
        return True  # 空文件(os.open 与 os.write 之间被杀)/乱码 → 陈旧


def _lock_token() -> str:
    tick = _proc_start_ticks(os.getpid())
    return f"{os.getpid()}\n{tick if tick is not None else ''}"


def _lock_content():
    """锁文件当前内容(strip 后)；读不到/不在 → None。"""
    try:
        return _LOCK_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _lock_backoff(attempt: int) -> float:
    """见 `app/filelock.py::_backoff`（同一条 A4 加固，两份实现刻意同构）。"""
    return 0.05 * (attempt + 1) + random.uniform(0.0, 0.05)


def _acquire_file_lock() -> bool:
    """跨进程互斥锁: 防多实例(服务+脚本+网页触发)并发执行同一管线写乱账。

    2026-09-05 审查修复 TOCTOU: 原"unlink 陈旧锁→递归重建"之间可被另一进程插入
    (双方都偷到锁并发跑管线)。改为 os.replace 原子搬走陈旧锁(同目录 rename 原子,
    失败说明已被他人抢先 → 重新循环), 最多重试 5 次。

    2026-09-12 审查 P2-15: `os.open(O_CREAT|O_EXCL)` 只保证**创建**原子, 内容随后才写 ——
    在 open 与 write 之间锁文件是**空的**, 而 `_lock_stale` 恰好把空文件判为陈旧
    (见其 except 分支), 于是另一进程可以合法地把它当死锁搬走 ⇒ **双方同时持锁**,
    正是这把锁要防的场景。改为「先写自己的临时文件, 再 `os.link` 到锁文件」:
    硬链接是原子的, 且链接成功时内容**已经完整**。

    2026-09-19 审查 A4: 与 `app/filelock.py::FileLock.acquire` 同步加固(搬走前复读比对、
    搬后退避、link 后回读认领) —— 原实现在两个进程同时判陈旧时, 慢的那个会把快的那个
    **刚建好的活锁**用 `os.replace` 搬走换成自己的 ⇒ 双持有者, 18:00 链与手动触发能同时
    写账本。改动的道理和残余窗口都写在那边的 docstring 里, 不在此重复。
    """
    tmp = Path(str(_LOCK_FILE) + f".tmp.{os.getpid()}")
    token = _lock_token().strip()
    for attempt in range(5):
        try:
            tmp.write_text(_lock_token(), encoding="utf-8")
        except OSError:
            log.exception("写锁临时文件失败: %s", tmp)
            return False
        try:
            os.link(tmp, _LOCK_FILE)          # 原子; 已存在 → FileExistsError
        except FileExistsError:
            _unlink_quiet(tmp)
            seen = _lock_content()
            if seen is None:
                continue                      # 刚好被搬走: 重试
            if not _lock_stale():
                return False
            if _lock_content() != seen:
                time.sleep(_lock_backoff(attempt))   # 已被他人接管, 别搬对方的活锁
                continue
            try:
                os.replace(_LOCK_FILE,
                           Path(str(_LOCK_FILE) + f".stale.{os.getpid()}.{int(time.time() * 1000)}"))
            except OSError:
                continue  # 已被他人搬走/重建: 重新循环
            time.sleep(_lock_backoff(attempt))
        except OSError:
            # 文件系统不支持硬链接(如 exFAT/网络盘) → 退回原 O_CREAT|O_EXCL 写法。
            # 有那个空文件窗口, 但好过完全拿不到锁; 打印一次以便发现。
            _unlink_quiet(tmp)
            log.warning("硬链接建锁失败, 退回 O_CREAT|O_EXCL(存在空窗口竞态): %s", _LOCK_FILE)
            return _acquire_file_lock_legacy()
        else:
            _unlink_quiet(tmp)
            if _lock_content() != token:
                # 我们的锁在 link 之后被人搬走并换上了他的 —— 本进程不是持有者, 退避重来
                log.warning("调度锁 link 后回读不是自己的 token, 判定接管失败并退避: %s",
                            _LOCK_FILE)
                time.sleep(_lock_backoff(attempt))
                continue
            _cleanup_stale_locks()
            return True
    return False


def _unlink_quiet(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def _acquire_file_lock_legacy() -> bool:
    """兜底获取(无硬链接的文件系统): O_CREAT|O_EXCL 后立刻写内容"""
    for _ in range(5):
        try:
            fd = os.open(_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, _lock_token().encode())
            finally:
                os.close(fd)
            _cleanup_stale_locks()
            return True
        except FileExistsError:
            if not _lock_stale():
                return False
            try:
                os.replace(_LOCK_FILE,
                           Path(str(_LOCK_FILE) + f".stale.{os.getpid()}.{int(time.time() * 1000)}"))
            except OSError:
                continue
    return False


def _cleanup_stale_locks() -> None:
    """尽力清理遗留的 .stale.* 中间文件(不影响其他进程: 搬走者不再依赖该文件)

    顺带清 `.tmp.*`, 但**只清 1 小时前的**: 新的 tmp 可能正被某个进程握在手里准备
    `os.link`(写与链接之间), 删掉它会逼对方走兜底路径 —— 那就等于人为制造竞态。
    """
    try:
        now = time.time()
        for p in _LOCK_FILE.parent.glob(_LOCK_FILE.name + ".stale.*"):
            _unlink_quiet(p)
        for p in _LOCK_FILE.parent.glob(_LOCK_FILE.name + ".tmp.*"):
            try:
                if now - p.stat().st_mtime > 3600:
                    _unlink_quiet(p)
            except OSError:
                pass
    except OSError:
        pass


def _release_file_lock() -> None:
    """仅当锁文件内容仍是本进程令牌时才删除(2026-09-05 审查修复: 原无条件 unlink
    会把别的进程刚重建的新锁删掉, 使互斥失效)"""
    try:
        if _LOCK_FILE.read_text(encoding="utf-8").strip() == _lock_token():
            _LOCK_FILE.unlink()
    except (OSError, ValueError):
        pass


def _guarded(fn, name, on_busy=None):
    """全局任务锁(进程内线程锁 + 跨进程文件锁): 定时/补跑/网页手动触发共用 (P2-1), 返回是否实际执行

    2026-09-04 修复: 文件锁获取放入 try/finally, 杀软/权限等异常不再泄漏进程内线程锁
    (原先异常在 try 前逸出, _lock 永不释放 → 本进程所有后续任务静默停摆到重启);
    且只有真正获取到文件锁才在 finally 释放, 避免误删其他进程的锁文件。

    on_busy(reason): 2026-09-12 审查 P1-06 新增 —— 抢锁失败时回调, 让调用方能区分
    「没跑成」与「跑完了」并自行补救(月链用它做退避重排)。默认 None = 行为不变。
    返回值仍是 bool(手动触发接口按它回报前端): 这里刻意不改成状态字符串。
    """
    def _busy(reason: str):
        if on_busy is None:
            return
        try:
            on_busy(reason)
        except Exception:
            log.exception("任务 %s 的 on_busy 回调失败", name)

    if not _lock.acquire(blocking=False):
        log.info("任务 %s 已在运行, 跳过本次触发", name)
        _busy("本进程已有任务在运行")
        return False
    acquired = False
    try:
        if not _acquire_file_lock():
            log.info("任务 %s 已被其他进程执行, 跳过本次触发", name)
            _busy("其他进程正在执行")
            return False
        acquired = True
        fn()
        return True
    except Exception as exc:
        log.exception("任务 %s 执行失败", name)
        # 2026-09-14 外部审查 P0-1: 失败**必须能自己找到人**（原来只有 log + pipeline_run，
        # 无人值守时整月没方案也没人知道）。同一天同一档只发一封。
        tail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        _ops_alert_once(
            "fail:" + name,
            "任务失败: %s" % name,
            "定时档「%s」执行失败（run_date=%s，本进程 pid=%s）。\n%s\n\n"
            "台账见 pipeline_run.status='error'（stage 由各档自己写）。\n"
            "排查顺序见知识库 03 号运行手册；本档若在「退避重排」名单里，稍后会自动重试。"
            % (name, _run_date(), os.getpid(), tail))
        return False
    finally:
        if acquired:
            _release_file_lock()
        _lock.release()


def _run_date() -> str:
    """2026-09-12 审查 P2-18: 改走 app.timeutil —— `run_date` 与 `pipeline_run.started_at`
    (db.stage_start 也走同一个钟)从此**同源**, 不再一个是东八区、一个是机器本地时区。"""
    return timeutil.date8()


# 2026-09-12 审查 P1-06: 月链抢锁失败后的退避重排间隔(分钟)
_MONTHLY_RETRY_MIN = (30, 60, 120)


def _record_skip(stage: str, reason: str) -> None:
    """抢锁失败 = 这次触发没跑成, 但原实现只在日志里留一句 log.info —— APScheduler
    认为任务已执行, 不会重试, pipeline_run 里连一行都没有, 「本月为什么没出方案」
    事后无据可查。补一行台账。

    status='skipped' 对既有逻辑是惰性的: _stage_watermark_ok 严格要求 'ok'(不会误判
    为已完成), _stage_failed 只认 'error'(不会被当成失败而反复重跑整条链)。
    """
    try:
        # 2026-09-13 独立审查 §3.2: 原来 get_conn() 无 try/finally —— 中途抛异常就漏一个连接
        # (长驻服务里累积句柄; 这里恰好在锁竞争路径上)。改走 conn_ctx()。
        with db.conn_ctx() as conn:
            sid = db.stage_start(conn, stage, _run_date())
            db.stage_finish(conn, sid, "skipped", error=reason)
    except Exception:
        log.exception("记录跳过状态失败")


def _weekly_staged(slot_fn, stage: str):
    """周频档的**带台账执行器**（2026-09-14，回应外部复核 D2/D2b/D3）。

    三个必须在同一处解决的点：

    1. **结果分类**：周频档按设计**不抛异常**（返回 `{"error": ...}` / `{"skipped": ...}` /
       正常 dict）⇒ 直接套 `_stage` 会把 `{"error": …}` 也记成 `ok`，台账"报喜不报忧"
       （外部复核实测：`weekly_sleeve` 返回出方案失败、行里却是 ok）。这里按返回值分档。
    2. **停用不写台账**：`weekly.enabled=false` 时直接调原函数（它在读日历前返回）——
       保持"sleeve 停用零副作用"的契约（挂 `_stage` 后停用也每天白写 12 行 `pipeline_run`）。
    3. **抢锁失败留痕**：交给 `_guarded(..., on_busy=…)`（见 `_weekly_job`），不再只有一行 log。
    """
    def _classify(r):
        if isinstance(r, dict):
            if r.get("error"):
                return "error", str(r["error"])
            if r.get("skipped"):
                return "skipped", str(r["skipped"])
        return "ok", None

    def _run():
        from app import config as cfg_mod
        if not (cfg_mod.load_config().get("weekly") or {}).get("enabled", False):
            return slot_fn()
        with db.conn_ctx() as conn:
            sid = db.stage_start(conn, stage, _run_date())
        try:
            r = slot_fn()
        except Exception as e:                     # 理论上周频档不抛；真抛就如实记 error 再上抛
            with db.conn_ctx() as conn:
                db.stage_finish(conn, sid, "error", error=e)
            raise
        status, err = _classify(r)
        with db.conn_ctx() as conn:
            db.stage_finish(conn, sid, status, error=err)
        # 2026-09-16 调度审查 SCH-03: 周频档按设计不抛、返回 {"error": …} ⇒ _guarded 的
        # "抛异常才告警"接不到它 —— error 只进台账、无邮件(非执行日的错误全天无人知晓,
        # doctor 只兜执行日)。分类为 error 时补一条运维告警(同档同日去重)。
        if status == "error":
            _ops_alert_once("fail:" + stage, "周频档 %s 返回错误" % stage,
                            "「%s」本次执行返回 error(已记台账): %s\nrun_date=%s"
                            % (stage, err, _run_date()))
        return r
    # 让"补跑必须复用同一个执行函数"这条既有守卫仍能**按恒等**检查（不执行就能看到内层）
    _run.__wrapped__ = slot_fn
    return _run


def _weekly_job(slot_fn, stage: str, name: str, on_busy=None):
    """注册用的小工厂：`_guarded(_weekly_staged(...), on_busy=记一行 skipped)`。

    `on_busy` 缺省 = 只记一行 skipped；传了就换成调用方的回调（退避重排用，见 `_retryable`）。
    """
    return lambda: _guarded(_weekly_staged(slot_fn, stage), name,
                            on_busy=on_busy or (lambda why: _record_skip(stage, why)))


# ---------------------------------------------------------------------------
# 2026-09-14 外部审查 P0-2: 抢不到锁 **不再直接丢弃** —— 统一退避重排
# ---------------------------------------------------------------------------
# 审查实测：所有调度档共用一把 `_lock`，而退避重排**只有月链**实现了；其余各档抢锁失败
# 就是一行 log.info + 一行 skipped 台账，然后这一天就过去了。后果链条最要命的三处：
#   · `weekly_sleeve`（09:32）= 本周**唯一**建仓档，被别的档挤掉 ⇒ 整周空仓；
#   · `weekly_evening`（18:05）= "开盘档没成交"的**唯一**补成交路径（KB 07 #48）；
#   · `weekly_health`（19:45）= 判据④（成交价 vs 当日开盘 ≤50bps）**唯一**核对点。
# 重排是幂等的：每个槽内部都有自己的"今天已做过"守卫（`_already_done` / 水位 / 空跑秒退）。
_RETRY_MIN = {
    "weekly_sleeve": (3, 6, 12),      # 09:32 建仓, 重排仍在盘中(今开价全天固定)
    "weekly_evening": (10, 20, 40),   # 18:05 唯一补成交路径(日链 18:00 常跑 15+ 分钟)
    "daily_summary": (10, 20),        # 18:30 汇总邮件(当日有效)
    "weekly_health": (15, 30),        # 19:45 健康/判据④核对
    # 2026-09-19 审查 A2: P0-2 那轮把"后果最要命的三处"配了重排, 但**日链自身**与
    # **唯一的周备份档**仍是裸 `_guarded(on_busy=None)` —— 抢锁失败既不进 pipeline_run
    # 也不重排, 事后只有一行 log。日链当晚重排(数据/因子/推分都要当天到位);
    # 备份在周日凌晨, 无盘中约束, 间隔放宽到小时级(一周只有这一次机会)。
    "daily": (10, 20, 40),            # 18:00 记账链(全市场同步→因子→推分→监控→模拟盘)
    "backup": (60, 240),              # 周日 09:15 唯一周备份
    # 2026-09-28 审查修复: daily_brief(08:30) 是 09-18 加入的 _retryable 档, 晚于 P0-2
    # 改造早于 09-19 重排表 ⇒ 漏配此键时 attempt>=len(()) 立即告警"今日放弃"不重排 ——
    # 晨间待办被日链长尾挤掉一次就永久缺失。宽限 2h 内两次重排足够(当天有效)。
    "daily_brief": (10, 20),          # 08:30 今日待办邮件
    "picks_email": (10, 20),          # 08:35 三模型选股交集早报(2026-10-08)
}


def _retryable(slot_fn, stage: str, name: str, attempt: int = 0):
    """注册用的工厂：抢锁失败 ⇒ 记一行 `skipped` + N 分钟后重排；用尽则**告警**。

    与月链 `_monthly_slot` 同一套语义（那里是 30/60/120 分钟，因为一个月只有一次机会）。
    这里的间隔按各档的**有效窗口**定：09:32 那档必须在盘中（今开价才有效），
    18:05 那档要避开 18:00 日链的尾巴，19:45 那档要赶在当晚。
    """
    mins = _RETRY_MIN.get(stage, ())

    def _on_busy(reason: str):
        _record_skip(stage, reason)
        if attempt >= len(mins):
            log.error("%s 连续 %d 次未抢到锁(最近一次: %s) —— 本档今日不再重排",
                      name, attempt + 1, reason)
            _ops_alert_once(
                "missed:" + stage,
                "定时档被挤掉: %s" % name,
                "「%s」连续 %d 次抢不到执行锁（最近一次原因: %s，run_date=%s）。\n"
                "该档今日已放弃自动重排 —— 请网页手动触发，或重启服务走开机补跑。\n"
                "（台账里会有 %d 行 stage='%s' status='skipped'）"
                % (name, attempt + 1, reason, _run_date(), attempt + 1, stage))
            return
        sched = _scheduler
        if sched is None:
            log.error("%s 抢锁失败(%s)且调度器不可用, 无法重排", name, reason)
            return
        delay = mins[attempt]
        run_at = datetime.datetime.now(_TZ) + datetime.timedelta(minutes=delay)
        try:
            sched.add_job(_retryable(slot_fn, stage, name, attempt + 1), "date",
                          run_date=run_at, id="%s_retry_%d" % (stage, attempt),
                          replace_existing=True, misfire_grace_time=2 * 3600)
            log.warning("%s 抢锁失败(%s), %d 分钟后重排(第 %d 次)", name, reason, delay, attempt + 1)
        except Exception:
            # 2026-09-16 调度审查 SCH-11: 重排这条"补救路径"自身失败也必须告警
            # (服务关闭窗口期 add_job 抛错 ⇒ 该档今日既没跑成也没排上, 仅日志=静默)。
            log.exception("%s 重排失败", name)
            _ops_alert_once("reschedule_failed:" + stage,
                            "定时档 %s 重排失败" % name,
                            "「%s」抢锁失败后重排也失败(服务正在关闭?): %s\n"
                            "该档今日既未跑成也未排上 —— 请网页手动触发或重启走开机补跑。"
                            % (name, reason))

    return _weekly_job(slot_fn, stage, "%s%s" % (name, "(重排%d)" % attempt if attempt else ""),
                       on_busy=_on_busy)


def _monthly_slot(attempt: int = 0):
    """月链定时槽(P1-06): 抢不到锁不再静默丢弃 —— 记账 + 退避重排。

    原始缺陷: monthly cron 定在每月 1 日 18:33, 只比日链(18:00)晚 33 分钟, 而日链
    (全市场同步+因子+推分+监控+模拟盘)轻易超过 33 分钟 ⇒ _guarded 抢锁失败只
    log.info 就返回, APScheduler 标记已执行、不重试, 下一次触发要等到**下月 1 日**:
    当月方案/训练/回测全缺, 只有重启走 _catch_up 才能救。
    重排是幂等的: 月链内部按阶段水位续跑(_stage_watermark_ok), 补跑不会白算。
    """
    def _on_busy(reason: str):
        _record_skip("monthly", reason)
        if attempt >= len(_MONTHLY_RETRY_MIN):
            # 2026-09-16 调度审查 SCH-01: 放弃分支原来只有 log ⇒ 违反"失败必告警"。
            # 月链当月整条放弃(方案/训练/回测全缺)必须自己找到人 —— 对齐 _retryable。
            log.error("月链连续 %d 次未抢到锁(最近一次: %s), 本月不再重排 —— "
                      "请网页手动触发月度全流程, 或重启服务走开机补跑",
                      attempt + 1, reason)
            _ops_alert_once(
                "missed:monthly",
                "月度全流程连续抢锁失败, 本月已放弃重排",
                "月链 18:33 及全部重排窗口连续 %d 次抢不到执行锁(最近一次: %s, run_date=%s)。\n"
                "当月方案/训练/回测整条未跑 —— 请网页手动触发月度全流程, 或重启服务走开机补跑。\n"
                "(台账里会有 stage='monthly' status='skipped' 的记录)" % (attempt + 1, reason, _run_date()))
            return
        sched = _scheduler
        if sched is None:
            log.error("月链抢锁失败(%s)且调度器不可用, 无法重排", reason)
            return
        delay = _MONTHLY_RETRY_MIN[attempt]
        run_at = datetime.datetime.now(_TZ) + datetime.timedelta(minutes=delay)
        try:
            sched.add_job(lambda: _monthly_slot(attempt + 1), "date", run_date=run_at,
                          id="monthly_retry_%d" % attempt, replace_existing=True,
                          misfire_grace_time=2 * 3600)
            log.warning("月链抢锁失败(%s), %d 分钟后重排(第 %d 次)", reason, delay, attempt + 1)
        except Exception:
            # SCH-11: 月链重排自身失败也必须告警
            log.exception("月链重排失败")
            _ops_alert_once("reschedule_failed:monthly",
                            "月度全流程重排失败",
                            "月链抢锁失败后重排也失败: %s\n当月链今日既未跑成也未排上 —— "
                            "请网页手动触发月度全流程。" % reason)

    return _guarded(lambda: monthly_pipeline(end_date=_safe_end()),
                    "定时-月度全流程", on_busy=_on_busy)


def _stage(fn, stage: str):
    """阶段状态包装(2026-09-04 审查 P1-7): 半成品不再被下游当真, 失败阶段可被补跑识别重试"""
    # 2026-09-13: 三处连接都改 conn_ctx() —— 中间跑的是 fn()(可能几分钟), 绝不能跨它持连接;
    # 但每一段自身必须保证关闭(原来任一 stage_finish 抛错都会漏连接)。
    # 2026-09-19 审查: 原来只有 try/except 没有 finally ⇒ **强杀/断电**(走不到 except)会把行
    # 永久留在 status='running'。两个读者都会被带偏: `_stage_watermark_ok` 只看"最新一行是不是
    # ok" ⇒ 卡在 running 的 factors/train 每次开机都整段重算(1197 万行); 而 `_stage_failed` 只认
    # 'error' ⇒ 卡住的阶段**既不告警也不补跑**。补 finally: 没走到收口的行按 'aborted' 落定。
    # ⚠️ 'aborted' 是新状态, 谁按 status 分类都必须把它当"没跑完", 不能当 ok 也不能当 error 丢。
    with db.conn_ctx() as conn:
        sid = db.stage_start(conn, stage, _run_date())
    settled = False
    try:
        r = fn()
        with db.conn_ctx() as conn:
            db.stage_finish(conn, sid, "ok", rows=r if isinstance(r, int) else None)
        settled = True
        return r
    except Exception as e:
        with db.conn_ctx() as conn:
            db.stage_finish(conn, sid, "error", error=e)
        settled = True
        raise
    finally:
        if not settled:          # KeyboardInterrupt / SystemExit / 上面那次 finish 自身抛错
            try:
                with db.conn_ctx() as conn:
                    db.stage_finish(conn, sid, "aborted",
                                    error="阶段未正常收口(强杀/关机/中断/收口写入失败)")
            except Exception:
                log.exception("把 %s (id=%s) 收口成 aborted 也失败了 —— 这行会留在 running", stage, sid)


def daily_update(end_date=None):
    # 2026-09-05 审查修复: 工作日节假日(元旦/春节/国庆等)照常触发 cron 但交易日历不开市
    # —— 整条日链应跳过: 原会重复发同批信号邮件、重建挂单、向休市日发请求污染计数
    # 2026-09-08 审查 P1: 守卫原以"今天"是否交易日为准 —— 周末/节假日开机补跑被跳过
    # (周五缺口拖到周一、挂单按错日成交)。改为按"目标截止日"(end_date)判定: 补跑
    # 目标日是交易日即放行, 今天休市本身不是跳过理由
    if end_date is None:
        end_date = _safe_end()
    with db.conn_ctx() as conn:
        is_open = conn.execute("SELECT is_open FROM trade_cal WHERE cal_date=?",
                               (end_date,)).fetchone()
    if not is_open or not is_open[0]:
        log.info("目标日 %s 非交易日, 跳过日频链(数据/推分/监控/模拟盘)", end_date)
        return
    from app.datasource.updater import DataUpdater
    from app.models.regime import compute_regime
    u = DataUpdater()
    _stage(lambda: u.run_daily(full=True, end_date=end_date), "daily_sync")
    # 池子指数(2026-09-10): 等权全A 风格基准, 也是 regime 风格防守的输入 —— 必须在
    # compute_regime 之前重算。全量重算(一次 GROUP BY)自愈, 失败不阻断链
    try:
        from app.models.pool_index import build_pool_index
        _stage(build_pool_index, "pool_index")
    except Exception:
        log.exception("池子指数重算失败(不影响数据更新与策略链)")
    # 2026-09-05 审查修复: compute_regime 无保护调用 tushare, 限流耗尽 RuntimeError 会
    # 通过 _stage re-raise 炸断整条日链(推分/监控/模拟盘全跳过) —— 单独兜底
    try:
        _stage(compute_regime, "regime")
    except Exception:
        log.exception("市场状态计算失败(不影响数据更新与策略链)")
    # 动态监控链路: 日频因子 → 每日推分 → 持仓体检 → 有动作才发邮件
    try:
        from app.factors.daily_engine import compute_daily
        from app.models.daily_score import score_today
        from app.portfolio.monitor import run_monitor
        from app.notify import send_signal_email
        _stage(compute_daily, "daily_factor")
        _stage(score_today, "daily_score")
        sig = _stage(run_monitor, "monitor")
        if sig.get("has_action"):
            send_signal_email(sig)
    except Exception:
        log.exception("动态监控链路失败(不影响数据更新)")
    # 模拟盘: 执行挂单 → 应用方案/信号 → 记净值。成交邮件只在开盘档即时发送
    # (2026-09-07 用户拍板: 晚间链不发成交邮件, 晚间成交并入 18:30 日终汇总)
    try:
        from app.paper.engine import run_daily as paper_daily
        _stage(paper_daily, "paper")
    except Exception:
        log.exception("模拟盘链路失败(不影响数据更新)")


def trigger_daily():
    """网页手动触发入口, 与定时/补跑共用全局锁 (P2-1)"""
    return _guarded(lambda: daily_update(end_date=_safe_end()), "手动-每日更新")


def trigger_monthly():
    """网页手动触发入口, 与定时/补跑共用全局锁 (P2-1)"""
    return _guarded(lambda: monthly_pipeline(end_date=_safe_end()), "手动-月度全流程")


def _safe_end() -> str:
    """当前时刻安全的行情截止日: 17点后含今天(已收盘), 盘中/盘前只到上一收盘日。
    防 misfire/手动触发落在盘中窗口把不完整日线永久入库(审查发现)。"""
    today = datetime.datetime.now(_TZ).strftime("%Y%m%d")
    if datetime.datetime.now(_TZ).hour >= 17:
        return today
    with db.conn_ctx() as conn:                     # 2026-09-13: 连接卫生（独立审查 §3.2）
        end = conn.execute(
            "SELECT MAX(cal_date) FROM trade_cal WHERE is_open=1 AND cal_date<?",
            (today,)).fetchone()[0]
    return end or today


def open_exec_slot() -> dict:
    """开盘档(2026-09-07): 集合竞价定盘后(09:31)执行昨日盘后挂单, 盘中即可看到真实持仓。
    自门控: config paper.open_exec / 今日交易日 / 09:31-15:00 窗口 / 挂单存在 / 实时行情可得,
    任一不满足即静默跳过 —— 晚间链 18:00 用日线开盘价兜底, 成交价口径一致。
    """
    conn = db.get_conn()
    try:
        enabled = bool(cfg_mod.load_config().get("paper", {}).get("open_exec", False))
        # 2026-09-09 交易总闸(与 paper.engine.trading_paused 同源): 暂停期开盘档直接静默跳过
        paused = bool(cfg_mod.load_config().get("paper", {}).get("paused", False))
        today = datetime.datetime.now(_TZ).strftime("%Y%m%d")
        is_open = conn.execute("SELECT is_open FROM trade_cal WHERE cal_date=?",
                               (today,)).fetchone()
        n_pending = conn.execute(
            "SELECT COUNT(*) FROM paper_order WHERE status='pending'").fetchone()[0]
    finally:
        conn.close()
    if not enabled:
        return {"enabled": False}
    if paused:
        log.info("模拟盘已暂停(paper.paused=true), 开盘档跳过")
        return {"paused": True, "trade_date": today, "trades": 0}
    if not is_open or not is_open[0] or not n_pending:
        return {"trade_date": today, "trades": 0}
    now = datetime.datetime.now(_TZ)
    mins = now.hour * 60 + now.minute
    if not (9 * 60 + 31 <= mins <= 15 * 60):
        log.info("开盘档: %02d:%02d 不在 09:31-15:00 窗口, 跳过(晚间链兜底)", now.hour, now.minute)
        return {"trade_date": today, "trades": 0}
    try:
        from app.paper.engine import execute_open
        from app.notify import send_paper_email
        trades = _stage(lambda: execute_open(today), "open_exec")
        if trades:
            send_paper_email({"trade_date": today, "trades": trades})
        return {"trade_date": today, "trades": len(trades) if trades else 0}
    except Exception:
        log.exception("开盘档执行失败(晚间链兜底)")
        return {"trade_date": today, "trades": 0}


def daily_summary_slot() -> dict:
    """日终汇总邮件档(2026-09-07): 18:30 发送当天记账信息(净值/现金/市值/持仓/当日成交)。
    自门控: 今日交易日 + 当日净值已记账(18:00 链已跑完); 链未跑完/失败时跳过并告警
    (状态页可查), 不补发旧日汇总 —— 成交邮件已即时发送, 汇总只是当日台账快照。
    """
    conn = db.get_conn()
    try:
        today = datetime.datetime.now(_TZ).strftime("%Y%m%d")
        is_open = conn.execute("SELECT is_open FROM trade_cal WHERE cal_date=?",
                               (today,)).fetchone()
    finally:
        conn.close()
    if not is_open or not is_open[0]:
        return {"trade_date": today, "sent": False}
    try:
        from app.paper.engine import build_daily_summary
        from app.notify import send_daily_summary_email
        s = build_daily_summary(today)
        if not s:
            log.warning("日终汇总: %s 尚未记账(18:00 链未完成/失败), 跳过本次发送", today)
            return {"trade_date": today, "sent": False}
        return {"trade_date": today, "sent": bool(send_daily_summary_email(s))}
    except Exception:
        log.exception("日终汇总邮件发送失败")
        return {"trade_date": today, "sent": False}


def daily_brief_slot() -> dict:
    """开盘前「今日待办」邮件档（2026-09-18 邮件优化新增）：每交易日 08:30。

    为什么放在**开盘前**：这封要回答的是"今天要不要我动手"。挂单 09:31 就撮合了，
    风控停买、数据落后、邮件链路不通这类事收盘后才说就晚了半天。
    自门控：非交易日（周末/长假）直接返回不发信 —— 判据与 18:30 档同源（`trade_cal`）。
    全程只读（`build_daily_brief` 只 SELECT），不碰任何执行档的状态。
    """
    today = timeutil.date8()
    conn = db.get_conn()
    try:
        r = conn.execute("SELECT is_open FROM trade_cal WHERE cal_date=?", (today,)).fetchone()
    finally:
        conn.close()
    if not r or not r[0]:
        return {"trade_date": today, "sent": False, "skipped": "非交易日"}
    try:
        from app.notify import send_daily_brief_email
        return {"trade_date": today, "sent": bool(send_daily_brief_email())}
    except Exception:
        log.exception("今日待办邮件发送失败")
        return {"trade_date": today, "sent": False}


def picks_email_slot() -> dict:
    """开盘前「三模型选股交集」早报档（2026-10-08 用户需求）：每交易日 08:35。

    每日早上发三模型 Top100 交集（用户拍板"选股交集也发邮件/每日早上发"）。
    放 08:35 错开 08:30 今日待办（同属开盘前读数，各自独立去重）。
    自门控：非交易日（周末/长假）直接返回不发信 —— 判据与 daily_brief 同源（trade_cal）。
    数据源 app/model_picks.py 纯只读；发送组装分离（accounts_mail.build_picks_email）。
    """
    today = timeutil.date8()
    conn = db.get_conn()
    try:
        r = conn.execute("SELECT is_open FROM trade_cal WHERE cal_date=?", (today,)).fetchone()
    finally:
        conn.close()
    if not r or not r[0]:
        return {"trade_date": today, "sent": False, "skipped": "非交易日"}
    try:
        from app.accounts_mail import send_picks_email
        return {"trade_date": today, "sent": bool(send_picks_email())}
    except Exception:
        log.exception("三模型选股交集早报发送失败")
        return {"trade_date": today, "sent": False}


def _stage_watermark_ok(conn, stage: str, month_end: str) -> bool:
    """阶段级水位续跑(2026-09-06 第十轮 N10-3): 阶段最近一次为 ok 且对应数据水位已
    覆盖 month_end → 可跳过。factor 看月末截面行数, train 看 model_score 最新评分日"""
    row = conn.execute(
        "SELECT status FROM pipeline_run WHERE id="
        "(SELECT MAX(id) FROM pipeline_run WHERE stage=?)", (stage,)).fetchone()
    if not row or row[0] != "ok" or not month_end:
        return False
    if stage == "factors":
        n = conn.execute("SELECT COUNT(*) FROM factor WHERE trade_date=?",
                         (month_end,)).fetchone()[0]
        return n >= 1000
    if stage == "train":
        mx = conn.execute("SELECT MAX(trade_date) FROM model_score").fetchone()[0]
        return mx is not None and mx >= month_end
    return False


def _factor_ready(conn, month_end: str) -> bool:
    """训练就绪门控(2026-09-04 审查 P1-7): 最新已完结月末截面的因子行数必须达标,
    否则开训只会训出 0 期(9/1 调试实证: 因子没算完就开训, 234 条 valid=0 告警)"""
    if not month_end:
        return False
    n = conn.execute("SELECT COUNT(*) FROM factor WHERE trade_date=?", (month_end,)).fetchone()[0]
    ok = n >= 1000  # 全市场约 5000 只(2013 年约 1800), 月末截面至少千级行数
    if not ok:
        log.error("训练就绪门控失败: %s 因子仅 %d 行(<1000), 跳过训练(因子补齐后重试)", month_end, n)
    return ok


def monthly_pipeline(end_date=None):
    # 2026-09-05 第四轮复查 P3-4: 月链与日链同口径 —— 交易日早退。每月 1 日逢国庆/
    # 春节等假期时空转整条链路并重发无变化方案邮件; 下一交易日由开机补跑/错过补跑触发
    # 2026-09-08 审查 P1: 守卫原以"今天是否交易日"为准 —— 非交易日开机补跑被跳过,
    # 该月整月无方案。改为"最新已完结月末的方案是否已生成"判定(与 _catch_up 的
    # need_monthly 同口径; 补跑目标月的数据已完结, 休市日执行本身安全)
    with db.conn_ctx() as conn:                     # 2026-09-13: 连接卫生
        month_end = _last_month_end(conn)
        if not month_end:
            log.info("交易日历为空或尚无已完结自然月, 跳过月度全流程(先运行 scripts/init_data.py)")
            return
        plan_last = conn.execute(
            "SELECT MAX(plan_date) FROM plan WHERE plan_type='stock'").fetchone()[0]
    if plan_last and plan_last >= month_end:
        log.info("方案已覆盖 %s, 跳过月度全流程", month_end)
        return
    from app.datasource.updater import DataUpdater
    from app.datasource import akshare_client
    from app.factors.factor_engine import compute_all
    from app.models.trainer import run as train_run
    from app.models.trainer import ENSEMBLE_SEEDS, ENSEMBLE_ROUNDS
    from app.models.regime import compute_regime
    from app.backtest.engine import run_backtest
    from app.portfolio.builder import build_plan
    from app.notify import send_plan_email
    u = DataUpdater()
    _stage(lambda: u.run_daily(full=True, end_date=end_date), "monthly_sync")
    _stage(akshare_client.fund_rank_snapshot, "fund_rank")
    # 训练前就绪门控: 因子必须覆盖最新已完结月末 (P1-7)
    # 2026-09-14 外部审查 P2-5: 这三处仍是 `conn = db.get_conn() … conn.close()`, 中途抛异常
    # 会漏一个连接(月链 30~60 分钟, 异常路径上累积句柄并持写锁) —— 同文件其它处早已改
    # `conn_ctx()`(2026-09-13 连接卫生)。这里补齐, 行为不变。
    with db.conn_ctx() as conn:
        month_end = _last_month_end(conn)
        ready = _factor_ready(conn, month_end)
        # 2026-09-06 第十轮 N10-3: 阶段级水位续跑 —— 阶段最新一次 ok 且数据水位已覆盖
        # 月末时跳过昂贵阶段(因子 1197 万行全量重算/116 期重训), 避免任一下游失败时整链
        # 重跑把已完成的昂贵工作白算一遍
        factors_done = _stage_watermark_ok(conn, "factors", month_end)
    if not ready:
        with db.conn_ctx() as conn:
            sid = db.stage_start(conn, "train", _run_date())
            db.stage_finish(conn, sid, "error", error="因子覆盖不足")
        log.error("月度流程中止: 训练就绪门控未通过, 等待因子补齐后补跑")
        return
    if factors_done:
        log.info("因子阶段水位已覆盖 %s 且最近一次成功, 跳过 compute_all", month_end)
    else:
        _stage(compute_all, "factors")
    # 池子指数(2026-09-10): 同日常链 —— regime 风格防守的输入, 先于 compute_regime
    # 2026-09-12 审查 P1-07: 这两行原为裸调用, 而 _stage 失败会 re-raise —— 日链早在
    # 2026-09-05 就用 try/except 包住同型的 compute_regime(限流 RuntimeError 会炸断
    # 整条链), 月链却漏了: 一次 Tushare 限流 ⇒ 训练/回测/方案/模拟盘**整月**静默缺失。
    # 与 210-213 行同口径: 只兜底, 不让它带走整条月链。
    try:
        from app.models.pool_index import build_pool_index
        _stage(build_pool_index, "pool_index")
    except Exception:
        log.exception("池子指数重算失败(不影响训练与选股链)")
    try:
        _stage(compute_regime, "regime")
    except Exception:
        log.exception("市场状态计算失败(不影响训练与选股链)")
    # 生产训练: 4 种子集成 × 固定 50 轮无早停 (2026-09-04 轮数扫描结论, 集成降种子方差)
    with db.conn_ctx() as conn:
        train_done = _stage_watermark_ok(conn, "train", month_end)
    # 训练起点与评估起点**是两个独立旋钮**(2026-09-11 用户拍板, 详见 config.py 同名键注释):
    # data_start → train_start, 收紧训练数据与评分月; eval_start → run_backtest(start=),
    # 只裁回测评估窗口。实测两者效应方向相反(训练截断 -5.22pp, 评估窗 +9.59pp 纯口径),
    # 用户拍板恢复全历史训练并保留评估窗。缺键/空值 = None = 该处不设限。
    _cfg = cfg_mod.load_config()
    _data_start = _cfg.get("data_start") or None
    _eval_start = _cfg.get("eval_start") or None
    # 生产因子剔除（2026-09-27 第 6 次重钉接线; 缺省 [] = 不过滤）
    _excl = _cfg.get("factor_exclusions") or None
    if train_done:
        log.info("训练水位已覆盖 %s 且最近一次成功, 跳过训练", month_end)
    else:
        _stage(lambda: train_run(ensemble_seeds=ENSEMBLE_SEEDS,
                                 num_boost_round=ENSEMBLE_ROUNDS, early_stopping_rounds=0,
                                 train_start=_data_start,
                                 exclude_factors=_excl), "train")
    _stage(lambda: run_backtest(start=_eval_start), "backtest")
    summary = _stage(build_plan, "plan")
    try:
        send_plan_email(summary)
    except Exception:
        log.exception("邮件发送失败(不影响方案生成)")
    # 模拟盘应用新方案
    try:
        from app.paper.engine import run_daily as paper_daily
        _stage(paper_daily, "paper")
    except Exception:
        log.exception("模拟盘应用新方案失败")
    # 框架阶段(2026-09-11, 方案 plans/abstract-sleeping-rainbow.md §3 不变量 #7):
    # Alpha Factory / Pool→Critic→OOS→Bank / Ensemble / Optimizer / Health-Decay。
    # **跟在最后、默认全关、且绝不 re-raise** —— 三条合起来才等于"不碰实盘链路":
    #   · 位置在最后 ⇒ 即使它跑一小时, 方案与挂单也已生成完毕
    #   · 默认全关   ⇒ `run_stages` 在任何 import 之前返回, 执行路径**逐位不变**
    #   · 不 re-raise ⇒ 故意**不用 `_stage`**(那个包装器失败时会 raise, 见 scheduler:179)
    # 上下文也是惰性的(`ctx=build_ctx` 传的是函数) ⇒ 全关时连行情面板都不会加载。
    # 框架各阶段的副作用边界见 app/framework.py 的模块 docstring。
    try:
        from app import framework
        _fwres = framework.run_stages(_cfg, ctx=framework.build_ctx)
        if _fwres["enabled"]:
            log.info("[框架] 阶段 %s 完成, 失败: %s", _fwres["enabled"],
                     sorted(_fwres["failed"]) or "无")
    except Exception:
        log.exception("框架阶段失败(不影响方案与模拟盘)")


# ---------- 补跑判定 ----------
def _last_month_end(conn):
    """最新已完结自然月的月末交易日(2026-09-05 审查修复): 原实现返回当月月末日
    (如 8/31 当天返回 8/31), 与 month_end_dates(today=) 排除当月的口径矛盾 ——
    月末最后交易日因子门控必失败, 每次开机空跑完整月度流程后中止"""
    month_first = datetime.datetime.now(_TZ).strftime("%Y%m") + "01"
    row = conn.execute(
        "SELECT MAX(cal_date) FROM trade_cal WHERE is_open=1 AND cal_date<?",
        (month_first,)).fetchone()
    return row[0] if row and row[0] else None


def _weekly_backup():
    """每周备份: 先 WAL checkpoint(TRUNCATE) 压缩 -wal 文件, 再做在线备份 (2026-09-04 审查 P1-6)"""
    try:
        with db.conn_ctx() as conn:                 # 2026-09-13: 连接卫生(checkpoint 失败会漏连接)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        log.info("WAL checkpoint(TRUNCATE) 完成")
    except Exception:
        log.exception("checkpoint 失败(不影响备份)")
    target = db.backup_db()
    # 2026-09-13: 备份"文件在"≠"能恢复"。周日档顺手做一次只读可读性校验
    # (quick_check, 10.33 GB 实测 21 秒); 校验失败**必须有人知道** —— 否则等真出事那天
    # 才会发现手里那份备份也是坏的。校验不通过不中断本档(账本快照还要照做)。
    try:
        ok, detail = db.verify_backup(target)
        if ok:
            log.info("备份可读性校验: ok (quick_check)")
        else:
            log.error("备份可读性校验**失败**: %s (%s)", target, detail)
            # 2026-09-18 审查 Q-15：原来直调 `send_ops_alert` —— 那条路径**没有去重**，
            # 而告警页脚写着"同类告警 6 小时内只发一条"（6h 去重只在 service_watchdog 里）。
            # 备份档每周日跑一次，连续坏的两周会发两封同标题邮件，运维会以为是重复/漏收。
            # 换成 `_ops_alert_once`（按 key + 当天去重），让页脚那句话对得上实际语义。
            _ops_alert_once("backup_verify",
                            "数据库备份校验失败",
                            f"{target}\n校验结果: {detail}\n"
                            f"备份文件在, 但读不出来 ⇒ 请人工确认, 并重跑 scripts/backup_db.py")
    except Exception:
        log.exception("备份可读性校验异常(不中断备份档)")
    # 2026-09-13: **周频袖套的账本不在数据库里** —— 它是 data/weekly/ledger.json,
    # 而那是袖套唯一的成交记录(丢了无法重建)。周日档顺带留一份按日快照并清理旧份。
    try:
        from app.weekly.ledger import backup_snapshots
        p = backup_snapshots()
        log.info("周频账本快照: %s", p or "(账本还不存在, 跳过)")
    except Exception:
        log.exception("周频账本快照失败(不影响数据库备份)")


def _weekly_sleeve_slot():
    """周频袖套执行档(2026-09-12 新增): 锚点后首个交易日 09:32 出方案 + 成交。

    **默认空操作**: `config.yaml` 的 `weekly.enabled` 缺省 false 时,
    `run_open_slot` 在读日历之前就返回 ⇒ 不产生任何订单/文件写入,
    月频链与现有账本完全不受影响。关闭状态下本档只是每工作日空跑一次。
    """
    from app.weekly.pipeline import run_open_slot
    return run_open_slot()


def _weekly_score_slot():
    """周频**打分保鲜档**(2026-09-12 新增): 每日 19:30 检查打分存档是否覆盖最新锚点。

    没有它, 新锚点出方案时会因"存档未覆盖该锚点"而失败 ⇒ 袖套静默不交易
    (实测过的真实缺口)。已覆盖时只做一次 npz 读取 + 一次日历查询, 代价可忽略;
    未覆盖才重训(5 日标签 LightGBM + 生产月频模型, 约 6 分钟)。
    `weekly.enabled=false` 时直接返回。
    """
    from app.weekly.pipeline import run_score_slot
    return run_score_slot()


def _weekly_evening_slot():
    """周频袖套**晚间兜底档**(2026-09-12 新增, 18:05): 用日线开盘价补齐开盘档未成交的部分。

    与开盘档同一套账/同一方案/同一个成交价口径(当日开盘价), 只是数据来源换成已入库日线
    —— 生产模拟盘同款两档设计。开盘档已成交的持仓差额为 0, 本档不产生重复成交。
    """
    from app.weekly.pipeline import run_evening_slot
    return run_evening_slot()


def _weekly_health_slot():
    """周频袖套**健康监控档**(2026-09-13 新增): 每工作日 **19:45**。

    算三类读数(滚动 IC / 账本换手 / 投入率) → 四档健康度; **非 healthy 时发告警邮件**
    (邮件开关仍受 `weekly.email` 控制)。

    ⚠️ 2026-09-13 措辞修正（独立审查 §3.3）：原文写"只报警, 不自动改任何东西"，与实现不符 ——
    本档**会**调用 `risk.apply`：健康度 `degrading`/`dead` 时**自动停止开新仓**（只卖不买、
    敞口只降不升）。按方案不变量禁止的是**自动改模型/权重/约束**，
    而"停不停买"是 §6.1-4 明确要求的自动动作。两者不是一回事，注释不许混着写。
    解除（2026-09-28 用户拍板"改成自动解除"）也挂在本档：`auto_resume` 开启时读到
    **healthy** 即自动解除停买（`risk.maybe_auto_resume`，滞回：warning 不解除）；
    关闭时维持旧口径 —— 须人工 resume。解除成功/落账失败都发运维告警（同 key 一日一封）。
    `weekly.enabled=false` 时直接返回。
    """
    from app import config as cfg_mod
    if not (cfg_mod.load_config().get("weekly") or {}).get("enabled", False):
        return {"skipped": "weekly.enabled=false"}
    cfg = cfg_mod.load_config()
    from app.weekly import health as wk_health
    res = wk_health.current()
    # §6.1-4 风控自动动作: degrading/dead ⇒ **停止开新仓**(只降不升)。
    # 只报警不动作是原来的行为; 现在报警之后必须有人管 —— 没人看邮件时终端要自己反应。
    # 解除(2026-09-28 改): auto_resume 开启时本档读到 healthy 即自动解除(滞回), 见下。
    try:
        from app.weekly import risk as wk_risk
        from app.weekly.ledger import Ledger
        led = Ledger.load(0.0)
        res["risk"] = wk_risk.apply(led, wk_risk.decide(res, cfg),
                                    timeutil.date8())      # 2026-09-15: 东八区(原裸 date.today)
        # 2026-09-15（外部独立审查 F4）：停买**没落账**必须自己找到人 —— 下面的 except 只
        # 记日志，而"现在停买"是安全动作（内存 active / 磁盘没有 ⇒ 重启即失效）。
        # 同 key 同日只发一封（`_ops_alert_once` 自带去重）。
        if res["risk"].get("saved") is False:
            _ops_alert_once(
                "risk_halt_not_saved",
                "周频风控停买未能落账",
                "健康度已判 %s，但 `Ledger.save()` 被拒绝（账本被其他进程修改或锁被占用）。\n"
                "影响：停买只存在于内存，重启后失效；已有持仓的卖出不受影响。\n"
                "处置：确认没有并行的人工 CLI 在操作账本，然后手动跑一次 "
                "`run_weekly.py health` 或等次日 19:45 档重判。" % res.get("level"))
        # **自动解除**(2026-09-28 用户拍板"改成自动解除"): 停买中且本档健康度回到 **healthy**
        # ⇒ 解除并落账(auto=True 留痕; 滞回: warning 不解除, 宁可多停一档)。与上面 risk_halt
        # 的分工: 那层是"信号还活着吗"的保护性置位, 这层是"确认恢复"的解除 —— 恢复性动作
        # 同样必须让人看见, 成功/落账失败各发一条运维告警(同 key 一日一封)。
        res["auto_resume"] = wk_risk.maybe_auto_resume(led, res, cfg, timeutil.date8())
        _ar = res["auto_resume"]
        if _ar.get("changed") and _ar.get("saved") is False:
            _ops_alert_once(
                "weekly_risk_auto_resume_not_saved",
                "周频袖套停买自动解除**未能落账**",
                "健康度已回 healthy，但 `Ledger.save()` 被拒绝（账本被其他进程修改或锁被占用）。\n"
                "影响：磁盘上仍是停买态，下个执行档继续只卖不买；次日 19:45 档会照常重试。\n"
                "处置：确认没有并行的人工 CLI 在操作账本，然后手动跑一次 "
                "`run_weekly.py resume --reason \"...\"`。")
        elif _ar.get("changed"):
            _ops_alert_once(
                "weekly_risk_auto_resume",
                "周频袖套风控停买已自动解除",
                "健康度回到 healthy，风控停买已自动解除（auto_resume 滞回：触发要 degrading、"
                "解除要 healthy；账本 halt 记录带 auto=True 留痕）。\n"
                "效果：下个执行档恢复开新仓。本条仅告知，无需处置；"
                "如需立即恢复（不等次日档）也可人工 `run_weekly.py resume`。")
        # 回撤治理(2026-09-25, C 层闸)的**翻转告警** —— 执行档只静默停买, 这里负责"让人看见":
        # 谨慎态触发⇄解除翻转时把留痕副本落盘, 触发方向再发一条运维告警(同 key 一日一封)。
        # 与上面 risk_halt 的分工: 那层(信号 IC 退化)触发停买、恢复 healthy 后由上面的
        # auto_resume 自动解除(或人工 resume);
        # 本层(净值回撤)回升过解除带自动恢复。
        prev_cautious = bool((led.state.get("governor") or {}).get("cautious"))
        gov = wk_risk.governor(led, cfg)
        res["governor"] = gov
        if bool(gov.get("cautious")) != prev_cautious:
            if not led.save():
                log.error("回撤治理留痕副本未能落账(账本被其他进程修改或锁被占用)")
            elif gov.get("cautious"):
                _ops_alert_once(
                    "weekly_governor_cautious",
                    "周频袖套回撤治理触发: 停开新仓",
                    "100万周盘净值自峰值回撤已触发治理线。\n"
                    "效果: 周度重建仓停开新仓(退出/止损照旧), 敞口只降不升;\n"
                    "解除: 回撤回升过解除带后自动恢复, 无需人工。\n"
                    "读数: %s" % gov.get("reason"))
    except Exception:
        log.exception("袖套风控动作失败(不影响健康读数)")
    # **执行档看门狗**(2026-09-13): 今天若是执行日而账本里没有当日净值点 ⇒ 执行档没跑/中途失败。
    # 这类事故和"策略退化"完全不同(整周没有仓位), 必须单独告警 —— 无人值守终端最怕静默空转。
    try:
        from app.weekly import doctor as wk_doctor
        _today = timeutil.date8()                          # 同上
        # 2026-09-16 调度审查 SCH-05: 本档 misfire_grace=18h ⇒ 补跑可能发生在**次日早晨**
        # (09:32 窗口之前)。那时 check_exec_ran(今天) 会判"非执行日/尚未到窗口" ⇒ 昨天的
        # 执行档没跑这件事无人告警(18:05 兜底次日会自愈, 但成交价已换且无告警)。
        # ⇒ 09:32 窗口前的运行按**上一应执行日**(跳过周末)评估; 正常 19:45 档行为不变。
        import datetime as _dtmod
        _nowts = _dtmod.datetime.now(_TZ)
        _check_day = _today
        if _nowts.time() < _dtmod.time(9, 32):
            _d = _nowts.date() - _dtmod.timedelta(days=1)
            while _d.weekday() >= 5:
                _d -= _dtmod.timedelta(days=1)
            _check_day = _d.strftime("%Y%m%d")
        wd = wk_doctor.check_exec_ran(cfg, _check_day)
        res["exec_watchdog"] = wd
        if not wd["ok"]:
            log.warning("执行档看门狗告警: %s", wd["detail"])
            from app.weekly.report import send_watchdog_email
            send_watchdog_email(wd)
    except Exception:
        log.exception("执行档看门狗失败(不影响健康读数)")
    # **账本按日快照**（2026-09-14 补）：`data/weekly/ledger.json` **不在** `neweco.db` 里
    # （物理隔离是设计的一部分）⇒ 周日的 `db.backup_db()` 保护不到它，而它是袖套**唯一的成交记录**
    # （丢了无法重建）。原来只有周日备份档顺带快照一次 ⇒ 一周内新写的成交没有副本，
    # 且 `doctor.check_backup` 的"账本快照"那一项在首个周日之前一直是红的（13 号实测）。
    # 放在本档（19:45，日链与两档成交都已完成）⇒ 每个工作日留一份，保留份数由 backup_snapshots 管理。
    try:
        from app.weekly.ledger import backup_snapshots
        _snap = backup_snapshots()
        log.info("账本按日快照: %s", _snap or "(账本还不存在, 跳过)")
    except Exception:
        log.exception("账本按日快照失败(不影响健康档)")
    # **成交核对接线**(2026-09-13): `reconcile`（方案意图 vs 账本实际 vs 当日开盘价）
    # 一直只有 CLI，**没有自动跑** —— 成交那天"想买的没买到 / 成交价离谱 / 现金对不上"
    # 三类问题只能靠人敲命令才看得见。这里接上：只对**结构性**问题告警
    #   · 资金恒等式不成立（现金+市值 ≠ 净值，差 >1 元）⇒ 账务错;
    #   · 成交价**执行残差**均值 >50bps（残差=偏离−该方向假设滑点 10bps）⇒ 撮合口径异常。
    # 缺仓/方案外残留**不告警**：一字板/停牌顺延是设计内的正常现象, 报了就是狼来了。
    try:
        from app.weekly import reconcile as wk_rec
        rec = wk_rec.report()
        if rec.get("available"):
            idt = rec.get("identity") or {}
            pr = rec.get("prices") or {}
            bad = []
            if idt.get("n") and not idt.get("ok"):
                bad.append(f"资金恒等式不成立: 差 {idt.get('gap')} 元")
            # 2026-09-19 审查 B3: 这里原来判 `abs(mean_bps) > 50`, 而买单偏离(≈+10bps)与卖单偏离
            # (≈−10bps)在均值里**互相抵消** ⇒ 混合账本上这条自动化告警永远不会响(实测 425 笔回放簿
            # mean=−0.0)。与放行协议 G2 一起改判残差均值, 阈值 50bps 不动。拿不到 `resid_mean_bps`
            # (核对端是旧版本)时**退回旧字段并 log 一句** —— 宁可用一条钝的判据, 也不要静默不告警。
            resid = pr.get("resid_mean_bps")
            legacy = resid is None
            judged = pr.get("mean_bps") if legacy else resid
            if pr.get("n") and judged is not None:
                if legacy:
                    log.warning("成交核对结果里没有 resid_mean_bps ⇒ 本档退回旧口径 mean_bps 判 50bps"
                                "(该判据会被买卖抵消, 混合账本上近乎恒绿)")
                if abs(float(judged)) > 50:
                    bad.append("成交价执行残差均值 {}bps{}（回测假设滑点 {}bps，单笔最差偏离 {}bps）".format(
                        judged, "，**旧口径均值**" if legacy else "",
                        pr.get("assumed_slip_bps"), pr.get("max_abs_bps")))
            res["reconcile"] = {"ok": not bad, "issues": bad}
            if bad:
                log.error("成交核对异常: %s", " | ".join(bad))
                # 2026-09-18 审查 Q-15：同上，直调没有去重 ⇒ 走 `_ops_alert_once`。
                # 本档周一至五 19:45 + misfire 追补，一天最多正常 1 次 + 追补 1 次；
                # 没有 key 去重时"连续坏的第二天再收一封同标题邮件"。
                _ops_alert_once("weekly_reconcile",
                                "周频成交核对异常", "\n".join(bad + wk_rec.lines(rec)))
            else:
                log.info("成交核对: %s", (wk_rec.lines(rec) or [""])[0])
    except Exception:
        log.exception("成交核对接线失败(不影响健康读数)")
    # 同一天顺带刷新**归因缓存**(规划 §6.1-5): 它要拉约两年行情 + 跑横截面回归(实测 ~60s),
    # 不能挂在 HTTP 端点上, 所以在这里算一次、看板只读缓存。
    try:
        from app.weekly import attrib as wk_attrib
        wk_attrib.refresh()
    except Exception:
        log.exception("袖套归因刷新失败(不影响健康监控读数)")
    # **块级 IC**（D 案 ③-1，2026-09-22）：六块各自的健康读数，同一档算一次、看板/doctor 只读缓存
    # （要读约 70 个因子锚点，实测 ~26s，不能挂 HTTP 端点）。
    # P0 块跌穿 0.5× 该块历史均值 ⇒ 运维邮件；P1/P2 只记录不上报（D 案定死的分级）。
    try:
        from app.weekly import health as wk_health
        from app.weekly import blocks as wk_blocks
        from app.weekly.report import block_ic_lines
        bres = wk_health.block_ic_refresh()
        res["block_ic"] = {b: (bres.get("blocks") or {}).get(b, {}).get("ratio")
                           for b in wk_blocks.BLOCK_KEYS}
        p0 = bres.get("p0_alarms") or []
        if p0:
            log.warning("块级 IC 报警(P0): %s", ", ".join(p0))
            _ops_alert_once("weekly_block_ic:" + ",".join(p0),
                            "周频块级 IC 报警: " + ", ".join(p0),
                            "\n".join(block_ic_lines(bres))
                            + "\n\n块读数只报警，不自动改任何东西（输入层由 C 案封死）；"
                              "定位看 `block_ic.json` 里的 factor_baseline_ic。")
        else:
            log.info("块级 IC: 无 P0 报警 %s", res["block_ic"])
    except Exception:
        log.exception("块级 IC 刷新失败(不影响健康监控读数)")
    # **边沿触发**告警（2026-09-18 邮件优化）：原来这里是"level 不健康就发" ⇒ 一旦退化会
    # 连着每天发同一封。现在由 `health.decide_alert` 判断（等级变化 / 同一档满 7 天 / 恢复），
    # 且**只有真的发出去了才记账**：邮件本身发送失败时状态不落盘，第二天会再试一次，
    # 失败次数也能从 notify_log 与早上那封「今日待办」里看到。
    try:
        from app.weekly.report import send_health_email
        prev = wk_health.load_notified()
        kind = wk_health.decide_alert(prev.get("level"), res.get("level"),
                                      prev.get("date"), timeutil.date8())
        if kind:
            if send_health_email(res, kind=kind):
                if kind == "recovered":
                    wk_health.save_notified("healthy", timeutil.date8())
                else:
                    wk_health.save_notified(res.get("level"), timeutil.date8())
            else:
                log.info("健康告警未发送(开关关闭或发送失败), 状态不落盘: level=%s kind=%s",
                         res.get("level"), kind)
    except Exception:
        log.exception("袖套健康告警发送失败")
    return res


def _stage_failed(conn, stages, since_days: int = 3) -> bool:
    """最近 N 天内指定阶段是否存在"最新记录为 error/aborted"的阶段。
    2026-09-05 审查修复: 原按任意 error 行匹配, 而成功重跑只新增 ok 行、旧 error 行
    永不清除 → 一次瞬态失败后 3 天内每次开机都重跑完整日频+月度链(浪费配额并反复
    重建方案/挂单)。改为只看每个阶段的最新一行。
    2026-09-19 审查: 加 'aborted'(`_stage` 的 finally 为"被强杀/中断"落的行) —— 那种情况
    原样会**既没有 error 行、也没有补跑**, 缺口就此消失。判据仍是"只看每个阶段的最新一行",
    所以不会因为历史里有 aborted 而反复重跑。"""
    q = ",".join("?" * len(stages))
    # 2026-09-12 审查 P2-18: 窗口下界由 app.timeutil 算(东八区), 不再用 SQLite 的
    # `datetime('now','localtime',?)` —— started_at 的写入侧已同源, 读侧必须同一个钟,
    # 否则非东八区部署下窗口会错一整天(该补的没补, 或白白重复整链重跑)。
    row = conn.execute(
        f"SELECT 1 FROM pipeline_run p WHERE p.status IN ('error','aborted') AND p.stage IN ({q}) "
        "AND p.started_at>=? "
        "AND p.id=(SELECT MAX(id) FROM pipeline_run q WHERE q.stage=p.stage) LIMIT 1",
        (*stages, timeutil.days_ago(since_days))).fetchone()
    return row is not None


def _catch_up(now=None):
    """开机补跑。`now` 可注入(测试用), 缺省取本机时区当前时间。"""
    try:
        today = datetime.datetime.now(_TZ).strftime("%Y%m%d")
        # 2026-09-13: 这一段原来 get_conn() 无 try/finally —— 下面任何一条查询抛错就漏连接,
        # 而这是**每次启动都跑**的路径(独立审查 §3.2)。改 conn_ctx()。
        with db.conn_ctx() as conn:
            data_last = conn.execute("SELECT MAX(trade_date) FROM daily").fetchone()[0]
            model_last = conn.execute("SELECT MAX(trade_date) FROM model_score").fetchone()[0]
            latest_open = conn.execute(
                "SELECT MAX(cal_date) FROM trade_cal WHERE is_open=1 AND cal_date<=?",
                (today,)).fetchone()[0]
            latest_closed = conn.execute(
                "SELECT MAX(cal_date) FROM trade_cal WHERE is_open=1 AND cal_date<?",
                (today,)).fetchone()[0]
            month_end = _last_month_end(conn)
            # 失败阶段补跑(P1-7): 日频链阶段失败 → 补每日更新; 月度链阶段失败 → 补月度全流程
            retry_daily = _stage_failed(conn, ("daily_factor", "daily_score", "monitor", "paper"))
            retry_monthly = _stage_failed(conn, ("factors", "train", "backtest", "plan"))
            # 2026-09-08 审查 P1: 原 need_* 只查数据/模型水位与 3 天窗口内失败阶段 ——
            # plan/backtest 失败超过 3 天且机器常开不重启时, 当月静默无方案、纸面不调仓;
            # paper 记账落后同理。改为把方案水位与净值水位纳入判定(水位检查幂等,
            # 覆盖即跳过, 不会重复触发)
            plan_last = conn.execute(
                "SELECT MAX(plan_date) FROM plan WHERE plan_type='stock'").fetchone()[0]
            nav_last = conn.execute("SELECT MAX(trade_date) FROM paper_nav").fetchone()[0]
        now = now or datetime.datetime.now(_TZ)
        # 补数据截止日: 17点后(已收盘)补到今天; 盘中/盘前只补到上一个已收盘交易日,
        # 避免把当天不完整的盘中行情拉进库(首次初始化同样遵守, 缺的当天由当晚18:00补全)
        if now.hour >= 17:
            end = latest_open
        else:
            end = latest_closed
        # 第九轮 N9-1(2026-09-06): 显式括号消除 and/or 优先级依赖(语义不变)
        need_daily = (end is not None and (data_last is None or data_last < end)) or retry_daily \
            or (data_last is not None and (nav_last is None or nav_last < data_last))
        need_monthly = (model_last is None) or (month_end and model_last < month_end) \
            or (month_end and (plan_last is None or plan_last < month_end)) or retry_monthly
        if need_daily:
            log.info("开机补跑: 数据落后或阶段失败(库内 %s / 目标 %s), 启动每日更新", data_last, end)
            _guarded(lambda: daily_update(end_date=end), "开机补跑-每日")
        if need_monthly:
            log.info("开机补跑: 缺少 %s 的月度方案或阶段失败, 启动月度全流程", month_end)
            _guarded(lambda: monthly_pipeline(end_date=end), "开机补跑-月度")
        # 开盘档补跑(2026-09-07): 交易日盘中启动时, 若仍有未执行挂单按今开价补撮合;
        # 上方的日/月链补跑若已回放成交, 此处自然无挂单即空转(自身幂等+晚间链兜底)
        _guarded(open_exec_slot, "开机补跑-开盘执行")
        # 周频袖套补跑(2026-09-13): 袖套的三个档(09:32 / 18:05 / 19:30)**都活在调度器进程里**
        # ⇒ 服务若在 18:05 之后才起来(重启/看门狗拉回/半夜掉线), 本周成交会拖到**下一个交易日**,
        # 成交价也跟着换成那天的开盘价。这里补一刀, 且**直接复用晚间档那个函数** —— 成交价口径
        # (当日日线开盘价)与 18:05 完全一致, 不写第二份口径。
        # 2026-09-14（07 #50）: 补跑前**先**补一次打分保鲜（幂等：已覆盖即秒退）。理由：
        # "服务周末/周一早上才起来"时，打分存档可能还停在上一根锚点 —— 那会让 09:32 出方案失败、
        # 而补跑只执行不建造 ⇒ 整周空仓。保鲜档内部判 `weekly.enabled`，停用时零副作用。
        # 2026-09-14 外部复核 D1/D3：走 `_weekly_job`（停用零写入 + 按结果分类 + 抢锁留痕）
        _weekly_job(_weekly_score_slot, "weekly_score_catchup", "开机补跑-周频打分保鲜")()
        # 安全性: 当日日线还没入库时(盘前/盘中/收盘后未更新), `_exec_price` 取不到当日行返回 None
        # ⇒ 成交顺延, 不会用不存在的价格下单; 已执行过的方案本身就是幂等(差额为 0)。
        # ⚠️ **2026-09-14 修 (07 号 #34)**: 上面那句"0 成交也安全"漏了一个后果 ——
        # `execute_plan` **一笔没成也会写 `last_plan` + 落一个当日净值点**, 而"当日有没有净值点"
        # 正是执行档看门狗的判据 ⇒ **盘前启动会把看门狗短路**。实测证据:
        #   2026-09-14 06:04:45 `周频袖套 20260914: 成交 0 笔(买 0 / 卖 0), NAV 1000000`
        #   ⇒ 之后 09:32/18:05 两档即便都失败, 看门狗也会报 ✓(整周没仓位却没人知道)。
        # ⇒ 只在**晚间档窗口(18:05)之后**才补跑; 18:05 之前交给当天的 09:32 / 18:05 两档。
        # `weekly.enabled=false`(缺省)时该档在读日历前返回, 零副作用。
        # ⚠️ 已知边界（2026-09-14 外部复核 A2，**接受不改**）：上面那次保鲜可能跑 6~8 分钟，
        # 而 `now` 是进函数时抓的 ⇒ 23:57 启动、训练跨过午夜时，这里仍按"22:57 已过 18:05"补跑，
        # 但 `run_evening_slot()` 会自己取**新的一天**（那天还没开盘）⇒ 取不到价 ⇒ 0 成交、
        # 只落一个"盘前空占位"净值点（当日 18:15 前看门狗容忍，`mark` 同日覆盖）。
        # **为什么不"重取 now"**：那会破坏 `_catch_up(now=…)` 的可注入性（测试靠假时钟测 18:05 闸），
        # 也会破坏"补跑必须复用 `run_evening_slot` 本身"的恒等守卫；代价（仅在 23:57~00:05 这个
        # 极窄窗口多一个空占位点）小于这两条纪律的价值。
        if now.hour * 60 + now.minute >= 18 * 60 + 5:
            from app.weekly.pipeline import run_evening_slot
            _weekly_job(run_evening_slot, "weekly_evening_catchup", "开机补跑-周频袖套")()
        else:
            log.info("开机补跑-周频袖套: 现在 %s 未到 18:05 ⇒ 今日交给 09:32/18:05 档"
                     "(盘前补跑只会落一个 0 成交的空占位净值点, 见 07 号 #34)",
                     now.strftime("%H:%M"))
        # 2026-09-16 调度审查 SCH-10: 18:30 汇总档的重排是**内存任务**, 重启即丢 ——
        # 日链已完成、汇总未发时崩溃重启 ⇒ 当日汇总邮件永久缺失且无告警。补一条腿:
        # 18:35 后启动 + 今日已记账 + 今日还没发过汇总 ⇒ 补发一次(槽自身有"已记账"门控,
        # 重复调用在邮件未发时安全; notify_log 的 tag='模拟盘日终汇总' 作同日去重)。
        if now.hour * 60 + now.minute >= 18 * 60 + 35:
            with db.conn_ctx() as conn:
                _nav_today = conn.execute(
                    "SELECT COUNT(*) FROM paper_nav WHERE trade_date=?", (today,)).fetchone()[0]
                _sent = conn.execute(
                    "SELECT COUNT(*) FROM notify_log WHERE tag='模拟盘日终汇总' "
                    "AND ok=1 AND substr(created_at,1,10)=?",
                    (f"{today[:4]}-{today[4:6]}-{today[6:]}",)).fetchone()[0]
            if _nav_today and not _sent:
                log.info("开机补跑: 当日已记账但日终汇总未发 ⇒ 补发一次")
                _guarded(daily_summary_slot, "开机补跑-日终汇总")
        # 全账户日报补跑（2026-10-05，SCH-10 同族）: 21:15 档同样是内存任务, 重启即丢 ——
        # 21:20 后启动 + 今日 notify_log 无 ok=1 的「全账户日报」⇒ 补跑一次
        # (槽内 send_daily_report 再按同 tag 同日去重, 重复调用只会发一封)。
        if now.hour * 60 + now.minute >= 21 * 60 + 20:
            _guarded(accounts_mail_slot, "开机补跑-全账户邮件")
    except Exception:
        # 2026-09-16 调度审查 SCH-04: 开机补跑整体失败原来只有 log —— 探活 200、
        # 看门狗判健康, 数据缺口静默拖到下一个 18:00 档。补一条运维告警。
        log.exception("开机补跑检查失败")
        try:
            _ops_alert_once("catchup_failed", "开机补跑检查失败",
                            "服务启动时的开机补跑逻辑抛错(可能是 DB 忙/表缺失), "
                            "本次补跑整体放弃 —— 数据缺口将拖到下一个 18:00 档, 请查 logs/app.log。")
        except Exception:
            log.exception("开机补跑失败告警也失败了")



def shutdown_scheduler():
    """退出时关闭调度器 (2026-09-04 审查 P3-6: 原 lifespan 只启动不关闭)"""
    global _scheduler
    if _scheduler:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            log.exception("调度器关闭异常")
        _scheduler = None
        log.info("调度器已关闭")


def start_scheduler():
    global _scheduler
    if _scheduler:
        return
    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    # 开盘前「今日待办」邮件(2026-09-18 邮件优化): 08:30 —— 挂单 09:31 就撮合,
    # "今天要不要我动手"必须在那之前送到。非交易日档内秒退不发信。
    # 宽限只给 2 小时: 再晚就错过开盘, 一封过期的"今日待办"比没有更误导。
    sched.add_job(_retryable(daily_brief_slot, "daily_brief", "定时-今日待办"),
                  "cron", day_of_week="mon-fri", hour=8, minute=30, id="daily_brief",
                  misfire_grace_time=2 * 3600)
    # 三模型选股交集早报(2026-10-08): 08:35 —— 错开今日待办；同日去重在 send 侧
    sched.add_job(_retryable(picks_email_slot, "picks_email", "定时-选股交集"),
                  "cron", day_of_week="mon-fri", hour=8, minute=35, id="picks_email",
                  misfire_grace_time=2 * 3600)
    # 开盘档(2026-09-07): 09:31 集合竞价定盘后执行昨日挂单; 宽限到 15:00(盘中补启动
    # 仍按今开价补撮合, 今开价全天固定), 15:00 后错过则晚间链 18:00 兜底
    # 2026-09-19 审查 A2 的例外(有意保持裸 `_guarded`)：本档内部已 `_stage(..., "open_exec")`
    # 自己留痕, 再套 `_weekly_staged` 会写下**同名**的第二行 —— 顶层行按"id 最大"覆盖内层
    # error, 反而把"开盘执行失败"这件事从台账里抹掉。错过窗口由 18:00 日链兜底(见上注释)。
    sched.add_job(lambda: _guarded(open_exec_slot, "定时-开盘执行"),
                  "cron", day_of_week="mon-fri", hour=9, minute=31, id="open_exec",
                  misfire_grace_time=5 * 3600 + 29 * 60)
    # 每日记账链(2026-09-07 由 17:23 改到 18:00): 数据→regime→因子/推分→监控→模拟盘
    # 执行+净值→成交邮件(即时)
    # 2026-09-19 审查 A2: 原为裸 `_guarded` —— 抢锁失败连一行 pipeline_run 都不留、也不重排
    # (misfire_grace_time 只兜"进程没起来", 兜不住"进程在但锁被 18:05 周频档/手动触发占着")。
    # 现在与 18:30 汇总档同一路子: 留痕 + 10/20/40 分钟退避重排。daily_update 内部各子阶段
    # 自己写 stage('daily_sync'/'regime'/...), 与这里的 stage='daily' 顶层行不重名。
    sched.add_job(_retryable(lambda: daily_update(end_date=_safe_end()), "daily", "定时-每日更新"),
                  "cron", day_of_week="mon-fri", hour=18, minute=0, id="daily",
                  misfire_grace_time=24 * 3600)
    # 日终汇总邮件(2026-09-07): 18:00 记账完成后 18:30 发当天信息; 宽限 2 小时(服务
    # 晚启动可补发), 更晚错过当日不补(成交邮件已即时发过, 汇总只是台账快照)
    # 2026-09-14（外部审查 P0-2）：汇总档原先抢锁失败即丢弃 —— 现在退避重排（10/20 分钟）
    sched.add_job(_retryable(daily_summary_slot, "daily_summary", "定时-日终汇总"),
                  "cron", day_of_week="mon-fri", hour=18, minute=30, id="daily_summary",
                  misfire_grace_time=2 * 3600)
    # 2026-09-12 审查 P1-06: 走 _monthly_slot(抢锁失败退避重排), 不再裸 _guarded
    sched.add_job(_monthly_slot, "cron", day=1, hour=18, minute=33, id="monthly",
                  misfire_grace_time=3 * 24 * 3600)
    # 2026-09-19 审查 A2: 唯一的周备份档原为裸 `_guarded` —— 抢锁失败(服务重启窗口里
    # 手动脚本持锁 / 开机补跑在跑)就一行 log 都不留地丢掉**整周**的备份。周日无盘中约束,
    # 退避放到 60/240 分钟(见 `_RETRY_MIN`); 备份自身还补了 busy_timeout(db.backup_db, A8)。
    sched.add_job(_retryable(_weekly_backup, "backup", "每周备份"), "cron",
                  day_of_week="sun", hour=9, minute=15, id="backup",
                  misfire_grace_time=7 * 24 * 3600)
    # 周频袖套执行档(2026-09-12 新增): 锚点后首个交易日 09:32(错开月频开盘档 09:31);
    # weekly.enabled=false(缺省) 时内部直接返回, 零副作用。宽限到 15:00(盘中补启动仍按今开价)
    # 2026-09-14: 四个周频档统一走 `_weekly_job`（= `_weekly_staged` + `_guarded(on_busy=…)`）
    # ⇒ 进 `pipeline_run` 且**按返回值分类**（error/skipped/ok）、停用时零写入、抢锁失败留痕
    # （原来"今天 09:32 跑没跑"在库里查不到，只能翻日志/邮件 —— 07 #34/#48 一类缺口）。
    sched.add_job(_retryable(_weekly_sleeve_slot, "weekly_sleeve", "定时-周频袖套"),
                  "cron", day_of_week="mon-fri", hour=9, minute=32, id="weekly_sleeve",
                  misfire_grace_time=5 * 3600 + 28 * 60)
    # 周频打分保鲜档(2026-09-12 新增): 每日 19:30 检查存档是否覆盖最新锚点, 未覆盖才重训。
    # 排在 18:00 日链之后(那时当日行情已入库, 周五当晚即可识别出"当天就是新锚点")
    # 2026-09-14（07 #50）：**加两次重试**（20:30 / 21:30）—— 日链偶尔会晚于 19:30 完成
    # （实测 09-11 是 19:41），那一晚保鲜档会看到"旧锚点"而跳过；而周一 09:32 只有一次机会。
    # 三档同一函数（幂等：已覆盖即秒退），代价可忽略。
    # ⚠️ 三次注册**逐条写出**（不用循环）：档位契约测试用 AST 读 `id` 常量，
    # 循环里的变量 id 会让它"看不见"这两个档 ⇒ 等于没被钉住。
    sched.add_job(_weekly_job(_weekly_score_slot, "weekly_score", "定时-周频打分保鲜"),
                  "cron", day_of_week="mon-fri", hour=19, minute=30, id="weekly_score",
                  misfire_grace_time=18 * 3600)
    sched.add_job(_weekly_job(_weekly_score_slot, "weekly_score", "定时-周频打分保鲜(重试1)"),
                  "cron", day_of_week="mon-fri", hour=20, minute=30, id="weekly_score_retry1",
                  misfire_grace_time=18 * 3600)
    sched.add_job(_weekly_job(_weekly_score_slot, "weekly_score", "定时-周频打分保鲜(重试2)"),
                  "cron", day_of_week="mon-fri", hour=21, minute=30, id="weekly_score_retry2",
                  misfire_grace_time=18 * 3600)
    # 周频袖套晚间兜底档: 18:05 用日线开盘价补齐开盘档(实时行情不可用/一字板/停牌)未成交的部分。
    # 排在 18:00 日链刚入库之后; 与开盘档账务等价, 差额为 0 时不成交
    # 2026-09-14（07 #50）：本档现在也会**自建方案**（缺方案且打得开分时）—— 见函数 docstring。
    sched.add_job(_retryable(_weekly_evening_slot, "weekly_evening", "定时-周频袖套晚间"),
                  "cron", day_of_week="mon-fri", hour=18, minute=5, id="weekly_evening",
                  misfire_grace_time=6 * 3600)
    # 周频袖套健康监控(2026-09-13): 19:45 算 滚动 IC/换手/投入率 → 四档; 非 healthy 发告警。
    # 排在 19:30 打分保鲜之后(那时存档是最新的)。注意: 本档**会**按 §6.1-4 自动停止开新仓
    # (risk.apply, 只降不升、人工才解除) —— 2026-09-13 修正了原注释"不自动改任何东西"的误导。
    sched.add_job(_retryable(_weekly_health_slot, "weekly_health", "定时-周频健康"),
                  "cron", day_of_week="mon-fri", hour=19, minute=45, id="weekly_health",
                  misfire_grace_time=18 * 3600)
    # 全账户邮件档(2026-10-05): 每日 21:15 —— 三模型 7 纸面账户日报 + 按账户异常告警
    # (读取失败/数据滞后/fterm 熔断线与本地告警与心跳/ECO-W2 熔断三层/月频盘总闸)。
    # **不设 day_of_week**: fterm 20:05 与 ECO-W2 20:30/20:35 计划任务每日都跑,
    # 周末账本不更新时"数据滞后"体检项正好把"该跑没跑"暴露出来。
    # 排在 21:00 NEWECO_irm_attention_daily 之后, 读到的都是当日落账结果。
    # 邮件层任何失败只允许少发一封信(slot 内全吞), 不影响数据链。
    sched.add_job(_retryable(accounts_mail_slot, "accounts_mail", "定时-全账户邮件"),
                  "cron", hour=21, minute=15, id="accounts_mail",
                  misfire_grace_time=2 * 3600)
    sched.start()
    _scheduler = sched
    log.info("定时任务已启动: 每工作日 08:30 今日待办 + 08:35 三模型选股交集邮件, 09:31 开盘执行 + 09:32 周频袖套, "
             "18:00 数据更新+记账, "
             "18:05 周频晚间兜底, 18:30 日终汇总, 19:30/20:30/21:30 周频打分保鲜(后两档为重试), "
             "19:45 周频健康监控, 每日 21:15 全账户邮件, 每月1日 18:33 月度全流程")
    threading.Thread(target=_catch_up, daemon=True).start()
