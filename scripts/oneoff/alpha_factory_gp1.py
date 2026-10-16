# -*- coding: utf-8 -*-
"""GP 首次真实试挖 —— `plans/gp-mining-prereg-20260926.md` 的执行体。

机制: `Factory(PreRegistration) → add(GPProposer) → declare()`(冻结分母) `→ run()`
(死区+PIT 域求值 → gate 六判据 → alpha_bank 登记)。判据与出口声明全在预注册文件:
**PASS = POOLED = 有资格进组合层回测, 不等于可用**; 本轮"成功"定义 = 机制首跑 +
全部求值行入册 + 分母入账, **不是**挖出 PASS 因子。

用法:
    --smoke   接线验证: GPBudget(8,3)、**save=False 不入册不计数**(分母零成本)
    (默认)    真实轮: GPBudget(60,15)=900 分母, save=True, N_used = max(900, 在册+900)
产物: experiments/alpha_factory_gp1_20260926.json
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app import db
from app.alpha import gate
from app.alpha import bank
from app.alpha.factory import Factory, GPBudget, GPProposer, PreRegistration
from app.logger import setup_logger

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "alpha_factory_gp1_20260926.json"
OUT_SMOKE = ROOT / "experiments" / "alpha_factory_gp1_smoke.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true",
                    help="接线验证: 小预算 + save=False(不入册不计数)")
    args = ap.parse_args()
    setup_logger()
    db.init_db()

    if args.smoke:
        budget = GPBudget(population=8, generations=3)
        intent = ("wiring smoke — save=False, 不入册不计分母 "
                  "(gp-mining-prereg-20260926 冻结前接线验证)")
        save, out = False, OUT_SMOKE
    else:
        budget = GPBudget()          # 60 × 15 = 900, seed=20260911 (冻结)
        intent = ("GP 首次真实试挖: 机制首跑 + Pareto 前沿死区读数 + 分母 900 永久入账; "
                  "成功定义 = 跑通入册, 不是挖出 PASS "
                  "(plans/gp-mining-prereg-20260926.md §三)")
        save, out = True, OUT

    n_prior = bank.cumulative_tests()
    f = Factory(PreRegistration(intent=intent))
    m, dates_all, md_all = f.market()          # 幂等加载(声明前允许, 非求值)
    dead_dates = md_all[md_all >= pd.Timestamp(gate.DEAD[0])]
    f.add(GPProposer(m, dead_dates, budget=budget))
    denom = f.declare()
    print(f"[预注册] 分母 {denom} | 跑前在册 {n_prior} | "
          f"N_used = max({denom}, {n_prior}+{denom})", flush=True)

    t0 = time.time()
    records = f.run(save=save)
    elapsed = time.time() - t0
    n_used = max(denom, n_prior + denom) if save else denom
    recs = records.replace({np.nan: None}).to_dict("records") if len(records) else []
    n_pass = sum(1 for r in recs if r.get("PASS"))
    payload = {
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "prereg": "plans/gp-mining-prereg-20260926.md",
        "smoke": args.smoke, "save": save,
        "budget": {"population": budget.population, "generations": budget.generations,
                   "seed": budget.seed, "max_depth": budget.max_depth},
        "denominator": denom, "n_prior": n_prior, "n_used": n_used,
        "n_records": len(recs), "n_pass": n_pass,
        "n_duplicates": len(f.duplicates), "duplicates": f.duplicates,
        "elapsed_sec": round(elapsed, 1),
        "records": recs,
        "exit_note": "PASS=POOLED=有资格进组合层回测, 不等于可用; "
                     "0 PASS 的结论 = GP 在本仓死区×900 预算下无可检出增量",
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"\n产物: {out}")
    print(f"[读数] 求值记录 {len(recs)} | PASS {n_pass} | 撞车拒跑 {len(f.duplicates)} "
          f"| {elapsed:.0f}s")
    if len(records):
        cols = [c for c in ("alpha_id", "DEAD_head", "DEAD_head_t", "ACTIVE_head",
                            "p_adj", "PASS") if c in records.columns]
        print(records[cols].to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
