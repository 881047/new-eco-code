# -*- coding: utf-8 -*-
"""概率审计 —— "炒股是讲究概率的事情, 我们做得如何"的机械回答(2026-10-02, 只读)。

三层面:
  A. 单注优势: 月度超额的 胜率/均值/赔率(均盈/均亏)/盈亏比/利润因子/最长连亏/偏度
  B. 广度: 独立注数(月频 top6, 考虑持仓重叠) + 每注 base rate(top6 个股跑赢截面均值比例)
  C. 校准: IC>0 月份占比(正态近似)、月度超额 HAC t 与年化置信区间——我们的优势
     有多少是统计上"站得住"的
产物 experiments/probability_audit_20261002.json
"""
import json
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

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "probability_audit_20261002.json"
RUNS = {"prod": "20260928_exe", "full": "20260928_exe_full"}
HORIZON = 21
TOPN = 6


def hac_t(x: np.ndarray, lag: int = 3) -> float:
    raw = np.asarray(x, dtype=float)
    n = len(raw)
    mu = float(raw.mean())
    x = raw - mu                      # 残差只用于协方差; 分子必须是原始均值(首版自 demean 再取均值 = t 恒 0 的自吞 bug)
    s = float((x * x).sum())
    for k in range(1, lag + 1):
        g = float((x[k:] * x[:-k]).sum())
        s += 2 * (1 - k / (lag + 1.0)) * g
    se = math.sqrt(s / n)
    return float(mu / se * math.sqrt(n)) if se > 0 else float("nan")


def audit_series(exc: pd.Series) -> dict:
    s = exc.dropna().to_numpy()
    wins, losses = s[s > 0], s[s < 0]
    # 最长连亏
    max_lose = cur = 0
    for v in s:
        cur = cur + 1 if v < 0 else 0
        max_lose = max(max_lose, cur)
    mean, sd = float(s.mean()), float(s.std(ddof=1))
    return {
        "n": int(len(s)),
        "win_rate": round(float((s > 0).mean()), 4),
        "mean_monthly": round(mean, 5),
        "median_monthly": round(float(np.median(s)), 5),
        "avg_win": round(float(wins.mean()), 5) if len(wins) else None,
        "avg_loss": round(float(losses.mean()), 5) if len(losses) else None,
        "payoff_ratio": round(float(abs(wins.mean() / losses.mean())), 3)
        if len(wins) and len(losses) else None,
        "profit_factor": round(float(wins.sum() / abs(losses.sum())), 3)
        if len(losses) and losses.sum() != 0 else None,
        "max_consec_losses": int(max_lose),
        "skew": round(float(pd.Series(s).skew()), 3),
        "excess_kurtosis": round(float(pd.Series(s).kurt()), 3),
        "monthly_vol": round(sd, 5),
        "hac_t_lag3": round(hac_t(s), 2),
        "ann_excess_ci95": [round(12 * (mean - 1.96 * sd / math.sqrt(len(s))), 4),
                            round(12 * (mean + 1.96 * sd / math.sqrt(len(s))), 4)],
    }


def main() -> int:
    conn = db.get_conn()
    try:
        navs = {}
        for wname, run in RUNS.items():
            df = pd.read_sql("SELECT trade_date, sys_nav, bench_nav FROM backtest_nav "
                             "WHERE run_date=? ORDER BY trade_date", conn, params=(run,))
            navs[wname] = df
        ms = pd.read_sql("SELECT trade_date, ts_code, score FROM model_score", conn)
    finally:
        conn.close()

    m = load_market()
    ac = m["close"] * m["adj"]
    fwd = trainer.forward_return(ac, HORIZON)
    pool_mean = fwd.mean(axis=1)                     # 截面等权均值(每注的及格线)

    # B. 每注 base rate: top6 个股 21 日收益 > 截面均值 的比例(vs 50% 无技能)
    hits, tot = 0, 0
    monthly_hit = {}
    for d, g in ms.groupby("trade_date"):
        t = pd.Timestamp(d)
        if t not in fwd.index:
            continue
        f = fwd.loc[t].dropna()
        pm = float(pool_mean.loc[t]) if t in pool_mean.index else None
        g = g.dropna(subset=["score"])
        if len(g) < 50 or pm is None:
            continue
        top6 = g.nlargest(TOPN, "score")["ts_code"].tolist()
        r6 = f.reindex(top6).dropna()
        h = int((r6 > pm).sum())
        hits += h
        tot += len(r6)
        monthly_hit[d] = h / len(r6) if len(r6) else None
    hit_rate = hits / tot if tot else None
    # 二项检验: 命中率 vs 0.5
    z = (hit_rate - 0.5) / math.sqrt(0.25 / tot) if tot else None

    out = {"asof": datetime.now().isoformat(timespec="seconds"),
           "per_stock_base_rate": {
               "n_bets": int(tot), "hit_rate_vs_pool": round(hit_rate, 4),
               "binomial_z_vs_50pct": round(z, 2),
               "monthly_hit_rate_mean": round(float(np.nanmean(
                   [v for v in monthly_hit.values() if v is not None])), 4)},
           "windows": {}}
    for wname, df in navs.items():
        df["sys_ret"] = df["sys_nav"].pct_change()
        df["bench_ret"] = df["bench_nav"].pct_change()
        exc = (df["sys_ret"] - df["bench_ret"]).iloc[1:]
        out["windows"][wname] = audit_series(exc)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str)[:1400])
    print(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
