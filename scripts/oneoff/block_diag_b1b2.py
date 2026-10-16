"""B1/B2 块级报警的**只读定位诊断**（预注册 `plans/block-diag-b1b2-prereg-20260922.md`，D1–D8）。

只回答"**哪个成员在掉、翻转还是衰减、什么时候开始、风格切换还是失效**"，
**不做任何处置**（剔除/降权 = 换 pin 级，须另立预注册）。

判据**不在本脚本里**：取数复用 `block_ic_replay_2021.build_ic_rows`（内含 `health` 三件套），
块读数走 `health.block_ic_from_ic`，窗口/基线/前瞻全部取 `health` 的常量。

用法：
    D:\\Python313\\python.exe -X utf8 scripts/oneoff/block_diag_b1b2.py
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

import numpy as np                                    # noqa: E402

from app import db                                     # noqa: E402
from app.weekly import blocks as wk_blocks             # noqa: E402
from app.weekly import health as wh                    # noqa: E402
from block_ic_replay_2021 import build_ic_rows         # noqa: E402  同一份取数实现

OUT = ROOT / "experiments" / "block_diag_b1b2.json"
CACHE = ROOT / "data" / "weekly" / "block_ic.json"
DIAG_BLOCKS = ("B1", "B2")
#: D7-③ 旁证：小盘 vs 大盘（中证500 / 沪深300）
SMALL, LARGE = "000905.SH", "000300.SH"
FLIP_MIN_ABS = 0.005          # D5：符号翻转还要求当期 |IC| 超过这个量级（否则算噪声）


def _git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return "unknown"


def _mean(ic_rows, dates, f):
    v = [ic_rows[d][f] for d in dates if f in ic_rows.get(d, {})]
    return float(np.nanmean(v)) if v else float("nan")


def _rolling(ic_rows, avail, f, win):
    """逐锚点 `win` 期滚动均值（与锚点对齐，返回 [(date, roll)]）。"""
    out = []
    for i in range(win - 1, len(avail)):
        seg = avail[i - win + 1:i + 1]
        out.append((avail[i], _mean(ic_rows, seg, f)))
    return out


def _inflection(rolls, half_line):
    """D6：滚动均值**首次**跌破 0.5×基线的锚点；以及末尾连续跌破期数。"""
    first, consec = None, 0
    for d, v in rolls:
        if np.isfinite(v) and v < half_line:
            if first is None:
                first = d
    for d, v in reversed(rolls):
        if np.isfinite(v) and v < half_line:
            consec += 1
        else:
            break
    return first, consec


def _index_proxy(conn, anchors):
    """D7-③：`index_daily` 里小盘/大盘比值的走势（数据不可得 ⇒ 如实标注）。"""
    try:
        rows = conn.execute(
            "SELECT trade_date, ts_code, close FROM index_daily WHERE ts_code IN (?,?) "
            "ORDER BY trade_date", (SMALL, LARGE)).fetchall()
    except Exception as e:
        return {"available": False, "note": f"index_daily 读取失败: {type(e).__name__}: {e}"}
    if not rows:
        return {"available": False, "note": "index_daily 无这两个指数"}
    px = {}
    for d, c, v in rows:
        px.setdefault(d, {})[c] = v
    dates = sorted(px)
    ratio = {d: (px[d][SMALL] / px[d][LARGE])
             for d in dates if SMALL in px[d] and LARGE in px[d] and px[d][LARGE]}
    if not ratio:
        return {"available": False, "note": "两指数无重叠日期"}
    use = [d for d in anchors if d in ratio]
    out = {
        "available": True,
        "note": f"{SMALL}(中证500) / {LARGE}(沪深300) 收盘比值；升=小盘相对走强",
        "first": use[0] if use else None,
        "last": use[-1] if use else None,
    }
    if len(use) >= 2:
        out["ratio_first"] = round(ratio[use[0]], 5)
        out["ratio_last"] = round(ratio[use[-1]], 5)
        out["chg_pct_over_window"] = round((ratio[use[-1]] / ratio[use[0]] - 1) * 100, 2)
    if len(use) >= 13:
        out["chg_pct_last12"] = round((ratio[use[-1]] / ratio[use[-13]] - 1) * 100, 2)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    t0 = time.time()

    members = {b: list(wk_blocks.members(b)) for b in DIAG_BLOCKS}
    universe = sorted({f for fs in members.values() for f in fs})
    print(f"诊断成员：B1={members['B1']} | B2={members['B2']}")

    conn = db.get_conn()
    try:
        # ---- D2/D3：与生产同口径取锚点（含前瞻窗 cutoff）----
        cutoff = wh._forward_cutoff(conn, wh.BLOCK_IC_HORIZON)
        total = wh.BLOCK_IC_WINDOW + wh.BLOCK_IC_BASELINE_ANCHORS + 2
        dates = wh._factor_anchors(conn, total, cutoff)
        ic_rows = build_ic_rows(conn, dates, wh.BLOCK_IC_HORIZON)
        avail = sorted(ic_rows)
        if len(avail) < wh.BLOCK_IC_WINDOW + 4:
            print("锚点不足，退出")
            return
        cur = avail[-wh.BLOCK_IC_WINDOW:]
        base = avail[:-wh.BLOCK_IC_WINDOW][-wh.BLOCK_IC_BASELINE_ANCHORS:]
        print(f"锚点 {len(dates)} 个（cutoff {cutoff}）⇒ 算出 IC {len(avail)} 个；"
              f"当期 {cur[0]}…{cur[-1]}；基线 {base[0]}…{base[-1]}")

        # ---- D4：可复现性自检（硬闸）----
        judged = wh.block_ic_from_ic(ic_rows, cur, base)
        cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
        repro = {}
        for b in DIAG_BLOCKS:
            cb = (cache.get("blocks") or {}).get(b, {})
            jb = judged["blocks"][b]
            repro[b] = {
                "cache": {"ic": cb.get("ic"), "baseline": cb.get("baseline"),
                          "ratio": cb.get("ratio"), "alarm": cb.get("alarm")},
                "calc": {"ic": jb["ic"], "baseline": jb["baseline"],
                         "ratio": jb["ratio"], "alarm": jb["alarm"]},
                "match": (cb.get("ic") == jb["ic"] and cb.get("baseline") == jb["baseline"]),
            }
            print(f"  [自检] {b}: 缓存 {cb.get('ic')}/{cb.get('baseline')} vs 现算 "
                  f"{jb['ic']}/{jb['baseline']} ⇒ {'一致' if repro[b]['match'] else '不一致'}")

        # ---- 逐成员诊断 ----
        out_members = {}
        for b in DIAG_BLOCKS:
            for f in members[b]:
                ic_cur, ic_base = _mean(ic_rows, cur, f), _mean(ic_rows, base, f)
                ratio = (ic_cur / ic_base) if (np.isfinite(ic_base) and ic_base != 0) else float("nan")
                flip = (np.isfinite(ic_cur) and np.isfinite(ic_base)
                        and np.sign(ic_cur) != np.sign(ic_base) and abs(ic_cur) > FLIP_MIN_ABS)
                verdict = ("符号翻转" if flip else
                           ("幅度衰减" if (np.isfinite(ratio) and ratio < 1) else
                            ("未劣化" if np.isfinite(ratio) else "读数不足")))
                rolls = _rolling(ic_rows, avail, f, wh.BLOCK_IC_WINDOW)
                # D6（**定向空间**，与报警同空间）：原始空间对"基线为负"的因子方向相反
                # —— 那儿的"跌破 0.5×基线"反而代表**更强**，会把拐点算反。
                s = -1.0 if (np.isfinite(ic_base) and ic_base < 0) else 1.0
                o_rolls = [(d, s * v) for d, v in rolls]
                half = 0.5 * abs(ic_base) if np.isfinite(ic_base) else float("nan")
                inflect, consec = _inflection(o_rolls, half)
                sign_hits = sum(1 for d in cur
                                if f in ic_rows.get(d, {})
                                and np.sign(ic_rows[d][f]) == np.sign(ic_base))
                sign_n = sum(1 for d in cur if f in ic_rows.get(d, {}))
                out_members[f] = {
                    "block": b,
                    "ic_current": None if not np.isfinite(ic_cur) else round(ic_cur, 5),
                    "ic_baseline": None if not np.isfinite(ic_base) else round(ic_base, 5),
                    "ratio": None if not np.isfinite(ratio) else round(ratio, 3),
                    "verdict": verdict,
                    "inflection": {"anchor": inflect, "threshold": round(half, 5),
                                   "consecutive_below": consec},
                    "sign_stability_recent": {"same_sign": sign_hits, "n": sign_n,
                                              "frac": round(sign_hits / sign_n, 3) if sign_n else None},
                    "series": [{"d": d, "ic": (None if v is None or not np.isfinite(v) else round(v, 5)),
                                "roll12": None} for d, v in
                               [(d, ic_rows.get(d, {}).get(f)) for d in avail]],
                }
                for i, (d, v) in enumerate(rolls):
                    idx = len(avail) - len(rolls) + i
                    cell = out_members[f]["series"][idx]
                    cell["roll12"] = (None if not np.isfinite(v) else round(v, 5))
                    ov = o_rolls[i][1]
                    cell["roll12_oriented"] = (None if not np.isfinite(ov) else round(ov, 5))

        # ---- D7-① 同块同步性 ----
        for b in DIAG_BLOCKS:
            fs = members[b]
            for f in fs:
                others = [o for o in fs if o != f]
                lt1 = [o for o in others
                       if out_members[o]["ratio"] is not None and out_members[o]["ratio"] < 1]
                out_members[f]["sync"] = {
                    "others": others,
                    "others_ratio_lt1": lt1,
                    "frac_others_weak": round(len(lt1) / len(others), 3) if others else None,
                    "synced": bool(others) and len(lt1) / len(others) >= 0.5,
                }

        # ---- D8 混频标注 ----
        month_end = [d for d in avail if d <= "20260731"]
        daily = [d for d in avail if d > "20260731"]
        freq = {
            "month_end_anchors": len(month_end),
            "daily_anchors_since_2026_08": len(daily),
            "note": "2026-08 起 factor 表插日频行 ⇒ 近端 12 期是**日频**，不是 12 个月",
            "current_window_dates": cur,
        }
        extra = {}
        for f in universe:
            extra[f] = {
                "ic_last12_month_end_only": (None if len(month_end) < wh.BLOCK_IC_WINDOW else
                                             round(_mean(ic_rows, month_end[-wh.BLOCK_IC_WINDOW:], f), 5)),
            }
        for f in universe:
            out_members[f].update(extra[f])

        out = {
            "meta": {
                "script": "scripts/oneoff/block_diag_b1b2.py",
                "prereg": "plans/block-diag-b1b2-prereg-20260922.md",
                "git_head": _git_head(),
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "seconds": round(time.time() - t0, 1),
                "read_only": True,
                "params": {"horizon": wh.BLOCK_IC_HORIZON, "window": wh.BLOCK_IC_WINDOW,
                           "baseline_anchors": wh.BLOCK_IC_BASELINE_ANCHORS,
                           "flip_min_abs": FLIP_MIN_ABS},
                "anchors": {"requested": len(dates), "with_ic": len(avail),
                            "cutoff": cutoff, "first": avail[0], "last": avail[-1]},
            },
            "reproduce_check": repro,
            "frequency": freq,
            "members": out_members,
            "index_proxy": _index_proxy(conn, avail),
        }
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                                  encoding="utf-8")

        print("\n=== 逐成员 ===")
        for f in universe:
            m = out_members[f]
            print(f"  {m['block']} {f:14s} 当期 {m['ic_current']} vs 基线 {m['ic_baseline']} "
                  f"= {m['ratio']}× ⇒ {m['verdict']} | 拐点 {m['inflection']['anchor']} "
                  f"(连续 {m['inflection']['consecutive_below']} 期) | 同号 "
                  f"{m['sign_stability_recent']['frac']} | 同步 {m['sync']['synced']}")
        print(f"\n产物：{args.out}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
