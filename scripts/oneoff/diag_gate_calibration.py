# -*- coding: utf-8 -*-
"""门禁校准诊断 —— 回答"门禁是不是不合理"(2026-09-26, 用户问询; 只读)。

方法: 把**现役生产模型自己的打分**(model_score, 4 种子集成)与最强现役因子(size/rev_5)
放进**同一道门禁**(gate.evaluate + PIT 域 + naive 标签)量读数 —— 如果在产信号自己都
过不了门, 门禁就是"比部署标准更严"的不合理; 如果能过, 0 PASS 的挖掘史就是候选真弱。

结论(详见输出): 生产打分 DEAD head +1.05%/月(t=5.21)、同号 ✓、top6 +0.48% —— 轻松过
0.5% 门槛与 N=249 隐含 t=3.72 ⇒ 门禁 bar = 在产强度, 校准合理。
只读生产表; 产物 experiments/diag_gate_calibration_20260926.json。
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app.alpha import gate
from app.factors.factor_engine import load_market, month_end_dates
from app.pit.engine import PITEngine

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "diag_gate_calibration_20260926.json"


def implied_t(n: int) -> float:
    """p_adj<0.05 (双侧 p × N) 的隐含 t 门槛。"""
    from statistics import NormalDist
    return NormalDist().inv_cdf(1 - 0.05 / (2 * n))


def main() -> int:
    m = load_market()
    md_all = month_end_dates(pd.DatetimeIndex(m["close"].index))
    md_eval = pd.DatetimeIndex(md_all[md_all >= pd.Timestamp("2019-01-01")])
    zones = gate.zone_of(md_eval)
    fwd = gate.forward_return(m, gate.HORIZON)
    pit = PITEngine(m=m)
    tradable = pit.tradable_matrix(md_eval)
    conn = sqlite3.connect(f"{(ROOT / 'data' / 'neweco.db').as_uri()}?mode=ro", uri=True)

    def run_gate(panel_wide, name):
        pm = panel_wide.reindex(index=md_eval, columns=panel_wide.columns)
        pm = pm.where(tradable.reindex(index=md_eval, columns=pm.columns).fillna(False))
        det = gate.evaluate(pm, fwd, md_eval, zones)
        if det.empty:
            return {"name": name, "note": "无有效截面"}
        dead, act = det[det.zone == "DEAD"], det[det.zone == "ACTIVE"]
        h_d, h_a = float(dead.head_excess.mean()), float(act.head_excess.mean())
        t_d = float(dead.head_excess.mean() /
                    (dead.head_excess.std(ddof=1) / np.sqrt(len(dead))))
        return {"name": name, "dead_head": round(h_d, 4), "dead_t": round(t_d, 2),
                "active_head": round(h_a, 4), "same_sign": bool(h_d * h_a > 0),
                "dead_top6": round(float(dead.top6_excess.mean()), 4),
                "head_bar_pass": bool(abs(h_d) >= 0.005)}

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"),
           "implied_t_by_N": {str(n): round(implied_t(n), 2)
                              for n in (7, 25, 100, 173, 249, 400, 1089)},
           "targets": []}
    sc = pd.read_sql("SELECT trade_date, ts_code, score FROM model_score", conn)
    w_sc = sc.pivot_table(index="trade_date", columns="ts_code", values="score", aggfunc="last")
    w_sc.index = pd.to_datetime(w_sc.index, format="%Y%m%d")
    out["targets"].append(run_gate(
        w_sc.reindex(sorted(set(md_eval) & set(w_sc.index))), "生产打分(model_score)"))
    for f in ("size", "rev_5", "turnover_20", "ep"):
        d = pd.read_sql("SELECT trade_date, ts_code, value FROM factor WHERE factor=?",
                        conn, params=(f,))
        w = d.pivot_table(index="trade_date", columns="ts_code", values="value", aggfunc="last")
        w.index = pd.to_datetime(w.index, format="%Y%m%d")
        out["targets"].append(run_gate(
            w.reindex(sorted(set(md_eval) & set(w.index))), f))
    con_rows = conn.execute(
        "SELECT COUNT(*) FROM alpha_bank WHERE DEAD_head IS NOT NULL").fetchone()[0]
    heads_sorted = [r[0] for r in conn.execute(
        "SELECT DEAD_head FROM alpha_bank WHERE DEAD_head IS NOT NULL ORDER BY DEAD_head")]
    out["bank_context"] = {"candidates_with_readouts": con_rows,
                           "median_dead_head": round(float(heads_sorted[len(heads_sorted) // 2]), 4)}
    conn.close()
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    for t in out["targets"]:
        print(t)
    print(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
