"""跨进程文件锁（2026-09-15，从 `app/scheduler.py` 的那套经验抽出通用版）。

## 为什么要有这个模块

调度器的 `.task.lock` 解决了"服务 + 脚本 + 网页触发同时跑管线"，但**只覆盖调度管线**。
袖套账本（`data/weekly/ledger.json`）还有一条调度器之外的写入路径：
`scripts/run_weekly.py exec` / `resume` 是**人工**跑的，不经过 `_guarded`。
两者同时写账本 ⇒ 后完成的那次覆盖前一次 ⇒ **成交记录静默消失**（外部审查 P1-3）。

## 与调度器那套的差别（有意为之）

1. `path` 可配置（调度器那份写死 `.task.lock`）；
2. **陈旧判据跨平台**：Windows 用 FILETIME 滴答数核对 pid 复用；非 Windows 退回
   `os.kill(pid, 0)` 探活（两者都拿不到时保守视为"锁有效"，宁可等也不偷活锁）；
3. 不做退避重试、不做重排 —— 这里只提供"拿到/没拿到"，何时重试由调用方决定。

## 纪律（踩过的坑，都在 scheduler 的注释里）

- 锁文件内容必须**先写完整再原子挂上**：`os.open(O_CREAT|O_EXCL)` 只能保证创建原子，
  open 与 write 之间文件是空的，而"空锁 = 陈旧"的判据会把它当死锁偷走 ⇒ 双方同时持锁。
  故用「写临时文件 → `os.link` 硬链接」。
- 清陈旧锁用 `os.replace` **搬走**（不是 unlink）：搬走的赢家唯一，避免 TOCTOU。
  ⚠️ 但"搬"这一步本身不校验搬的是谁 —— 见 `acquire()` 的 A4 三道加固。
"""
from __future__ import annotations

import logging
import os
import random
import time
from pathlib import Path

from app import timeutil

log = logging.getLogger(__name__)


def _backoff(attempt: int) -> float:
    """接管陈旧锁后的退避秒数：让抢赢的那个先把锁建上，输的那个下一轮自然看到活锁。
    带抖动 —— 否则两个进程的退避节奏同步，会一遍遍同时撞在同一个窗口上。"""
    return 0.05 * (attempt + 1) + random.uniform(0.0, 0.05)


def _proc_start_ticks(pid: int):
    """Windows: 进程创建时间(FILETIME 滴答数)。
    返回 滴答数 / -1(进程不存在) / None(无法确定，如提权进程)。
    非 Windows 平台统一返回 None（由 `_pid_alive` 兜底）。

    2026-09-20 审查 D-06 / P1-2: 实现已收敛到 `timeutil.proc_start_ticks`（**单一实现**）——
    原 scheduler.py 另有一份几乎逐行相同的副本，两处若只改一处会对同一 pid 给出相反的
    "是否被复用"结论 ⇒ 并发互斥静默失效。此处**保留模块级名字**再转调，
    既消重又不破坏测试对 `filelock._proc_start_ticks` 的 mock。"""
    return timeutil.proc_start_ticks(pid)


def _proc_alive_strict(pid: int):
    """转调 `timeutil.proc_alive_strict` —— 与 `_proc_start_ticks` 同一条纪律：
    保留模块级名字，测试可 `mock.patch.object(filelock, "_proc_alive_strict")`。
    2026-10-08: `_stale` 用它识破"持有者已终止但进程对象未销毁"的假活锁。"""
    return timeutil.proc_alive_strict(pid)


def _pid_alive(pid: int):
    """存活探测，返回 True/False；**判断不了返回 None**。

    ⚠️⚠️ 2026-09-15（外部独立审查 F1，实测 CPython 源码确认为真）：**Windows 上
    `os.kill(pid, 0)` 不是"探活"而是"杀进程"** —— CPython `os_kill_impl` 的 `MS_WINDOWS`
    分支对非 CTRL_C_EVENT/CTRL_BREAK_EVENT 的信号一律 `OpenProcess(PROCESS_ALL_ACCESS)
    + TerminateProcess(handle, sig)`。所以在这里调 `os.kill` 会**误杀持锁进程**。
    旧路径：`_proc_start_ticks` 返回 None（OpenProcess 成功但 GetProcessTimes 失败/异常）
    ⇒ `_stale()` 调本函数 ⇒ 误杀。危害不是"判错"而是"杀服务"。

    ⇒ 平台分派：**Windows 一律返回 None**（判不了 ⇒ 由调用方当"锁有效"处理，宁可等）；
    只有 POSIX 才用 `kill(pid, 0)` 的真正探活语义。
    """
    if os.name == "nt":
        return None            # 绝不在此平台调 os.kill（见 docstring）
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # 存在但无权限 → 视为活着（保守）
    except Exception:
        return None          # 判不了 → 由调用方保守处理


class FileLock:
    """一个锁文件 = 一把跨进程互斥锁。用法::

        lock = FileLock(path)
        if lock.acquire():
            try:
                ...
            finally:
                lock.release()
    """

    def __init__(self, path):
        self.path = Path(path)

    # ------------------------------------------------------------------ 内部
    def _token(self) -> str:
        tick = _proc_start_ticks(os.getpid())
        return f"{os.getpid()}\n{tick if tick is not None else ''}"

    def _stale(self) -> bool:
        """空/乱码、持有者已退出、或 pid 被复用 ⇒ True。判不了 ⇒ False（保守）。"""
        try:
            text = self.path.read_text(encoding="utf-8").strip()
            pid_s, _, tick_s = text.partition("\n")
            pid = int(pid_s)
            if pid <= 0:
                return True
            # 2026-10-08: 先问退出码。持有者已终止但进程对象未销毁时（拉起者的 Popen
            # 没 wait、任务计划/wscript 包装链还握着句柄），下面的 GetProcessTimes 仍
            # 成功且滴答数与锁里一致 ⇒ 滴答比对把死锁当活锁，新实例永远撞锁（当日
            # 09:01 看门狗拉起撞锁、误报"需要人工"的直接原因）。GetExitCodeProcess
            # 是 Windows 上唯一不依赖"对象已销毁"的识破手段；-1/None（不存在/判不了）
            # 交回下面的原逻辑。
            if _proc_alive_strict(pid) is False:
                return True
            ticks = _proc_start_ticks(pid)
            if ticks == -1:
                return True
            if ticks is None:
                # 拿不到滴答数（如提权进程/GetProcessTimes 失败）：
                # ⚠️ Windows 上**绝不**走 os.kill 探活（那是杀进程，见 `_pid_alive` docstring）。
                # `_pid_alive` 因此返回 True/False/None；None = 判不了 ⇒ **保守视为锁有效**
                # （宁可等锁，也不误杀持锁进程、也不偷活锁）。
                alive = _pid_alive(pid)
                return False if alive is None else (not alive)
            if tick_s.strip():
                try:
                    if int(tick_s) != ticks:
                        return True          # pid 被复用（创建时间对不上）
                except ValueError:
                    return True              # 内容损坏
            return False
        except FileNotFoundError:
            return True
        except Exception:
            return True                      # 空文件/乱码 → 陈旧

    def _content(self):
        """锁文件当前内容(strip 后)；文件不在/读不到 → None。"""
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return None

    def _unlink_quiet(self, p: Path) -> None:
        try:
            p.unlink()
        except OSError:
            pass

    def _cleanup_stale_locks(self, max_age_s: int = 3600) -> None:
        """清扫**本锁自己的**历史垃圾：`.stale.*`（陈旧锁被搬走后留下的）与 `.tmp.*`。

        为什么要有（2026-09-15 外部审查 F2）：`scheduler.py` 的旧实现有同款清扫
        （见其 `_cleanup_stale_locks`），本模块抽通用版时**漏带了这一步** ⇒
        `data/weekly/` 会随崩溃/陈旧锁事件慢慢积累垃圾文件。属能力倒退，补回。

        口径与 scheduler 一致：`.stale.*` 一律清（它们已无锁语义）、`.tmp.*` 只清
        **1 小时前**的（可能属于另一个正在建锁的进程，太新的不能碰）。
        任何清理失败都只记 debug —— 清扫是卫生，不是正确性。
        """
        try:
            for p in self.path.parent.glob(self.path.name + ".stale.*"):
                self._unlink_quiet(p)
            now = time.time()
            for p in self.path.parent.glob(self.path.name + ".tmp.*"):
                try:
                    if now - p.stat().st_mtime > max_age_s:
                        self._unlink_quiet(p)
                except OSError:
                    pass
        except OSError:
            pass

    # ------------------------------------------------------------------ 公开
    def acquire(self, retries: int = 5) -> bool:
        """尝试拿锁；拿到返回 True（调用方负责 `release()`）。拿不到返回 False。

        2026-09-19 审查 A4：清陈旧锁那一步原有一个**双持有者**窗口 —— 两个进程各自
        判 `_stale()` 为真后，慢的那个会用 `os.replace` 把**快的那个刚建好的活锁**搬走
        再换上自己的（`os.replace` 只保证"搬"这步原子，不校验搬的是谁）。于是互斥失效，
        而它守的正是账本/管线"两个进程同时写"。三道加固把窗口从"`_stale()` 里那几次
        OpenProcess 系统调用(毫秒级)"压到"两条相邻语句之间"，并在link后**回读认领**：
          ① 搬之前重新读一遍内容，与刚判定陈旧的那一份不一致 ⇒ 说明已被他人接管 → 退避重来；
          ② 搬走后不立刻重 link，先退避(带抖动)让抢赢的那个把锁建上，本轮 link 自然失败
             并在下一轮看到对方的活锁 → 干净认输；
          ③ link 成功后回读内容确认是自己的 ⇒ 不是就说明被搬走了，放弃而不是继续以为持锁。
        ⚠️ 残余窗口不为零（POSIX/Windows 都没有"内容条件交换"原语），故账本那条路还留着
        `Ledger.save()` 的加载指纹校验做第二道防线(07 号 #54)。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        token = self._token().strip()
        tmp = Path(str(self.path) + f".tmp.{os.getpid()}")
        for attempt in range(max(1, retries)):
            try:
                tmp.write_text(self._token(), encoding="utf-8")
            except OSError:
                log.exception("写锁临时文件失败: %s", tmp)
                return False
            try:
                os.link(tmp, self.path)      # 原子；已存在 ⇒ FileExistsError
            except FileExistsError:
                self._unlink_quiet(tmp)
                seen = self._content()
                if seen is None:
                    continue                 # 刚好被搬走：重试
                if not self._stale():
                    return False
                if self._content() != seen:
                    time.sleep(_backoff(attempt))   # ① 已被他人接管，别搬对方的活锁
                    continue
                try:                         # 搬走陈旧锁（搬走者唯一）
                    os.replace(self.path, Path(
                        str(self.path) + f".stale.{os.getpid()}.{int(time.time() * 1000)}"))
                except OSError:
                    continue                 # 被别人抢先了，重试
                time.sleep(_backoff(attempt))       # ②
                continue
            except OSError:
                log.exception("创建锁文件失败: %s", self.path)
                self._unlink_quiet(tmp)
                return False
            else:
                self._unlink_quiet(tmp)
                if self._content() != token:
                    # ③ 我们的锁在 link 之后被人搬走并换上了他的 —— 本进程**不是**持有者。
                    # 不能继续以为自己在锁内；退避后重走一遍（下一轮会看到对方的活锁而认输）。
                    log.warning("文件锁 link 后回读不是自己的 token，判定接管失败并退避: %s",
                                self.path)
                    time.sleep(_backoff(attempt))
                    continue
                self._cleanup_stale_locks()      # F2：顺手打扫自己的历史垃圾
                return True
        self._unlink_quiet(tmp)
        return False

    def release(self) -> None:
        """只删**自己**持有的锁（token 里含 pid；pid 复用由 acquire 侧的滴答数兜住）。"""
        try:
            text = self.path.read_text(encoding="utf-8").strip()
            pid_s, _, _ = text.partition("\n")
            if int(pid_s) != os.getpid():
                log.warning("不删他人锁: %s (持有者 pid=%s)", self.path, pid_s)
                return
        except FileNotFoundError:
            return
        except Exception:
            log.warning("锁文件内容不可解析, 保守不删: %s", self.path)
            return
        self._unlink_quiet(self.path)
