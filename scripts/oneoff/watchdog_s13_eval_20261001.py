# -*- coding: utf-8 -*-
"""S13 取证复跑（2026-10-01 到期项）—— CREATE_BREAKAWAY_FROM_JOB 猜测性处置的验收体。

判据 F0-F2 **逐字同源** `scripts/oneoff/watchdog_restart_forensics.py`（冻结于
`plans/watchdog-restart-forensics-prereg-20260922.md`），只换评估窗与判决语义：
  - 基线（确认集 09-15~09-21，处置前）：定时档自愈率 **0.81%**（8/985）≈ 1.14 次/天
    真·自发死亡；额外调用率 86.7%（计划性重启为主）。
  - 评估窗 = **2026-09-25 ~ 2026-10-01**（处置 09-24 23:17 最后一次计划内重启后启用；
    按日期过滤 ⇒ 07 号在册的 09-24 四次计划内重启天然落在窗外，不污染任何桶）。
  - S13 主判据（07 号冻结）：**定时档自愈率明显下降（趋近 0）⇒ "被作业对象带走"解释
    得到支持；数字不变（≈0.8%）⇒ 改动无效，回到"不猜"，不得继续加码改配置**。
  - 辅助 = C2（每次额外型自愈 ±10min 内有无 app/** 提交）识别计划性重启，
    **不参与主判据**（主判据纯看定时档桶）。
产物 experiments/watchdog_s13_eval_20261001.json。只读。
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
OUT = BASE / "experiments" / "watchdog_s13_eval_20261001.json"

RESTART_MARK = "已发出拉起命令"
LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2}) (.*)$")

EVAL_FROM = "2026-09-25"          # 处置启用后首个完整日
EVAL_TO = "2026-10-01"            # 07 号冻结的验收日
BASELINE = {"rate_sched": 0.0081, "restarts_sched": 8, "days": 7,
            "source": "确认集 09-15~09-21（experiments/watchdog_restart_forensics.json）"}
C2_WINDOW_MIN = 10


def _parse_log() -> "OrderedDict[str, dict]":
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
    return "定时档" if (call["minute"] % 10 == 5 and call["second"] <= 9) else "额外"


def _commit_hits_app(timestamp: str, window_min: int = C2_WINDOW_MIN) -> dict:
    t = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
    since = (t - timedelta(minutes=window_min)).strftime("%Y-%m-%d %H:%M:%S")
    until = (t + timedelta(minutes=window_min)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        r = subprocess.run(
            ["git", "log", f"--since={since}", f"--until={until}",
             "--name-only", "--format=%H"],
            cwd=str(BASE), capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace")
    except Exception as e:
        return {"window": f"±{window_min}min", "app_hit": None, "error": repr(e)}
    lines = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
    paths = [x for x in lines if not re.fullmatch(r"[0-9a-f]{40}", x)]
    hits = [p for p in paths if p.startswith("app/")]
    return {"window": f"±{window_min}min", "app_hit": bool(hits),
            "hit_files": sorted(set(hits))[:10]}


def main() -> int:
    calls = _parse_log()
    ns = ds = nx = dx = 0
    sched_events, extra_events = [], []
    for key, c in calls.items():
        if not (EVAL_FROM <= c["date"] <= EVAL_TO):
            continue
        kind = _classified(c)
        if kind == "定时档":
            ns += 1
            if c["mark"]:
                ds += 1
                sched_events.append({"time": c["time"]})
        else:
            nx += 1
            if c["mark"]:
                dx += 1
                row = {"time": c["time"]}
                row.update(_commit_hits_app(c["time"]))
                extra_events.append(row)
    rate_sched = round(ds / ns, 6) if ns else None
    rate_extra = round(dx / nx, 6) if nx else None
    planned_like = [e for e in extra_events if e.get("app_hit")]
    days = (datetime.strptime(EVAL_TO, "%Y-%m-%d")
            - datetime.strptime(EVAL_FROM, "%Y-%m-%d")).days + 1
    lam = BASELINE["restarts_sched"] / BASELINE["days"]
    import math
    p_zero = round(math.exp(-lam * days), 6) if ds == 0 else None

    if ds == 0:
        verdict = ("S13 支持：定时档自愈 0 次（基线同窗期望 ≈"
                   f"{lam * days:.1f} 次、P(0)= {p_zero}）⇒ '被作业对象带走'解释得到支持"
                   "（因果仍属事后一致，非机制证明）")
    elif rate_sched is not None and rate_sched <= BASELINE["rate_sched"] * 0.5:
        verdict = "S13 部分支持：定时档自愈率较基线明显下降（未到 0），继续观察"
    elif rate_sched is not None and abs(rate_sched - BASELINE["rate_sched"]) <= 0.005:
        verdict = "S13 无效：定时档自愈率与基线不变 ⇒ 回到'不猜'，不得继续加码改配置"
    else:
        verdict = "S13 INDETERMINATE：读数不足以按冻结判据二分"

    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "eval_window": f"{EVAL_FROM} ~ {EVAL_TO}（{days} 天，处置 09-24 23:17 启用后）",
        "criteria": {
            "F1": "定时档 = 分钟尾数 5 且秒 ≤9；其余 = 额外（逐字同源 0922 取证脚本）",
            "F2": "自愈事件 = 本次调用内出现「已发出拉起命令」；每次调用至多计 1",
            "S13_main": "定时档自愈率 vs 基线 0.81%：趋近 0 = 支持 / 不变 = 无效",
            "contamination": "09-24 四次计划内重启（22:15/22:32/23:05/23:17）按日期过滤天然窗外",
        },
        "baseline": BASELINE,
        "eval": {"calls_sched": ns, "restarts_sched": ds, "rate_sched": rate_sched,
                 "calls_extra": nx, "restarts_extra": dx, "rate_extra": rate_extra,
                 "sched_events": sched_events, "extra_events": extra_events,
                 "extra_planned_like": len(planned_like)},
        "verdict": verdict,
        "limits": [
            "TaskScheduler/Operational 仍 enabled:false ⇒ 无 OS 级证据；因果属事后一致非机制证明",
            "额外调用桶含计划性重启（C2 仅作识别注记，不参与主判据）",
            "只读：未改计划任务、日志通道、阈值或任何配置",
        ],
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"评估窗 {out['eval_window']}")
    print(f"定时档: {ns} 次调用 / {ds} 次自愈 = {rate_sched}（基线 {BASELINE['rate_sched']}）")
    print(f"额外调用: {nx} 次 / {dx} 次自愈（其中疑似计划性 {len(planned_like)}）")
    for e in sched_events:
        print("  定时档自愈:", e["time"])
    print("判决:", verdict)
    print(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
