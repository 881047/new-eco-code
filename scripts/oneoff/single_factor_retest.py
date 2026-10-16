# -*- coding: utf-8 -*-
"""单因子档因子重测 —— `plans/single-factor-gate-prereg-20260926.md` 的执行体（v3）。

v3（2026-09-27）：**S5 时效轴正式并入判据序列（S1-S5）**——拍板依据 = 用户指令
"继续，完成为止"（09-27，台账十七补逐字登记；S5 判据本体由 plans/s5-half-life-
prereg-20260927.md 跑前冻结）。批① 31 在池因子全量测 HL（bar 校准读数，prereg §三）；
批② bank 候选仅对 S1-S4 过档且有 npz 面板者测 HL（S1-S4 未过档者 S5 无判决后果，
HL 记 None 不否决——沿"未测不否决"惯例）。产物**新文件**
experiments/single_factor_retest_s5_20260927.json（20260926 旧产物原样保留作证据）；
内置对 20260926 读数的逐位复现校验（S1-S4 机械未变 ⇒ 应 max|Δ|≈0）。

v2 修正(v1 首跑实测): ① ρ 矩阵原要求 31 因子同日全覆盖 ⇒ 只剩 3 锚点(不可信) —— 改逐对
可得锚点累加; ② 批② ρ 逐候选×31 因子无缓存 ⇒ 超时 —— 改锚点秩缓存 + 一次 corrcoef 向量化。
判据: S1(|head|≥0.3%) / S2(|t|≥2) / S3(同号) / S4(与现役 |ρ|max≤0.7, 未测不否决) /
S5(执行延迟 IC 半衰期 ≥ 2×调仓周期 = 2.0 月, 未测不否决)。
"""
import json
import sqlite3
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
DB = ROOT / "data" / "neweco.db"
POOL = ROOT / "data" / "alpha_pool"
PREV = ROOT / "experiments" / "single_factor_retest_20260926.json"
OUT = ROOT / "experiments" / "single_factor_retest_s5_20260927.json"


def _hl_readout(panel: pd.DataFrame, tradable: pd.DataFrame, fwd: pd.DataFrame,
                md_eval: pd.DatetimeIndex, zones: np.ndarray) -> dict:
    """S5 曲线 + 半衰期（判定区 DEAD；ACTIVE 曲线照记读数）。"""
    cur_d = gate.ic_by_lag(panel, tradable, fwd, md_eval, zones)
    cur_a = gate.ic_by_lag(panel, tradable, fwd, md_eval, zones, zone="ACTIVE")
    ic_dead = {L: (float(s.mean()) if len(s) else np.nan, len(s)) for L, s in cur_d.items()}
    hl = gate.half_life_from_curve(ic_dead)
    curve = {int(L): {"ic": (float(s.mean()) if len(s) else None), "n": int(len(s))}
             for L, s in cur_d.items()}
    curve_a = {int(L): {"ic": (float(s.mean()) if len(s) else None), "n": int(len(s))}
               for L, s in cur_a.items()}
    return {**hl, "ic_curve_dead": curve, "ic_curve_active": curve_a}


def main() -> int:
    t0 = time.time()
    prev = json.loads(PREV.read_text(encoding="utf-8")) if PREV.exists() else None
    m = load_market()
    md_all = month_end_dates(pd.DatetimeIndex(m["close"].index))
    md_eval = pd.DatetimeIndex(md_all[md_all >= pd.Timestamp("2019-01-01")])
    zones = gate.zone_of(md_eval)
    fwd = gate.forward_return(m, gate.HORIZON)
    pit = PITEngine(m=m)
    tradable = pit.tradable_matrix(md_eval)
    conn = sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)

    # ---------- 批 ①: 31 在池因子（S1-S5，全量 HL = bar 校准读数） ----------
    wide31 = T._load_factors_wide()
    wide31r = wide31.reorder_levels(["factor", "ts_code"], axis=1).sort_index(axis=1)
    factors = sorted(wide31r.columns.get_level_values("factor").unique())
    print(f"在池因子 {len(factors)} 个", flush=True)

    panels = {}
    readouts = {}
    hls = {}
    for f in factors:
        sub = wide31r.xs(f, axis=1, level="factor")
        sub.index = pd.to_datetime(sub.index)
        panels[f] = sub
        sub_eval = sub.reindex(index=md_eval, columns=sub.columns)
        pm = sub_eval.where(tradable.reindex(index=md_eval, columns=sub_eval.columns).fillna(False))
        det = gate.evaluate(pm, fwd, md_eval, zones)
        if det.empty:
            readouts[f] = None
            continue
        dead, act = det[det.zone == "DEAD"], det[det.zone == "ACTIVE"]
        h_d = float(dead.head_excess.mean())
        t_d = float(h_d / (dead.head_excess.std(ddof=1) / np.sqrt(len(dead))))
        readouts[f] = {"dead_head": round(h_d, 4), "dead_t": round(t_d, 2),
                       "active_head": round(float(act.head_excess.mean()), 4)}
        hls[f] = _hl_readout(sub_eval, tradable, fwd, md_eval, zones)
        print(f"  {f:24s} HL={hls[f]['half_life_months']} "
              f"IC0={hls[f]['ic_curve_dead'][0]['ic']:+.4f}", flush=True)

    # ρ 矩阵: 逐对逐锚点(每对用自己的可得锚点与成对完整行, 不再要求 31 列同日全覆盖)
    anchors_sel = list(md_eval)          # 全量月频锚点(共 ~93 个, v1 的 [120:] 切片为空)
    k = len(factors)
    s_sum = np.zeros((k, k))
    s_cnt = np.zeros((k, k))
    for i in range(k):
        ri = panels[factors[i]].rank(axis=1)
        for j in range(i + 1, k):
            rj = panels[factors[j]].rank(axis=1)
            for dt in anchors_sel:
                if dt not in ri.index or dt not in rj.index:
                    continue
                a = ri.loc[dt]
                b = rj.loc[dt]
                jn = pd.concat([a.rename("a"), b.rename("b")], axis=1).dropna()
                if len(jn) < 200:
                    continue
                c = jn["a"].corr(jn["b"])
                s_sum[i, j] += c
                s_cnt[i, j] += 1
    n_pairs_done = int((s_cnt > 0).sum() // 2)
    pair_anchor_med = int(np.median(s_cnt[s_cnt > 0])) if n_pairs_done else 0
    print(f"ρ 矩阵: {n_pairs_done} 对完成(逐对累加, 每对锚点中位数 {pair_anchor_med})",
          flush=True)

    pool_rows = []
    for i, f in enumerate(factors):
        r = readouts.get(f) or {}
        if not r:
            pool_rows.append({"factor": f, "note": "无有效截面"})
            continue
        others = [g for g in factors if g != f]
        vals = []
        for g in others:
            gi = factors.index(g)
            a, b = (i, gi) if i < gi else (gi, i)
            if s_cnt[a, b] > 0:
                sign = 1.0 if i < gi else -1.0
                vals.append(sign * s_sum[a, b] / s_cnt[a, b])
        rho_max = float(np.max(np.abs(vals))) if vals else None
        rho_f = factors[ int(np.argmax(np.abs(vals))) ] if vals else None
        hl = hls.get(f, {})
        v = gate.verdict_single(r["dead_head"], r["dead_t"], r["active_head"], rho_max,
                                half_life_months=hl.get("half_life_months"))
        pool_rows.append({"factor": f, **r, "shadow_factor": rho_f,
                          "shadow_abs_rho": round(rho_max, 3) if rho_max is not None else None,
                          **{k2: hl[k2] for k2 in ("half_life_months", "censored",
                                                   "n_anchors_min", "s5_half_life",
                                                   "ic_curve_dead", "ic_curve_active") if k2 in hl},
                          **v})
    pool_pass = [r for r in pool_rows if r.get("PASS_single")]
    pool_pass_s14 = [r for r in pool_rows if r.get("s1_head") and r.get("s2_t")
                     and r.get("s3_same_sign") and r.get("s4_decor") is not False]
    print(f"批①: 31 在池因子 S1-S4 过档 {len(pool_pass_s14)} → S1-S5 过档 {len(pool_pass)} 个",
          flush=True)
    for r in pool_pass:
        print(f"  ✓ {r['factor']:20s} head {r['dead_head']:+.4f} t {r['dead_t']:+.2f} "
              f"|ρ|max {r['shadow_abs_rho']} HL {r['half_life_months']}", flush=True)

    # ---------- 批 ②: bank 候选（秩缓存向量化 ρ；S5 仅对 S1-S4 过档且有面板者） ----------
    rank_cache = {}
    for dt in anchors_sel:
        cols = {}
        for f in factors:
            if dt in panels[f].index:
                cols[f] = panels[f].loc[dt].rank()
        if len(cols) == len(factors):
            rank_cache[dt] = pd.DataFrame(cols)

    rows = conn.execute(
        "SELECT alpha_id, DEAD_head, DEAD_head_t, ACTIVE_head FROM alpha_bank "
        "WHERE DEAD_head IS NOT NULL").fetchall()
    conn.close()
    bank_rows, pass_list = [], []
    n_null = 0
    for aid, h, t, h_a in rows:
        if h is None or t is None or h_a is None:
            n_null += 1
            continue
        npz = POOL / f"{aid}.npz"
        rho = None
        shadow_f = None
        pw = None
        if npz.exists():
            try:
                d = np.load(npz, allow_pickle=True)
                pw = pd.DataFrame(d["values"].astype(float),
                                  index=pd.to_datetime([str(x) for x in d["dates"].tolist()]),
                                  columns=[str(x) for x in d["codes"].tolist()])
                per_factor = {f: [] for f in factors}
                for dt, R in rank_cache.items():
                    if dt not in pw.index:
                        continue
                    cr = pw.loc[dt].rank()
                    j = pd.concat([cr.rename("c"), R], axis=1).dropna()
                    if len(j) < 200:
                        continue
                    cc = j.corr()["c"]
                    for f in factors:
                        per_factor[f].append(cc[f])
                means = {f: float(np.mean(v)) for f, v in per_factor.items() if v}
                if means:
                    shadow_f = max(means, key=lambda f: abs(means[f]))
                    rho = round(abs(means[shadow_f]), 3)
            except Exception:                             # noqa: BLE001
                rho = None
        pre = gate.verdict_single(h, t, h_a, rho)         # S1-S4（S5 未测不否决）
        s14_pass = bool(pre["PASS_single"])
        hl = {}
        if s14_pass and pw is not None:
            hl = _hl_readout(pw.reindex(index=md_eval), tradable, fwd, md_eval, zones)
            print(f"  bank {aid:28s} S1-S4 过档 → HL={hl['half_life_months']}", flush=True)
        v = gate.verdict_single(h, t, h_a, rho,
                                half_life_months=hl.get("half_life_months"))
        row = {"alpha_id": aid, "dead_head": h, "dead_t": t, "active_head": h_a,
               "shadow_factor": shadow_f, "shadow_abs_rho": rho,
               **{k2: hl[k2] for k2 in ("half_life_months", "censored",
                                        "n_anchors_min", "s5_half_life",
                                        "ic_curve_dead", "ic_curve_active") if k2 in hl},
               **v}
        bank_rows.append(row)
        if v["PASS_single"]:
            pass_list.append(aid)
    n_pass_s14 = sum(1 for r in bank_rows if r.get("s1_head") and r.get("s2_t")
                     and r.get("s3_same_sign") and r.get("s4_decor") is not False)
    print(f"批②: bank 候选 {len(bank_rows)} | S1-S4 过档 {n_pass_s14} → S1-S5 过档 "
          f"{len(pass_list)}: {pass_list}", flush=True)

    # ---------- 复现校验: S1-S4 机械未变 ⇒ 与 20260926 读数逐位一致 ----------
    repro = None
    if prev:
        prev_pool = {r["factor"]: r for r in prev["pool"] if "dead_head" in r}
        diffs = []
        for r in pool_rows:
            p = prev_pool.get(r.get("factor"))
            if p:
                for key in ("dead_head", "dead_t", "active_head", "shadow_abs_rho"):
                    if p.get(key) is not None and r.get(key) is not None:
                        diffs.append(abs(float(p[key]) - float(r[key])))
        prev_bank = {r["alpha_id"]: r for r in prev["bank"]["rows"]}
        for r in bank_rows:
            p = prev_bank.get(r.get("alpha_id"))
            if p:
                for key in ("dead_head", "dead_t", "active_head", "shadow_abs_rho"):
                    if p.get(key) is not None and r.get(key) is not None:
                        diffs.append(abs(float(p[key]) - float(r[key])))
        repro = {"max_abs_diff": max(diffs) if diffs else None,
                 "n_compared": len(diffs)}

    flips_pool = [r["factor"] for r in pool_rows if r.get("s5_half_life") is False]
    flips_bank = [r["alpha_id"] for r in bank_rows if r.get("s5_half_life") is False]
    hls_pool = sorted(r["half_life_months"] for r in pool_rows
                      if r.get("half_life_months") is not None)
    out = {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "prereg": "plans/single-factor-gate-prereg-20260926.md + plans/s5-half-life-prereg-20260927.md",
        "adoption_basis": "用户指令'继续，完成为止'(2026-09-27; 台账十七补逐字登记)",
        "criteria": {"head_bar": 0.003, "t_bar": 2.0, "rho_bar": 0.7,
                     "hl_bar": gate.S5_HL_BAR, "min_anchors": gate.S5_MIN_ANCHORS},
        "pool": {"rows": pool_rows,
                 "pool_pass_rate": f"{len(pool_pass)}/{len(factors)}",
                 "n_pass_s1s4": len(pool_pass_s14),
                 "n_pass_s1s5": len(pool_pass),
                 "hl_median": float(np.median(hls_pool)) if hls_pool else None,
                 "flips_by_s5": flips_pool},
        "bank": {"n_scored": len(bank_rows),
                 "n_pass_s1s4": n_pass_s14,
                 "n_pass_single": len(pass_list),
                 "pass_list": pass_list,
                 "n_skipped_null_readout": n_null,
                 "flips_by_s5": flips_bank,
                 "rows": bank_rows},
        "reproduction_vs_20260926": repro,
        "runtime_sec": round(time.time() - t0, 1),
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"复现校验 vs 20260926: {repro}", flush=True)
    print(f"产物: {OUT}  ({time.time() - t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
