"""F60 「正作用因子 + 副作用因子」一起作用（2026-09-13，预注册）

**用户提问**：能不能把"副作用因子"和"正作用因子"**放在一起用**，看合起来是好是坏？

**事实基础（前两步已测）**：
  · 正作用因子 = 阶段 0c 门禁唯二通过的 `margin_net_share`(+0.64%/月,t=4.19) 与
    `mf_main_sm_div`(+0.56%/月,t=4.31) —— 但它们在组合层**已被证明单独用没用**
    （F16 作为特征只 +0.32pp；F17~F21 四种融合器全否）；
  · 副作用因子（死区坏侧 t≤−2 且与模型 top100 有实质重叠）= `br_cnt`（券商推荐数）与
    `ih_fund_ratio`（机构持仓比）；F59 单因子剔除：`br_cnt` **+2.08pp（全史）/ +0.78pp（活跃区）**、
    `ih_fund_ratio` +0.49/+0.12，两条判据全过 ⇒ 记为候选。
  ⇒ 本实验就是把两边**合起来**，看是加成、抵消还是互相破坏。

**预注册（跑数前冻结）**：
  基准 BASE     生产融合分 top100 等权（生产 PIT 掩码 + 5 日 forward_excess + 真实卖出费率）
  臂（全部在同一套打分上做，不重训）：
    `NEG_EXCL`   剔除 {br_cnt, ih_fund_ratio} **坏侧十分位的并集** ⇒ 落选名单顺位补足（=F59 两臂的合体）
    `POS_BLEND`  打分替换为 z(生产分) + 0.5·[z(margin_net_share) + z(mf_main_sm_div)]（截面 z 后等权相加）
    `NEGINV`     打分替换为 z(生产分) − 0.5·[z(br_cnt) + z(ih_fund_ratio)]（把负因子**反过来当正向**用）
    `BOTH`       NEG_EXCL 的剔除 + POS_BLEND 的打分（正负同时作用）
    `ALL`        NEG_EXCL 的剔除 + POS_BLEND 打分 + NEGINV 倾斜（三样一起）
  判据（每条臂独立判，四条全过 = 候选，**通过≠上线**）：
    ① 全史净超额 ≥ BASE ② 活跃区净超额 ≥ BASE ③ |MDD| ≤ |MDD_BASE| + 2pp ④ 换手 ≤ 1.20×BASE
  另报成对逐期差 t（诊断）。
  ⚠️ **多重比较**：一次测 5 臂且互相高度相关 ⇒ 单臂"通过"不构成证据（F56: PBO=0.434），
    任何通过者都必须再走一次独立 A/B（换 pin 流程）才谈上线。

运行：``D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F60_pos_neg_together.py``
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
OUT = HERE / "F60_pos_neg_together_result.json"
NPZ = BASE_DIR / "data" / "weekly" / "scores_20260911.npz"
TOP_N = 100
DECILE = 0.10
BLEND_W = 0.5                      # 预注册权重：正/负因子各占 0.5 个 z 单位
PER_YEAR = 52.0
MAX_MDD_WORSE_PP = 2.0
MAX_TURNOVER_RATIO = 1.20
WINDOWS = {"FULL": ("2010-01-01", "2030-12-31"),
           "ACTIVE": ("2024-01-01", "2026-08-31"),
           "DEAD": ("2020-09-01", "2023-12-31")}
POS_FACTORS = ["margin_net_share", "mf_main_sm_div"]     # 门禁唯二通过
NEG_FACTORS = ["br_cnt", "ih_fund_ratio"]                # F59 两条候选（坏侧 = 高分位）
log = logging.getLogger("f60")


def _z(s: np.ndarray, ok: np.ndarray) -> np.ndarray:
    """截面 z 分数（只在可交易且有限的样本上标准化，其余保持 NaN）。"""
    out = np.full(len(s), np.nan)
    v = s[ok]
    sd = float(np.nanstd(v))
    if sd > 0:
        out[ok] = (v - float(np.nanmean(v))) / sd
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
    missing = [f for f in POS_FACTORS + NEG_FACTORS if f not in panels]
    if missing:
        log.error("缺因子 %s ⇒ 终止", missing)
        return 2
    P = {k: panels[k].reindex(index=anchors, columns=cols).to_numpy(dtype=float)
         for k in POS_FACTORS + NEG_FACTORS}
    log.info("面板就绪（正 %d / 负 %d）| %.0fs", len(POS_FACTORS), len(NEG_FACTORS),
             time.time() - t0)

    arms = ["BASE", "NEG_EXCL", "POS_BLEND", "NEGINV", "BOTH", "ALL"]
    series = {a: [] for a in arms}
    turn_acc = {a: 0.0 for a in arms}
    prev = {a: None for a in arms}
    used, n_excl = [], []
    for k, d in enumerate(anchors):
        y, s = Y[k], S[k]
        ok = mask.iloc[k].to_numpy() & np.isfinite(s) & np.isfinite(y)
        if ok.sum() < 200:
            continue
        zs = _z(s, ok)
        zp = [_z(P[f][k], ok) for f in POS_FACTORS]
        zn = [_z(P[f][k], ok) for f in NEG_FACTORS]
        blend_pos = zs + BLEND_W * sum(zp)
        blend_inv = zs - BLEND_W * sum(zn)
        # 坏侧剔除集：两负因子各自"全池十分位"的并集
        bad = np.zeros(len(s), dtype=bool)
        for f, zz in zip(NEG_FACTORS, zn):
            both = ok & np.isfinite(zz)
            if both.sum() >= 100:
                thr = np.quantile(zz[both], 1 - DECILE)
                bad |= both & (zz >= thr)
        order_base = np.argsort(-np.where(ok, s, -np.inf))
        order_neg = order_base[~bad[order_base]]
        if len(order_neg) < TOP_N:
            order_neg = order_base
        order_pos = np.argsort(-np.where(ok, blend_pos, -np.inf))
        order_inv = np.argsort(-np.where(ok, blend_inv, -np.inf))
        order_both = order_pos[~bad[order_pos]]
        if len(order_both) < TOP_N:
            order_both = order_pos
        order_all = order_inv[~bad[order_inv]]
        if len(order_all) < TOP_N:
            order_all = order_inv
        picks = {"BASE": order_base[:TOP_N], "NEG_EXCL": order_neg[:TOP_N],
                 "POS_BLEND": order_pos[:TOP_N], "NEGINV": order_inv[:TOP_N],
                 "BOTH": order_both[:TOP_N], "ALL": order_all[:TOP_N]}
        n_excl.append(int(bad[order_base[:TOP_N]].sum()))
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
    base_stats = {tag: window_stats(df["BASE"], tag) for tag in WINDOWS}
    res = {"protocol": {"top_n": TOP_N, "decile": DECILE, "blend_w": BLEND_W,
                        "pos_factors": POS_FACTORS, "neg_factors": NEG_FACTORS,
                        "windows": WINDOWS, "sell_rate_bp": round(sell_rate * 1e4, 3),
                        "multiplicity_warning": "一次 6 臂且高度相关 ⇒ 单臂通过不构成证据，须独立 A/B"},
           "n_periods": int(len(df)), "turnover": turns,
           "mean_bad_in_top100": round(float(np.mean(n_excl)), 2), "base": base_stats, "arms": {}}

    def _t(x):
        return round(float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))), 2) if len(x) > 2 else None
    for arm in arms[1:]:
        st = {tag: window_stats(df[arm], tag) for tag in WINDOWS}
        d_full = (df[arm] - df["BASE"])
        d_act = d_full.loc[WINDOWS["ACTIVE"][0]:WINDOWS["ACTIVE"][1]].dropna()
        v = {"delta_full_pp": round(st["FULL"]["excess_pp"] - base_stats["FULL"]["excess_pp"], 2),
             "delta_active_pp": round(st["ACTIVE"]["excess_pp"] - base_stats["ACTIVE"]["excess_pp"], 2),
             "full_excess_ok": bool(st["FULL"]["excess_pp"] >= base_stats["FULL"]["excess_pp"]),
             "active_excess_ok": bool(st["ACTIVE"]["excess_pp"] >= base_stats["ACTIVE"]["excess_pp"]),
             "mdd_ok": bool(abs(st["FULL"]["mdd"]) <= abs(base_stats["FULL"]["mdd"]) + MAX_MDD_WORSE_PP / 100),
             "turnover_ok": bool(turns[arm] <= MAX_TURNOVER_RATIO * turns["BASE"]),
             "paired_t_full": _t(d_full.dropna()), "paired_t_active": _t(d_act)}
        v["candidate"] = bool(all(v[x] for x in ("full_excess_ok", "active_excess_ok",
                                                 "mdd_ok", "turnover_ok")))
        res["arms"][arm] = {"stats": st, "verdict": v}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    print(f"\n{len(df)} 期 | 卖出费率 {sell_rate*1e4:.2f}bp | 模型 top100 里平均落在坏集 "
          f"{res['mean_bad_in_top100']} 只")
    print(f"{'臂':<12}{'全史超额':>10}{'Δ全史':>8}{'活跃区':>9}{'Δ活跃':>8}{'MDD':>9}"
          f"{'换手':>8}{'t全史':>7}{'t活跃':>7}  判定")
    print(f"{'BASE':<12}{base_stats['FULL']['excess_pp']:>10.2f}{'—':>8}"
          f"{base_stats['ACTIVE']['excess_pp']:>9.2f}{'—':>8}{base_stats['FULL']['mdd']:>9.4f}"
          f"{turns['BASE']:>8.4f}{'—':>7}{'—':>7}  —")
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
