"""块级 IC 的**窗长/前瞻口径**研究（预注册 `plans/block-ic-window-calib-prereg-20260922.md`，W1–W7）。

只**测量**：日频段 vs 月频段的 报警占比（M1）与 状态抖动（M2），外加 horizon=10/21 的稳健性对照。
**不改任何生产参数**（改 window/horizon = 改判据 = 须另立预注册 + 用户拍板）。

判据不在本脚本：取数复用 `block_ic_replay_2021.build_ic_rows`，块读数走 `health.block_ic_from_ic`。

用法：
    D:\\Python313\\python.exe -X utf8 scripts/oneoff/block_ic_window_study.py
"""
from __future__ import annotations

import argparse
import json
import math
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

OUT = ROOT / "experiments" / "block_ic_window_study.json"
MAIN_HORIZON = wh.BLOCK_IC_HORIZON          # W1：主口径 = 生产值 5
ALT_HORIZONS = (10, 21)                     # W7：只报不判
P0 = list(wk_blocks.DOCTOR_BLOCKS)          # B1..B4（主看 B1/B2）
DAILY_FROM = "20260803"                     # W3：日频段起点
MONTHLY_UNTIL = "20260731"                  # W3：月频段终点
ALARM_FRAC_MIN = 0.50                       # W5 门槛
FLIP_RATE_MIN = 0.30                        # W5 门槛


def _git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return "unknown"


def _stats(flags):
    """M1 报警占比 / M2 状态抖动（W4）。"""
    n = len(flags)
    if n == 0:
        return {"n": 0, "alarm_frac": None, "flips": None, "flip_rate": None}
    flips = sum(1 for i in range(1, n) if flags[i] != flags[i - 1])
    return {"n": n, "alarm_frac": round(sum(1 for f in flags if f) / n, 3),
            "flips": flips,
            "flip_rate": (None if n < 2 else round(flips / (n - 1), 3))}


def run_horizon(conn, horizon, window, base_n):
    """逐 asof 滚动重算块报警（W2），返回 {block: [(asof, alarm)]}。"""
    cutoff = wh._forward_cutoff(conn, horizon)
    anchors = wh._factor_anchors(conn, 0, cutoff) if False else [
        r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM factor WHERE trade_date<=? ORDER BY trade_date",
            (cutoff,)).fetchall()]
    ic_rows = build_ic_rows(conn, anchors, horizon)
    avail = sorted(ic_rows)
    series = {b: [] for b in wk_blocks.BLOCK_KEYS}
    for i in range(window + base_n, len(avail)):
        cur = avail[i - window:i]
        base = avail[i - window - base_n:i - window]
        judged = wh.block_ic_from_ic(ic_rows, cur, base)
        for b in wk_blocks.BLOCK_KEYS:
            series[b].append((avail[i], bool(judged["blocks"][b]["alarm"])))
    return avail, series


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    t0 = time.time()
    window, base_n = wh.BLOCK_IC_WINDOW, wh.BLOCK_IC_BASELINE_ANCHORS

    conn = db.get_conn()
    try:
        report = {}
        for h in (MAIN_HORIZON,) + ALT_HORIZONS:
            avail, series = run_horizon(conn, h, window, base_n)
            seg = {}
            for name, pred in (("monthly", lambda d: d <= MONTHLY_UNTIL),
                               ("daily", lambda d: d >= DAILY_FROM)):
                seg[name] = {b: _stats([a for d, a in series[b] if pred(d)])
                             for b in wk_blocks.BLOCK_KEYS}
            # W6：有效样本（日频：锚点间隔 1 日）
            last_cur = avail[-window:]
            days = (datetime.strptime(last_cur[-1], "%Y%m%d") -
                    datetime.strptime(last_cur[0], "%Y%m%d")).days + 1
            report[str(h)] = {
                "horizon": h, "window": window, "baseline_anchors": base_n,
                "asof_count": len(avail), "asof_first": avail[0] if avail else None,
                "asof_last": avail[-1] if avail else None,
                "segments": seg,
                "effective_sample": {
                    "window_natural_days": days,
                    "non_overlapping_fwd_windows": math.ceil(window / h),
                    "note": "日频锚点间隔 1 日、前瞻 %d 日 ⇒ 相邻锚点样本重叠" % h,
                },
            }
            print(f"horizon={h}: asof {len(avail)} 个（{avail[0]}…{avail[-1]}）| "
                  f"日频段 B1 报警占比 {seg['daily']['B1']['alarm_frac']} 抖动 "
                  f"{seg['daily']['B1']['flip_rate']} | B2 "
                  f"{seg['daily']['B2']['alarm_frac']}/{seg['daily']['B2']['flip_rate']} | "
                  f"窗口 {days} 天、不重叠前瞻窗 {report[str(h)]['effective_sample']['non_overlapping_fwd_windows']}")

        # W5：门槛判定（只对主口径 horizon=5 的日频段）
        d5 = report[str(MAIN_HORIZON)]["segments"]["daily"]
        allergic = []
        for b in P0:
            s = d5[b]
            hit = ((s["alarm_frac"] is not None and s["alarm_frac"] >= ALARM_FRAC_MIN) or
                   (s["flip_rate"] is not None and s["flip_rate"] >= FLIP_RATE_MIN))
            if hit:
                allergic.append(b)
        focus = [b for b in allergic if b in ("B1", "B2")]
        verdict = ("建议立项改窗长/前瞻" if focus else
                   ("有 P0 块过敏（非 B1/B2）" if allergic else "暂不需要改"))

        out = {
            "meta": {
                "script": "scripts/oneoff/block_ic_window_study.py",
                "prereg": "plans/block-ic-window-calib-prereg-20260922.md",
                "git_head": _git_head(),
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "seconds": round(time.time() - t0, 1),
                "read_only": True,
                "thresholds": {"alarm_frac_min": ALARM_FRAC_MIN, "flip_rate_min": FLIP_RATE_MIN},
                "note": "改 window/horizon = 改判据 ⇒ 本轮只测量不修改",
            },
            "by_horizon": report,
            "verdict": {
                "verdict": verdict,
                "allergic_p0_blocks": allergic,
                "focus_allergic": focus,
                "daily_stats_horizon5": {b: d5[b] for b in wk_blocks.BLOCK_KEYS},
                "monthly_stats_horizon5": report[str(MAIN_HORIZON)]["segments"]["monthly"],
            },
        }
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                                  encoding="utf-8")
        print(f"\n判决：{verdict}（过敏 P0 块 {allergic}；其中 B1/B2 {focus}）")
        print(f"产物：{args.out}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
