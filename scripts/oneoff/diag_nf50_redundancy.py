# -*- coding: utf-8 -*-
"""诊断：新过线 50 因子的内部冗余与家族结构（2026-09-27，只读）。

回答"58 因子构型组合层为什么崩"的机制问题（nf prereg §六 判定解读的定量支撑）：
① 50 个新过线因子两两 |ρ|（S4 只对 31 在池测过去相关，候选彼此之间没测过 —— 这就是缺口）；
② 家族内冗余（蜡烛五兄弟 / IMAX-IMXD / CORD / SUMP-VSUM / m60 / llm / margin-flow）；
③ 50 因子块 vs 保留 8 / 被剔除 23 的相关（被删因子的"影子"有没有被新因子带回来）；
④ 50 因子块的有效维度（秩相关矩阵谱）。
只读 factor 表与 npz，产物 experiments/diag_nf50_redundancy_20260927.json。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app.models import trainer as T

ROOT = Path(__file__).resolve().parents[2]
POOL = ROOT / "data" / "alpha_pool"
RETEST = ROOT / "experiments" / "single_factor_retest_20260926.json"
OUT = ROOT / "experiments" / "diag_nf50_redundancy_20260927.json"

FAMILIES = {
    "candle": ["KMID", "KLEN", "KUP", "KLOW", "KSFT"],
    "imax_imxd": ["IMAX20", "IMAX30", "IMAX60", "IMXD20", "IMXD30", "IMXD60"],
    "cord": ["CORD5", "CORD10", "CORD20", "CORD30", "CORD60"],
    "cntd": ["CNTP5", "CNTD10"],
    "sump_vsum": ["SUMP10", "SUMD10", "VSTD10", "VSUMP10", "VSUMP20", "VSUMP60",
                  "VSUMD10", "VSUMD20", "VSUMD60"],
    "beta_resi_rsv": ["BETA10", "RESI30", "RSV10"],
    "margin_flow": ["margin_buy_share", "margin_net_share", "margin_net_to_rzye",
                    "mf_lg_net", "mf_main_sm_div"],
    "m60": ["m60_cv10", "m60_cv20", "m60_cv5", "m60_lr10", "m60_lr20", "m60_lr5",
            "m60_lvsxlr"],
    "llm": ["llm_intraday_reversal_turnover", "llm_turnover_accel_5_20",
            "llm_turnover_to_pb", "llm_positive_day_ratio_20", "llm_turnover_accel",
            "llm_vwap_premium", "llm_price_skew_20"],
    "kg": ["kg_broker_rec_3m"],
}


def main() -> int:
    d = json.loads(RETEST.read_text(encoding="utf-8"))
    bank_pass = list(d["bank"]["pass_list"])
    pool_pass = sorted(r["factor"] for r in d["pool"] if r.get("PASS_single"))
    pool_fail = sorted(r["factor"] for r in d["pool"] if not r.get("PASS_single"))

    wide = T._load_factors_wide()
    wide_r = wide.reorder_levels(["factor", "ts_code"], axis=1).sort_index(axis=1)
    ref_index = pd.to_datetime(wide.index)

    panels = {}
    for f in pool_pass + pool_fail:
        p = wide_r.xs(f, axis=1, level="factor")
        p.index = pd.to_datetime(p.index)
        panels[f] = p
    for aid in bank_pass:
        z = np.load(POOL / f"{aid}.npz", allow_pickle=True)
        pw = pd.DataFrame(z["values"].astype(float),
                          index=pd.to_datetime([str(x) for x in z["dates"].tolist()]),
                          columns=[str(x) for x in z["codes"].tolist()])
        pw = pw.reindex(index=ref_index, columns=panels[pool_pass[0]].columns)
        panels[aid] = pw

    anchors = [dt for dt in ref_index[::4] if dt in panels[bank_pass[0]].index]
    names50 = bank_pass
    names_all = names50 + pool_pass + pool_fail
    print(f"锚点子样本 {len(anchors)} 个（每 4 期取 1）", flush=True)

    # 逐锚点秩相关 |ρ|（公共非缺失 ≥200），跨锚点平均 —— 与 single_factor_retest 同法
    abs_sum = pd.DataFrame(0.0, index=names_all, columns=names_all)
    cnt = pd.DataFrame(0, index=names_all, columns=names_all)
    for dt in anchors:
        df = pd.DataFrame({n: panels[n].loc[dt] for n in names_all})
        C = df.rank().corr(min_periods=200).abs()
        C = C.fillna(0.0)
        abs_sum += C
        cnt += (C > 0).astype(int)
    mean_c = abs_sum / cnt.replace(0, np.nan)

    def block_stats(cols):
        if len(cols) < 2:
            return None
        mat = mean_c.loc[cols, cols].to_numpy()
        iu = np.triu_indices(len(cols), 1)
        v = mat[iu]
        v = v[~np.isnan(v)]
        return (round(float(v.mean()), 3), round(float(v.max()), 3)) if len(v) else None

    mean50, max50 = block_stats(names50)
    fam = {k: block_stats([c for c in v if c in panels]) for k, v in FAMILIES.items()}
    fam = {k: v for k, v in fam.items() if v}
    mean_vs8 = float(mean_c.loc[names50, pool_pass].to_numpy().mean())
    mean_vs_fail23 = float(mean_c.loc[names50, pool_fail].to_numpy().mean())
    shadow = sorted(((a, b, round(float(mean_c.loc[a, b]), 3))
                     for a in names50 for b in pool_fail), key=lambda x: -x[2])[:8]

    X = mean_c.loc[names50, names50].to_numpy()
    X = np.nan_to_num(X)
    eig = np.sort(np.linalg.eigvalsh(X))[::-1]
    eig = np.clip(eig, 0, None)
    cum = np.cumsum(eig / eig.sum())
    eff90 = int(np.searchsorted(cum, 0.90) + 1)

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"),
           "n_anchors": len(anchors),
           "within_50_mean_abs_rho": round(mean50, 3), "within_50_max": round(max50, 3),
           "families": {k: {"mean": round(v[0], 3), "max": round(v[1], 3)}
                        for k, v in fam.items()},
           "mean_abs_rho_50_vs_kept8": round(mean_vs8, 3),
           "mean_abs_rho_50_vs_removed23": round(mean_vs_fail23, 3),
           "top_shadows_vs_removed23": shadow,
           "effective_dim_90pct": eff90}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
