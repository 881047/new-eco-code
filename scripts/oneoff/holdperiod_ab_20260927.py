# -*- coding: utf-8 -*-
"""创新③ 持有期组合层 A/B —— `plans/holdperiod-ab-prereg-20260927.md` 的执行体。

四臂双窗（预注册 §一 冻结）:
  A  = anchor="month"        （现役口径; 复现校验 vs nf_ab_backtest_20260927.json ctrl_sa，
                                其 prod 满仓 20.09%/−27.92% 与 pin 20260926_chase2 在册一致）
  B1 = anchor="2month_even"  （月末锚点每隔一个, 偶相位 ⇒ 持有 2 个月）
  B2 = anchor="2month_odd"   （奇相位, 防相位脆弱）
窗口: prod = config eval_start 起 / full = 全史。评分表 = 生产 model_score（单一表 ⇒
配置级 A/B, 无双种子集维度, 沿 chase6 两窗惯例）。
判据（§二 冻结）: 机制校验（ppy≈6 且**年化**换手降幅 ≥40%，年化=每期换手×ppy）→
P1 双窗 Δexcess 同向为正 → P2 Δmdd 劣化 ≤2pp → P3 双相位同号; 判决词 PASS(进拍板)/
REJECT(线结清)/INDETERMINATE。只读: run_tag=hpab_ 留痕, 不换 pin 不写生产表。
产物 experiments/holdperiod_ab_20260927.json。
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.backtest.engine import run_backtest
from app.config import load_config

ROOT = Path(__file__).resolve().parents[2]
NF = ROOT / "experiments" / "nf_ab_backtest_20260927.json"
OUT = ROOT / "experiments" / "holdperiod_ab_20260927.json"

ARMS = [("A_month", "month"), ("B1_2month_even", "2month_even"),
        ("B2_2month_odd", "2month_odd")]
TABLE = "model_score"
TURNOVER_CUT = 0.40     # 机制校验: 换手降幅 ≥40%
MDD_TOL = -0.02         # P2: 回撤劣化 ≤2pp


def _metrics(m: dict) -> dict:
    keys = ("periods", "periods_per_year", "ann_return", "bench_ann", "excess_ann",
            "max_drawdown", "win_rate", "ic_mean", "icir", "avg_turnover")
    return {k: m.get(k) for k in keys}


def main() -> int:
    t0 = time.time()
    cfg = load_config()
    eval_start = cfg.get("eval_start")
    windows = [("prod", eval_start), ("full", None)]
    print(f"eval_start={eval_start}, table={TABLE}, arms={[a[0] for a in ARMS]}", flush=True)

    runs = {}
    for arm, anchor in ARMS:
        for wname, start in windows:
            tag = f"hpab_{arm}_{wname}"
            m = run_backtest(score_table=TABLE, run_tag=tag, start=start, anchor=anchor)
            runs[f"{arm}_{wname}"] = {"anchor": anchor, **_metrics(m)}
            print(f"  {tag:28s} ann {m.get('ann_return'):+.4f} "
                  f"exc {m.get('excess_ann'):+.4f} mdd {m.get('max_drawdown'):+.4f} "
                  f"to {m.get('avg_turnover'):.4f} ppy {m.get('periods_per_year')}",
                  flush=True)

    # 复现校验: A 臂 vs nf ctrl_sa（其与 pin 在册读数逐位一致）
    repro = {}
    nf = json.loads(NF.read_text(encoding="utf-8")) if NF.exists() else {}
    for wname in ("prod", "full"):
        ref = nf.get("runs", {}).get(f"nfab_ctrl_sa_{wname}")
        mine = runs.get(f"A_month_{wname}")
        if ref and mine:
            diffs = {k: abs(float(ref[k]) - float(mine[k]))
                     for k in ("ann_return", "max_drawdown", "avg_turnover")
                     if ref.get(k) is not None and mine.get(k) is not None}
            repro[wname] = {"ref": "nfab_ctrl_sa", "max_abs_diff": max(diffs.values()),
                            "diffs": diffs}
    repro["ok"] = bool(repro.get("prod", {}).get("max_abs_diff", 1) < 1e-9
                       and repro.get("full", {}).get("max_abs_diff", 1) < 1e-9)

    deltas = {}
    for arm, _anchor in ARMS[1:]:
        for wname, _ in windows:
            a, b = runs[f"A_month_{wname}"], runs[f"{arm}_{wname}"]
            ann_to_a = a["avg_turnover"] * a["periods_per_year"]
            ann_to_b = b["avg_turnover"] * b["periods_per_year"]
            deltas[f"{arm}_{wname}"] = {
                "d_excess": b["excess_ann"] - a["excess_ann"],
                "d_mdd": b["max_drawdown"] - a["max_drawdown"],
                "annual_turnover_ratio": (ann_to_b / ann_to_a if ann_to_a else None),
                "periods_per_year": b["periods_per_year"]}

    mech = all(d["periods_per_year"] and abs(d["periods_per_year"] - 6) <= 1
               and d["annual_turnover_ratio"] is not None
               and d["annual_turnover_ratio"] <= (1 - TURNOVER_CUT)
               for d in deltas.values())
    p1 = {arm: all(deltas[f"{arm}_{w}"]["d_excess"] > 0 for w, _ in windows)
          for arm, _ in ARMS[1:]}
    p2 = {arm: all(deltas[f"{arm}_{w}"]["d_mdd"] >= MDD_TOL for w, _ in windows)
          for arm, _ in ARMS[1:]}
    b1, b2 = "B1_2month_even", "B2_2month_odd"
    p3 = all((deltas[f"{b1}_{w}"]["d_excess"] > 0)
             == (deltas[f"{b2}_{w}"]["d_excess"] > 0) for w, _ in windows)

    if not mech or not p3:
        verdict = "INDETERMINATE"
    elif all(p1.values()) and all(p2.values()):
        verdict = "PASS"
    else:
        verdict = "REJECT"
    out = {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "prereg": "plans/holdperiod-ab-prereg-20260927.md",
        "eval_start": eval_start, "table": TABLE, "runs": runs,
        "reproduction_A_vs_pin": repro,
        "deltas": deltas,
        "criteria": {"mech": {"ppy": 6, "turnover_cut": TURNOVER_CUT},
                     "p1": "双窗 Δexcess 同向为正", "p2": f"Δmdd ≥ {MDD_TOL}",
                     "p3": "双相位同号"},
        "checks": {"mechanism_ok": mech, "p1": p1, "p2": p2, "p3_phase": p3},
        "verdict": {"PASS": "PASS（进拍板）", "REJECT": "REJECT（线结清）",
                    "INDETERMINATE": "INDETERMINATE（读数不可辨）"}[verdict],
        "note": "配置级 A/B 无双种子集维度; 生产变更须拍板; run_tag=hpab_ 留痕不自清",
        "runtime_sec": round(time.time() - t0, 1),
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"复现校验 A vs pin(ctrl_sa): {repro.get('prod', {}).get('max_abs_diff')} / "
          f"{repro.get('full', {}).get('max_abs_diff')} ok={repro['ok']}", flush=True)
    print(f"mech={mech} p1={p1} p2={p2} p3={p3} ⇒ {out['verdict']}", flush=True)
    print(f"产物: {OUT}  ({time.time() - t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
