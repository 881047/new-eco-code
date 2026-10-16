# -*- coding: utf-8 -*-
"""python 进程普查（07 #68 待办②轻量版，2026-10-15）：全机 python 进程采样与差异入账。

背景：S13/#68 的"无声死亡"（外部 TerminateProcess）此前只能对**被 supervisor 包装的
服务**取证退出码；训练/脚本/其它会话的 python 进程被杀则无任何记录（10-15 19:41 实例）。
本普查每 2 分钟采一次全机 python.exe/pythonw.exe 进程表，与上次快照做差异：
  - 新进程   → `+ pid=... start=... parent=链 cmd=...`
  - 消失进程 → `- GONE pid=... (alive=..min, lastparent=链) cmd=...`
**同时刻批量消失 = 杀手"批处理"指纹**；父进程链归属到具体会话/任务树。
⚠ 2026-10-15 深夜修正：pytest 批次的"批量消失"须与**正常完成**甄别——pytest 只在
会话正常收尾时写 `.pytest_cache/v/cache/*`（nodeids 收尾必写、lastfailed 失败集变化时写）。
对 pytest 批次自动对照收尾写盘时刻（cwd 与 venv 根双路径候选）：
有痕迹 ⇒ 记"✓ 正常完成"，无痕迹 ⇒ 保留"!! 疑似杀手"指纹。
采样制（无持久进程）⇒ 死亡可见性 = 下一样本点（最多滞后 2 分钟）。

用法：D:\\Python313\\pythonw.exe -X utf8 py_census.py   （调度任务 NEWECO_py_census 每 2 分钟调用）
日志：D:\\NEW ECO\\logs\\py_census.log；状态：logs\\py_census_state.json。
只读采样，绝不杀进程、绝不写他人目录。
暂停/卸载 = Disable-ScheduledTask / Unregister-ScheduledTask -TaskName NEWECO_py_census。
"""
import json
import re
import time
from datetime import datetime
from pathlib import Path

import psutil

BASE = Path(r"D:\NEW ECO")
LOG = BASE / "logs" / "py_census.log"
STATE = BASE / "logs" / "py_census_state.json"
MAX_LOG_BYTES = 5 * 1024 * 1024
NAMES = ("python.exe", "pythonw.exe")


def snapshot() -> dict:
    out = {}
    for p in psutil.process_iter(["pid", "name", "ppid", "create_time", "cmdline"]):
        try:
            n = (p.info["name"] or "").lower()
            if n not in NAMES:
                continue
            cmd = " ".join(p.info["cmdline"] or [])
            if "py_census.py" in cmd:
                continue                     # 排除普查自身（避免每分钟把上次采样记为 GONE 的自噪声）
            try:
                cwd = p.cwd()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                cwd = None                   # 死了/无权限：甄别时退回 cmd 内 venv 路径推断
            out[str(p.info["pid"])] = {
                "name": p.info["name"],
                "ppid": p.info["ppid"],
                "start": round(p.info["create_time"], 0),
                "cmd": cmd[:220],
                "cwd": cwd,
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def chain(ppid: int) -> str:
    try:
        p = psutil.Process(ppid)
        parts = []
        for _ in range(3):
            parts.append(f"{p.name()}({p.pid})")
            pp = p.ppid()
            if pp is None or pp <= 0:
                break
            p = psutil.Process(pp)
        return " < ".join(parts)
    except Exception:
        return f"?(pid={ppid})"


def fmt_ts(x: float) -> str:
    try:
        return datetime.fromtimestamp(x).strftime("%m-%d %H:%M")
    except Exception:
        return "?"


def _cache_dirs(info: dict) -> list:
    """pytest 缓存目录候选：进程 cwd 与 cmd 内 venv 根（.venv*\\Scripts 前缀）双路径。"""
    dirs = []
    cwd = info.get("cwd")
    if cwd:
        dirs.append(Path(cwd) / ".pytest_cache" / "v" / "cache")
    m = re.search(r"([A-Za-z]:\\[^\"']*?)[\\/]\.venv\d*[\\/]Scripts", info.get("cmd", ""))
    if m:
        dirs.append(Path(m.group(1)) / ".pytest_cache" / "v" / "cache")
    seen, out = set(), []
    for d in dirs:
        k = str(d).lower()
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out


def _pytest_finish_marks(gone: list, now: float) -> list:
    """返回本窗口内的 pytest 收尾写盘痕迹（正常完成的证据），供批次甄别。

    依据 pytest cacheprovider 语义（9.1.1 源核对）：nodeids 在 pytest_sessionfinish
    无条件写；lastfailed 仅当失败集变化时在 sessionfinish 写。二者任一在窗口内
    （含 2 分钟采样间隔 + 余量）⇒ 该批 pytest 系正常收尾，而非被杀。
    """
    marks = []
    for _pid, info in gone:
        if "pytest" not in info.get("cmd", ""):
            continue
        for d in _cache_dirs(info):
            for fn in ("nodeids", "lastfailed"):
                try:
                    m = (d / fn).stat().st_mtime
                except OSError:
                    continue
                if now - 300 <= m <= now + 5:
                    t = datetime.fromtimestamp(m).strftime("%H:%M:%S")
                    marks.append(f"{fn}@{t} [{d.parents[2]}]")
    # 去重（同一痕迹可能经多进程/多路径重复命中）
    seen, out = set(), []
    for mk in marks:
        if mk not in seen:
            seen.add(mk)
            out.append(mk)
    return out


def main() -> int:
    now = time.time()
    snap = snapshot()
    prev = {}
    if STATE.exists():
        try:
            prev = json.loads(STATE.read_text(encoding="utf-8")).get("procs", {})
        except Exception:
            prev = {}
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    for pid, info in snap.items():
        if pid not in prev:
            lines.append(
                f"{ts} | + pid={pid} {info['name']} start={fmt_ts(info['start'])} "
                f"parent={chain(info['ppid'])} cmd={info['cmd']}")
    gone = [(pid, info) for pid, info in prev.items() if pid not in snap]
    for pid, info in gone:
        alive_min = (now - info.get("start", now)) / 60.0
        lines.append(
            f"{ts} | - GONE pid={pid} {info.get('name', '?')} "
            f"(start={fmt_ts(info.get('start', 0))}, alive={alive_min:.1f}min, "
            f"lastparent={chain(info.get('ppid', 0))}) cmd={info.get('cmd', '')}")
    if len(gone) >= 2:
        pids = [pid for pid, _ in gone]
        all_pytest = all("pytest" in i.get("cmd", "") for _, i in gone)
        marks = _pytest_finish_marks(gone, now) if all_pytest else []
        if all_pytest and marks:
            lines.append(f"{ts} | ✓ 判定：pytest 批次正常完成 x{len(gone)}：{pids}"
                         f"（收尾写盘：{'；'.join(marks[:2])}）")
        else:
            tag = "（pytest 批但无收尾写盘痕迹）" if all_pytest else ""
            lines.append(f"{ts} | !! 同窗批量消失 x{len(gone)}：{pids}"
                         f" —— 疑似'批处理'杀手指纹{tag}")
    if lines:
        if LOG.exists() and LOG.stat().st_size > MAX_LOG_BYTES:
            keep = LOG.read_text(encoding="utf-8", errors="ignore").splitlines()[-2000:]
            LOG.write_text("\n".join(keep) + "\n", encoding="utf-8")
        with LOG.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"ts": ts, "n": len(snap), "procs": snap},
                                ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
