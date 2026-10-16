# -*- coding: utf-8 -*-
"""PEAD 因子族评估 —— `plans/pead-factor-prereg-20260927.md` 激活后的执行体（§二 冻结设计）。

事件强度 E(i,t,c) = Σ_{公告 ∈ [t−W, t], 类=c} valence_signed（利好 +1 / 利空 −1 / 中性 0，
等权叠加无衰减）；W ∈ {21, 42, 63} 交易日（交易日历锚定）；候选 = 14 类 × 3 窗
（Bonferroni 分母 = 实测数）。口径与门禁家族逐字一致：fe.neutralize（稀疏守卫）、
PIT 可交易域、naive 21 日标签、zone_of 分区、月末锚点 ≥2019-01。
判读 = summarize（六判据门，p_adj 分母实测）+ verdict_single（S1-S5）。
组合层档：仅当有候选过前两层时另议（26+2 REJECT 先验已登记，预注册 §五.4）。
只读零写库。产物 experiments/pead_factor_eval_20260928.json。
"""
import json
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from app.alpha import critic, gate
from app import db as app_db
from app.factors import factor_engine as fe
from app.factors.factor_engine import load_market, month_end_dates
from app.pit.engine import PITEngine

ROOT = Path(__file__).resolve().parents[2]
TAGGED = ROOT / "data" / "text_corpus" / "tagged"
OUT = ROOT / "experiments" / "pead_factor_eval_20260928.json"
PREREG = "plans/pead-factor-prereg-20260927.md"
WINDOWS = {"21": 21, "42": 42, "63": 63}
MIN_NAMES = 200


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def to_ts_code(code: str) -> str:
    c = str(code).strip()
    if c.endswith((".SZ", ".SH", ".BJ")):
        return c
    return c + (".SH" if c.startswith("6") else ".BJ" if c[0] in "489" else ".SZ")


def load_events() -> pd.DataFrame:
    parts = []
    for p in sorted(TAGGED.glob("*.parquet")):
        try:
            d = pd.read_parquet(p, columns=["代码", "公告时间", "category", "valence"])
            parts.append(d)
        except Exception as e:                                 # noqa: BLE001
            log(f"  跳过损坏文件 {p.name}: {e}")
    df = pd.concat(parts, ignore_index=True)
    df = df.rename(columns={"代码": "ts_code", "公告时间": "t", "category": "cat"})
    df["ts_code"] = df["ts_code"].map(to_ts_code)
    df["t"] = pd.to_datetime(df["t"], format="mixed").dt.normalize()
    vs = df["valence"]
    df["signed"] = np.select([vs == "pos", vs == "neg"], [1.0, -1.0], default=0.0)
    df = df[df["cat"] != "other"]
    return df


def main() -> int:
    t0 = time.time()
    m = load_market()
    md_all = month_end_dates(pd.DatetimeIndex(m["close"].index))
    md_eval = pd.DatetimeIndex([d for d in md_all if d >= pd.Timestamp("2019-01-01")])
    zones = gate.zone_of(md_eval)
    fwd = gate.forward_return(m, gate.HORIZON)
    pit = PITEngine(m=m)
    tradable = pit.tradable_matrix(md_eval)
    codes = list(m["close"].columns)
    with app_db.conn_ctx() as conn:
        industry = fe.industry_map(conn)
    code_ix = {c: i for i, c in enumerate(codes)}

    ev = load_events()
    ev = ev[ev["ts_code"].isin(code_ix)]
    ev["j"] = ev["ts_code"].map(code_ix)
    n_classes = ev["cat"].nunique()
    log(f"事件表: {len(ev):,} 条 | 类 {n_classes} | 股票 {ev['ts_code'].nunique()}")

    # 交易日轴上的逐类符号分位矩阵 → 累计和 → 窗口差分
    td = pd.DatetimeIndex(sorted(m["close"].index))
    td_ix = {d: i for i, d in enumerate(td)}
    ev["ti"] = ev["t"].map(td_ix)
    ev = ev.dropna(subset=["ti"])
    ev["ti"] = ev["ti"].astype(int)

    cands = {}
    for cat in sorted(ev["cat"].unique()):
        sub = ev[ev["cat"] == cat]
        V = np.zeros((len(td), len(codes)), dtype=np.float32)
        np.add.at(V, (sub["ti"].to_numpy(), sub["j"].to_numpy()),
                  sub["signed"].to_numpy())
        cum = np.cumsum(V, axis=0)
        for wname, W in WINDOWS.items():
            # E[anchor] = cum[anchor] − cum[anchor−W]（最近 W 个交易日内的事件和）
            E = np.full((len(md_eval), len(codes)), np.nan, dtype=np.float32)
            for i, d in enumerate(md_eval):
                if d not in td_ix:
                    continue
                k = td_ix[d]
                k0 = k - W
                lo = cum[k0] if k0 >= 0 else np.zeros(len(codes), dtype=np.float32)
                E[i] = cum[k] - lo
            cands[f"{cat}_{wname}"] = pd.DataFrame(E, index=md_eval, columns=codes)
    log(f"候选面板 {len(cands)} 个（类 × 窗）({time.time()-t0:.0f}s)")

    n_test = len(cands)
    rows = []
    for name, panel in cands.items():
        sr = float(fe.sparse_ratio(panel))
        sub = (fe.neutralize_sparse(panel, industry) if sr < fe.SPARSE_NONZERO_RATIO
               else fe.neutralize(panel, industry))
        cost = critic.churn_cost(sub, md_eval)
        pm = sub.where(tradable.reindex(index=md_eval, columns=codes).fillna(False))
        det = gate.evaluate(pm, fwd, md_eval, zones)
        if det.empty:
            rows.append({"factor": name, "note": "无有效截面", "sparse_ratio": round(sr, 4)})
            continue
        dead, act = det[det.zone == "DEAD"], det[det.zone == "ACTIVE"]
        h_d = float(dead.head_excess.mean())
        t_d = float(h_d / (dead.head_excess.std(ddof=1) / np.sqrt(len(dead))))
        h_a = float(act.head_excess.mean())
        rec = gate.summarize(name, det, n_tests=n_test,
                             cost_per_month=(cost or {}).get("cost_per_month"))
        v5 = gate.verdict_single(rec["DEAD_head"], rec["DEAD_head_t"], rec["ACTIVE_head"],
                                 half_life_months=None)   # S1-S4（时效轴留待持有期匹配检验）
        rows.append({"factor": name, "sparse_ratio": round(sr, 4),
                     "DEAD_head": round(rec["DEAD_head"], 4),
                     "DEAD_head_t": round(rec["DEAD_head_t"], 2),
                     "ACTIVE_head": round(rec["ACTIVE_head"], 4),
                     "p_adj": round(rec["p_adj"], 4) if rec["p_adj"] == rec["p_adj"] else None,
                     "DEAD_top6": round(rec["DEAD_top6"], 4),
                     "cost_per_month": (cost or {}).get("cost_per_month"),
                     "gate6": {k: rec[k] for k in rec if k.startswith("c_") or k == "PASS"},
                     "s1_s4": {"s1_head": v5["s1_head"], "s2_t": v5["s2_t"],
                               "s3_same_sign": v5["s3_same_sign"],
                               "PASS_single_s1s4": v5["PASS_single"]},
                     "PASS_gate6": bool(rec["PASS"])})
        log(f"  {name:28s} DEAD head {h_d:+.4f} t {t_d:+.2f} "
            f"gate6 {rec['PASS']} S1-S4 {v5['PASS_single']}")

    n_gate6 = sum(1 for r in rows if r.get("PASS_gate6"))
    n_s14 = sum(1 for r in rows if r.get("s1_s4", {}).get("PASS_single_s1s4"))
    out = {"ran_at": datetime.now().isoformat(timespec="seconds"), "prereg": PREREG,
           "n_candidates": n_test, "n_events": int(len(ev)),
           "n_classes": int(n_classes),
           "windows": WINDOWS,
           "rows": rows,
           "verdict": {"n_pass_gate6": n_gate6, "n_pass_s1s4": n_s14,
                       "note": "readings; 组合层档仅在候选过前两层且拍板后另议"
                               "（26+2 REJECT 先验在册）"},
           "runtime_sec": round(time.time() - t0, 1)}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"判决读数: 六判据门 {n_gate6}/{n_test} | 单因子档 S1-S4 {n_s14}/{n_test}")
    print(f"产物: {OUT}  ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
