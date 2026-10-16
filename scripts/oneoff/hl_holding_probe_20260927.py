# -*- coding: utf-8 -*-
"""创新③ 衰减感知持有期探针 v2 —— `plans/hl-holding-probe-v2-prereg-20260927.md` 的执行体。

v1 作废（预注册推导符号错误 + IC/收益单位错配，v1 产物 hl_holding_probe_20260927.json
保留作证据，判决不采用）。v2 修正判据（重跑前冻结）：
  κ_f = |DEAD head / IC_0|（每因子 IC→收益换算锚；|IC_0|<0.005 ⇒ None 未测不否决）；
  净改善条件：**κ_f·c*(H) < c_ref**，c*(H)=(IC_0−G(H))/(1−1/H)，c_ref=0.003
  （=config single_side_cost，生产口径）——成本越高延长越有利（省成本 > 毛边缘老化）。
两问（§二 冻结）：
  R1 可行性: 池 31 中 viable 占比 ≥1/3 ⇒ 存在可行延长持有层;
  R2 连贯性: 冻结预测 HL≥4 组 viable 占比 > HL≤2 组（验证②读数的决策价值）。
R3 分层候选名单（读数非判决；生产分层须组合层 A/B + 拍板）。
只读零写库。产物 experiments/hl_holding_probe_v2_20260927.json。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "experiments" / "single_factor_retest_s5_20260927.json"
OUT = ROOT / "experiments" / "hl_holding_probe_v2_20260927.json"
C_REF = 0.003          # = config single_side_cost（生产回测口径，预注册 §一 冻结）
H_MAX = 3              # 预注册 §三：只测到 H=3
KAPPA_MIN_IC0 = 0.005  # |IC_0| 低于此 κ_f 不稳 ⇒ 未测


def _ic(curve: dict, L: int):
    node = curve.get(str(L), curve.get(L)) or {}
    return node.get("ic")


def readout(row: dict) -> dict:
    """单因子 v2 读数：κ_f / G(H) / c*(H) / viable。"""
    rec = {"family": row.get("family"),
           "name": row.get("factor") or row.get("alpha_id"),
           "hl": row.get("half_life_months"),
           "dead_head": row.get("dead_head")}
    curve = row.get("ic_curve_dead")
    if not curve:
        rec.update({"note": "无曲线（未测/无面板）", "kappa": None, "viable": None})
        return rec
    ic0 = _ic(curve, 0)
    if ic0 is None or ic0 != ic0 or abs(ic0) < KAPPA_MIN_IC0 or not rec["dead_head"]:
        rec.update({"note": "IC_0 过小或缺 head，κ_f 未测", "kappa": None, "viable": None})
        return rec
    kappa = abs(float(rec["dead_head"]) / ic0)
    rec["kappa"] = kappa
    rec["ic0"] = ic0
    for h in range(1, H_MAX + 1):
        ics = [_ic(curve, L) for L in range(h)]
        if any(v is None or v != v for v in ics):
            rec[f"g_{h}"] = rec[f"c_star_{h}"] = None
            continue
        g = float(np.mean(ics))
        rec[f"g_{h}"] = g
        rec[f"c_star_{h}"] = None if h == 1 else float((ic0 - g) / (1 - 1 / h))
    loss = [kappa * rec.get(f"c_star_{h}") for h in (2, 3)
            if rec.get(f"c_star_{h}") is not None]
    rec["viable"] = bool(loss) and min(loss) < C_REF
    rec["loss_h2_h3"] = loss
    return rec


def pct(seq):
    seq = [v for v in seq if v is not None]
    return (float(np.mean(seq)), len(seq)) if seq else (None, 0)


def main() -> int:
    src = json.loads(SRC.read_text(encoding="utf-8"))
    rows = []
    for family in ("pool", "bank"):
        for r in src[family]["rows"]:
            rows.append(readout({**r, "family": family}))

    pool = [r for r in rows if r["family"] == "pool" and r.get("viable") is not None]
    v_pool, n_pool = pct([r["viable"] for r in pool])
    r1 = {"n": n_pool, "viable_frac": v_pool, "threshold": "≥ 1/3",
          "verdict": ("PROCEED" if (v_pool is not None and v_pool >= 1 / 3) else "CLEARED")}

    measured = [r for r in rows if r.get("viable") is not None]
    long_grp = pct([r["viable"] for r in measured
                    if r.get("hl") is not None and r["hl"] >= 4])
    short_grp = pct([r["viable"] for r in measured
                     if r.get("hl") is not None and r["hl"] <= 2])
    r2 = {"viable_frac_hl_ge4": long_grp[0], "n_hl_ge4": long_grp[1],
          "viable_frac_hl_le2": short_grp[0], "n_hl_le2": short_grp[1],
          "frozen_prediction": "HL≥4 组 viable 占比 > HL≤2 组",
          "coherent": (None if long_grp[0] is None or short_grp[0] is None
                       else bool(long_grp[0] > short_grp[0]))}

    out = {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "prereg": "plans/hl-holding-probe-v2-prereg-20260927.md",
        "supersedes": "experiments/hl_holding_probe_20260927.json (v1 判据推导错误, 作废保留)",
        "source": SRC.name,
        "c_ref": C_REF,
        "R1_viability": r1,
        "R2_coherence": r2,
        "R3_layer_candidates": {
            "extend_hold_candidates": [r["name"] for r in rows if r.get("viable")],
            "monthly_keep": [r["name"] for r in rows if r.get("viable") is False],
            "note": "读数非判决——生产分层须组合层 A/B 预注册 + 拍板（chase6 惯例）"},
        "rows": rows,
        "note": "只读探针; κ_f 为 decile head 下界锚 ⇒ viable 读数偏乐观侧; 终审=组合层 A/B",
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"R1: viable {v_pool} ({n_pool} 因子) ⇒ {r1['verdict']}", flush=True)
    print(f"R2: HL≥4 {long_grp[0]} (n={long_grp[1]}) vs HL≤2 {short_grp[0]} "
          f"(n={short_grp[1]}) ⇒ coherent={r2['coherent']}", flush=True)
    cands = out["R3_layer_candidates"]["extend_hold_candidates"]
    print(f"延长持有候选 {len(cands)}: {cands}", flush=True)
    print(f"产物: {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
