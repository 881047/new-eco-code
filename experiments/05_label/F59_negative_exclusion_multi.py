"""F59 负向因子的「坏侧剔除器」组合层检验（2026-09-13，预注册）

**来自用户的一个问题**：那些负向因子能不能反着用 —— 把它们的"坏"信息当筛子、剔掉差股票、
从而起正向作用？

**F58 机制量化给出的答案**（`F58_negative_factor_overlap.py`）：
  · 若"坏集"定义为**全市场十分位**（可实现的口径），模型 top100 里真有不少落在坏侧：
    `rc_cnt_30` 49.0 只/期、`br_cnt` 8.5 只/期、`ih_fund_ratio` 3.4 只/期（死区）；
  · 而 F57 当时把"坏集"定义成"已上榜券商票里的头部"，才只有 0.1 只/期 ⇒ **口径决定可行动性**。
  ⇒ 所以这个问题值得**在组合层真做一遍**（不是只算因子层读数）。

**预注册（跑数前冻结，与 F57 同源）**：
  基准 BASE = 生产融合分 top100 等权（`data/weekly/scores_20260911.npz` + 生产 PIT 掩码 +
              5 日 forward_excess + 真实卖出费率）
  处理臂 = 同一套打分，但**剔掉该因子死区坏侧的十分位**（逐期截面分位），落选名单顺位补足 100 只
  判据（四条全过才算通过"值得进下一步 A/B"，**通过≠上线**）:
    ① 全史净超额 ≥ BASE  ② 活跃区(2024-01~2026-08)净超额 ≥ BASE
    ③ |MDD| ≤ |MDD_BASE| + 2pp  ④ 换手 ≤ 1.20 × BASE
  另报：成对逐期差的 t（诊断，不参与判决）；**三臂同时测 ⇒ 多重比较风险**，故即使通过也只当候选，
        真要上线必须再走一次独立 A/B（换 pin 流程）。
  不做：不重训、不改打分、不碰生产账本/方案。

运行：``D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F59_negative_exclusion_multi.py``
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
                                      compute_report_rc_factors,
                                      compute_top10_holder_factors)
from app.pit.engine import PITEngine                                 # noqa: E402
from app.portfolio import fees as fee_mod                            # noqa: E402
from app.weekly import scores as sc                                  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "F59_negative_exclusion_multi_result.json"
NPZ = BASE_DIR / "data" / "weekly" / "scores_20260911.npz"
TOP_N = 100
DECILE = 0.10
PER_YEAR = 52.0
MAX_MDD_WORSE_PP = 2.0
MAX_TURNOVER_RATIO = 1.20
WINDOWS = {"FULL": ("2010-01-01", "2030-12-31"),
           "ACTIVE": ("2024-01-01", "2026-08-31"),
           "DEAD": ("2020-09-01", "2023-12-31")}
# 只测 F58 里"坏侧超额 t≤−2 且与模型 top100 有实质重叠"的三个因子（口径事先生效）
CANDIDATES = {"rc_cnt_30": "high", "ih_fund_ratio": "high", "br_cnt": "high"}
log = logging.getLogger("f59")


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
    log.info("面板就绪 %d 因子 | %.0fs", len(panels), time.time() - t0)

    arms = {"BASE": None}
    arms.update({f"X_{k}": k for k in CANDIDATES})
    series = {a: [] for a in arms}
    turn_acc = {a: 0.0 for a in arms}
    prev = {a: None for a in arms}
    used = []
    n_excl = {k: [] for k in CANDIDATES}
    for idx_k, d in enumerate(anchors):
        y, s = Y[idx_k], S[idx_k]
        ok = mask.iloc[idx_k].to_numpy() & np.isfinite(s) & np.isfinite(y)
        if ok.sum() < 200:
            continue
        order = np.argsort(-np.where(ok, s, -np.inf))
        picks = {"BASE": order[:TOP_N]}
        for arm, fname in arms.items():
            if arm == "BASE":
                continue
            panel = panels[fname]
            f = panel.reindex(index=anchors, columns=cols).to_numpy(dtype=float)[idx_k]
            both = ok & np.isfinite(f)
            if both.sum() < 100:
                picks[arm] = picks["BASE"]
                continue
            thr = np.quantile(f[both], 1 - DECILE)
            bad = both & (f >= thr)            # "high = 坏"口径（见 CANDIDATES）
            n_excl[fname].append(int(bad[order[:TOP_N]].sum()))
            sel = order[~bad[order]][:TOP_N]
            picks[arm] = sel if len(sel) >= TOP_N else picks["BASE"]
        for arm, sel in picks.items():
            w = np.full(len(sel), 1.0 / len(sel))
            gross = float(np.dot(w, y[sel].astype(float)))
            delta, cur = 0.0, dict(zip(sel, w))
            if prev[arm]:
                names = set(cur) | set(prev[arm])
                delta = sum(abs(cur.get(i, 0.0) - prev[arm].get(i, 0.0)) for i in names)
            prev[arm] = cur
            turn_acc[arm] += delta
            series[arm].append(gross - delta * sell_rate)
        used.append(d)                     # 逐期记录**实际参与**的锚点（跳过的期不能错位）
    df = pd.DataFrame(series, index=pd.DatetimeIndex(used))
    turns = {a: round(v / max(1, len(df)), 4) for a, v in turn_acc.items()}

    res = {"protocol": {"top_n": TOP_N, "decile": DECILE, "candidates": CANDIDATES,
                        "windows": WINDOWS, "sell_rate_bp": round(sell_rate * 1e4, 3),
                        "max_mdd_worse_pp": MAX_MDD_WORSE_PP,
                        "max_turnover_ratio": MAX_TURNOVER_RATIO},
           "n_periods": int(len(df)), "turnover": turns, "arms": {}}
    base_stats = {tag: window_stats(df["BASE"], tag) for tag in WINDOWS}
    for arm in df.columns:
        if arm == "BASE":
            continue
        st = {tag: window_stats(df[arm], tag) for tag in WINDOWS}
        d_full = (df[arm] - df["BASE"]).loc[WINDOWS["FULL"][0]:WINDOWS["FULL"][1]].dropna()
        d_act = (df[arm] - df["BASE"]).loc[WINDOWS["ACTIVE"][0]:WINDOWS["ACTIVE"][1]].dropna()

        def _t(x):
            return round(float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))), 2) if len(x) > 2 else None
        verdict = {
            "full_excess_ok": bool(st["FULL"]["excess_pp"] >= base_stats["FULL"]["excess_pp"]),
            "active_excess_ok": bool(st["ACTIVE"]["excess_pp"] >= base_stats["ACTIVE"]["excess_pp"]),
            "mdd_ok": bool(abs(st["FULL"]["mdd"]) <= abs(base_stats["FULL"]["mdd"]) + MAX_MDD_WORSE_PP / 100),
            "turnover_ok": bool(turns[arm] <= MAX_TURNOVER_RATIO * turns["BASE"]),
            "delta_full_pp": round(st["FULL"]["excess_pp"] - base_stats["FULL"]["excess_pp"], 2),
            "delta_active_pp": round(st["ACTIVE"]["excess_pp"] - base_stats["ACTIVE"]["excess_pp"], 2),
            "paired_t_full": _t(d_full), "paired_t_active": _t(d_act),
            "mean_excluded_per_period": round(float(np.mean(n_excl[arms[arm]])), 2) if n_excl[arms[arm]] else 0.0,
        }
        verdict["adopt_candidate"] = bool(all(verdict[k] for k in
                                              ("full_excess_ok", "active_excess_ok",
                                               "mdd_ok", "turnover_ok")))
        res["arms"][arm] = {"stats": st, "verdict": verdict}
    res["base"] = base_stats
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    print(f"\n生产融合分 top100，剔掉该因子坏侧十分位（{len(df)} 期，卖出费率 "
          f"{sell_rate*1e4:.2f}bp）")
    print(f"{'臂':<18}{'全史超额':>10}{'Δ全史':>8}{'活跃区':>9}{'Δ活跃':>8}{'MDD':>9}"
          f"{'换手':>8}{'剔除/期':>9}{'t(全史)':>8}")
    print(f"{'BASE':<18}{base_stats['FULL']['excess_pp']:>10.2f}{'—':>8}"
          f"{base_stats['ACTIVE']['excess_pp']:>9.2f}{'—':>8}{base_stats['FULL']['mdd']:>9.4f}"
          f"{turns['BASE']:>8.4f}{'—':>9}{'—':>8}")
    for arm, e in res["arms"].items():
        v, st = e["verdict"], e["stats"]
        print(f"{arm:<18}{st['FULL']['excess_pp']:>10.2f}{v['delta_full_pp']:>8.2f}"
              f"{st['ACTIVE']['excess_pp']:>9.2f}{v['delta_active_pp']:>8.2f}"
              f"{st['FULL']['mdd']:>9.4f}{turns[arm]:>8.4f}"
              f"{v['mean_excluded_per_period']:>9.2f}{str(v['paired_t_full']):>8}"
              f"  {'候选' if v['adopt_candidate'] else '否'}")
    print(f"\n结果 → {OUT.name} | {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
