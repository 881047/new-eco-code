# -*- coding: utf-8 -*-
"""S1 置信度可观测性探针 —— "模型自身置信里有没有可预测的信号"。

plans/return-stability-deep-analysis-20260929.md §5.2（跑数前冻结）：
- 置信代理（逐月, t = 月末截面, 全部月内标准化）:
    C1 = top-20(按 score) 候选 pred_ret 的变异系数 std/|mean|
    C2 = (top-1 pred_ret − top-20 中位数) / std(top-20 pred_ret)
- 结果序列: 决策月 t 的"下月策略超额" = top6(按 score) 等权 forward 21 交易日收益
    − 同月截面等权均值(主判) / 行业中性均值(敏感性)。
    对齐口径: t 月选出的持仓赚的是 t→t+1 的收益 ⇒ C_t 对齐该超额(同期索引)。
- 判据(冻结):
    T1: corr(C_t, excess_t) 的 HAC |t| >= 2 (Newey-West lag=3)
    T2: IC 自相关 corr(IC_t, IC_{t+1}) HAC |t| >= 2 (信息性, 亦报)
    T3: 过判据的代理在两子窗(2017-01~2021-12 / 2022-01~2026-08)同号
    T4(信息性): C_t 与已实现市场波动 |rho|>0.7 ⇒ 与 vol 缩放冗余, 注记
- 判定: T1 ∧ T3 过(C1/C2 任一) ⇒ S1 进全量预注册; 否则 S1 CLEARED(本构造下未证实)线结清。
- ⚠️ 2026-10-02 审查修正: 首跑(20260929 证据)主判轴实现为"top6 原始收益"(f_x.mean()
  恒等 0 的截断), 与冻结判据不符; 三轴并排复算确认判决 CLEARED 不被翻转(修正轴
  T1 过门槛 −2.17 但 T3 异号), 判决维持 —— 详见 experiments/s1_excess_recheck_20261002.json。

read-only: 不写任何生产表。用法:
    .venv/Scripts/python.exe scripts/s1_confidence_probe.py
"""
from __future__ import annotations

import json
import logging
import math
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from app import db
from app.factors.factor_engine import load_market
from app.models import trainer

log = logging.getLogger(__name__)

BASE = Path(__file__).resolve().parent.parent
# v2 产物名(2026-10-02): v1 = s1_confidence_probe_20260929.json 是已登记判决证据
# (CLEARED), 且其主判轴是"top6 原始收益"的实现口径(见 excess 处事故注记) ——
# 修正轴的重跑写新文件, 不覆盖证据; 修正有效性由
# experiments/s1_excess_recheck_20261002.json 三轴并排读数背书。
OUT_PATH = BASE / "experiments" / "s1_confidence_probe_v2_20261002.json"
HORIZON = 21
TOPK_CONF = 20
TOPN_PORT = 6
HAC_LAG = 3
SUB_SPLIT = pd.Timestamp("2022-01-01")          # T3 两子窗分界
VOL_REDUNDANT_RHO = 0.7                          # T4


def hac_tstat(x: np.ndarray, y: np.ndarray) -> float:
    """单变量 OLS(带常数)斜率的 Newey-West t 统计量(lag=HAC_LAG)。x/y 已去 NaN 对齐。"""
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
    """相关系数的 HAC t(等价于对标准化序列做带常数回归)。"""
    df = pd.concat([a, b], axis=1).dropna()
    if len(df) < 10:
        return float("nan")
    x = df.iloc[:, 0].to_numpy(dtype=float)
    y = df.iloc[:, 1].to_numpy(dtype=float)
    sx, sy = x.std(), y.std()
    if sx == 0 or sy == 0:
        return float("nan")
    return hac_tstat((x - x.mean()) / sx, (y - y.mean()) / sy)


def build_monthly() -> pd.DataFrame:
    """逐月: C1/C2(置信) + excess(下月策略超额, 主/敏感性) + IC + 市场波动。"""
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
    fut = trainer.forward_return(ac, HORIZON)            # 21 日前瞻收益(退市回退口径)
    inds = ind.reindex(fut.columns)
    fut_in = fut - fut.T.groupby(inds, sort=False).transform("mean").T   # 行业中性(敏感性)

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
        excess = float(r6.mean() - f.mean())
        # 2026-10-02 审查修正(复算器 oneoff/s1_excess_recheck_20261002.py 三轴并排):
        # 原写 r6.mean() - f_x.mean(), 而 f_x 是截面去均值后的行 ⇒ f_x.mean() 恒等 0,
        # 主判轴实际 = top6 **原始**收益(市场分量混入, |t| 被稀释: C1 −1.45→修正后 −2.17)。
        # 冻结判据 §5.2 主判 = top6 forward − 同月截面等权均值(原始收益口径)。修正后
        # 主判轴 C1 t=−2.17 过 T1 门槛但 T3 两子窗仍异号 ⇒ 20260929 在册判决 CLEARED
        # 不被翻转; 行业中性轴(本就正确) T1∧T3 双过维持"旁证只登记"定位。
        excess_in = float(f_in.reindex(top6["ts_code"]).dropna().mean()
                          - f_in.mean())
        # IC: score 与 fwd21 的 Spearman(有限值交集)
        s_all = pd.Series(g["score"].to_numpy(dtype=float),
                          index=g["ts_code"]).reindex(f.index)
        pair = pd.concat([s_all, f], axis=1).dropna()
        ic = float(pair.iloc[:, 0].corr(pair.iloc[:, 1], method="spearman")) if len(pair) > 50 \
            else np.nan
        rows.append({"date": d, "c1": c1, "c2": c2, "excess": excess,
                     "excess_ind": excess_in, "ic": ic})
    dfm = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    # 市场波动(信息性): 基准日收益逐月年化 std
    bench_ret = m["bench"].pct_change()
    vol_m = bench_ret.groupby(bench_ret.index.to_period("M")).std() * math.sqrt(252)
    dfm["mvol"] = [float(vol_m.get(pd.Period(pd.Timestamp(d), "M"), np.nan))
                   for d in dfm["date"]]
    return dfm


def subwindow_same_sign(series: pd.Series, other: pd.Series, split=SUB_SPLIT) -> dict:
    idx = pd.to_datetime(series.index if not isinstance(series, pd.Series) else series.index)
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


def run() -> dict:
    dfm = build_monthly()
    idx = pd.to_datetime(dfm["date"])
    dfm = dfm.drop(columns=["date"]).set_index(idx)
    log.info("月度样本 %d 期 (%s ~ %s)", len(dfm), dfm.index[0].date(), dfm.index[-1].date())

    res = {"n_months": int(len(dfm))}
    # IC 自相关(T2)
    res["T2_ic_autocorr_hac_t"] = corr_hac(dfm["ic"], dfm["ic"].shift(-1))
    for c in ("c1", "c2"):
        t1 = corr_hac(dfm[c], dfm["excess"])
        t1_ind = corr_hac(dfm[c], dfm["excess_ind"])
        t3 = subwindow_same_sign(dfm[c], dfm["excess"])
        t4 = float(dfm[c].corr(dfm["mvol"]))
        res[f"T1_{c}_hac_t"] = t1
        res[f"T1_{c}_ind_hac_t"] = t1_ind
        res[f"T3_{c}"] = t3
        res[f"T4_{c}_vol_rho"] = t4
    pass_c = [c for c in ("c1", "c2")
              if res[f"T1_{c}_hac_t"] == res[f"T1_{c}_hac_t"]
              and abs(res[f"T1_{c}_hac_t"]) >= 2.0
              and res[f"T3_{c}"]["same_sign"]]
    res["verdict"] = ("PASS_TO_PREREG(置信代理 " + ",".join(pass_c)
                      + " 过 T1∧T3 ⇒ S1 进全量预注册)" if pass_c
                      else "CLEARED(本构造下未证实 ⇒ S1 线结清, 与拥挤度量器探针同措辞纪律)")
    out = {"asof": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "criteria_file": "plans/return-stability-deep-analysis-20260929.md §5.2",
           "monthly": dfm.reset_index().assign(date=lambda d: d["date"].dt.strftime("%Y%m%d")
                                               ).to_dict("records"),
           **{k: v for k, v in res.items() if k != "monthly"}}
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")
    log.info("T1: C1 t=%.2f / C2 t=%.2f | T2 IC 自相关 t=%.2f | T4: ρ(C1,mvol)=%.2f | %s",
             res.get("T1_c1_hac_t", float("nan")), res.get("T1_c2_hac_t", float("nan")),
             res.get("T2_ic_autocorr_hac_t", float("nan")),
             res.get("T4_c1_vol_rho", float("nan")), res["verdict"])
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    r = run()
    print(json.dumps({k: v for k, v in r.items() if k != "monthly"},
                     ensure_ascii=False, indent=1, default=str))
