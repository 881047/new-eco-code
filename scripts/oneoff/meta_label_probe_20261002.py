# -*- coding: utf-8 -*-
"""元标签层探针（plans/meta-labeling-prereg-20261002.md 规格与判据跑前冻结）。只读零写库。
赌注级数据集(696 注) → walk-forward 元模型(Logistic/LGBM 双变体) → 过滤/仓位/零ML对照
四臂 NAV 级模拟 → P1/P2/P3 判决。"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd
import lightgbm as lgb


def _fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1.0,
                  iters: int = 3000, lr: float = 0.3):
    """numpy 逻辑回归(full-batch GD + L2; sklearn 不在 venv 且 SAC 环境装依赖有风险)。"""
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd = np.where(sd > 1e-12, sd, 1.0)
    Z = (X - mu) / sd
    Z = np.column_stack([np.ones(len(Z)), Z])
    w = np.zeros(Z.shape[1])
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-Z @ w))
        grad = Z.T @ (p - y) / len(y) + l2 * np.r_[0.0, w[1:]] / len(y)
        w -= lr * grad
    return {"mu": mu, "sd": sd, "w": w}


def _predict_logistic(model, X: np.ndarray) -> np.ndarray:
    Z = (X - model["mu"]) / model["sd"]
    Z = np.column_stack([np.ones(len(Z)), Z])
    return 1.0 / (1.0 + np.exp(-Z @ model["w"]))

from app import db
from app.factors.factor_engine import load_market
from app.models import trainer

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "meta_label_probe_20261002.json"
TOPN = 6
MIN_BETS = 200
EMBARGO = 2
COST = 0.006


def log(m):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def build_bets():
    conn = db.get_conn()
    try:
        ms = pd.read_sql("SELECT trade_date, ts_code, score FROM model_score", conn)
    finally:
        conn.close()
    ms["d"] = pd.to_datetime(ms["trade_date"], format="%Y%m%d")
    m = load_market()
    ac = (m["close"] * m["adj"]).astype(float)
    fwd = trainer.forward_return(ac, trainer.HORIZON)
    pool_mean = fwd.mean(axis=1)
    circ = m.get("circ_mv")
    vol60 = ac.pct_change().rolling(60).std()
    vol60_pctl = vol60.rank(axis=1, pct=True)
    if circ is not None:
        size_pctl = circ.rank(axis=1, pct=True)
    else:
        size_pctl = ac.iloc[:, :].apply(lambda c: np.nan) * np.nan
    to = m.get("turnover")
    to_pctl = to.rank(axis=1, pct=True) if to is not None else size_pctl * np.nan
    bench = m["bench"]
    mvol = bench.pct_change().rolling(20).std()
    bench_3m = bench.pct_change(60)
    td = pd.DatetimeIndex(m["close"].index)
    rows = []
    for d, g in ms.groupby("d"):
        t = pd.Timestamp(d)
        if t not in fwd.index:
            continue
        g = g.dropna(subset=["score"])
        if len(g) < 50:
            continue
        top = g.nlargest(TOPN, "score")
        pm = float(pool_mean.loc[t]) if t in pool_mean.index else None
        if pm is None:
            continue
        # 行业集中度
        ind_series = None
        try:
            conn2 = db.get_conn()
            try:
                from app.models import trainer as _t
                ind_series = _t.industry_map(conn2)
            finally:
                conn2.close()
        except Exception:
            ind_series = None
        if ind_series is not None:
            inds = ind_series.reindex(top["ts_code"])
            conc = float(inds.value_counts(normalize=True).iloc[0]) if inds.notna().any() else np.nan
        else:
            conc = np.nan
        # 第 7 名分差
        srt = g.sort_values("score", ascending=False)
        margin = float(srt["score"].iloc[5] - srt["score"].iloc[6]) if len(srt) > 6 else np.nan
        mv = float(mvol.loc[t]) if t in mvol.index and pd.notna(mvol.loc[t]) else np.nan
        b3 = float(bench_3m.loc[t]) if t in bench_3m.index and pd.notna(bench_3m.loc[t]) else np.nan
        for rank, (_, r) in enumerate(top.iterrows(), start=1):
            c = r["ts_code"]
            f = fwd.loc[t, c] if c in fwd.columns else np.nan
            f = float(f) if pd.notna(f) else np.nan
            v60 = float(vol60_pctl.loc[t, c]) if c in vol60_pctl.columns and pd.notna(vol60_pctl.loc[t, c]) else np.nan
            sz = float(size_pctl.loc[t, c]) if c in size_pctl.columns and pd.notna(size_pctl.loc[t, c]) else np.nan
            tp = float(to_pctl.loc[t, c]) if c in to_pctl.columns and pd.notna(to_pctl.loc[t, c]) else np.nan
            rows.append({
                "date": d, "ts_code": c, "score": float(r["score"]), "rank": rank,
                "margin": margin, "v60": v60, "size": sz, "to": tp,
                "mvol": mv, "bench3m": b3, "conc": conc,
                "fwd": f, "win": int(f > pm) if pd.notna(f) else np.nan,
            })
    bets = pd.DataFrame(rows)
    bets["month"] = pd.to_datetime(bets["date"]).dt.strftime("%Y%m")
    return bets.sort_values("date").reset_index(drop=True)


FEATS = ["score", "rank", "margin", "v60", "size", "to", "mvol", "bench3m", "conc"]


def simulate(bets, kept_mask_by_month, label):
    """NAV 级月度模拟: 每月等权持有 kept 注(kept=0 全现金), 21 日收益。"""
    nav = 1.0
    prev_set = set()
    monthly = []
    trade_wins = trade_n = 0
    for month, g in bets.groupby("month", sort=True):
        mask = kept_mask_by_month.loc[g.index]
        held = g[mask]
        cur_set = set(held["ts_code"])
        churn = len(cur_set - prev_set) if prev_set else len(cur_set)
        cost = COST * churn / max(1, len(cur_set)) if len(cur_set) else 0.0
        rets = held["fwd"].dropna()
        port = float(rets.mean()) * len(held) / TOPN if len(held) else 0.0
        nav *= (1 + port - cost)
        monthly.append({"month": month, "ret": port - cost, "n_kept": int(len(held))})
        trade_wins += int(held["win"].sum())
        trade_n += int(held["win"].notna().sum())
        prev_set = cur_set
    s = pd.Series([x["ret"] for x in monthly]).dropna()
    years = len(s) / 12
    ann = float((np.prod(1.0 + s)) ** (1 / years) - 1) if years > 0 and (1.0 + s).prod() > 0 else np.nan
    nav_s = (1 + s).cumprod()
    mdd = float((nav_s / nav_s.cummax() - 1).min())
    return {"label": label, "ann": round(float(ann), 4), "mdd": round(mdd, 4),
            "bet_win_rate": round(trade_wins / trade_n, 4) if trade_n else None,
            "n_bets": int(trade_n), "monthly": monthly}


def main() -> int:
    bets = build_bets()
    log(f"赌注 {len(bets)} 注 / {bets['month'].nunique()} 月 | 整体单注胜率 "
        f"{bets['win'].mean():.1%}")
    X = bets[FEATS].astype(float)
    bets = bets.reset_index(drop=True)
    X = X.fillna(X.median())

    # walk-forward 元模型预测
    p_log = np.full(len(bets), np.nan)
    p_lgb = np.full(len(bets), np.nan)
    months = bets["month"].unique()
    resolved_count = 0
    for mi, month in enumerate(months):
        idx = bets.index[bets["month"] == month]
        resolved = bets.iloc[:idx[0]]
        resolved = resolved[resolved["win"].notna()]
        # embargo: 最近 2 个月的赌注窗未结算
        resolved = resolved[resolved["month"] < months[max(0, mi - EMBARGO)]]
        if len(resolved) < MIN_BETS:
            continue
        tr_X = X.loc[resolved.index]
        tr_y = resolved["win"].astype(int)
        clf = _fit_logistic(tr_X.to_numpy(dtype=float), tr_y.to_numpy(dtype=float))
        p_log[idx] = _predict_logistic(clf, X.loc[idx].to_numpy(dtype=float))
        gm = lgb.train(dict(objective="binary", metric="binary_logloss",
                            num_leaves=7, learning_rate=0.05, verbose=-1, seed=42),
                       lgb.Dataset(tr_X.to_numpy(dtype=float),
                                   label=tr_y.to_numpy(dtype=int)),
                       num_boost_round=30)
        p_lgb[idx] = gm.predict(X.loc[idx].to_numpy(dtype=float))
        resolved_count = len(resolved)
    log(f"元模型覆盖 {int(pd.notna(p_log).sum())} 注 (最终训练集 {resolved_count} 注)")

    bets["p_log"] = p_log
    bets["p_lgb"] = p_lgb
    # 未覆盖月份 → 退化 base(全保留等权)
    masks = {}
    masks["base"] = pd.Series(True, index=bets.index)
    for col, tag in (("p_log", "F_log"), ("p_lgb", "F_lgb")):
        keep = bets[col].isna() | (bets[col] >= 0.5)
        masks[tag] = keep

    results = {}
    results["base"] = simulate(bets, masks["base"], "base")
    results["F_log"] = simulate(bets, masks["F_log"], "F_log")
    results["F_lgb"] = simulate(bets, masks["F_lgb"], "F_lgb")
    # R 臂(零 ML): 权重 ∝ score 排名 → 等价于按 rank 加权的月收益
    monthly_r = []
    for month, g in bets.groupby("month", sort=True):
        w = g["rank"].map(lambda k: (TOPN - k + 1) / TOPN)
        w = w / w.sum()
        rets = g["fwd"].fillna(0)
        monthly_r.append(float((w * rets).sum()))
    s = pd.Series(monthly_r)
    years = len(s) / 12
    results["R_size"] = {"label": "R_size", "ann": round(float((np.prod(1.0 + s)) ** (1 / years) - 1), 4),
                         "mdd": None, "bet_win_rate": round(bets["win"].mean(), 4),
                         "n_bets": int(bets["win"].notna().sum()), "monthly": None}

    # 判据: 最优 F 变体
    best = max(("F_log", "F_lgb"), key=lambda k: results[k]["ann"])
    b = results[best]
    base = results["base"]
    r_ann = results["R_size"]["ann"]
    p1 = bool(b["ann"] >= base["ann"] - 0.01 and b["ann"] >= r_ann)
    p2 = bool(b["bet_win_rate"] is not None and base.get("bet_win_rate")
              and b["bet_win_rate"] >= base["bet_win_rate"] + 0.05)
    p3 = bool(b["mdd"] is not None and b["mdd"] >= base["mdd"] - 0.02)
    if p1 and p2 and p3:
        verdict = "GO(进 Phase 1 接线预注册)"
    elif p1:
        verdict = "INDETERMINATE(非劣但未过 H&T 门槛或胜率门槛)"
    else:
        verdict = "NO-GO(元标签线结清)"
    out = {"asof": datetime.now().isoformat(timespec="seconds"),
           "criteria_file": "plans/meta-labeling-prereg-20261002.md",
           "base_win_rate": round(float(bets["win"].mean()), 4),
           "arms": {k: {kk: vv for kk, vv in v.items() if kk != "monthly"}
                    for k, v in results.items()},
           "criteria": {"P1": p1, "P2": p2, "P3": p3, "best_F": best,
                        "verdict": verdict}}
    log(f"判决: {verdict} | P1={p1} P2={p2} P3={p3} | 最优 F={best}")
    for k, v in results.items():
        log(f"  {k}: ann={v['ann']:+.2%} mdd={v['mdd']} 单注胜率={v['bet_win_rate']} 注数={v['n_bets']}")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    log(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
