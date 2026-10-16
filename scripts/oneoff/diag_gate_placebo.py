# -*- coding: utf-8 -*-
"""门禁安慰剂检验（placebo）—— 量化两道门的假放行率（2026-09-26，用户指令；只读）。

问题: 单因子档(S1-S4)放行了 50/231 —— 它是不是太松? 集成六判据是不是太严?
方法: 生成**安慰剂因子**(破坏预测力、保留横截面分布)过同一套机器, 量假放行率。

  家族 A: 纯 iid 随机面板(100 个) —— "无任何信息的因子"的假放行率;
  家族 B: 真因子(size)逐锚点截面**重排**(50 个) —— "保分布、去预测力"的假放行率。

判读(跑前冻结):
  - 集成门(六判据, 除 cost 外四判据): 安慰剂通过率应 ≈ 0(p_adj 在 N=249 下隐含 t≈3.72);
  - 单因子档(S1-S4): 安慰剂通过率 = 该档的**假放行率** —— 它的下游是组合层 A/B(真过滤器),
    故 P% 温和(≲10%)可接受; 若 ≳20% 则 S 阈值过松需收紧(另立预注册)。
只读生产表; 产物 experiments/diag_gate_placebo_20260926.json。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app.alpha import gate
from app.factors.factor_engine import load_market, month_end_dates
from app.pit.engine import PITEngine
from statistics import NormalDist

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "diag_gate_placebo_20260926.json"
N_IID, N_PERM = 100, 50
N_BANK = 232          # 在册候选数(p_adj 分母的现实取值)


def implied_t(n: int) -> float:
    return NormalDist().inv_cdf(1 - 0.05 / (2 * n))


def main() -> int:
    m = load_market()
    md_all = month_end_dates(pd.DatetimeIndex(m["close"].index))
    md_eval = pd.DatetimeIndex(md_all[md_all >= pd.Timestamp("2019-01-01")])
    zones = gate.zone_of(md_eval)
    fwd = gate.forward_return(m, gate.HORIZON)
    pit = PITEngine(m=m)
    tradable = pit.tradable_matrix(md_eval)
    cols = list(m["close"].columns)
    rng = np.random.default_rng(20260926)

    def assess(panel_wide: pd.DataFrame) -> dict:
        pm = panel_wide.reindex(index=md_eval, columns=panel_wide.columns)
        pm = pm.where(tradable.reindex(index=md_eval, columns=pm.columns).fillna(False))
        det = gate.evaluate(pm, fwd, md_eval, zones)
        if det.empty:
            return {"empty": True}
        dead, act = det[det.zone == "DEAD"], det[det.zone == "ACTIVE"]
        h_d = float(dead.head_excess.mean())
        t_d = float(h_d / (dead.head_excess.std(ddof=1) / np.sqrt(len(dead))))
        h_a = float(act.head_excess.mean())
        # 集成门(除 cost 外四判据; p_adj 用 N_BANK 现实分母)
        e_pass = (abs(h_d) >= 0.005 and abs(t_d) >= implied_t(N_BANK)
                  and h_d * h_a > 0 and float(dead.top6_excess.mean()) >= 0)
        # 单因子档(S1-S4; 安慰剂与现役池独立 ⇒ S4 视为通过, 记同款保守豁免)
        s_pass = (abs(h_d) >= 0.003 and abs(t_d) >= 2.0 and h_d * h_a > 0)
        return {"dead_head": round(h_d, 4), "dead_t": round(t_d, 2),
                "active_head": round(h_a, 4),
                "ensemble_pass": bool(e_pass), "single_pass": bool(s_pass)}

    results = {"iid": [], "perm": []}
    for i in range(N_IID):
        panel = pd.DataFrame(rng.normal(size=(len(md_eval), len(cols))),
                             index=md_eval, columns=cols)
        results["iid"].append(assess(panel))
    # 家族 B: size 逐锚点截面重排(保分布、去预测力)
    import sqlite3
    conn = sqlite3.connect(f"{(ROOT / 'data' / 'neweco.db').as_uri()}?mode=ro", uri=True)
    d = pd.read_sql("SELECT trade_date, ts_code, value FROM factor WHERE factor='size'", conn)
    conn.close()
    base = d.pivot_table(index="trade_date", columns="ts_code", values="value", aggfunc="last")
    base.index = pd.to_datetime(base.index, format="%Y%m%d")
    base = base.reindex(md_eval)
    for i in range(N_PERM):
        pm = base.copy()
        for dt in pm.index:
            pm.loc[dt] = rng.permutation(pm.loc[dt].to_numpy())
        results["perm"].append(assess(pm))

    def summarize(rows):
        rows = [r for r in rows if not r.get("empty")]
        tmax = max((abs(r["dead_t"]) for r in rows), default=0)
        return {"n": len(rows),
                "ensemble_pass": sum(1 for r in rows if r.get("ensemble_pass")),
                "single_pass": sum(1 for r in rows if r.get("single_pass")),
                "max_abs_t": round(tmax, 2),
                "max_abs_head": round(max((abs(r["dead_head"]) for r in rows), default=0), 4)}

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"),
           "n_bank_real": N_BANK,
           "implied_t_at_n_bank": round(implied_t(N_BANK), 2),
           "placebo": {"iid": summarize(results["iid"]),
                       "perm_size": summarize(results["perm"])},
           "criteria_note": "集成门比较用四判据(cost 除外, 安慰剂无成本概念); "
                            "单因子档 S4 对安慰剂豁免(独立于现役池) ⇒ 单因子档的安慰剂通过率是"
                            "S1-S3 的假放行率上界(真实候选还要过 S4 去相关)",
           "verdict_rule": {"single_ok": "假放行率 ≤10%(下游组合层 A/B 是真过滤器)",
                            "single_too_loose": "假放行率 ≥20% ⇒ 收紧 S 阈值(另立预注册)"}}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps(out["placebo"], ensure_ascii=False, indent=1))
    print(f"隐含 t(N={N_BANK}) = {out['implied_t_at_n_bank']}")
    print(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
