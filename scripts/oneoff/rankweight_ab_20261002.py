# -*- coding: utf-8 -*-
"""分数排名加权 sizing A/B 判决器（plans/rankweight-prereg-20261002.md 判据跑前冻结）。
臂 = run_backtest(rank_weighted=True) 双窗;对照 = 生产 pin(库内 metrics)。
判据 P1/P2/P2b/P3 全量 Δ 数字写入产物(64 补惯例)。"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from app import db
from app.backtest import engine as E

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "rankweight_ab_20261002.json"
FULL_WIN = {"start": "20170228", "end": "20260831"}
BASELINES = {"full": "20260928_exe_full", "prod": "20260928_exe"}


def log(m):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def baseline_metrics(run_date):
    conn = db.get_conn()
    try:
        row = pd.read_sql("SELECT metrics FROM backtest_summary WHERE run_date=?",
                          conn, params=(run_date,))
    finally:
        conn.close()
    return json.loads(row["metrics"].iloc[0])


def monthly_excess_vol(run_date) -> float:
    conn = db.get_conn()
    try:
        df = pd.read_sql("SELECT trade_date, sys_nav, bench_nav FROM backtest_nav "
                         "WHERE run_date=? ORDER BY trade_date", conn, params=(run_date,))
    finally:
        conn.close()
    exc = df["sys_nav"].pct_change() - df["bench_nav"].pct_change()
    return float(exc.dropna().std())


def main() -> int:
    runs = {}
    for wname, kw in (("full", FULL_WIN), ("prod", {})):
        mtr = E.run_backtest(rank_weighted=True, run_tag=f"rankwt_{wname}", **kw)
        runs[wname] = mtr
        log(f"[rankwt/{wname}] sys_ann={mtr['sys_ann_return']:+.2%} "
            f"mdd={mtr['sys_max_drawdown']:+.2%} to={mtr.get('avg_turnover')} "
            f"idle={mtr.get('avg_cash_idle')}")

    deltas = {}
    for wname in ("full", "prod"):
        arm = runs[wname]
        base = baseline_metrics(BASELINES[wname])
        deltas[wname] = {
            "arm_sys_ann": round(arm["sys_ann_return"], 4),
            "base_sys_ann": round(base["sys_ann_return"], 4),
            "delta_sys_ann_pp": round(100 * (arm["sys_ann_return"] - base["sys_ann_return"]), 2),
            "arm_mdd": round(arm["sys_max_drawdown"], 4),
            "base_mdd": round(base["sys_max_drawdown"], 4),
            "delta_mdd_pp": round(100 * (arm["sys_max_drawdown"] - base["sys_max_drawdown"]), 2),
            "arm_turnover": arm.get("avg_turnover"),
            "base_turnover": base.get("avg_turnover"),
            "delta_turnover_pp": round(100 * ((arm.get("avg_turnover") or 0)
                                              - (base.get("avg_turnover") or 0)), 2),
            "arm_idle": arm.get("avg_cash_idle"),
            "base_idle": base.get("avg_cash_idle"),
            "delta_idle_pp": round(100 * ((arm.get("avg_cash_idle") or 0)
                                          - (base.get("avg_cash_idle") or 0)), 2),
        }
    # P2b 波动率比(臂 vs pin, 月度超额 std)
    vol_arm_prod = monthly_excess_vol("20261002_rankwt_prod"
                                      if False else runs["prod"]["run_date"])
    vol_pin_prod = monthly_excess_vol(BASELINES["prod"])
    vol_ratio = round(vol_arm_prod / vol_pin_prod, 3) if vol_pin_prod else None

    p1 = (deltas["prod"]["delta_sys_ann_pp"] >= 2.0
          and deltas["full"]["delta_sys_ann_pp"] >= 1.0
          and deltas["prod"]["delta_sys_ann_pp"] > 0
          and deltas["full"]["delta_sys_ann_pp"] > 0)
    p2 = (deltas["prod"]["delta_mdd_pp"] > -2.0
          and deltas["full"]["delta_mdd_pp"] > -3.0)
    p2b = bool(vol_ratio is not None and vol_ratio <= 1.10)
    p3 = (all(deltas[w]["delta_turnover_pp"] <= 2.0 for w in ("prod", "full"))
          and all(deltas[w]["delta_idle_pp"] <= 15.0 for w in ("prod", "full")))
    if p1 and p2 and p2b and p3:
        verdict = "GO(提请重钉拍板)"
    elif deltas["prod"]["delta_sys_ann_pp"] < 0 and deltas["full"]["delta_sys_ann_pp"] < 0:
        verdict = "NO-GO(线结清)"
    else:
        verdict = "INDETERMINATE"
    out = {"asof": datetime.now().isoformat(timespec="seconds"),
           "criteria_file": "plans/rankweight-prereg-20261002.md",
           "deltas": deltas, "vol_ratio_prod": vol_ratio,
           "criteria": {"P1": p1, "P2": p2, "P2b_vol_guard": p2b, "P3": p3,
                        "verdict": verdict}}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    log(f"判决: {verdict} | P1={p1} P2={p2} P2b={p2b} P3={p3}")
    for w in ("prod", "full"):
        d = deltas[w]
        log(f"  {w}: Δsys={d['delta_sys_ann_pp']:+.2f}pp Δmdd={d['delta_mdd_pp']:+.2f}pp "
            f"Δto={d['delta_turnover_pp']:+.2f}pp Δidle={d['delta_idle_pp']:+.2f}pp")
    log(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
