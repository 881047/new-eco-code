# -*- coding: utf-8 -*-
"""周频袖套 × bank 过档因子筛选 —— `plans/sleeve-bank-screen-prereg-20260927.md` 的执行体。

候选域（预注册 §一 机械派生）: bank S1-S5 过档 39 个 →
  qlib158 表达式族: 仓库自有引擎(`app/alpha/expr.py` + `zoos.py` 冻结表达式)逐字求值,
    日线窗口在周锚点取样(窗口终点=锚点, PIT 安全);
  margin/mf 族: `flow_factors` 按完整日频索引计算(**滚动按交易日计数**, 不得传周锚点),
    再在周锚点取样;
  后置族(llm/m60/kg): 面板注册表在 llm 文本线 / min60 表 / KG 管线 —— 本轮未测。
V0 验证门: 每候选在 npz 重叠月锚点上对照官方 npz 截面(逐锚点 Spearman, ≥12 锚点,
  中位数 ≥0.99)——不可信者剔除出读数。
三道门(预注册 §三): G1 时效(half_life_from_curve, lag=周锚步, bar=2.0, min 24) /
  G2 强度(|IC0(5d,DEAD)| ≥ 0.02) / G3 族增量(vs 袖套 31 |ρ|max ≤ 0.7, 逐锚点 Spearman)。
机械判决: 全过 ⇒ 袖套 A/B 候选名单; 0 个 ⇒ 线结清。只读零写库。
产物 experiments/sleeve_bank_screen_20260927.json。
"""
import json
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from app import db
from app import config as cfg_mod
from app.alpha import expr as AE
from app.alpha import gate, zoos
from app.factors import factor_engine as fe
from app.factors.flow_factors import compute_margin_factors, compute_moneyflow_factors
from app.factors.factor_engine import load_market
from app.weekly.scores import build_panel, forward_excess, week_end_dates, HORIZON

ROOT = Path(__file__).resolve().parents[2]
RETEST = ROOT / "experiments" / "single_factor_retest_s5_20260927.json"
OUT = ROOT / "experiments" / "sleeve_bank_screen_20260927.json"
PREREG = "plans/sleeve-bank-screen-prereg-20260927.md"

G2_IC0_BAR = 0.02
G3_RHO_BAR = 0.70
LAGS = (0, 1, 2, 3, 4, 5)
MIN_NAMES = 200
V0_MIN_ANCHORS = 12
V0_MEDIAN = 0.99


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def spearman_by_anchor(a_df: pd.DataFrame, b_df: pd.DataFrame, min_names=MIN_NAMES):
    """逐锚点 Spearman（先秩后 Pearson），≥min_names 只才计。"""
    ar, br = a_df.rank(axis=1), b_df.rank(axis=1)
    out = {}
    for d in a_df.index:
        if d not in br.index:
            continue
        jn = pd.concat([ar.loc[d].rename("a"), br.loc[d].rename("b")], axis=1).dropna()
        if len(jn) < min_names:
            continue
        out[d] = float(jn["a"].corr(jn["b"]))
    return out


def main() -> int:
    t0 = time.time()
    retest = json.loads(RETEST.read_text(encoding="utf-8"))
    pass_list = list(retest["bank"]["pass_list"])

    m = load_market()
    cfg = cfg_mod.load_config()
    daily_dates = pd.DatetimeIndex(m["close"].index)
    all_weeks = week_end_dates(daily_dates)
    # 判定区: DEAD(2020-09-01..2023-12-31, 与月频 S5 同区) + 前置(2020-06 起, lag-5 有数)
    anchors = all_weeks[(all_weeks >= pd.Timestamp("2020-06-01"))
                        & (all_weeks <= pd.Timestamp(gate.DEAD[1]))]

    # ---------- 候选域机械派生 ----------
    expr_map = {name: expr for name, expr, _ in zoos.qlib158()}
    with db.conn_ctx() as conn:
        industry = fe.industry_map(conn)
    flow_daily = {}
    flow_daily.update(compute_margin_factors(m, daily_dates))
    flow_daily.update(compute_moneyflow_factors(m, daily_dates))
    qlib_cands = [a for a in pass_list if a in expr_map]
    flow_cands = [a for a in pass_list if a in flow_daily]
    deferred = [a for a in pass_list if a not in set(qlib_cands) | set(flow_cands)]
    log(f"候选域: qlib158 {len(qlib_cands)} + margin/mf {len(flow_cands)}"
        f" + 后置 {len(deferred)}: {deferred}")

    # ---------- V0 验证门 ----------
    v0_rows, trusted = [], {}
    for aid in pass_list:
        npz = ROOT / "data" / "alpha_pool" / f"{aid}.npz"
        if not npz.exists():
            continue
        z = np.load(npz, allow_pickle=True)
        npz_df = pd.DataFrame(z["values"].astype(float),
                              index=pd.to_datetime([str(x) for x in z["dates"].tolist()]),
                              columns=[str(x) for x in z["codes"].tolist()])
        if aid in expr_map:
            try:
                daily = AE.Engine(AE.build_panel(m)).eval(expr_map[aid])
            except Exception as e:                                  # noqa: BLE001
                v0_rows.append({"factor": aid, "v0": "EVAL_FAIL", "err": str(e)[:120]})
                continue
        elif aid in flow_daily:
            daily = flow_daily[aid]
        else:
            continue
        common = [d for d in npz_df.index if d in daily.index]
        if len(common) < V0_MIN_ANCHORS:
            v0_rows.append({"factor": aid, "v0": "TOO_FEW_ANCHORS", "n": len(common)})
            continue
        # npz 存的是中性化后值(截面 mean 0/std 1, 实测) ⇒ 实现侧同款 fe.neutralize 再比
        daily_neu = fe.neutralize(daily.loc[common], industry)
        cors = list(spearman_by_anchor(daily_neu, npz_df,
                                       min_names=MIN_NAMES).values())
        med = float(np.median(cors)) if cors else np.nan
        ok = med >= V0_MEDIAN
        v0_rows.append({"factor": aid, "v0": "PASS" if ok else "FAIL",
                        "median_spearman": round(med, 4), "n_anchors": len(cors)})
        if ok:
            trusted[aid] = daily
    log(f"V0: 通过 {len(trusted)}/{len(pass_list)}"
        f"（fail {sum(1 for v in v0_rows if v.get('v0') == 'FAIL')}, "
        f"defered/err {sum(1 for v in v0_rows if v.get('v0') not in ('PASS', 'FAIL'))}）")

    # ---------- 周锚点面板（候选中性化 + 袖套 31 + 5 日标签） ----------
    cols = list(m["close"].columns)          # 与袖套 build_panel 同一列集
    cand_week = {}
    for aid, daily in trusted.items():
        sub = daily.reindex(index=anchors, columns=cols)
        cand_week[aid] = fe.neutralize(sub, industry)
    sleeve = build_panel(m, anchors, cfg)
    fwd = np.asarray(forward_excess(m, sleeve["mask"], HORIZON), dtype=float)
    mk = sleeve["mask"].to_numpy(dtype=bool)
    nA, nS, nSleeve = len(anchors), len(sleeve["cols"]), len(sleeve["names"])
    log(f"周锚点 {nA} | 袖套因子 {nSleeve} | 候选进入读数 {len(cand_week)}")

    # ---------- G1/G2: 候选 lag 衰减曲线（秩向量化, lag 行入掩码） ----------
    cand_names = sorted(cand_week)
    Rc = np.full((nA, nS, len(cand_names)), np.nan)
    for j, aid in enumerate(cand_names):
        Rc[:, :, j] = cand_week[aid].to_numpy(dtype=float)
    finC = np.isfinite(Rc)
    Rl = pd.DataFrame(fwd).rank(axis=1).to_numpy()
    finL = np.isfinite(fwd)
    base_i = mk & finL

    ic_curves = {j: {L: [] for L in LAGS} for j in range(len(cand_names))}
    for i in range(nA):
        bi = base_i[i]
        if bi.sum() < MIN_NAMES:
            continue
        rl = Rl[i]
        for L in LAGS:
            if i - L < 0:
                continue
            for j in range(len(cand_names)):
                vj = bi & finC[i, :, j] & finC[i - L, :, j]
                if vj.sum() < MIN_NAMES:
                    continue
                ic_curves[j][L].append(float(np.corrcoef(Rc[i - L, :, j][vj], rl[vj])[0, 1]))

    # ---------- G3: vs 袖套 31（逐锚点全对秩相关, 一次算完） ----------
    sleeve_F = sleeve["F"]
    finS = np.isfinite(sleeve_F)
    Rs = np.full_like(sleeve_F, np.nan)
    for j in range(nSleeve):
        Rs[:, :, j] = pd.DataFrame(sleeve_F[:, :, j]).rank(axis=1).to_numpy()
    rho_sum = np.zeros((len(cand_names), nSleeve))
    rho_cnt = np.zeros((len(cand_names), nSleeve))
    for i in range(nA):
        both = pd.concat([pd.DataFrame(Rc[i]).rename(columns=dict(enumerate(cand_names))),
                          pd.DataFrame(Rs[i]).rename(columns=dict(enumerate(sleeve["names"])))],
                         axis=1)
        cm = both.corr(min_periods=MIN_NAMES)
        if cm.empty:
            continue
        block = cm.loc[list(cand_names), list(sleeve["names"])].to_numpy()
        rho_sum += np.nan_to_num(block)
        rho_cnt += np.isfinite(block).astype(int)
    with np.errstate(invalid="ignore", divide="ignore"):
        rho_mean = np.where(rho_cnt > 0, rho_sum / np.maximum(rho_cnt, 1), np.nan)

    # ---------- 机械判决 ----------
    out_rows = []
    for j, aid in enumerate(cand_names):
        ic_dead = {L: (float(np.mean(v)), len(v)) for L, v in ic_curves[j].items() if v}
        hl = gate.half_life_from_curve(ic_dead)
        g1 = hl["s5_half_life"]
        ic0 = ic_dead.get(0, (np.nan, 0))[0]
        g2 = None if ic0 is None or ic0 != ic0 else bool(abs(ic0) >= G2_IC0_BAR)
        rhos = rho_mean[j]
        g3 = None if np.all(~np.isfinite(rhos)) else bool(np.nanmax(np.abs(rhos)) <= G3_RHO_BAR)
        g3_max = float(np.nanmax(np.abs(rhos))) if np.any(np.isfinite(rhos)) else None
        g3_worst = (sleeve["names"][int(np.nanargmax(np.abs(rhos)))]
                    if g3_max is not None else None)
        passed = bool(g1 and g2 and g3)
        out_rows.append({"factor": aid, "ic0_5d": None if ic0 is None or ic0 != ic0
                         else round(ic0, 4), "hl_weeks": hl["half_life_months"],
                         "censored": hl["censored"], "g1_timeliness": g1,
                         "g2_strength": g2, "g3_max_rho": (None if g3_max is None
                                                           else round(g3_max, 3)),
                         "g3_worst": g3_worst, "g3_increment": g3,
                         "PASS_screen": passed})
    for r in sorted(out_rows, key=lambda r: -(r["ic0_5d"] or 0)):
        log(f"  {r['factor']:14s} IC0(5d) {r['ic0_5d']} HL {r['hl_weeks']} "
            f"G1 {r['g1_timeliness']} G2 {r['g2_strength']} G3 {r['g3_increment']} "
            f"(max|ρ| {r['g3_max_rho']} vs {r['g3_worst']})"
            f"{' ⇒ 候选' if r['PASS_screen'] else ''}")
    cands = [r for r in out_rows if r["PASS_screen"]]
    verdict = ("CANDIDATES" if cands else "CLEARED")
    log(f"判决: {verdict} —— 候选 {len(cands)} 个: {[r['factor'] for r in cands]}")

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"), "prereg": PREREG,
           "pass_list_n": len(pass_list),
           "candidate_domains": {"qlib158": qlib_cands, "flow": flow_cands,
                                 "deferred": deferred},
           "v0": v0_rows, "n_anchors": nA,
           "criteria": {"g1": "HL≥2 周锚步或删失", "g2": f"|IC0(5d)|≥{G2_IC0_BAR}",
                        "g3": f"vs 袖套31 |ρ|max≤{G3_RHO_BAR}"},
           "rows": out_rows,
           "verdict": {"PASS_screen_n": len(cands),
                       "candidates": [r["factor"] for r in cands],
                       "verdict": verdict,
                       "next": ("袖套级 A/B 预注册(31 vs 31+N, F52 式基线回测)"
                                if cands else "线结清, 登记勿重测")},
           "note": "readings 级筛选; 生产变更须袖套 A/B 预注册+拍板; 后置族未测如实列报",
           "runtime_sec": round(time.time() - t0, 1)}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"产物: {OUT}  ({time.time() - t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
