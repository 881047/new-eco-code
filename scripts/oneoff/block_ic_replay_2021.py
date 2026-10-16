"""2021 年块级 IC 预警有效性回放（**只读**：不写任何生产表）。

判据**不在这里**：块报警一律走 `app.weekly.health.block_ic_from_ic`；
本脚本只做「取数 + 逐锚点循环 + 合成全模型读数 + 落盘」。

预注册：`plans/block-ic-replay-2021-prereg-20260922.md`（R1–R9，跑数前已冻结）。
用法：
    D:\\Python313\\python.exe -X utf8 scripts/oneoff/block_ic_replay_2021.py
    D:\\Python313\\python.exe -X utf8 scripts/oneoff/block_ic_replay_2021.py --years 2021
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

import numpy as np                                   # noqa: E402
import pandas as pd                                  # noqa: E402

from app import db                                    # noqa: E402
from app.weekly import blocks as wk_blocks            # noqa: E402
from app.weekly import health as wh                   # noqa: E402

OUT = ROOT / "experiments" / "block_ic_replay_2021.json"

ALL_FACTORS = list(wk_blocks.BLOCK_FACTORS)
P0_FACTORS = [f for b in wk_blocks.DOCTOR_BLOCKS for f in wk_blocks.members(b)]


def _git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return "unknown"


def build_ic_rows(conn, anchors, horizon):
    """逐锚点因子 IC —— 复用生产取数三件套，口径与 `health.block_ic` 完全一致。"""
    rows = {}
    for d in anchors:
        w = wh._cross_section(conn, d, ALL_FACTORS)
        if w.empty:
            continue
        r = wh._fwd_excess(conn, d, horizon)
        if len(r) < wh.BLOCK_MIN_NAMES:
            continue
        row = wh._factor_ic_row(w, r)
        if row:
            rows[d] = row
    return rows


def _sign_map(ic_rows, base_dates, factors):
    """定向符号：**与 `health.block_ic_from_ic` 同一规则**（基线期 IC 为负 ⇒ 定向 −1）。

    只为合成"全模型"读数而重算一遍符号；报警比较用的 window/baseline/ratio 全部
    直接引用 `health` 的常量，不在本文件另立一份。
    """
    out = {}
    for f in factors:
        v = [ic_rows[d][f] for d in base_dates if f in ic_rows.get(d, {})]
        m = float(np.nanmean(v)) if v else np.nan
        out[f] = -1.0 if (np.isfinite(m) and m < 0) else 1.0
    return out


def composite_reading(ic_rows, avail, factors, oriented=True):
    """全模型读数：window 期均值 / baseline 期均值 / 报警 —— 判据与块级同形。"""
    window, base_n, ratio = wh.BLOCK_IC_WINDOW, wh.BLOCK_IC_BASELINE_ANCHORS, wh.BLOCK_IC_ALARM_RATIO
    cur, base = avail[-window:], avail[:-window][-base_n:]
    sign = _sign_map(ic_rows, base, factors)

    def _mean(dates):
        vals = [(sign[f] if oriented else 1.0) * ic_rows[d][f]
                for d in dates for f in factors if f in ic_rows.get(d, {})]
        return float(np.nanmean(vals)) if vals else float("nan")

    c, bl = _mean(cur), _mean(base)
    res = {"ic": None, "baseline": None, "ratio": None, "alarm": False, "note": ""}
    if not np.isfinite(c) or not np.isfinite(bl):
        res["note"] = "读数不足"
        return res
    res["ic"] = round(c, 5)
    res["baseline"] = round(bl, 5)
    if bl <= 0:
        res["note"] = f"基线 {bl:.4f} ≤ 0 ⇒ {ratio}× 判据不适用（如实标注，不按健康处理）"
        return res
    res["ratio"] = round(c / bl, 3)
    res["alarm"] = bool(c < ratio * bl)
    return res


def model_score_sidecar(conn, anchors, horizon):
    """旁证（**不作判决**）：现役模型回填的 `model_score` 秩 IC —— 含前视风险，只留档。"""
    out = {}
    for d in anchors:
        s = pd.read_sql("SELECT ts_code, score FROM model_score WHERE trade_date=?",
                        conn, params=(d,)).set_index("ts_code")["score"]
        r = wh._fwd_excess(conn, d, horizon)
        m = s.notna() & r.notna()
        if int(m.sum()) < wh.BLOCK_MIN_NAMES:
            continue
        rr = r[m].rank()
        v = float(s[m].rank().corr(rr))
        if np.isfinite(v):
            out[d] = round(v, 5)
    return out


def first_alarm_in_year(per_anchor, year, key):
    """某年首次报警锚点（key: 块名 / 'main' / 'a' / 'b'）。"""
    for row in per_anchor:
        if not row["date"].startswith(str(year)):
            continue
        if key in row["blocks"]:
            if row["blocks"][key].get("alarm"):
                return row["date"]
        elif row["composite"].get(key, {}).get("alarm"):
            return row["date"]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2019,2020,2021",
                    help="判决年=2021；其余年份作对照（不改判决）")
    ap.add_argument("--end", default="20211231")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    t0 = time.time()
    years = [int(y) for y in args.years.split(",") if y.strip()]
    conn = db.get_conn()
    try:
        anchors = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM factor WHERE trade_date<=? ORDER BY trade_date",
            (args.end,)).fetchall()]
        print(f"锚点 {len(anchors)} 个：{anchors[0]} … {anchors[-1]}")

        ic_rows = build_ic_rows(conn, anchors, wh.BLOCK_IC_HORIZON)
        avail_all = sorted(ic_rows)
        print(f"算出 IC 的锚点 {len(avail_all)} 个（{avail_all[0]} … {avail_all[-1]}）")

        per_anchor = []
        for a in avail_all:
            avail = [d for d in avail_all if d <= a]
            if len(avail) < wh.BLOCK_IC_WINDOW + 4:      # 与生产同门槛（<16 不判）
                continue
            judged = wh.block_ic_from_ic(ic_rows, avail[-wh.BLOCK_IC_WINDOW:],
                                         avail[:-wh.BLOCK_IC_WINDOW][-wh.BLOCK_IC_BASELINE_ANCHORS:])
            per_anchor.append({
                "date": a,
                "n_avail": len(avail),
                "blocks": {b: {"alarm": v["alarm"], "ic": v["ic"], "baseline": v["baseline"],
                               "ratio": v["ratio"], "priority": v["priority"], "note": v["note"]}
                           for b, v in judged["blocks"].items()},
                "composite": {
                    "main": composite_reading(ic_rows, avail, ALL_FACTORS, oriented=True),
                    "a_unoriented": composite_reading(ic_rows, avail, ALL_FACTORS, oriented=False),
                    "b_p0only": composite_reading(ic_rows, avail, P0_FACTORS, oriented=True),
                },
            })
            if a.startswith("2021") or a.startswith("2020"):
                print(f"  {a}: 块报警={judged['alarms']} | 全模型 main="
                      f"{per_anchor[-1]['composite']['main']['ratio']} "
                      f"alarm={per_anchor[-1]['composite']['main']['alarm']}")

        sidecar = model_score_sidecar(conn, [d for d in avail_all if d.startswith("2021")],
                                      wh.BLOCK_IC_HORIZON)

        # ---- 判决（只对 2021）----
        lead_rows = []
        for b in wk_blocks.DOCTOR_BLOCKS:                      # 只 P0 块参与判决
            d_b = first_alarm_in_year(per_anchor, 2021, b)
            d_m = first_alarm_in_year(per_anchor, 2021, "main")
            rec = {"block": b, "block_first_alarm": d_b, "model_first_alarm": d_m,
                   "lead_days": None, "lead_weeks": None, "lead_anchors": None, "censored": False}
            if d_b and d_m:
                days = (datetime.strptime(d_m, "%Y%m%d") - datetime.strptime(d_b, "%Y%m%d")).days
                rec["lead_days"] = days
                rec["lead_weeks"] = days // 7
                rec["lead_anchors"] = (avail_all.index(d_m) - avail_all.index(d_b))
            elif d_b and not d_m:
                days = (datetime.strptime("20211231", "%Y%m%d") -
                        datetime.strptime(d_b, "%Y%m%d")).days
                rec["censored"] = True
                rec["lead_days"] = days
                rec["lead_weeks"] = days // 7
            lead_rows.append(rec)

        passed = [r for r in lead_rows
                  if r["lead_days"] is not None and r["lead_days"] >= 28]
        verdict = "PASS" if passed else "NULL"
        if passed and any(r["censored"] for r in passed):
            verdict = "PASS(censored)"

        control = {}
        for y in years:
            if y == 2021:
                continue
            control[str(y)] = {
                "blocks": {b: first_alarm_in_year(per_anchor, y, b) for b in wk_blocks.BLOCK_KEYS},
                "composite_main": first_alarm_in_year(per_anchor, y, "main"),
            }

        out = {
            "meta": {
                "script": "scripts/oneoff/block_ic_replay_2021.py",
                "prereg": "plans/block-ic-replay-2021-prereg-20260922.md",
                "git_head": _git_head(),
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "seconds": round(time.time() - t0, 1),
                "read_only": True,
                "params": {"horizon": wh.BLOCK_IC_HORIZON, "window": wh.BLOCK_IC_WINDOW,
                           "baseline_anchors": wh.BLOCK_IC_BASELINE_ANCHORS,
                           "alarm_ratio": wh.BLOCK_IC_ALARM_RATIO,
                           "min_names": wh.BLOCK_MIN_NAMES},
                "n_factors": len(ALL_FACTORS), "n_p0_factors": len(P0_FACTORS),
                "anchors_total": len(anchors), "anchors_with_ic": len(avail_all),
            },
            "per_anchor": per_anchor,
            "verdict_2021": {
                "verdict": verdict,
                "lead_by_block": lead_rows,
                "passing_blocks": [r["block"] for r in passed],
                "resolution_note": "月频网格：1 锚点 ≈ 4 周 ⇒ 只能证明「早 ≥1 个锚点/≥28 自然日」，"
                                   "不能给出更细的提前量估计（预注册约束 A）",
                "scale_note": "2021 的 window 12 期 = 12 个月；生产当前 12 期 ≈ 12 个自然日 "
                              "⇒ 验证的是判据结构，不是生产日频窗长（预注册约束 B）",
            },
            "control_years": control,
            "model_score_sidecar_2021": {
                "note": "现役模型回填 ⇒ 含前视，不作判决依据",
                "ic": sidecar,
            },
        }
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                                  encoding="utf-8")
        print(f"\n判决：{verdict}（通过块 {[r['block'] for r in passed]}）")
        for r in lead_rows:
            print(f"  {r['block']}: 块首警 {r['block_first_alarm']} | 全模型首警 "
                  f"{r['model_first_alarm']} | 提前 {r['lead_days']} 天 / "
                  f"{r['lead_weeks']} 周{'（右截尾）' if r['censored'] else ''}")
        print(f"产物：{args.out}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
