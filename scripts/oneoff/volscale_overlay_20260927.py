# -*- coding: utf-8 -*-
"""月频波动率缩放层 A/B —— `plans/volscale-overlay-prereg-20260927.md` 的执行体。

基础序列 = pin 20260927_excl5(prod) / 20260927_excl5_full(full) 的满仓日线 NAV。
机制（§一 冻结）: 每月度期起点 w = clip(σ_base/σ_est, 0.5, 1.0)，σ_est = 期前 63 交易日
年化波动，σ_base = σ_est 的 252 日滚动均值（全 real-time，无全样本前视）；叠加期日收益
r' = w × r（现金 0，保守）。判据（§二 冻结）: P1 MDD 改善 ≥5pp（一窗达标另一窗 ≤2pp 劣化）
/ P2 年化非劣 ≥−1.5pp / P3 触发覆盖率 15-60%。判决词机械。只读零写库。
产物 experiments/volscale_overlay_20260927.json。
"""
import json
import sqlite3
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "volscale_overlay_20260927.json"
PREREG = "plans/volscale-overlay-prereg-20260927.md"
RUNS = {"prod": "20260927_excl5", "full": "20260927_excl5_full"}
EST_WIN, BASE_WIN = 63, 252
W_LO, W_HI = 0.5, 1.0
RF = 0.02


def metrics(daily_ret: pd.Series) -> dict:
    r = daily_ret.dropna()
    n = len(r)
    eq = float(np.prod(1 + r) ** (252 / n) - 1)
    nav = np.cumprod(1 + r)
    peak = np.maximum.accumulate(nav)
    mdd = float((nav / peak - 1).min())
    vol = float(r.std(ddof=1) * np.sqrt(252))
    return {"n_days": int(n), "ann_return": round(eq, 4), "mdd": round(mdd, 4),
            "vol_ann": round(vol, 4),
            "sharpe": round((eq - RF) / vol, 3) if vol > 0 else None}


def overlay(base_ret: pd.Series) -> tuple:
    """逐期缩放叠加。期 = trade_date 的自然月；w 只用期前数据。"""
    r = base_ret.dropna()
    vol_d = r.rolling(EST_WIN).std(ddof=1) * np.sqrt(252)   # 逐日滚动已实现波动
    months = r.index.to_period("M")
    w_by_day = pd.Series(1.0, index=r.index)
    w_periods = []
    for mth in months.unique():
        idx_p = r.index[months == mth]
        d_start_loc = r.index.get_loc(idx_p[0])
        if d_start_loc < BASE_WIN:
            continue                                          # 前导空窗: w=1.0
        hist = vol_d.iloc[max(0, d_start_loc - BASE_WIN):d_start_loc]
        sigma_est = vol_d.iloc[d_start_loc - 1]
        sigma_base = float(hist.mean()) if hist.notna().sum() >= 60 else np.nan
        if sigma_base != sigma_base or sigma_base <= 0 or sigma_est != sigma_est:
            w = 1.0
        else:
            w = float(np.clip(sigma_base / sigma_est, W_LO, W_HI))
        w_by_day.loc[idx_p] = w
        w_periods.append({"period": str(mth), "w": round(w, 3),
                          "sigma_est": round(float(sigma_est), 4),
                          "sigma_base": round(sigma_base, 4)})
    r_ov = (r * w_by_day).dropna()
    return r_ov, w_by_day, w_periods


def main() -> int:
    t0 = time.time()
    conn = sqlite3.connect(f"{(ROOT / 'data' / 'neweco.db').as_uri()}?mode=ro", uri=True)
    out_runs, checks_all = {}, {}
    for wname, run_date in RUNS.items():
        rows = pd.read_sql("SELECT trade_date, nav FROM backtest_nav_daily WHERE run_date=? "
                           "ORDER BY trade_date", conn, params=(run_date,))
        nav = rows.set_index(pd.to_datetime(rows["trade_date"], format="%Y%m%d"))["nav"]
        base_ret = nav.pct_change().dropna()
        r_ov, w_by_day, w_periods = overlay(base_ret)
        m_base, m_ov = metrics(base_ret), metrics(r_ov)
        ov_periods = [p for p in w_periods if p["w"] < 1.0]
        cover = len(ov_periods) / max(1, len(w_periods))
        mdd_impr = m_ov["mdd"] - m_base["mdd"]                # 正 = 改善(overlay 更浅)
        ann_gap = m_ov["ann_return"] - m_base["ann_return"]
        out_runs[wname] = {"base": m_base, "overlay": m_ov,
                           "mdd_improvement": round(mdd_impr, 4),
                           "ann_gap": round(ann_gap, 4),
                           "w_coverage": round(cover, 3),
                           "avg_w_when_active": round(float(np.mean([p["w"] for p in ov_periods])) if ov_periods else 1.0, 3),
                           "n_periods": len(w_periods),
                           "w_series_sample": w_periods[-12:]}
        checks_all[wname] = {"p1_mdd_improve_ge_5pp": bool(mdd_impr >= 0.05),
                             "p2_ann_non_inferior": bool(ann_gap >= -0.015),
                             "p3_coverage_ok": bool(0.15 <= cover <= 0.60)}
        log = (f"  {wname}: base ann {m_base['ann_return']:+.4f} mdd {m_base['mdd']:+.4f} | "
               f"overlay ann {m_ov['ann_return']:+.4f} mdd {m_ov['mdd']:+.4f} | "
               f"MDD改善 {mdd_impr:+.4f} 覆盖 {cover:.0%}")
        print(log, flush=True)

    # 预注册 §二: 至少一窗达标 且 另一窗劣化 ≤2pp
    p1 = checks_all["prod"]["p1_mdd_improve_ge_5pp"] or checks_all["full"]["p1_mdd_improve_ge_5pp"]
    other_ok = all((checks_all[w]["p1_mdd_improve_ge_5pp"] or
                    out_runs[w]["mdd_improvement"] >= -0.02) for w in checks_all)
    p1 = bool(p1 and other_ok)
    p2 = all(c["p2_ann_non_inferior"] for c in checks_all.values())
    p3 = all(c["p3_coverage_ok"] for c in checks_all.values())
    if p1 and p2 and p3:
        verdict = "PASS"
    elif (not p2) or all(out_runs[w]["mdd_improvement"] < 0.02 for w in out_runs):
        verdict = "REJECT"
    else:
        verdict = "INDETERMINATE"
    print(f"判决: {verdict} (P1={p1} P2={p2} P3={p3})", flush=True)

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"), "prereg": PREREG,
           "base_runs": RUNS, "params": {"est_win": EST_WIN, "base_win": BASE_WIN,
                                          "w_lo": W_LO, "w_hi": W_HI, "rf": RF},
           "runs": out_runs, "checks": checks_all,
           "criteria": {"p1": "MDD 改善≥5pp(一窗达标另一窗劣化≤2pp)",
                        "p2": "年化非劣 ≥−1.5pp", "p3": "覆盖率 15-60%"},
           "checks_summary": {"p1": p1, "p2": p2, "p3": p3},
           "verdict": {"PASS": "PASS（进生产接线预注册）", "REJECT": "REJECT（线结清）",
                       "INDETERMINATE": "INDETERMINATE"}[verdict],
           "note": "研究层线性缩放叠加(零引擎改动); PASS 后生产接线另走预注册+回放复核",
           "runtime_sec": round(time.time() - t0, 1)}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"产物: {OUT}  ({time.time() - t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
