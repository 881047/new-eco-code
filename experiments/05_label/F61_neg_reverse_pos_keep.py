"""F61 「负因子反向筛 + 正因子正向筛」组合（2026-09-13，预注册）

**用户澄清后的思路**（2026-09-13 晚）：
  · 副作用因子要**反向筛**：它"高分侧"其实是坏的（例：被券商推荐越多、未来越差）
    ⇒ **剔掉它的高分侧、保留低分侧**（= 把它的判断翻过来用）；
  · 正作用因子**正向筛**：直接保留它认为好的那一侧；
  · 两者**叠加**；若可行，再改造模型层去适配。

F60 测过的与这里**只差一处**：F60 的 `POS_BLEND` 是把正因子**倾斜**进打分（z 加权），
结果 −27pp 灾难；本实验改成**筛选**（good side 优先，不够 100 只再由其余按分数补足）。
负因子侧与 F60 的 `NEG_EXCL` 同一机制（剔掉并集坏侧），单测已 +2.41pp/+0.91pp（候选）。

**预注册（跑数前冻结）**
  基准 BASE       生产融合分 top100 等权（生产 PIT 掩码 + 5 日 forward_excess + 真实卖出费率）
  负因子 NEG     = {`br_cnt`(券商推荐数), `ih_fund_ratio`(机构持仓比)}，**剔掉两者坏侧十分位的并集**
  正因子 POS     = {`margin_net_share`, `mf_main_sm_div`}（阶段 0c 唯二通过门禁者）：
                   **good side = 两者都高于各自截面中位数**（"正向筛"的中性口径）
  臂：BASE / `NEG_EXCL` / `POS_KEEP`（good side 优先、余量按分数补足到 100）/
      `COMBO`（先剔坏侧、再 good side 优先）
  判据（每条臂独立判，四条全过 = 候选，**通过≠上线**）：
    ① 全史净超额 ≥ BASE ② 活跃区净超额 ≥ BASE ③ |MDD| ≤ |MDD_BASE|+2pp ④ 换手 ≤ 1.20×BASE
  另报成对逐期差 t、以及"top100 里来自 good side 的只数"（机制读数：筛选到底有没有作用对象）。
  ⚠️ 多重比较（4 臂高度相关）⇒ 单臂通过不构成证据，须独立 A/B。

运行：``D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F61_neg_reverse_pos_keep.py``
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
OUT = HERE / "F61_neg_reverse_pos_keep_result.json"
NPZ = BASE_DIR / "data" / "weekly" / "scores_20260911.npz"
TOP_N, DECILE, PER_YEAR = 100, 0.10, 52.0
MAX_MDD_WORSE_PP, MAX_TURNOVER_RATIO = 2.0, 1.20
WINDOWS = {"FULL": ("2010-01-01", "2030-12-31"),
           "ACTIVE": ("2024-01-01", "2026-08-31"),
           "DEAD": ("2020-09-01", "2023-12-31")}
NEG_FACTORS = ["br_cnt", "ih_fund_ratio"]      # 坏侧 = 高分位（死区 head t≤−2）
POS_FACTORS = ["margin_net_share", "mf_main_sm_div"]   # 门禁唯二通过；good side = 双上中位数
log = logging.getLogger("f61")


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
        log.error("缺因子 %s ⇒ 终止", miss)
        return 2
    P = {k: panels[k].reindex(index=anchors, columns=cols).to_numpy(dtype=float)
         for k in NEG_FACTORS + POS_FACTORS}
    log.info("面板就绪（负 %d / 正 %d）| %.0fs", len(NEG_FACTORS), len(POS_FACTORS),
             time.time() - t0)

    arms = ["BASE", "NEG_EXCL", "POS_KEEP", "COMBO"]
    series = {a: [] for a in arms}
    turn_acc = {a: 0.0 for a in arms}
    prev = {a: None for a in arms}
    used, n_bad, n_good = [], [], []
    for k, d in enumerate(anchors):
        y, s = Y[k], S[k]
        ok = mask.iloc[k].to_numpy() & np.isfinite(s) & np.isfinite(y)
        if ok.sum() < 200:
            continue
        order = np.argsort(-np.where(ok, s, -np.inf))
        # 负因子：坏侧并集（高分位）
        bad = np.zeros(len(s), dtype=bool)
        for f in NEG_FACTORS:
            v = P[f][k]
            b = ok & np.isfinite(v)
            if b.sum() >= 100:
                bad |= b & (v >= np.quantile(v[b], 1 - DECILE))
        # 正因子：good side = 全部正因子都高于各自截面中位数
        good = ok.copy()
        for f in POS_FACTORS:
            v = P[f][k]
            b = ok & np.isfinite(v)
            if b.sum() >= 100:
                good &= b & (v >= np.quantile(v[b], 0.5))
            else:
                good &= ok
        order_neg = order[~bad[order]]
        order_pos = np.concatenate([order[good[order]], order[~good[order]]])
        order_combo = order_pos[~bad[order_pos]]
        if len(order_combo) < TOP_N:
            order_combo = order_neg
        picks = {"BASE": order[:TOP_N],
                 "NEG_EXCL": order_neg[:TOP_N] if len(order_neg) >= TOP_N else order[:TOP_N],
                 "POS_KEEP": order_pos[:TOP_N],
                 "COMBO": order_combo[:TOP_N]}
        n_bad.append(int(bad[order[:TOP_N]].sum()))
        n_good.append(int(good[order[:TOP_N]].sum()))
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
        used.append(d)
    df = pd.DataFrame(series, index=pd.DatetimeIndex(used))
    turns = {a: round(v / max(1, len(df)), 4) for a, v in turn_acc.items()}
    base = {tag: window_stats(df["BASE"], tag) for tag in WINDOWS}
    res = {"protocol": {"top_n": TOP_N, "decile": DECILE, "neg": NEG_FACTORS, "pos": POS_FACTORS,
                        "pos_rule": "全部正因子高于各自截面中位数", "windows": WINDOWS,
                        "sell_rate_bp": round(sell_rate * 1e4, 3),
                        "multiplicity_warning": "4 臂高度相关 ⇒ 单臂通过须独立 A/B 才算数"},
           "n_periods": int(len(df)), "turnover": turns, "base": base,
           "mean_bad_in_top100": round(float(np.mean(n_bad)), 2),
           "mean_good_in_top100": round(float(np.mean(n_good)), 2), "arms": {}}

    def _t(x):
        return round(float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))), 2) if len(x) > 2 else None
    for arm in arms[1:]:
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

    print(f"\n{len(df)} 期 | 模型 top100 里：坏侧 {res['mean_bad_in_top100']} 只 / "
          f"正因子 good side {res['mean_good_in_top100']} 只")
    print(f"{'臂':<12}{'全史超额':>10}{'Δ全史':>8}{'活跃区':>9}{'Δ活跃':>8}{'MDD':>9}"
          f"{'换手':>8}{'t全史':>7}{'t活跃':>7}  判定")
    print(f"{'BASE':<12}{base['FULL']['excess_pp']:>10.2f}{'—':>8}{base['ACTIVE']['excess_pp']:>9.2f}"
          f"{'—':>8}{base['FULL']['mdd']:>9.4f}{turns['BASE']:>8.4f}{'—':>7}{'—':>7}  —")
    for arm, e in res["arms"].items():
        v, st = e["verdict"], e["stats"]
        print(f"{arm:<12}{st['FULL']['excess_pp']:>10.2f}{v['delta_full_pp']:>8.2f}"
              f"{st['ACTIVE']['excess_pp']:>9.2f}{v['delta_active_pp']:>8.2f}"
              f"{st['FULL']['mdd']:>9.4f}{turns[arm]:>8.4f}{str(v['paired_t_full']):>7}"
              f"{str(v['paired_t_active']):>7}  {'候选' if v['candidate'] else '否'}")
    print(f"\n结果 → {OUT.name} | {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
