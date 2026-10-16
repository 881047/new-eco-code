# -*- coding: utf-8 -*-
"""关注度型拥挤度量器探针 —— `plans/crowding-meter-probe-prereg-20260927.md` 的执行体。

三问（预注册 §二，跑前冻结）:
  R1 二维体制正交性: 锚点级市场拥挤度按 DEAD/ACTIVE 分组（强共线 ⇒ "第二体制轴"被否）;
  R2 拥挤 → 因子健康（核心）: 31 在池因子逐锚点 IC 按同区拥挤度中位数分高低桶，
     逐因子 ΔIC + 跨因子汇总（中位数 <0 且 t ≤ −2 且负向占比 ≥2/3 ⇒ 进 zone_of 扩展预注册）;
  R3 量器自身读数（sanity，非发现声明——家族已在门禁证伪，本轮不翻案）。
构造（预注册 §一）: 组件域 = data/alpha_pool/ 全部 kg_*.npz + llm_kg_*.npz（18 块，
  机械枚举不挑拣）；C1=全家族等权截面 z（个股 ≥3 组件非 NaN），C2=kg_broker_rec_3m 单组件。
只读: 不写库、不改 config、不动门禁。产物 experiments/crowding_meter_probe_20260927.json。
"""
import glob
import json
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
POOL = ROOT / "data" / "alpha_pool"
OUT = ROOT / "experiments" / "crowding_meter_probe_20260927.json"
MIN_COMPONENTS = 3


def load_npz(path: Path) -> pd.DataFrame:
    d = np.load(path, allow_pickle=True)
    return pd.DataFrame(d["values"].astype(float),
                        index=pd.to_datetime([str(x) for x in d["dates"].tolist()]),
                        columns=[str(x) for x in d["codes"].tolist()])


def zscore_row(s: pd.Series) -> pd.Series:
    mu, sd = s.mean(), s.std(ddof=1)
    if not sd or sd != sd:
        return s * np.nan
    return (s - mu) / sd


def build_crowding(panels: dict, md_eval: pd.DatetimeIndex) -> pd.DataFrame:
    """C1: 逐锚点等权截面 z（个股 ≥MIN_COMPONENTS 组件非 NaN 才有分）。"""
    rows = {}
    for d in md_eval:
        zs = []
        for name, pw in panels.items():
            if d not in pw.index:
                continue
            z = zscore_row(pw.loc[d].dropna())
            if len(z) >= 200:
                zs.append(z.rename(name))
        if len(zs) < MIN_COMPONENTS:
            continue
        Z = pd.concat(zs, axis=1)
        cnt = Z.notna().sum(axis=1)
        score = Z.mean(axis=1).where(cnt >= MIN_COMPONENTS)
        score = score.dropna()
        if len(score) >= 200:
            rows[d] = score
    return pd.DataFrame(rows).T.sort_index()


def welch_t(a: np.ndarray, b: np.ndarray) -> float:
    a, b = a[~np.isnan(a)], b[~np.isnan(b)]
    if len(a) < 3 or len(b) < 3:
        return np.nan
    va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
    return float((a.mean() - b.mean()) / np.sqrt(va + vb))


def main() -> int:
    t0 = time.time()
    paths = sorted(glob.glob(str(POOL / "kg_*.npz")) +
                   [p for p in glob.glob(str(POOL / "llm_kg_*.npz"))])
    names = [Path(p).stem for p in paths]
    print(f"组件域: {len(paths)} 块 npz（机械枚举 kg_* + llm_kg_*）", flush=True)

    m = load_market()
    md_all = month_end_dates(pd.DatetimeIndex(m["close"].index))
    md_eval = pd.DatetimeIndex(md_all[md_all >= pd.Timestamp("2019-01-01")])
    zones = gate.zone_of(md_eval)
    fwd = gate.forward_return(m, gate.HORIZON)
    pit = PITEngine(m=m)
    tradable = pit.tradable_matrix(md_eval)

    panels = {n: load_npz(Path(p)).reindex(index=md_eval) for n, p in zip(names, paths)}
    c1 = build_crowding(panels, md_eval)
    c2 = panels["kg_broker_rec_3m"]
    print(f"C1 覆盖锚点 {len(c1)}，C2（单组件）锚点 {int(c2.notna().any(axis=1).sum())}", flush=True)

    def level_of(score_wide: pd.DataFrame) -> pd.Series:
        return score_wide.mean(axis=1, skipna=True)

    # ---- R1: 二维体制正交性 ----
    lvl = level_of(c1).reindex(md_eval)
    zmask = np.array([z in ("DEAD", "ACTIVE") for z in zones])
    dead_l, act_l = lvl[zmask & (zones == "DEAD")].to_numpy(), lvl[zmask & (zones == "ACTIVE")].to_numpy()
    trend_rho = float(lvl.dropna().corr(pd.Series(np.arange(len(lvl.dropna())),
                                                  index=lvl.dropna().index), method="spearman"))
    r1 = {"n_dead_anchors": int(len(dead_l)), "n_active_anchors": int(len(act_l)),
          "mean_dead": float(np.nanmean(dead_l)), "mean_active": float(np.nanmean(act_l)),
          "welch_t_dead_vs_active": welch_t(dead_l, act_l), "spearman_vs_time": trend_rho}

    # ---- R2: 31 在池因子逐锚点 IC × 拥挤分桶（区内中位数） ----
    wide31 = T._load_factors_wide()
    wide31r = wide31.reorder_levels(["factor", "ts_code"], axis=1).sort_index(axis=1)
    factors = sorted(wide31r.columns.get_level_values("factor").unique())
    bucket = {}
    for zone in ("DEAD", "ACTIVE"):
        idx = [d for d, z in zip(md_eval, zones) if z == zone and d in lvl.index
               and not np.isnan(lvl.loc[d])]
        if len(idx) < 6:
            continue
        med = float(np.median(lvl.loc[idx]))
        bucket[zone] = {"high": [d for d in idx if lvl.loc[d] >= med],
                        "low": [d for d in idx if lvl.loc[d] < med]}
    per_factor = []
    for f in factors:
        sub = wide31r.xs(f, axis=1, level="factor")
        sub.index = pd.to_datetime(sub.index)
        sub_eval = sub.reindex(index=md_eval, columns=sub.columns)
        rec = {"factor": f}
        for zone, b in bucket.items():
            cur = gate.ic_by_lag(sub_eval, tradable, fwd, md_eval,
                                 zones, lags=(0,), zone=zone)[0]
            hi = cur.reindex(b["high"]).dropna()
            lo = cur.reindex(b["low"]).dropna()
            rec[f"ic_high_{zone}"] = float(hi.mean()) if len(hi) else None
            rec[f"ic_low_{zone}"] = float(lo.mean()) if len(lo) else None
            rec[f"n_high_{zone}"], rec[f"n_low_{zone}"] = len(hi), len(lo)
        d_ic = [rec[f"ic_high_{z}"] - rec[f"ic_low_{z}"]
                for z in bucket if rec.get(f"ic_high_{z}") is not None
                and rec.get(f"ic_low_{z}") is not None]
        rec["delta_ic"] = float(np.mean(d_ic)) if d_ic else None
        # 无取向读数（预注册 §二 同冻 结）：|IC| 差消除因子取向混合的歧义
        d_abs = [abs(rec[f"ic_high_{z}"]) - abs(rec[f"ic_low_{z}"])
                 for z in bucket if rec.get(f"ic_high_{z}") is not None
                 and rec.get(f"ic_low_{z}") is not None]
        rec["delta_abs_ic"] = float(np.mean(d_abs)) if d_abs else None
        per_factor.append(rec)
        print(f"  {f:24s} ΔIC={rec['delta_ic'] if rec['delta_ic'] is None else round(rec['delta_ic'], 4)}",
              flush=True)
    deltas = np.array([r["delta_ic"] for r in per_factor if r["delta_ic"] is not None])
    deltas_abs = np.array([r["delta_abs_ic"] for r in per_factor
                           if r.get("delta_abs_ic") is not None])
    if len(deltas) < 3:
        r2 = {"n_factors": len(deltas), "verdict": "CLEARED",
              "note": "可用锚点/因子不足，探针不成立（如实登记）"}
    else:
        t_cross = float(deltas.mean() / (deltas.std(ddof=1) / np.sqrt(len(deltas)))) \
            if deltas.std(ddof=1) > 0 else np.nan
        r2 = {"n_factors": len(deltas), "median_delta_ic": float(np.median(deltas)),
              "frac_negative": float((deltas < 0).mean()), "cross_factor_t": t_cross,
              "criteria": "median<0 且 t<=-2 且 frac_negative>=2/3 ⇒ 进 zone_of 扩展预注册",
              "verdict": ("PROCEED" if (np.median(deltas) < 0 and t_cross <= -2
                                        and (deltas < 0).mean() >= 2 / 3) else "CLEARED")}
        if len(deltas_abs) >= 3:
            t_abs = float(deltas_abs.mean() / (deltas_abs.std(ddof=1)
                                               / np.sqrt(len(deltas_abs)))) \
                if deltas_abs.std(ddof=1) > 0 else np.nan
            r2["orientation_free_abs_ic"] = {
                "median": float(np.median(deltas_abs)), "mean": float(deltas_abs.mean()),
                "cross_factor_t": t_abs, "frac_negative": float((deltas_abs < 0).mean()),
                "note": "带号 ΔIC 对负向取向池有方向歧义；|IC| 差为无取向补充读数（同预注册冻结），"
                        "判决判据仍按 §二 冻结的带号口径"}

    # ---- R3: 量器自身读数（sanity，非发现声明） ----
    r3 = {}
    for tag, wide in (("C1_equal_weight", c1), ("C2_broker_rec", c2)):
        wide_e = wide.reindex(index=md_eval)
        det = gate.evaluate(wide_e.where(
            tradable.reindex(index=md_eval, columns=wide_e.columns).fillna(False)),
            fwd, md_eval, zones)
        if det.empty:
            r3[tag] = {"note": "无有效截面"}
            continue
        dead = det[det.zone == "DEAD"]
        r3[tag] = {"dead_head": float(dead.head_excess.mean()),
                   "dead_ic": float(dead.ic_full.mean()),
                   "active_head": float(det[det.zone == "ACTIVE"].head_excess.mean()),
                   "n_dead": int(len(dead))}

    out = {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "prereg": "plans/crowding-meter-probe-prereg-20260927.md",
        "components": names,
        "n_anchors_c1": len(c1),
        "R1_regime_orthogonality": r1,
        "R2_crowding_factor_health": {**r2, "bucket_sizes": {
            z: {"high": len(b["high"]), "low": len(b["low"])} for z, b in bucket.items()},
            "per_factor": per_factor},
        "R3_meter_self_readout": r3,
        "note": "探针只读不决策; 四象限体制+bar 自适应若 PROCEED 另立预注册",
        "runtime_sec": round(time.time() - t0, 1),
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"R1: DEAD {r1['mean_dead']:+.4f} vs ACTIVE {r1['mean_active']:+.4f} "
          f"(t={r1['welch_t_dead_vs_active']:.2f})", flush=True)
    print(f"R2: {r2['verdict']} —— median ΔIC {r2['median_delta_ic']:+.4f} "
          f"t={t_cross:.2f} 负向占比 {r2['frac_negative']:.2f}", flush=True)
    print(f"产物: {OUT}  ({time.time() - t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
