"""F63 「筛法本身」的方法学网格（2026-09-13，预注册）

**用户判断（2026-09-13 晚）**："因子的**用法逻辑**没问题，但**实践方法**有问题" —— 我同意并照此重做：
前几轮的失败（F60 正因子倾斜 −27pp、F61 正因子硬筛 −28pp）**不是逻辑错，而是筛法太钝**：
  · 负因子用"全池十分位"⇒ 对 `br_cnt` 来说等于"凡有券商覆盖就算坏"（~300 只），剔得又钝又不准；
  · 正因子用"两个都高于中位数"⇒ 只剩 4.2/100 只，等于把 96% 的持仓换掉；
  · 正因子还可能是**周期不匹配**（它们是 21 日口径的因子，被我用在了周频两端）。

**本实验换更精细的实现（逻辑不变：负的剔、正的留）**：
  负筛 N2（**只剔极端**）：合成坏分 = z(`br_cnt`) + z(`ih_fund_ratio`)（都是"高=坏"），
                     只剔 **合成分的前 5%**（不再是两个十分位的并集）
  正用 P2（**只在边界起作用**）：先按生产分取 **前 150 名**，在这 150 名内用
                     `z(生产分) + 0.3·z(合成好分)` 重排后取 100 —— 正因子只决定"边缘席位"，
                     不再动核心持仓（合成好分 = z(`margin_net_share`) + z(`mf_main_sm_div`)）
  正用 P3（**当权重、不当闸门**）：前 100 名不变，权重 ∝ 1 + 0.5·z(合成好分)（截断到 [0.5, 1.5]）
  臂：BASE / N2 / P2 / P3 / N2+P2 / N2+P3（6 臂，预注册）
  判据（与 F57/F59/F60/F61 同门槛，四条全过 = 候选）：全史净超额不劣化 + 活跃区不劣化 +
    |MDD| 不劣化超 2pp + 换手 ≤1.20×BASE。另报成对 t。
  ⚠️ 6 臂高度相关 ⇒ 单臂胜出不算证据（F56：PBO=0.434），仍须独立 A/B 才能上线。
  口径与生产一致：`sc.forward_excess` 与 F1b 的 `forward_excess_open` **同一公式**（已逐行核对，见 05 号）。

运行：``D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F63_screen_method_grid.py``
"""
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from app import config as cfg_mod                                    # noqa: E402
from app.factors import factor_engine as fe                          # noqa: E402
from app.factors.flow_factors import (compute_broker_factors,        # noqa: E402
                                      compute_flow_factors,
                                      compute_report_rc_factors,
                                      compute_top10_holder_factors)
from app.pit.engine import PITEngine                                 # noqa: E402
from app.portfolio import fees as fee_mod                            # noqa: E402
from app.weekly import scores as sc                                  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "F63_screen_method_grid_result.json"
NPZ = BASE_DIR / "data" / "weekly" / "scores_20260911.npz"
TOP_N, N_POOL, PER_YEAR = 100, 150, 52.0
NEG_Q, POS_W, WEIGHT_W, WEIGHT_CLIP = 0.05, 0.30, 0.50, (0.5, 1.5)
MAX_MDD_WORSE_PP, MAX_TURNOVER_RATIO = 2.0, 1.20
WINDOWS = {"FULL": ("2010-01-01", "2030-12-31"),
           "ACTIVE": ("2024-01-01", "2026-08-31"),
           "DEAD": ("2020-09-01", "2023-12-31")}
NEG_FACTORS = ["br_cnt", "ih_fund_ratio"]
POS_FACTORS = ["margin_net_share", "mf_main_sm_div"]
ARMS = ["BASE", "N2", "P2", "P3", "N2_P2", "N2_P3"]
log = logging.getLogger("f63")


def _z(v: np.ndarray, ok: np.ndarray) -> np.ndarray:
    out = np.full(len(v), np.nan)
    if ok.sum() >= 50:
        sd = float(np.nanstd(v[ok]))
        if sd > 0:
            out[ok] = (v[ok] - float(np.nanmean(v[ok]))) / sd
    return out


def window_stats(ret: pd.Series, tag: str) -> dict:
    lo, hi = WINDOWS[tag]
    r = ret.loc[(ret.index >= lo) & (ret.index <= hi)].dropna()
    if len(r) < 3:
        return {"n": int(len(r))}
    nav = (1 + r).cumprod()
    return {"n": int(len(r)),
            "excess_pp": round(100 * float((1 + r).prod() ** (PER_YEAR / len(r)) - 1), 2),
            "mean_pp": round(100 * float(r.mean()), 3),
            "sharpe": round(float(r.mean() / r.std(ddof=1)), 3),
            "mdd": round(float((nav / nav.cummax() - 1).min()), 4)}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    with np.load(NPZ, allow_pickle=True) as z:
        S = np.asarray(z["scores"], dtype=float)
        anchors = pd.to_datetime([str(d) for d in z["anchors"]])
        cols = [str(c) for c in z["cols"]]
    m = fe.load_market(since=sc.SINCE)
    pit = PITEngine(m=m, cfg=cfg)
    mask = pit.tradable_matrix(anchors, columns=cols).fillna(False)
    Y = sc.forward_excess(m, mask, sc.HORIZON)
    sell_rate = fee_mod.effective_rate(25_000.0, "sell", "20260914")
    daily = pd.to_datetime(m["close"].index)
    panels = {}
    panels.update(compute_broker_factors(daily))
    panels.update(compute_report_rc_factors(m, daily, source="em"))
    panels.update(compute_top10_holder_factors(m, daily))
    panels.update(compute_flow_factors(m, daily, include_research=False))
    miss = [f for f in NEG_FACTORS + POS_FACTORS if f not in panels]
    if miss:
        log.error("缺因子 %s", miss)
        return 2
    P = {k: panels[k].reindex(index=anchors, columns=cols).to_numpy(dtype=float)
         for k in NEG_FACTORS + POS_FACTORS}
    log.info("面板就绪 | %.0fs", time.time() - t0)

    series = {a: [] for a in ARMS}
    turn_acc = {a: 0.0 for a in ARMS}
    prev = {a: None for a in ARMS}
    used, n_excl, n_edge = [], [], []
    for k, d in enumerate(anchors):
        y, s = Y[k], S[k]
        ok = mask.iloc[k].to_numpy() & np.isfinite(s) & np.isfinite(y)
        if ok.sum() < 200:
            continue
        zn = sum(_z(P[f][k], ok & np.isfinite(P[f][k])) for f in NEG_FACTORS)   # 高=坏
        zp = sum(_z(P[f][k], ok & np.isfinite(P[f][k])) for f in POS_FACTORS)   # 高=好
        zs = _z(s, ok)
        order = np.argsort(-np.where(ok, s, -np.inf))
        # N2: 只剔合成坏分前 5%
        bad = ok & np.isfinite(zn)
        if bad.sum() >= 100:
            thr = np.quantile(zn[bad], 1 - NEG_Q)
            bad &= zn >= thr
        order_n = order[~bad[order]]
        if len(order_n) < TOP_N:
            order_n = order
        # P2: 前 150 名内重排（正因子只决定边缘席位）
        pool = order[:N_POOL]
        combo = np.where(ok, zs + POS_W * np.nan_to_num(zp), -np.inf)
        order_p2 = pool[np.argsort(-combo[pool])]
        # P3: 前 100 名不变，权重按好分倾斜
        sel_p3 = order[:TOP_N]
        w_p3 = np.clip(1.0 + WEIGHT_W * np.nan_to_num(zp[sel_p3]), *WEIGHT_CLIP)
        w_p3 = w_p3 / w_p3.sum()
        picks = {"BASE": order[:TOP_N], "N2": order_n[:TOP_N], "P2": order_p2[:TOP_N],
                 "P3": sel_p3,
                 "N2_P2": order_n[:N_POOL][np.argsort(-combo[order_n[:N_POOL]])][:TOP_N]
                 if len(order_n) >= N_POOL else order_n[:TOP_N],
                 "N2_P3": order_n[:TOP_N]}
        wmap = {"P3": (sel_p3, w_p3), "N2_P3": (order_n[:TOP_N], None)}
        n_excl.append(int(bad[order[:TOP_N]].sum()))
        n_edge.append(int(len(set(pool[:TOP_N]) - set(order_p2[:TOP_N]))))
        for arm, sel in picks.items():
            if arm == "P3":
                w = w_p3
            elif arm == "N2_P3":
                ww = np.clip(1.0 + WEIGHT_W * np.nan_to_num(zp[sel]), *WEIGHT_CLIP)
                w = ww / ww.sum()
            else:
                w = np.full(len(sel), 1.0 / len(sel))
            gross = float(np.dot(w, y[sel].astype(float)))
            delta, cur = 0.0, dict(zip(sel, w))
            if prev[arm]:
                names = set(cur) | set(prev[arm])
                delta = sum(abs(cur.get(i, 0.0) - prev[arm].get(i, 0.0)) for i in names)
            prev[arm] = cur
            turn_acc[arm] += delta
            series[arm].append(gross - delta * sell_rate)
        used.append(d)
    df = pd.DataFrame(series, index=pd.DatetimeIndex(used))
    turns = {a: round(v / max(1, len(df)), 4) for a, v in turn_acc.items()}
    base = {tag: window_stats(df["BASE"], tag) for tag in WINDOWS}
    res = {"protocol": {"top_n": TOP_N, "pool": N_POOL, "neg_q": NEG_Q, "pos_w": POS_W,
                        "weight_w": WEIGHT_W, "weight_clip": WEIGHT_CLIP,
                        "arms": ARMS, "windows": WINDOWS,
                        "sell_rate_bp": round(sell_rate * 1e4, 3),
                        "multiplicity_warning": "6 臂高度相关 ⇒ 单臂胜出须独立 A/B"},
           "n_periods": int(len(df)), "turnover": turns, "base": base,
           "mean_excluded_per_period": round(float(np.mean(n_excl)), 2),
           "mean_edge_swaps_per_period": round(float(np.mean(n_edge)), 2), "arms": {}}

    def _t(x):
        return round(float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))), 2) if len(x) > 2 else None
    for arm in ARMS[1:]:
        st = {tag: window_stats(df[arm], tag) for tag in WINDOWS}
        d_all = (df[arm] - df["BASE"]).dropna()
        d_act = d_all.loc[WINDOWS["ACTIVE"][0]:WINDOWS["ACTIVE"][1]]
        v = {"delta_full_pp": round(st["FULL"]["excess_pp"] - base["FULL"]["excess_pp"], 2),
             "delta_active_pp": round(st["ACTIVE"]["excess_pp"] - base["ACTIVE"]["excess_pp"], 2),
             "full_excess_ok": bool(st["FULL"]["excess_pp"] >= base["FULL"]["excess_pp"]),
             "active_excess_ok": bool(st["ACTIVE"]["excess_pp"] >= base["ACTIVE"]["excess_pp"]),
             "mdd_ok": bool(abs(st["FULL"]["mdd"]) <= abs(base["FULL"]["mdd"]) + MAX_MDD_WORSE_PP / 100),
             "turnover_ok": bool(turns[arm] <= MAX_TURNOVER_RATIO * turns["BASE"]),
             "paired_t_full": _t(d_all), "paired_t_active": _t(d_act)}
        v["candidate"] = bool(all(v[x] for x in ("full_excess_ok", "active_excess_ok",
                                                 "mdd_ok", "turnover_ok")))
        res["arms"][arm] = {"stats": st, "verdict": v}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    print(f"\n{len(df)} 期 | 负筛平均剔 {res['mean_excluded_per_period']} 只/期 | "
          f"正因子(边界)平均换位 {res['mean_edge_swaps_per_period']} 只/期")
    print(f"{'臂':<8}{'全史超额':>10}{'Δ全史':>8}{'活跃区':>9}{'Δ活跃':>8}{'MDD':>9}"
          f"{'换手':>8}{'t全史':>7}{'t活跃':>7}  判定")
    print(f"{'BASE':<8}{base['FULL']['excess_pp']:>10.2f}{'—':>8}{base['ACTIVE']['excess_pp']:>9.2f}"
          f"{'—':>8}{base['FULL']['mdd']:>9.4f}{turns['BASE']:>8.4f}{'—':>7}{'—':>7}  —")
    for arm, e in res["arms"].items():
        v, st = e["verdict"], e["stats"]
        print(f"{arm:<8}{st['FULL']['excess_pp']:>10.2f}{v['delta_full_pp']:>8.2f}"
              f"{st['ACTIVE']['excess_pp']:>9.2f}{v['delta_active_pp']:>8.2f}"
              f"{st['FULL']['mdd']:>9.4f}{turns[arm]:>8.4f}{str(v['paired_t_full']):>7}"
              f"{str(v['paired_t_active']):>7}  {'候选' if v['candidate'] else '否'}")
    print(f"\n结果 → {OUT.name} | {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
