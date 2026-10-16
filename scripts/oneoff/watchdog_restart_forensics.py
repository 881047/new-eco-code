# -*- coding: utf-8 -*-
"""S13 取证:看门狗「自愈」计数的来源分类 —— **只读**(DB/服务/判据一律不动)。

为什么有这个脚本
----------------
14 号 S13 记的是「服务每天自愈 3~8 次、死因未明」。但本项目的运行手册要求
改 `app/**` 必须重启服务,推荐手法是先杀掉 8787 持有者再 `schtasks /run`
⇒ 「自愈」这个计数里很可能混着**我们自己的计划性重启**。在一个可能被污染的
统计量上找生理解释,是把错命题当真 —— 所以第一轮先把**计数本身**拆干净。

判据**先冻结**在 `plans/watchdog-restart-forensics-prereg-20260922.md`(F0~F5),
本文件只做执行体:
  F0 一次调用 = 同一分钟内的连续日志行合并
  F1 定时档调用 = 分钟尾数 5 且秒 ≤9;其余 = 额外调用
  F2 自愈事件 = 该次调用里出现「已发出拉起命令」(每次调用至多计 1)
  F3 只在**确认集**(09-15~09-21)判决 C1;今天(09-22)单列为探索集
  F4 TaskScheduler/Operational = enabled:false ⇒ **没有 OS 级证据**可区分
     「手动 /run」与「计划任务延迟启动」⇒ 结论只到"不合计划 cadence"这一层
  F5 不改任何东西(不启用日志通道、不动计划任务、不改阈值)

只读声明:只读 `logs/watchdog.log` 与 `git log`,不写任何生产表,不停服务。
"""
from __future__ import annotations

import json
import re
import subprocess
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
LOG = BASE / "logs" / "watchdog.log"
OUT = BASE / "experiments" / "watchdog_restart_forensics.json"
PREREG = "plans/watchdog-restart-forensics-prereg-20260922.md"

RESTART_MARK = "已发出拉起命令"
LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2}) (.*)$")

CONFIRM_FROM = "2026-09-15"
CONFIRM_TO = "2026-09-21"
EXPLORE_DAY = "2026-09-22"

# F3 冻结的阈值
C1_MIN_EXTRA_RATE = 0.50
C1_MAX_SCHED_RATE = 0.05
C2_WINDOW_MIN = 10


def _parse_log() -> "OrderedDict[str, dict]":
    """返回 {调用键: {date, minute, sec, mark}} —— 同一分钟内多行合并为一次调用(F0)。"""
    calls: "OrderedDict[str, dict]" = OrderedDict()
    for raw in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        m = LINE_RE.match(raw.strip())
        if not m:
            continue
        date, hh, mm, ss, msg = m.groups()
        key = f"{date} {hh}:{mm}"
        call = calls.get(key)
        if call is None:
            call = calls[key] = {"time": f"{date} {hh}:{mm}:{ss}", "date": date,
                                 "minute": int(mm), "second": int(ss), "mark": False,
                                 "lines": 0}
        call["lines"] += 1
        if RESTART_MARK in msg:
            call["mark"] = True
    return calls


def _classified(call: dict) -> str:
    """F1:定时档 = 分钟尾数 5 且秒 ≤9;其余 = 额外。"""
    return "定时档" if (call["minute"] % 10 == 5 and call["second"] <= 9) else "额外"


def _commit_hits_app(timestamp: str, window_min: int = C2_WINDOW_MIN) -> dict:
    """C2:该时刻 ±10 分钟内有没有动过 `app/**` 的提交(只读 git)。"""
    t = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
    since = (t - timedelta(minutes=window_min)).strftime("%Y-%m-%d %H:%M:%S")
    until = (t + timedelta(minutes=window_min)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        r = subprocess.run(
            ["git", "log", f"--since={since}", f"--until={until}",
             "--name-only", "--format=%H"],
            cwd=str(BASE), capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace")
    except Exception as e:                       # 取证不许把脚本带崩
        return {"window": f"±{window_min}min", "app_hit": None, "error": repr(e)}
    lines = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
    paths = [x for x in lines if not re.fullmatch(r"[0-9a-f]{40}", x)]
    hits = [p for p in paths if p.startswith("app/")]
    return {"window": f"±{window_min}min", "app_hit": bool(hits),
            "hit_files": sorted(set(hits))[:10], "commits": len([x for x in lines if re.fullmatch(r"[0-9a-f]{40}", x)])}


def _stats(calls: dict, date_from: str, date_to: str) -> dict:
    ns = ds = nx = dx = 0
    events = []
    for key, c in calls.items():
        if not (date_from <= c["date"] <= date_to):
            continue
        kind = _classified(c)
        if kind == "定时档":
            ns += 1
            ds += 1 if c["mark"] else 0
        else:
            nx += 1
            dx += 1 if c["mark"] else 0
            if c["mark"]:
                events.append(c["time"])
    return {"calls_sched": ns, "restarts_sched": ds,
            "rate_sched": round(ds / ns, 6) if ns else None,
            "calls_extra": nx, "restarts_extra": dx,
            "rate_extra": round(dx / nx, 6) if nx else None,
            "extra_events": events}


def main() -> int:
    calls = _parse_log()
    confirm = _stats(calls, CONFIRM_FROM, CONFIRM_TO)
    explore = _stats(calls, EXPLORE_DAY, EXPLORE_DAY)

    # C2(辅助,不参与判决):确认集里每次"额外调用型自愈"附近有没有动过 app/**
    c2 = []
    for t in confirm["extra_events"]:
        row = {"time": t}
        row.update(_commit_hits_app(t))
        c2.append(row)
    hit = sum(1 for r in c2 if r.get("app_hit"))
    c2_rate = round(hit / len(c2), 6) if c2 else None

    # C1(主判据,只在确认集上判)
    ok_extra = (confirm["rate_extra"] or 0) >= C1_MIN_EXTRA_RATE
    ok_sched = (confirm["rate_sched"] or 1) <= C1_MAX_SCHED_RATE
    verdict = ("自愈计数主要来自非定时的计划性重启" if (ok_extra and ok_sched)
               else "不能排除自发死亡为主")

    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "prereg": PREREG,
        "criteria": {
            "F1": "定时档 = 分钟尾数 5 且秒 ≤9(计划任务 09:45 起 PT10M);其余 = 额外",
            "F2": "自愈事件 = 本次调用内出现「已发出拉起命令」;每次调用至多计 1",
            "F3_confirm_set": f"{CONFIRM_FROM} ~ {CONFIRM_TO}(判决集)",
            "F3_explore_set": f"{EXPLORE_DAY}(探索集,驱动判据设计,不参与判决)",
            "C1": f"rate_extra ≥ {C1_MIN_EXTRA_RATE} 且 rate_sched ≤ {C1_MAX_SCHED_RATE}",
            "C2": f"每次额外型自愈 ±{C2_WINDOW_MIN}min 内是否有 app/** 提交(辅助,不判决)",
        },
        "confirm": confirm,
        "explore": explore,
        "c2_details": c2,
        "c2_app_hit_rate": c2_rate,
        "c1_pass": bool(ok_extra and ok_sched),
        "verdict": verdict,
        "limits": [
            "TaskScheduler/Operational 日志 enabled:false ⇒ 无 OS 级证据区分「手动 /run」与「任务延迟启动」",
            "结论只到『这些调用不合计划 cadence』一层,禁止写成『已证实是手动重启』",
            "剩余定时档内检测到的自愈事件仍是未解释的自发死亡,本轮不解释其机制",
            "本轮只读:未启用任何日志通道、未改计划任务、未改任何阈值",
        ],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"产物: {OUT}")
    print(f"确认集 {CONFIRM_FROM}~{CONFIRM_TO}: 定时档 {confirm['calls_sched']} 次调用/"
          f"{confirm['restarts_sched']} 次自愈 = {confirm['rate_sched']};"
          f"额外 {confirm['calls_extra']}/{confirm['restarts_extra']} = {confirm['rate_extra']}")
    print(f"探索集 {EXPLORE_DAY}: 定时档 {explore['calls_sched']}/{explore['restarts_sched']};"
          f"额外 {explore['calls_extra']}/{explore['restarts_extra']}")
    print(f"C2 附条件(app/** 提交命中率) = {c2_rate} ({hit}/{len(c2)})")
    print(f"C1 = {'PASS' if out['c1_pass'] else 'FAIL'} ⇒ 判决: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
