"""F22 · 终选：W5+pit 之后还要不要 M —— 预注册（两套种子）

## 背景

F21 的固定配对矩阵（两套种子、491 期同窗口）给出：

    W5+M(现役)   +12.02 / +11.52pp     W5+head_vt +10.78 / +11.31pp(动态 D 臂的增益是选择运气)
    W5+pit       +13.16 / +12.48pp ← **唯一两套种子都过线**   W5+snap +13.00 / +12.26pp(IC 差一点)

⇒ 现役的第二路 M 应该换成 **pit**（申万一级 PIT 时点行业中性化的同一套模型, 数据与映射函数
都已在本地: `sw_member` + `holder_factors.sw_industry_map_pit`）。

## 本实验(唯一问题: 换掉 M 之后, 还要不要 M 当第三路?)

    A0 W5+M                (旧现役, 基准)
    A1 W5+pit              (F21 胜者)
    A2 W5+pit+M            (三等权)
    A3 W5+pit+snap         (三等权)
    A4 2×W5 + 1×pit        (向 W5 倾斜)

## 预注册判据

    采纳 = 两套种子上都满足: 超额 ≥ A0 + 0.5pp, MDD 不劣于 A0 的 1.10 倍, 且 IC ≥ A0 的 IC。
    多个臂同时满足 ⇒ 取**两套种子超额均值最高**者(平手取 die 数更少者, 更简单)。

用法:
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F22_pair_final_selection.py
"""
import hashlib
import importlib.util
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent
sys.path.insert(0, str(BASE))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


f1 = _load("f1", HERE / "F1_weekly_ic_decay.py")
f1b = _load("f1b", HERE / "F1b_weekly_open_overlap.py")
f17 = _load("f17", HERE / "F17_die_registry_kgd.py")

from app import config as cfg_mod            # noqa: E402
from app.factors import factor_engine as fe  # noqa: E402
from app.pit.engine import PITEngine         # noqa: E402

log = logging.getLogger("f22")
OUT = HERE / "F22_pair_final_selection_result.json"
CAPITAL = 1_000_000.0
TOP_N, BAND, HYST = 100, 8.0, 0.2
SEED_FILES = {"seedA": "F4_scores_seedA.npz", "seedB": "F4_scores_seedB.npz"}


def wr(*pairs):
    """加权分位排名平均: pairs = ((df, w), ...)。"""
    acc, tot = None, 0.0
    for df, w in pairs:
        r = pd.DataFrame(df).rank(axis=1, pct=True) * w
        acc = r if acc is None else acc + r
        tot += w
    return acc / tot


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    pit = PITEngine(m=m, cfg=cfg)
    cols = list(m["close"].columns)
    mask = f1.tradable_frame(pit, wk, cols)
    conn = sqlite3.connect(f"file:{BASE/'data'/'neweco.db'}?mode=ro", uri=True)
    M = f17.table_scores(conn, "model_score", wk, cols)
    PIT = f17.table_scores(conn, "model_score_die_pit", wk, cols)
    SNAP = f17.table_scores(conn, "model_score_die_snap", wk, cols)
    conn.close()
    lab = f1b.forward_excess_open(m, mask, 5)
    Yr = f1._rank(lab)
    okv = lab.notna().to_numpy()
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    mask_np = mask.to_numpy()

    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "seeds": {}, "verdict": {}, "secs": None}
    for seed, fn in SEED_FILES.items():
        z = np.load(HERE / fn, allow_pickle=True)
        a5 = pd.to_datetime([str(d) for d in z["anchors"]])
        c5 = [str(c) for c in z["cols"]]
        W5 = pd.DataFrame(z["scores"], index=a5, columns=c5).reindex(index=wk, columns=cols)
        arms = {"A0_W5+M": wr((W5, 1), (M, 1)),
                "A1_W5+pit": wr((W5, 1), (PIT, 1)),
                "A2_W5+pit+M": wr((W5, 1), (PIT, 1), (M, 1)),
                "A3_W5+pit+snap": wr((W5, 1), (PIT, 1), (SNAP, 1)),
                "A4_2W5+pit": wr((W5, 2), (PIT, 1))}
        series = {}
        for key, Sdf in arms.items():
            Xr = f1._rank(Sdf)
            s_ic = pd.Series(f1.row_ic(Xr, Yr, okv & np.isfinite(Xr)), index=wk).dropna()
            df = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, {**cfg, "capital": CAPITAL}, pit, 0,
                                           score_matrix=pd.DataFrame(Sdf, index=wk, columns=cols),
                                           top_n=TOP_N, rank_band=BAND, hyst=HYST)
            if df is None:
                continue
            bench, prev_exec, prev_ap = [], None, None
            for _, r in df.iterrows():
                e = pos_of[r["date"]]
                if prev_exec is None or prev_ap is None:
                    bench.append(np.nan)
                else:
                    good = (mask_np[int(prev_ap)] & np.isfinite(op_adj[prev_exec])
                            & np.isfinite(op_adj[e]) & (op_adj[prev_exec] > 0))
                    p0, p1 = op_adj[prev_exec], op_adj[e]
                    bench.append(float(np.mean(p1[good] / p0[good] - 1)) if good.sum() >= 200
                                 else np.nan)
                prev_exec, prev_ap = e, int(r["anchor_pos"])
            df["bench_ret"] = bench
            series[key] = (df[["date", "ret", "bench_ret", "turnover"]],
                           round(float(s_ic.mean()), 4) if len(s_ic) else None)
        common = None
        for s, _ in series.values():
            ds = set(pd.to_datetime(s.loc[s["ret"].notna() & s["bench_ret"].notna(), "date"]))
            common = ds if common is None else (common & ds)
        common = sorted(common or [])
        rows = {}
        for key, (s, ic) in series.items():
            s2 = s.copy()
            s2["date"] = pd.to_datetime(s2["date"])
            s2 = s2[s2["date"].isin(set(common))].dropna(subset=["ret", "bench_ret"])
            a_, b_ = f1b.ann(s2["ret"], 52.0), f1b.ann(s2["bench_ret"], 52.0)
            nav = (1 + s2["ret"]).cumprod()
            rows[key] = {"n": int(len(s2)), "excess_pp": round(100 * (a_ - b_), 2),
                         "ann": round(a_, 4), "mdd": round(float((nav / nav.cummax() - 1).min()), 4),
                         "turnover": round(float(s2["turnover"].mean()), 3), "ic": ic}
            log.info("[%s|%s] 超额 %+.2fpp | 年化 %+.1f%% | MDD %.1f%% | IC %s",
                     seed, key, rows[key]["excess_pp"], 100 * rows[key]["ann"],
                     100 * rows[key]["mdd"], ic)
        res["seeds"][seed] = {"common_window": len(common), "arms": rows}

    a, b = res["seeds"]["seedA"]["arms"], res["seeds"]["seedB"]["arms"]
    cands = []
    for key in a:
        if key == "A0_W5+M" or key not in b:
            continue
        if (a[key]["excess_pp"] >= a["A0_W5+M"]["excess_pp"] + 0.5
                and b[key]["excess_pp"] >= b["A0_W5+M"]["excess_pp"] + 0.5
                and a[key]["mdd"] >= a["A0_W5+M"]["mdd"] * 1.10
                and b[key]["mdd"] >= b["A0_W5+M"]["mdd"] * 1.10
                and (a[key]["ic"] or 0) >= (a["A0_W5+M"]["ic"] or 0)):
            cands.append((key, (a[key]["excess_pp"] + b[key]["excess_pp"]) / 2))
    cands.sort(key=lambda kv: -kv[1])
    res["verdict"] = {"baseline": {"seedA": a["A0_W5+M"]["excess_pp"], "seedB": b["A0_W5+M"]["excess_pp"]},
                      "candidates": cands, "adopt": cands[0][0] if cands else None}
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 判决: 采纳 = %s | 候选 = %s | %.1fs → %s ==",
             res["verdict"]["adopt"] or "维持 W5+M", cands, res["secs"], OUT)


if __name__ == "__main__":
    main()
