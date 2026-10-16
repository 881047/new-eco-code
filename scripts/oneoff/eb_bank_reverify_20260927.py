# -*- coding: utf-8 -*-
"""创新④ JKE 式贝叶斯复验第一轮 —— `plans/eb-bank-reverify-prereg-20260927.md` 的执行体。

EB 两组模型（预注册 §一 冻结）:
  数据 = 每因子月频 IC_0 序列（gate.ic_by_lag L=0, PIT 域, ≥2019-01 全部 DEAD+ACTIVE 锚点）;
  估计段 E = 除最后 12 锚点, 前瞻段 F = 最后 12 锚点;
  先验 = f0=N(0, s²), s=1.4826×MAD{t_E}; π0=Storey 式; f1=N(μ̂, s²+1/n_E), μ̂=mean{t_E:|t_E|≥2};
  证据 = t_F; 后验 eff_prob = (1−π0)f1 / [(1−π0)f1 + π0 f0]。
校准三问（§二 冻结）:
  C1 分离度: S1-S5 过档组 eff_prob 中位数 vs 拒绝组（反向/无差 ⇒ EB 无增量结清）;
  C2 与②冗余: eff_prob 与 HL 的 Spearman（|ρ|>0.7 ⇒ 同源, 如实登记）;
  C3 eff_prob ≥ 0.9 名单（读数非判决; 前瞻验证=月链累积后复跑, 登记待办）。
只读零写库。产物 experiments/eb_bank_reverify_20260927.json。
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
POOL_DIR = ROOT / "data" / "alpha_pool"
RETEST = ROOT / "experiments" / "single_factor_retest_s5_20260927.json"
OUT = ROOT / "experiments" / "eb_bank_reverify_20260927.json"
N_FORWARD = 12          # 前瞻段 F 锚点数
MIN_E, MIN_F = 24, 10   # 预注册 §一：段长下限


def robust_stats(t_series: np.ndarray) -> dict:
    s = 1.4826 * float(np.median(np.abs(t_series - np.median(t_series))))
    pi0 = min(1.0, float((np.abs(t_series) <= 1).sum()) / (0.6826 * len(t_series)))
    strong = t_series[np.abs(t_series) >= 2]
    mu = float(strong.mean()) if len(strong) else 0.0
    return {"s": s, "pi0": pi0, "mu_hat": mu, "n_strong": int(len(strong))}


def eff_prob(t_f: float, prior: dict) -> float:
    s = prior["s"]
    f0 = float(np.exp(-0.5 * (t_f / s) ** 2) / (s * np.sqrt(2 * np.pi)))
    s1 = float(np.sqrt(s ** 2 + 1.0 / 12))
    f1 = float(np.exp(-0.5 * ((t_f - prior["mu_hat"]) / s1) ** 2) / (s1 * np.sqrt(2 * np.pi)))
    num = (1 - prior["pi0"]) * f1
    den = num + prior["pi0"] * f0
    return float(num / den) if den > 0 else 0.0


def main() -> int:
    t0 = time.time()
    retest = json.loads(RETEST.read_text(encoding="utf-8"))
    s5_pass = {r["factor"] for r in retest["pool"]["rows"] if r.get("PASS_single")}
    s5_pass |= set(retest["bank"].get("pass_list") or [])
    s5_hl = {r["factor"]: r.get("half_life_months") for r in retest["pool"]["rows"]}
    s5_hl.update({r["alpha_id"]: r.get("half_life_months") for r in retest["bank"]["rows"]})
    s14_pass = {r["factor"] for r in retest["pool"]["rows"]
                if r.get("s1_head") and r.get("s2_t") and r.get("s3_same_sign")
                and r.get("s4_decor") is not False}
    s14_pass |= {r["alpha_id"] for r in retest["bank"]["rows"]
                 if r.get("s1_head") and r.get("s2_t") and r.get("s3_same_sign")
                 and r.get("s4_decor") is not False}

    m = load_market()
    md_all = month_end_dates(pd.DatetimeIndex(m["close"].index))
    md_eval = pd.DatetimeIndex(md_all[md_all >= pd.Timestamp("2019-01-01")])
    zones = gate.zone_of(md_eval)
    fwd = gate.forward_return(m, gate.HORIZON)
    pit = PITEngine(m=m)
    tradable = pit.tradable_matrix(md_eval)

    panels = []          # (family, name, panel)
    wide31 = T._load_factors_wide()
    wide31r = wide31.reorder_levels(["factor", "ts_code"], axis=1).sort_index(axis=1)
    for f in sorted(wide31r.columns.get_level_values("factor").unique()):
        sub = wide31r.xs(f, axis=1, level="factor")
        sub.index = pd.to_datetime(sub.index)
        panels.append(("pool", f, sub.reindex(index=md_eval)))
    conn = sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)
    bank_ids = [r[0] for r in conn.execute(
        "SELECT alpha_id FROM alpha_bank WHERE DEAD_head IS NOT NULL").fetchall()]
    conn.close()
    n_no_npz = 0
    for aid in bank_ids:
        npz = POOL_DIR / f"{aid}.npz"
        if not npz.exists():
            n_no_npz += 1
            continue
        d = np.load(npz, allow_pickle=True)
        pw = pd.DataFrame(d["values"].astype(float),
                          index=pd.to_datetime([str(x) for x in d["dates"].tolist()]),
                          columns=[str(x) for x in d["codes"].tolist()])
        panels.append(("bank", aid, pw.reindex(index=md_eval)))
    print(f"宇宙: 池 {sum(1 for p in panels if p[0]=='pool')} + bank 有读数 {len(bank_ids)}"
          f"（无 npz 跳过 {n_no_npz}）", flush=True)

    rows = []
    for family, name, panel in panels:
        cur_d = gate.ic_by_lag(panel, tradable, fwd, md_eval, zones, lags=(0,),
                               zone="DEAD")[0]
        cur_a = gate.ic_by_lag(panel, tradable, fwd, md_eval, zones, lags=(0,),
                               zone="ACTIVE")[0]
        ic_all = pd.concat([cur_d, cur_a]).sort_index().dropna()
        n = len(ic_all)
        if n < MIN_E + 3:
            rows.append({"family": family, "name": name, "note": f"锚点不足 n={n}",
                         "eff_prob": None, "hl": s5_hl.get(name)})
            continue
        ic_e, ic_f = ic_all.iloc[:-N_FORWARD], ic_all.iloc[-N_FORWARD:]
        t_e = float(ic_e.mean() / (ic_e.std(ddof=1) / np.sqrt(len(ic_e)))) \
            if len(ic_e) >= MIN_E and ic_e.std(ddof=1) > 0 else None
        t_f = float(ic_f.mean() / (ic_f.std(ddof=1) / np.sqrt(len(ic_f)))) \
            if len(ic_f) >= MIN_F and ic_f.std(ddof=1) > 0 else None
        rows.append({"family": family, "name": name, "n_anchors": int(n),
                     "t_e": t_e, "t_f": t_f, "ic_mean_full": float(ic_all.mean()),
                     "eff_prob": None, "hl": s5_hl.get(name),
                     "s5_pass": name in s5_pass, "s14_pass": name in s14_pass})
    print(f"IC 序列完成 {len(rows)} 因子（{time.time()-t0:.0f}s）", flush=True)

    fit = [r["t_e"] for r in rows if r.get("t_e") is not None]
    prior = robust_stats(np.array(fit)) if len(fit) >= 10 else None
    print(f"先验: s={prior['s']:.3f} π0={prior['pi0']:.3f} μ̂={prior['mu_hat']:+.3f} "
          f"(n_strong={prior['n_strong']}/{len(fit)})", flush=True)
    for r in rows:
        if prior and r.get("t_f") is not None:
            r["eff_prob"] = round(eff_prob(r["t_f"], prior), 4)

    meas = [r for r in rows if r.get("eff_prob") is not None]
    grp_pass = [r["eff_prob"] for r in meas if r.get("s5_pass")]
    grp_rej = [r["eff_prob"] for r in meas if not r.get("s14_pass")]
    med_pass = float(np.median(grp_pass)) if grp_pass else None
    med_rej = float(np.median(grp_rej)) if grp_rej else None
    c1 = {"n_pass": len(grp_pass), "median_pass": med_pass,
          "n_reject": len(grp_rej), "median_reject": med_rej,
          "verdict": ("SEPARATED" if (med_pass is not None and med_rej is not None
                                      and med_pass > med_rej) else "CLEARED")}
    both = [(r["eff_prob"], r["hl"]) for r in meas
            if r.get("hl") is not None and r["eff_prob"] is not None]
    if len(both) >= 10:
        a, b = zip(*both)
        c2 = {"spearman_effprob_hl": float(pd.Series(a).corr(pd.Series(b),
                                                             method="spearman")),
              "n": len(both), "note": "|ρ|>0.7 ⇒ 与 S5 同源（应正交互补）"}
    else:
        c2 = {"note": "样本不足"}
    top = sorted([r for r in meas if r["eff_prob"] >= 0.9],
                 key=lambda r: -r["eff_prob"])
    out = {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "prereg": "plans/eb-bank-reverify-prereg-20260927.md",
        "n_universe": len(rows), "n_no_npz_skipped": n_no_npz,
        "prior": prior, "n_forward_anchors": N_FORWARD,
        "C1_separation": c1,
        "C2_redundancy_with_s5": c2,
        "C3_eff_prob_ge_090": [r["name"] for r in top],
        "rows": rows,
        "note": "纯读数工具; 前瞻验证=月链累积新锚点后复跑（预注册 §三 待办）; 判决词由人填",
        "runtime_sec": round(time.time() - t0, 1),
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"C1: 过档组 {med_pass} (n={len(grp_pass)}) vs 拒绝组 {med_rej} "
          f"(n={len(grp_rej)}) ⇒ {c1['verdict']}", flush=True)
    print(f"C2: {json.dumps(c2, ensure_ascii=False)}", flush=True)
    print(f"C3: eff_prob≥0.9 共 {len(top)} 个", flush=True)
    print(f"产物: {OUT}  ({time.time() - t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
