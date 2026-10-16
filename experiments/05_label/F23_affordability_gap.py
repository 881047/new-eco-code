"""F23 · 可负担性缺口 —— 新账户只投出 64% 的成因与对策（预注册）

## 缺口

2026-09-13 实跑: 新构型(2×W5+pit)在 100 万 / top100 下, 锚点 20260911 的方案只投出 **63.5 万**
（85 只; 15 只因"一手都买不起"被丢弃）。而回测是从 100 万**复利到上千万**的,
每只预算从 8,730 涨到 ~8 万, 早年才受这个约束 ⇒ **回测的平均投入率高于新账户的起点**。
⇒ 新账户的实际敞口 ≈ 方案的投入率/0.873, 比回测读数低一截; 这是"回测 vs 实盘"最后一道已知差距。

## 三个候选对策（都在**选股阶段**动手, 不动费用/撮合）

    A0 现状      : 买不起的票在**买入时**才跳过 ⇒ 名额空置、钱留现金(回测口径)
    A1 价格上限  : 选股阶段就要求 `价格×100 ≤ 1.25 × 等权预算`（把高价票换成下一名）
    A2 价格上限  : 同上, 倍数 1.5
    A3 价格上限  : 同上, 倍数 2.0
    A4 顺延候选  : 不做价格上限, 但买不起时把名额**顺延**给下一名(等价于 A1 的软版本)

## 预注册判据

    采纳 = 两套种子上同时: ① 超额 ≥ A0 + 0.5pp; ② MDD 不劣于 A0 的 1.10 倍;
    ③ **投入率 ≥ 85%**(这是本实验的目的, 单列)。
    多个臂满足 ⇒ 取两套种子超额均值最高者。

用法:
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F23_affordability_gap.py
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

log = logging.getLogger("f23")
OUT = HERE / "F23_affordability_gap_result.json"
CAPITAL = 1_000_000.0
TOP_N, BAND, HYST = 100, 8.0, 0.2
SEED_FILES = {"seedA": "F4_scores_seedA.npz", "seedB": "F4_scores_seedB.npz"}
PIT_TABLE = "model_score_die_pit"


def wr(*pairs):
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
    PITD = f17.table_scores(conn, PIT_TABLE, wk, cols)
    conn.close()
    lab = f1b.forward_excess_open(m, mask, 5)
    Yr = f1._rank(lab)
    okv = lab.notna().to_numpy()
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    mask_np = mask.to_numpy()

    arms = {
        "A0_现状(买入时跳过)": dict(),
        "A1_价格上限1.25x": dict(max_lot_cost_mult=1.25),
        "A2_价格上限1.5x": dict(max_lot_cost_mult=1.5),
        "A3_价格上限2.0x": dict(max_lot_cost_mult=2.0),
        "A4_顺延候选": dict(fill_through=True),
    }
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "capital": CAPITAL, "seeds": {}, "verdict": {}, "secs": None}
    for seed, fn in SEED_FILES.items():
        z = np.load(HERE / fn, allow_pickle=True)
        a5 = pd.to_datetime([str(d) for d in z["anchors"]])
        c5 = [str(c) for c in z["cols"]]
        W5 = pd.DataFrame(z["scores"], index=a5, columns=c5).reindex(index=wk, columns=cols)
        comp = wr((W5, 2), (PITD, 1))
        rows = {}
        for key, kw in arms.items():
            df = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, {**cfg, "capital": CAPITAL}, pit, 0,
                                           score_matrix=pd.DataFrame(comp, index=wk, columns=cols),
                                           top_n=TOP_N, rank_band=BAND, hyst=HYST, **kw)
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
            ok = df["ret"].notna() & df["bench_ret"].notna()
            a_, b_ = f1b.ann(df.loc[ok, "ret"], 52.0), f1b.ann(df.loc[ok, "bench_ret"], 52.0)
            nav = (1 + df.loc[ok, "ret"]).cumprod()
            rows[key] = {"n": int(ok.sum()), "excess_pp": round(100 * (a_ - b_), 2),
                         "ann": round(a_, 4),
                         "mdd": round(float((nav / nav.cummax() - 1).min()), 4),
                         "invested_ratio": round(float(df.loc[ok, "invested_ratio"].mean()), 4),
                         "avg_names": round(float(df.loc[ok, "n"].mean()), 1),
                         "turnover": round(float(df.loc[ok, "turnover"].mean()), 3)}
            d = rows[key]
            log.info("[%s|%s] 超额 %+.2fpp | 年化 %+.1f%% | MDD %.1f%% | **投入率 %.1f%%** | "
                     "均持仓 %.0f 只 | 换手 %.3f",
                     seed, key, d["excess_pp"], 100 * d["ann"], 100 * d["mdd"],
                     100 * d["invested_ratio"], d["avg_names"], d["turnover"])
        res["seeds"][seed] = rows

    a, b = res["seeds"]["seedA"], res["seeds"]["seedB"]
    base = "A0_现状(买入时跳过)"
    cands = []
    for key in a:
        if key == base or key not in b:
            continue
        if (a[key]["excess_pp"] >= a[base]["excess_pp"] + 0.5
                and b[key]["excess_pp"] >= b[base]["excess_pp"] + 0.5
                and a[key]["mdd"] >= a[base]["mdd"] * 1.10
                and b[key]["mdd"] >= b[base]["mdd"] * 1.10
                and a[key]["invested_ratio"] >= 0.85 and b[key]["invested_ratio"] >= 0.85):
            cands.append((key, round((a[key]["excess_pp"] + b[key]["excess_pp"]) / 2, 2),
                          round(100 * a[key]["invested_ratio"], 1)))
    cands.sort(key=lambda kv: -kv[1])
    res["verdict"] = {"baseline": {"seedA_excess": a[base]["excess_pp"],
                                   "seedB_excess": b[base]["excess_pp"],
                                   "seedA_invested": a[base]["invested_ratio"],
                                   "seedB_invested": b[base]["invested_ratio"]},
                      "candidates": cands, "adopt": cands[0][0] if cands else None}
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 判决: 采纳 = %s | 基线投入率 %.1f%%/%.1f%% | %.1fs → %s ==",
             res["verdict"]["adopt"] or "维持现状",
             100 * a[base]["invested_ratio"], 100 * b[base]["invested_ratio"],
             res["secs"], OUT)


if __name__ == "__main__":
    main()
