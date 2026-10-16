"""预注册 v2：块级读数**独立信息**判决（2022+2023 未见区间）。

预注册：`plans/block-ic-replay-v2-holdout-prereg-20260922.md`（V1–V8，跑数前已冻结）。
本脚本**不含判据**：块报警走 `app.weekly.health.block_ic_from_ic`；取数/合成/旁证**直接
import v1 脚本**（`block_ic_replay_2021`）⇒ 两份回放共用同一份实现，不会各写一套。

判决（V1 主）：2022+2023 是否存在 ≥1 个锚点 =「全模型主口径健康 且 ≥1 个 P0 块报警」。
只读：不写任何生产表。
用法：
    D:\\Python313\\python.exe -X utf8 scripts/oneoff/block_ic_replay_holdout.py
"""
from __future__ import annotations

import argparse
import json
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

from app import db                                    # noqa: E402
from app.weekly import blocks as wk_blocks            # noqa: E402
from app.weekly import health as wh                   # noqa: E402
from block_ic_replay_2021 import (                    # noqa: E402  v1 同一份实现
    build_ic_rows, composite_reading, model_score_sidecar, first_alarm_in_year,
)

OUT = ROOT / "experiments" / "block_ic_replay_holdout_2022_2023.json"
P0_BLOCKS = list(wk_blocks.DOCTOR_BLOCKS)             # B1..B4


def _git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return "unknown"


def hits_in_year(per_anchor, year):
    """V1 主判决的命中锚点：全模型主口径**健康** 且 ≥1 个 P0 块报警。"""
    out = []
    for row in per_anchor:
        if not row["date"].startswith(str(year)):
            continue
        main = row["composite"].get("main", {})
        if main.get("alarm"):
            continue
        if not main.get("ratio") and main.get("note"):
            continue                                  # 判据不适用（基线≤0）不算"健康"
        p0 = [b for b in P0_BLOCKS if row["blocks"].get(b, {}).get("alarm")]
        if p0:
            out.append({"date": row["date"], "blocks": p0,
                        "composite_ratio": main.get("ratio")})
    return out


def healthy_anchors(per_anchor, year):
    n = 0
    for row in per_anchor:
        if not row["date"].startswith(str(year)):
            continue
        main = row["composite"].get("main", {})
        if not main.get("alarm") and not main.get("note"):
            n += 1
    return n


def lead_rows(per_anchor, year, avail_all):
    """V7 辅测度：v1 的**首警提前量**口径（不参与判决）。"""
    out = []
    d_m = first_alarm_in_year(per_anchor, year, "main")
    for b in P0_BLOCKS:
        d_b = first_alarm_in_year(per_anchor, year, b)
        rec = {"block": b, "block_first_alarm": d_b, "model_first_alarm": d_m,
               "lead_days": None, "censored": False}
        if d_b and d_m:
            rec["lead_days"] = (datetime.strptime(d_m, "%Y%m%d") -
                                datetime.strptime(d_b, "%Y%m%d")).days
        elif d_b and not d_m:
            rec["lead_days"] = (datetime.strptime(f"{year}1231", "%Y%m%d") -
                                datetime.strptime(d_b, "%Y%m%d")).days
            rec["censored"] = True
        out.append(rec)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", default="20231231")
    ap.add_argument("--decision-years", default="2022,2023")
    ap.add_argument("--describe-years", default="2019,2020,2021",
                    help="已见数据 ⇒ 只作描述性对照，不参与判决")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    t0 = time.time()

    dyears = [int(y) for y in args.decision_years.split(",") if y.strip()]
    syears = [int(y) for y in args.describe_years.split(",") if y.strip()]

    conn = db.get_conn()
    try:
        anchors = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM factor WHERE trade_date<=? ORDER BY trade_date",
            (args.end,)).fetchall()]
        ic_rows = build_ic_rows(conn, anchors, wh.BLOCK_IC_HORIZON)
        avail_all = sorted(ic_rows)
        print(f"锚点 {len(anchors)} 个（{anchors[0]}…{anchors[-1]}）；"
              f"算出 IC {len(avail_all)} 个")

        per_anchor = []
        for a in avail_all:
            avail = [d for d in avail_all if d <= a]
            if len(avail) < wh.BLOCK_IC_WINDOW + 4:
                continue
            judged = wh.block_ic_from_ic(ic_rows, avail[-wh.BLOCK_IC_WINDOW:],
                                         avail[:-wh.BLOCK_IC_WINDOW][-wh.BLOCK_IC_BASELINE_ANCHORS:])
            per_anchor.append({
                "date": a, "n_avail": len(avail),
                "blocks": {b: {"alarm": v["alarm"], "ic": v["ic"], "baseline": v["baseline"],
                               "ratio": v["ratio"], "priority": v["priority"], "note": v["note"]}
                           for b, v in judged["blocks"].items()},
                "composite": {
                    "main": composite_reading(ic_rows, avail, list(wk_blocks.BLOCK_FACTORS), True),
                    "a_unoriented": composite_reading(ic_rows, avail, list(wk_blocks.BLOCK_FACTORS), False),
                    "b_p0only": composite_reading(
                        ic_rows, avail,
                        [f for b in P0_BLOCKS for f in wk_blocks.members(b)], True),
                },
            })

        # ---- 判决（只对 decision-years）----
        hits = {str(y): hits_in_year(per_anchor, y) for y in dyears}
        healthy = {str(y): healthy_anchors(per_anchor, y) for y in dyears}
        total_hits = sum(len(v) for v in hits.values())
        verdict = "PASS" if total_hits >= 1 else "NULL"

        describe = {}
        for y in syears:
            describe[str(y)] = {
                "hits": hits_in_year(per_anchor, y),
                "healthy_anchors": healthy_anchors(per_anchor, y),
                "note": "已见数据 ⇒ 只作描述性对照，不参与判决",
            }

        sidecar = model_score_sidecar(
            conn, [d for d in avail_all if any(d.startswith(str(y)) for y in dyears)],
            wh.BLOCK_IC_HORIZON)

        out = {
            "meta": {
                "script": "scripts/oneoff/block_ic_replay_holdout.py",
                "prereg": "plans/block-ic-replay-v2-holdout-prereg-20260922.md",
                "git_head": _git_head(),
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "seconds": round(time.time() - t0, 1),
                "read_only": True,
                "params": {"horizon": wh.BLOCK_IC_HORIZON, "window": wh.BLOCK_IC_WINDOW,
                           "baseline_anchors": wh.BLOCK_IC_BASELINE_ANCHORS,
                           "alarm_ratio": wh.BLOCK_IC_ALARM_RATIO,
                           "min_names": wh.BLOCK_MIN_NAMES},
                "anchors_total": len(anchors), "anchors_with_ic": len(avail_all),
            },
            "verdict_v2": {
                "verdict": verdict,
                "criterion": "≥1 个锚点：全模型主口径健康 且 ≥1 个 P0 块报警（只判存在性，无自由阈值）",
                "decision_years": [str(y) for y in dyears],
                "hits_by_year": hits,
                "healthy_anchors_by_year": healthy,
                "total_hits": total_hits,
                "lead_by_year": {str(y): lead_rows(per_anchor, y, avail_all) for y in dyears},
                "limits": ["分辨率仍是月末 12 锚点/年（≈4 周）",
                           "判决区间是连续两年 = 一个市场阶段，n≈1",
                           "PASS 只证明独立信息，不证明按它处置能改善收益/回撤"],
            },
            "descriptive_only": describe,
            "per_anchor": per_anchor,
            "model_score_sidecar": {"note": "现役模型回填 ⇒ 含前视，不作判决依据",
                                    "ic": sidecar},
        }
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                                  encoding="utf-8")

        print(f"\n判决：{verdict}（命中 {total_hits} 个锚点）")
        for y in dyears:
            print(f"  {y}: 全模型健康锚点 {healthy[str(y)]} 个；命中 "
                  f"{[h['date'] + ':' + ','.join(h['blocks']) for h in hits[str(y)]]}")
        for y in syears:
            print(f"  [描述] {y}: 健康 {describe[str(y)]['healthy_anchors']} 个；命中 "
                  f"{[h['date'] + ':' + ','.join(h['blocks']) for h in describe[str(y)]['hits']]}")
        print(f"产物：{args.out}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
