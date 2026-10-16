"""块级报警的**持续期分布**（预注册 `plans/block-alarm-persistence-prereg-20260922.md`，P1–P6）。

回答"报警一旦触发，历史上通常持续几个 asof" ⇒ 给运维一个"观察几期"的经验依据。
**只读测量**：不改判据、不改参数、不换 pin。

判据不在本脚本：取数复用 `block_ic_replay_2021.build_ic_rows`，块读数走 `health.block_ic_from_ic`。

用法：
    D:\\Python313\\python.exe -X utf8 scripts/oneoff/block_alarm_persistence.py
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts" / "oneoff") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts" / "oneoff"))

from app import db                                     # noqa: E402
from app.weekly import blocks as wk_blocks             # noqa: E402
from app.weekly import health as wh                    # noqa: E402
from block_ic_replay_2021 import build_ic_rows         # noqa: E402  同一份取数实现

OUT = ROOT / "experiments" / "block_alarm_persistence.json"
DAILY_FROM = "20260803"
MONTHLY_UNTIL = "20260731"
SHORT_RUN = 2          # P5：≤2 期视为"短促扰动"
SHORT_FRAC_MIN = 0.50  # P5 门槛


def _git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return "unknown"


def _runs(pairs):
    """(asof, alarm) 序列 → 连续报警段（run）的长度列表与明细。"""
    out, cur = [], []
    for d, a in pairs:
        if a:
            cur.append(d)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _dist(runs):
    if not runs:
        return {"n_runs": 0, "lens": [], "mean": None, "median": None,
                "p90": None, "max": None, "frac_short": None}
    lens = sorted(len(r) for r in runs)
    short = sum(1 for L in lens if L <= SHORT_RUN)
    return {"n_runs": len(lens), "lens": lens,
            "mean": round(statistics.fmean(lens), 2),
            "median": statistics.median(lens),
            "p90": lens[max(0, int(round(0.9 * (len(lens) - 1))))],
            "max": lens[-1],
            "frac_short": round(short / len(lens), 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    t0 = time.time()
    window, base_n = wh.BLOCK_IC_WINDOW, wh.BLOCK_IC_BASELINE_ANCHORS
    horizon = wh.BLOCK_IC_HORIZON

    conn = db.get_conn()
    try:
        cutoff = wh._forward_cutoff(conn, horizon)
        anchors = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM factor WHERE trade_date<=? ORDER BY trade_date",
            (cutoff,)).fetchall()]
        ic_rows = build_ic_rows(conn, anchors, horizon)
        avail = sorted(ic_rows)

        series = {b: [] for b in wk_blocks.BLOCK_KEYS}
        for i in range(window + base_n, len(avail)):
            judged = wh.block_ic_from_ic(ic_rows, avail[i - window:i],
                                         avail[i - window - base_n:i - window])
            for b in wk_blocks.BLOCK_KEYS:
                series[b].append((avail[i], bool(judged["blocks"][b]["alarm"])))
        print(f"asof {len(avail)} 个；可评估 {len(series['B1'])} 个（{series['B1'][0][0]}…{series['B1'][-1][0]}）")

        res = {}
        for b in wk_blocks.BLOCK_KEYS:
            mon = [p for p in series[b] if p[0] <= MONTHLY_UNTIL]
            day = [p for p in series[b] if p[0] >= DAILY_FROM]
            res[b] = {
                "monthly": _dist(_runs(mon)),
                "daily": _dist(_runs(day)),
                "monthly_runs": [{"from": r[0], "to": r[-1], "len": len(r)}
                                 for r in _runs(mon)],
                "daily_runs": [{"from": r[0], "to": r[-1], "len": len(r)}
                               for r in _runs(day)],
            }

        # P5：门槛判定（只用月频段；日频段样本小、只报不判）
        advice = {}
        for b in wk_blocks.DOCTOR_BLOCKS:
            m = res[b]["monthly"]
            if m["n_runs"] == 0:
                advice[b] = {"watch_periods": None, "basis": "月频段无报警 run（历史从未触发）"}
                continue
            if m["frac_short"] >= SHORT_FRAC_MIN:
                advice[b] = {"watch_periods": SHORT_RUN,
                             "basis": f"≤{SHORT_RUN} 期就消退的 run 占 {m['frac_short']} ≥ {SHORT_FRAC_MIN}"
                                      f" ⇒ 多为短促扰动"}
            else:
                advice[b] = {"watch_periods": m["median"],
                             "basis": f"短促 run 占比 {m['frac_short']} < {SHORT_FRAC_MIN}"
                                      f" ⇒ 观察窗口取中位持续期数"}

        out = {
            "meta": {
                "script": "scripts/oneoff/block_alarm_persistence.py",
                "prereg": "plans/block-alarm-persistence-prereg-20260922.md",
                "git_head": _git_head(),
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "seconds": round(time.time() - t0, 1),
                "read_only": True,
                "params": {"horizon": horizon, "window": window,
                           "baseline_anchors": base_n,
                           "short_run": SHORT_RUN, "short_frac_min": SHORT_FRAC_MIN},
            },
            "by_block": res,
            "advice": advice,
            "unit_note": "月频段 1 期 ≈ 1 个月；日频段 1 期 ≈ 1 个交易日 —— 两种单位不可混用",
        }
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                                  encoding="utf-8")

        print("\n=== 月频段（1 期 ≈ 1 个月）===")
        for b in wk_blocks.BLOCK_KEYS:
            m = res[b]["monthly"]
            if m["n_runs"]:
                print(f"  {b}: run {m['n_runs']} 段 | 长度 {m['lens']} | 中位 {m['median']} "
                      f"均值 {m['mean']} p90 {m['p90']} 最大 {m['max']} | ≤2 期占比 {m['frac_short']}")
            else:
                print(f"  {b}: 月频段无报警")
        print("\n=== 观察窗口建议（P5，只按月频段）===")
        for b in wk_blocks.DOCTOR_BLOCKS:
            print(f"  {b}: {advice[b]['watch_periods']} 期 —— {advice[b]['basis']}")
        print(f"\n产物：{args.out}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
