# -*- coding: utf-8 -*-
"""S5 时效轴（IC 半衰期）重测 —— `plans/s5-half-life-prereg-20260927.md` 的执行体。

范围（预注册 §三，跑前冻结）:
  ① 31 在池因子全量（HL 分布 = bar 的校准读数）;
  ② bank 单因子档过档 50（single_factor_retest_20260926.json 的 pass_list）——
     S5 只对 S1-S4 过档者有判决后果。合计 81 面板。
口径（预注册 §二）: lag 变体执行延迟衰减曲线（ic_by_lag，L=0..5 锚点步数）、
  naive fwd21、PIT 可交易域、判定区 DEAD（ACTIVE 只记读数）、与重测同面板不另行中性化。
交叉校验: 8 个池过档因子的 IC_0 与 gate.evaluate DEAD 区 mean(ic_full) 比对，max|Δ|<1e-9。
本轮只测量与分级: 不重写 20260926 重测的官方判决，产物给出"若并入 S5 将翻转的名单"作为读数。
产物 experiments/s5_decay_retest_20260927.json。
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app.alpha import gate
from app.factors.factor_engine import load_market, month_end_dates
from app.models import trainer as T
from app.pit.engine import PITEngine

ROOT = Path(__file__).resolve().parents[2]
POOL = ROOT / "data" / "alpha_pool"
RETEST = ROOT / "experiments" / "single_factor_retest_20260926.json"
OUT = ROOT / "experiments" / "s5_decay_retest_20260927.json"
XCHK_TOL = 1e-9


def _curve_dict(cur: dict) -> dict:
    return {int(L): {"ic": (float(s.mean()) if len(s) else None), "n": int(len(s))}
            for L, s in cur.items()}


def _measure(panel: pd.DataFrame, tradable: pd.DataFrame, fwd: pd.DataFrame,
             md_eval: pd.DatetimeIndex, zones: np.ndarray) -> tuple:
    cur_d = gate.ic_by_lag(panel, tradable, fwd, md_eval, zones)
    cur_a = gate.ic_by_lag(panel, tradable, fwd, md_eval, zones, zone="ACTIVE")
    ic_dead = {L: (float(s.mean()) if len(s) else np.nan, len(s))
               for L, s in cur_d.items()}
    hl = gate.half_life_from_curve(ic_dead)
    return _curve_dict(cur_d), _curve_dict(cur_a), hl


def _row(family: str, name: str, s14: dict, hl: dict, dead_c: dict, act_c: dict) -> dict:
    v5 = gate.verdict_single(s14["dead_head"], s14["dead_t"], s14["active_head"],
                             s14.get("shadow_abs_rho"),
                             half_life_months=hl["half_life_months"])
    old_pass = bool(s14.get("PASS_single"))
    return {"family": family, "name": name,
            "dead_head": s14["dead_head"], "dead_t": s14["dead_t"],
            "active_head": s14["active_head"],
            "shadow_abs_rho": s14.get("shadow_abs_rho"),
            "PASS_single_s1s4": old_pass,
            "ic_curve_dead": dead_c, "ic_curve_active": act_c,
            "half_life_months": hl["half_life_months"],
            "censored": hl["censored"], "n_anchors_min": hl["n_anchors_min"],
            "s5_half_life": hl["s5_half_life"],
            "PASS_single_s1s5": bool(v5["PASS_single"]),
            "flip_s1s4_to_s1s5": old_pass and not v5["PASS_single"]}


def main() -> int:
    t0 = time.time()
    retest = json.loads(RETEST.read_text(encoding="utf-8"))
    pool_factors = sorted(r["factor"] for r in retest["pool"])
    pool_s14 = {r["factor"]: r for r in retest["pool"]}
    bank_pass = list(retest["bank"]["pass_list"])
    bank_s14 = {r["alpha_id"]: r for r in retest["bank"]["rows"]}
    print(f"范围: 池 {len(pool_factors)} + bank 过档 {len(bank_pass)}", flush=True)

    m = load_market()
    md_all = month_end_dates(pd.DatetimeIndex(m["close"].index))
    md_eval = pd.DatetimeIndex(md_all[md_all >= pd.Timestamp("2019-01-01")])
    zones = gate.zone_of(md_eval)
    fwd = gate.forward_return(m, gate.HORIZON)
    pit = PITEngine(m=m)
    tradable = pit.tradable_matrix(md_eval)

    rows, xchk = [], []
    wide31 = T._load_factors_wide()
    wide31r = wide31.reorder_levels(["factor", "ts_code"], axis=1).sort_index(axis=1)
    for f in pool_factors:
        sub = wide31r.xs(f, axis=1, level="factor")
        sub.index = pd.to_datetime(sub.index)
        sub_eval = sub.reindex(index=md_eval, columns=sub.columns)
        dead_c, act_c, hl = _measure(sub_eval, tradable, fwd, md_eval, zones)
        if pool_s14[f].get("PASS_single"):
            det = gate.evaluate(sub_eval.where(
                tradable.reindex(index=md_eval, columns=sub_eval.columns).fillna(False)),
                fwd, md_eval, zones)
            if not det.empty:
                ref = float(det[det.zone == "DEAD"]["ic_full"].mean())
                mine = dead_c[0]["ic"]
                if mine is not None:
                    xchk.append({"factor": f, "ic0": mine, "evaluate_dead_ic": ref,
                                 "abs_diff": abs(mine - ref)})
        rows.append(_row("pool", f, pool_s14[f], hl, dead_c, act_c))
        r = rows[-1]
        print(f"  pool {f:24s} HL={r['half_life_months']} "
              f"IC0={dead_c[0]['ic'] if dead_c[0]['ic'] is not None else float('nan'):+.4f} "
              f"n_min={r['n_anchors_min']}", flush=True)

    for aid in bank_pass:
        npz = POOL / f"{aid}.npz"
        if not npz.exists():
            print(f"  bank {aid:28s} 无 npz 面板 —— 未测", flush=True)
            rows.append({"family": "bank", "name": aid, **{k: bank_s14[aid].get(k)
                         for k in ("dead_head", "dead_t", "active_head", "shadow_abs_rho")},
                         "PASS_single_s1s4": bool(bank_s14[aid].get("PASS_single")),
                         "note": "无 npz 面板，S5 未测不否决（沿 S4 惯例）"})
            continue
        d = np.load(npz, allow_pickle=True)
        pw = pd.DataFrame(d["values"].astype(float),
                          index=pd.to_datetime([str(x) for x in d["dates"].tolist()]),
                          columns=[str(x) for x in d["codes"].tolist()])
        pw = pw.reindex(index=md_eval)
        dead_c, act_c, hl = _measure(pw, tradable, fwd, md_eval, zones)
        rows.append(_row("bank", aid, bank_s14[aid], hl, dead_c, act_c))
        r = rows[-1]
        print(f"  bank {aid:28s} HL={r['half_life_months']} "
              f"IC0={dead_c[0]['ic'] if dead_c[0]['ic'] is not None else float('nan'):+.4f} "
              f"n_min={r['n_anchors_min']}", flush=True)

    pool_rows = [r for r in rows if r["family"] == "pool"]
    bank_rows = [r for r in rows if r["family"] == "bank"]
    flips = {"pool": [r["name"] for r in pool_rows if r.get("flip_s1s4_to_s1s5")],
             "bank": [r["name"] for r in bank_rows if r.get("flip_s1s4_to_s1s5")]}
    hls_pool = sorted(r["half_life_months"] for r in pool_rows
                      if r.get("half_life_months") is not None)
    max_xchk = max((x["abs_diff"] for x in xchk), default=None)
    out = {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "prereg": "plans/s5-half-life-prereg-20260927.md",
        "criteria": {"hl_bar": gate.S5_HL_BAR, "lags": list(gate.S5_LAGS),
                     "min_anchors": gate.S5_MIN_ANCHORS,
                     "zone": "DEAD", "label": "naive_fwd21", "ic": "spearman_lag_variant"},
        "retest_source": "experiments/single_factor_retest_20260926.json",
        "crosscheck_ic0_vs_evaluate": {"n": len(xchk), "max_abs_diff": max_xchk,
                                       "tol": XCHK_TOL, "pass": max_xchk is not None
                                       and max_xchk < XCHK_TOL, "rows": xchk},
        "pool": {"rows": pool_rows,
                 "n_measured": len(hls_pool),
                 "hl_median": float(np.median(hls_pool)) if hls_pool else None,
                 "n_pass_s1s4": sum(1 for r in pool_rows if r["PASS_single_s1s4"]),
                 "n_pass_s1s5": sum(1 for r in pool_rows if r["PASS_single_s1s5"])},
        "bank": {"rows": bank_rows,
                 "n_pass_s1s4": sum(1 for r in bank_rows if r["PASS_single_s1s4"]),
                 "n_pass_s1s5": sum(1 for r in bank_rows if r["PASS_single_s1s5"])},
        "flip_list": flips,
        "note": "本轮只测量与分级; S5 正式并入单因子档判据序列待用户拍板",
        "runtime_sec": round(time.time() - t0, 1),
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"交叉校验 max|ΔIC0| = {max_xchk} (tol {XCHK_TOL})", flush=True)
    print(f"若并入 S5 将翻转: pool {flips['pool']} | bank {flips['bank']}", flush=True)
    print(f"产物: {OUT}  ({time.time() - t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
