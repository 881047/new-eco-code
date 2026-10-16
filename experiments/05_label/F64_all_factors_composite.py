"""F64 「所有因子一起干活」——符号自适应的等权合成（2026-09-13，预注册）

**用户思路**（2026-09-13 夜，第三次澄清）：让**所有**因子一起干活 —— 正因子做正面筛查、
负因子反向用，**在不同位置/角度上搭配**，应该能发挥更大功效。

**本实验把它落成一个单一、预注册的合成（不做权重搜索）**：
  1) 取阶段 0c 门禁评估过的**全部新族因子**（资金类 15 + 股东户数 2 + 券商金股 4 + 研报 3 +
     十大流通股东 3 ≈ 27 个），每个因子在每个锚点做**截面 z 分数**（只在该锚点可交易且有限处算）；
  2) **符号自适应**：每个因子的方向 = 它在**死区**（2020-09~2023-12）的头部超额符号
     （正 ⇒ 直接用，负 ⇒ 取反）——这就是用户说的"正的正面筛、负的反向用"，**一次性、等权**，
     不搜索权重、不挑因子；
  3) 合成 `C` = 各因子**符号校正后 z 分数的等权平均**（要求该锚点至少 8 个因子可用，否则该期跳过）；
  4) 让 `C` 在**三个不同位置**起作用（这对应"不同的位置角度"）：
     · `TILT`  ：打分 = z(生产分) + w·z(C)，只改排序、不动持仓数（w=0.3 与 0.5 两档）
     · `SCREEN`：剔掉 C 的**最差十分位**、顺位补足 100（只做减法）
     · `BOTH`  ：先剔最差十分位，再用 `TILT`(w=0.3) 在剩下的名次里排序
  基准 BASE = 生产融合分 top100 等权（生产 PIT 掩码 + `sc.forward_excess` 5 日 + 真实卖出费率）。
  判据（与 F57/F59/F60/F61/F63 同门槛，四条全过 = 候选）：全史净超额不劣化 + 活跃区不劣化 +
    |MDD| 不劣化超 2pp + 换手 ≤1.20×BASE；另报成对 t。

**诚实限定**：① 符号取自死区 ⇒ 死区读数是**样本内**、活跃区才是样本外确认（且活跃区是 F28 已开封
  的留出集）；② 一次 5 臂且高度相关 ⇒ 单臂胜出不算证据（F56：PBO=0.434）；③ 通过也只算候选，
  上线必须走独立 A/B + 换 pin。

运行：``D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F64_all_factors_composite.py``
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

from app import config as cfg_mod                                     # noqa: E402
from app import db                                                    # noqa: E402
from app.factors import factor_engine as fe                           # noqa: E402
from app.factors.flow_factors import (compute_broker_factors,         # noqa: E402
                                      compute_flow_factors,
                                      compute_report_rc_factors,
                                      compute_top10_holder_factors)
from app.factors.holder_factors import compute_holder_factors_safe    # noqa: E402
from app.pit.engine import PITEngine                                  # noqa: E402
from app.portfolio import fees as fee_mod                             # noqa: E402
from app.weekly import scores as sc                                   # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "F64_all_factors_composite_result.json"
NPZ = BASE_DIR / "data" / "weekly" / "scores_20260911.npz"
TOP_N, PER_YEAR, DECILE = 100, 52.0, 0.10
TILT_WS = (0.3, 0.5)
MIN_FACTORS = 8
MAX_MDD_WORSE_PP, MAX_TURNOVER_RATIO = 2.0, 1.20
DEAD = ("2020-09-01", "2023-12-31")
WINDOWS = {"FULL": ("2010-01-01", "2030-12-31"),
           "ACTIVE": ("2024-01-01", "2026-08-31"),
           "DEAD": DEAD}
log = logging.getLogger("f64")


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
    facs = {}
    facs.update(compute_flow_factors(m, daily, include_research=False))
    facs.update(compute_holder_factors_safe(daily))
    facs.update(compute_broker_factors(daily))
    facs.update(compute_report_rc_factors(m, daily, source="em"))
    facs.update(compute_top10_holder_factors(m, daily))
    log.info("候选因子 %d 个 | %.0fs", len(facs), time.time() - t0)
    # 每个因子在锚点上的 z 分数矩阵 (T × N × F)
    Z, names = [], []
    for nm, panel in sorted(facs.items()):
        P = panel.reindex(index=anchors, columns=cols).to_numpy(dtype=float)
        Zf = np.full(P.shape, np.nan)
        for k in range(len(anchors)):
            ok = mask.iloc[k].to_numpy() & np.isfinite(P[k]) & np.isfinite(Y[k])
            if ok.sum() >= 100:
                sd = float(np.nanstd(P[k][ok]))
                if sd > 0:
                    Zf[k, ok] = (P[k][ok] - float(np.nanmean(P[k][ok]))) / sd
        Z.append(Zf)
        names.append(nm)
    Z = np.stack(Z, axis=2)
    # ---- 符号自适应：按**死区**头部超额符号（一次性、等权、不搜索）----
    in_dead = (anchors >= DEAD[0]) & (anchors <= DEAD[1])
    signs, head_dead = {}, {}
    for j, nm in enumerate(names):
        vals = []
        for k in np.where(in_dead)[0]:
            ok = mask.iloc[k].to_numpy() & np.isfinite(Z[k, :, j]) & np.isfinite(Y[k])
            if ok.sum() < 100:
                continue
            v = Z[k, ok, j]
            hi = np.quantile(v, 1 - DECILE)
            head = float(Y[k][ok][v >= hi].mean() - Y[k][ok].mean())
            vals.append(head)
        head_dead[nm] = round(100 * float(np.mean(vals)), 4) if vals else None
        signs[nm] = 1.0 if (vals and np.mean(vals) > 0) else -1.0    # 负 ⇒ 反向用（取负号）
    n_pos = sum(1 for s in signs.values() if s > 0)
    log.info("符号自适应完成：%d 个因子（正用 %d / 反用 %d）| %.0fs",
             len(signs), n_pos, len(signs) - n_pos, time.time() - t0)
    sign_vec = np.array([signs[nm] for nm in names])
    C = np.nanmean(Z * sign_vec[None, None, :], axis=2)      # 等权合成（跳过多因子缺失）
    have = np.isfinite(Z).sum(axis=2)
    C[have < MIN_FACTORS] = np.nan                            # 因子太少的期不用合成

    arms = ["BASE", "TILT0.3", "TILT0.5", "SCREEN", "BOTH"]
    series = {a: [] for a in arms}
    turn_acc = {a: 0.0 for a in arms}
    prev = {a: None for a in arms}
    used, n_excl = [], []
    for k, d in enumerate(anchors):
        y, s, c = Y[k], S[k], C[k]
        ok = mask.iloc[k].to_numpy() & np.isfinite(s) & np.isfinite(y)
        if ok.sum() < 200:
            continue
        zs, zc = np.full(len(s), np.nan), np.full(len(s), np.nan)
        if np.isfinite(c[ok]).sum() >= 100:
            zc[ok & np.isfinite(c)] = ((c[ok & np.isfinite(c)] -
                                       float(np.mean(c[ok & np.isfinite(c)]))) /
                                       (float(np.std(c[ok & np.isfinite(c)])) or 1.0))
        zs[ok] = ((s[ok] - float(np.mean(s[ok]))) / (float(np.std(s[ok])) or 1.0))
        order = np.argsort(-np.where(ok, s, -np.inf))
        bad = ok & np.isfinite(zc) & (zc <= np.quantile(zc[ok & np.isfinite(zc)], DECILE))
        order_scr = order[~bad[order]]
        if len(order_scr) < TOP_N:
            order_scr = order
        tilt = {w: np.argsort(-np.where(ok, zs + w * np.nan_to_num(zc), -np.inf))[:TOP_N]
                for w in TILT_WS}
        both = np.argsort(-np.where(ok, zs + TILT_WS[0] * np.nan_to_num(zc), -np.inf))
        both = both[~bad[both]]
        if len(both) < TOP_N:
            both = order_scr
        picks = {"BASE": order[:TOP_N], "TILT0.3": tilt[0.3], "TILT0.5": tilt[0.5],
                 "SCREEN": order_scr[:TOP_N], "BOTH": both[:TOP_N]}
        n_excl.append(int(bad[order[:TOP_N]].sum()))
        for arm, sel in picks.items():
            w = np.full(len(sel), 1.0 / len(sel))
            gross = float(np.dot(w, y[sel].astype(float)))
            delta, cur = 0.0, dict(zip(sel, w))
            if prev[arm]:
                names2 = set(cur) | set(prev[arm])
                delta = sum(abs(cur.get(i, 0.0) - prev[arm].get(i, 0.0)) for i in names2)
            prev[arm] = cur
            turn_acc[arm] += delta
            series[arm].append(gross - delta * sell_rate)
        used.append(d)
    df = pd.DataFrame(series, index=pd.DatetimeIndex(used))
    turns = {a: round(v / max(1, len(df)), 4) for a, v in turn_acc.items()}
    base = {tag: window_stats(df["BASE"], tag) for tag in WINDOWS}
    res = {"protocol": {"n_factors": len(names), "min_factors": MIN_FACTORS, "decile": DECILE,
                        "tilt_ws": list(TILT_WS), "windows": WINDOWS,
                        "sign_rule": "死区头部超额符号（一次性、等权、不搜索权重）",
                        "sell_rate_bp": round(sell_rate * 1e4, 3),
                        "caveat": "符号取自死区 ⇒ 死区样本内、活跃区为样本外确认；5 臂高度相关"},
           "n_periods": int(len(df)), "turnover": turns,
           "factor_signs": {nm: signs[nm] for nm in names},
           "factor_dead_head_pp": head_dead,
           "mean_screen_excluded": round(float(np.mean(n_excl)), 2),
           "base": base, "arms": {}}

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

    print(f"\n合成：{len(names)} 个因子（正用 {n_pos} / 反用 {len(names)-n_pos}）等权 | "
          f"{len(df)} 期 | 筛选平均剔 {res['mean_screen_excluded']} 只")
    print(f"{'臂':<10}{'全史超额':>10}{'Δ全史':>8}{'活跃区':>9}{'Δ活跃':>8}{'MDD':>9}"
          f"{'换手':>8}{'t全史':>7}{'t活跃':>7}  判定")
    print(f"{'BASE':<10}{base['FULL']['excess_pp']:>10.2f}{'—':>8}{base['ACTIVE']['excess_pp']:>9.2f}"
          f"{'—':>8}{base['FULL']['mdd']:>9.4f}{turns['BASE']:>8.4f}{'—':>7}{'—':>7}  —")
    for arm, e in res["arms"].items():
        v, st = e["verdict"], e["stats"]
        print(f"{arm:<10}{st['FULL']['excess_pp']:>10.2f}{v['delta_full_pp']:>8.2f}"
              f"{st['ACTIVE']['excess_pp']:>9.2f}{v['delta_active_pp']:>8.2f}"
              f"{st['FULL']['mdd']:>9.4f}{turns[arm]:>8.4f}{str(v['paired_t_full']):>7}"
              f"{str(v['paired_t_active']):>7}  {'候选' if v['candidate'] else '否'}")
    print(f"\n结果 → {OUT.name} | {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
