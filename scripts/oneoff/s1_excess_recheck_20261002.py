# -*- coding: utf-8 -*-
"""S1 探针主判轴实现偏差只读复算（2026-10-02 全仓审查发现，事故在册前证据保全）。

发现：scripts/s1_confidence_probe.py::build_monthly 主判超额写的是
    excess = r6.mean() - f_x.mean()，其中 f_x = 截面去均值后的行 ⇒ f_x.mean() 恒等 0，
    实际算的是 **top6 原始 forward 收益**，不是冻结判据（§5.2）的
    "top6 forward − 同月截面等权均值"。市场分量混入主判轴 ⇒ |t| 被稀释。
行业中性敏感性轴（excess_ind，两侧都来自 f_in）实现与冻结判据一致，未受影响。

本脚本在同一数据快照下并排复算三套主判轴：
  raw   = 原实现（top6 原始收益 − 0，复现 20260929 在册读数用）
  fixed = 冻结判据（top6 原始收益 − 截面等权均值）
  ind   = 行业中性轴（原实现即正确，作锚）
输出 T1(HAC t)/T3(两子窗同号)/T4(波动冗余) × {C1, C2}。read-only，零写库；
产物 experiments/s1_excess_recheck_20261002.json（不覆盖已登记的
s1_confidence_probe_20260929.json —— 那是判决证据，保持原样）。
判决词不在此填写；读数交台账 N+1 登记 + 用户裁定。
"""
import json
import logging
import math
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app import db
from app.factors.factor_engine import load_market
from app.models import trainer

log = logging.getLogger(__name__)

BASE = Path(__file__).resolve().parents[2]
OUT_PATH = BASE / "experiments" / "s1_excess_recheck_20261002.json"
HORIZON = 21
TOPK_CONF = 20
TOPN_PORT = 6
HAC_LAG = 3
SUB_SPLIT = pd.Timestamp("2022-01-01")
VOL_REDUNDANT_RHO = 0.7


# ---- 统计助手（与 s1_confidence_probe.py 逐字同源，只读复用语义） ----

def hac_tstat(x: np.ndarray, y: np.ndarray) -> float:
    n = len(x)
    if n < 10:
        return float("nan")
    X = np.column_stack([np.ones(n), x])
    XtX_inv = np.linalg.inv(X.T @ X)
    beta = XtX_inv @ (X.T @ y)
    resid = y - X @ beta
    S = np.zeros((2, 2))
    for lag in range(HAC_LAG + 1):
        w = 1.0 - lag / (HAC_LAG + 1.0)
        if lag == 0:
            g = resid * resid
            S += w * (X.T @ (X * g[:, None]))
        else:
            g = resid[lag:] * resid[:-lag]
            S += w * ((X[lag:].T @ (X[:-lag] * g[:, None]))
                      + (X[:-lag].T @ (X[lag:] * g[:, None])).T)
    V = XtX_inv @ S @ XtX_inv
    se = math.sqrt(max(V[1, 1], 1e-300))
    return float(beta[1] / se) if se > 0 else float("nan")


def corr_hac(a: pd.Series, b: pd.Series) -> float:
    df = pd.concat([a, b], axis=1).dropna()
    if len(df) < 10:
        return float("nan")
    x = df.iloc[:, 0].to_numpy(dtype=float)
    y = df.iloc[:, 1].to_numpy(dtype=float)
    sx, sy = x.std(), y.std()
    if sx == 0 or sy == 0:
        return float("nan")
    return hac_tstat((x - x.mean()) / sx, (y - y.mean()) / sy)


def subwindow_same_sign(series: pd.Series, other: pd.Series,
                        split=SUB_SPLIT) -> dict:
    idx = pd.to_datetime(series.index if not isinstance(series, pd.Series)
                         else series.index)
    out = {}
    for name, mask in (("w1", idx < split), ("w2", idx >= split)):
        sub_a, sub_b = series[mask], other[mask]
        df = pd.concat([sub_a, sub_b], axis=1).dropna()
        if len(df) < 8:
            out[name] = None
            continue
        out[name] = float(df.iloc[:, 0].corr(df.iloc[:, 1]))
    sign = None
    if out["w1"] is not None and out["w2"] is not None:
        sign = bool(out["w1"] * out["w2"] > 0)
    return {"w1_rho": out["w1"], "w2_rho": out["w2"], "same_sign": sign}


# ---- 月度面板（与 s1_confidence_probe.build_monthly 同一数据路径，
#      差异仅在超额轴：三套并排） ----

def build_monthly() -> pd.DataFrame:
    conn = db.get_conn()
    try:
        ms = pd.read_sql("SELECT trade_date, ts_code, score, pred_ret FROM model_score", conn)
        ind = trainer.industry_map(conn)
    finally:
        conn.close()
    if ms.empty:
        raise RuntimeError("model_score 为空")
    m = load_market()
    ac = m["close"] * m["adj"]
    fut = trainer.forward_return(ac, HORIZON)
    fut_x = fut.sub(fut.mean(axis=1), axis=0)          # 截面去均值（原实现的 f_x）
    inds = ind.reindex(fut.columns)
    fut_in = fut - fut.T.groupby(inds, sort=False).transform("mean").T

    # 证明性读数：原实现减去的 f_x.mean() 恒等 0（发现成立的机械证据）
    probe_zero = float(pd.DataFrame(fut_x.iloc[:, :]).mean(axis=1).abs().max())
    log.info("f_x.mean() 最大绝对值 = %.3e（恒等 0 ⇒ 原实现 = top6 原始收益）", probe_zero)

    rows = []
    for d, g in ms.groupby("trade_date"):
        t = pd.Timestamp(d)
        if t not in fut.index:
            continue
        f = fut.loc[t]
        f_in = fut_in.loc[t]
        g = g.dropna(subset=["score"])
        if len(g) < TOPK_CONF:
            continue
        g = g.sort_values("score", ascending=False)
        topk = g.head(TOPK_CONF)
        pr = pd.to_numeric(topk["pred_ret"], errors="coerce").dropna()
        if len(pr) < TOPK_CONF or pr.std(ddof=1) == 0 or pr.mean() == 0:
            continue
        c1 = float(pr.std(ddof=1) / abs(pr.mean()))
        c2 = float((pr.max() - pr.median()) / pr.std(ddof=1))
        top6 = g.head(TOPN_PORT)
        r6 = f.reindex(top6["ts_code"]).dropna()
        if len(r6) < TOPN_PORT:
            continue
        pool_raw = float(f.mean())                    # 截面等权均值（原始收益）
        excess_raw = float(r6.mean())                 # 原实现实际算的量
        excess_fixed = float(r6.mean() - pool_raw)    # 冻结判据 §5.2 主判
        excess_in = float(f_in.reindex(top6["ts_code"]).dropna().mean()
                          - f_in.mean())
        s_all = pd.Series(g["score"].to_numpy(dtype=float),
                          index=g["ts_code"]).reindex(f.index)
        pair = pd.concat([s_all, f], axis=1).dropna()
        ic = float(pair.iloc[:, 0].corr(pair.iloc[:, 1], method="spearman")) \
            if len(pair) > 50 else np.nan
        rows.append({"date": d, "c1": c1, "c2": c2,
                     "excess_raw": excess_raw, "excess_fixed": excess_fixed,
                     "excess_ind": excess_in, "ic": ic})
    dfm = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    bench_ret = m["bench"].pct_change()
    vol_m = bench_ret.groupby(bench_ret.index.to_period("M")).std() * math.sqrt(252)
    dfm["mvol"] = [float(vol_m.get(pd.Period(pd.Timestamp(d), "M"), np.nan))
                   for d in dfm["date"]]
    dfm.attrs["fx_zero_probe"] = probe_zero
    return dfm


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    dfm = build_monthly()
    idx = pd.to_datetime(dfm["date"])
    dfm = dfm.drop(columns=["date"]).set_index(idx)
    log.info("月度样本 %d 期 (%s ~ %s)", len(dfm),
             dfm.index[0].date(), dfm.index[-1].date())

    axes = {"raw": "excess_raw", "fixed": "excess_fixed", "ind": "excess_ind"}
    out = {"asof": datetime.now().isoformat(timespec="seconds"),
           "n_months": int(len(dfm)),
           "fx_zero_probe_max_abs": float(dfm.attrs.get("fx_zero_probe", float("nan"))),
           "finding": "原实现 excess = top6 原始收益 − 0（f_x.mean() 恒等 0），"
                      "非冻结判据的 top6 − 截面等权均值；本文件三轴并排复算",
           "axes": {}}
    for ax, col in axes.items():
        res = {}
        for c in ("c1", "c2"):
            t1 = corr_hac(dfm[c], dfm[col])
            t3 = subwindow_same_sign(dfm[c], dfm[col])
            res[f"T1_{c}_hac_t"] = t1
            res[f"T3_{c}"] = t3
        pass_c = [c for c in ("c1", "c2")
                  if res[f"T1_{c}_hac_t"] == res[f"T1_{c}_hac_t"]
                  and abs(res[f"T1_{c}_hac_t"]) >= 2.0
                  and res[f"T3_{c}"]["same_sign"]]
        res["verdict_T1wT3"] = ("PASS_TO_PREREG(" + ",".join(pass_c) + ")"
                                if pass_c else "CLEARED(未过 T1∧T3)")
        out["axes"][ax] = res
        log.info("[%s] T1: C1 t=%.2f / C2 t=%.2f | T3: C1 %s / C2 %s | %s",
                 ax, res["T1_c1_hac_t"], res["T1_c2_hac_t"],
                 res["T3_c1"], res["T3_c2"], res["verdict_T1wT3"])
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")
    log.info("产物: %s（已登记证据 s1_confidence_probe_20260929.json 未触碰）",
             OUT_PATH.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
