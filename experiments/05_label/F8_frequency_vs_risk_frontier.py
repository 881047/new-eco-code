"""F8 · 「≥3% 的周有多频繁」vs 「代价是什么」—— 频率-风险前沿

## 用户目标的第二种读法

上一轮(F7)已证:**周均 3%** 不可达(现实 +0.391%、幻想上界 +1.115%)。
但目标还可以读成「**经常出现 3% 的周**」——那是一个**频率**问题,不是均值问题,
而且它有一个真实的旋钮:**波动率**。

## 本实验的预注册问题

    Q1 把组合推向高波动(更集中 / 高波动子样本 / 小市值子样本 / 二者叠加),
       ≥3% 的周占比能从 28.5%(top6 毛口径)推到多少?
    Q2 代价是什么?(周均、最差周、MDD、Sharpe)
    Q3 若把周均补到 3%,需要多少倍杠杆,以及那时代价是多少(爆仓/深回撤)?

## 口径(如实声明)

- 信号与打分固定:沿用 `F4_scores_seedA.npz`(4 种子 LightGBM),**不重训、不调参**;
- 各臂**全部使用"毛口径"**(零费用零滑点、且不设涨跌停闸门)⇒ 这是**上界**,
  现实口径会出现多大衰减,见 F7 的 A/B 对照(周均 1.115% → 0.391%,约 −65%);
- 子样本按**锚点当日截面**取分位(高波动 = `vol20` 前 30%;小市值 = `total_mv` 后 30%),
  逐周重算,无前视;
- 杠杆只做**解析缩放**(均值/σ/MDD 同比放大,并单列融资成本 ≈5.5%/年),不重跑撮合。

用法:
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F8_frequency_vs_risk_frontier.py
"""
import argparse
import hashlib
import importlib.util
import json
import logging
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent
sys.path.insert(0, str(BASE))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


f1 = _load("f1", HERE / "F1_weekly_ic_decay.py")
f1b = _load("f1b", HERE / "F1b_weekly_open_overlap.py")

from app import config as cfg_mod            # noqa: E402
from app.factors import factor_engine as fe  # noqa: E402
from app.pit.engine import PITEngine         # noqa: E402

log = logging.getLogger("f8")
OUT = HERE / "F8_frequency_vs_risk_frontier_result.json"
TARGET = 0.03
MARGIN = 0.055          # 融资年化成本(解析缩放用)


def dist(ret: pd.Series, per_year: float = 52.0) -> dict:
    r = ret.dropna()
    if len(r) < 10:
        return {"n": int(len(r))}
    nav = (1 + r).cumprod()
    ann = float(nav.iloc[-1] ** (per_year / len(r)) - 1)
    mdd = float((nav / nav.cummax() - 1).min())
    mu, sd = float(r.mean()), float(r.std(ddof=1))
    lev = TARGET / mu if mu > 0 else None
    return {
        "n": int(len(r)),
        "mean_pct": round(100 * mu, 3),
        "median_pct": round(100 * float(r.median()), 3),
        "std_pct": round(100 * sd, 3),
        "ge3_frac": round(float((r >= 0.03).mean()), 4),
        "ge5_frac": round(float((r >= 0.05).mean()), 4),
        "le_neg10_frac": round(float((r <= -0.10).mean()), 4),
        "best_pct": round(100 * float(r.max()), 2),
        "worst_pct": round(100 * float(r.min()), 2),
        "ann_pct": round(100 * ann, 1),
        "mdd_pct": round(100 * mdd, 1),
        "sharpe": round(mu / sd * math.sqrt(per_year), 3) if sd > 0 else None,
        "lev_to_mean3x": round(lev, 2) if lev else None,
        "mdd_at_lev_pct": round(100 * mdd * lev, 1) if lev else None,
        "ann_at_lev_pct": round(100 * ((1 + mu - MARGIN / per_year) * lev
                                        + (1 - lev)) ** per_year - 100, 1) if lev else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="20140101")
    ap.add_argument("--scores", default=str(HERE / "F4_scores_seedA.npz"))
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since=a.since)
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    pit = PITEngine(m=m, cfg=cfg)
    cols = list(m["close"].columns)
    base_mask = f1.tradable_frame(pit, wk, cols)

    ac = (m["close"] * m["adj"]).astype(np.float64)
    ac.index = dates
    ret = ac.pct_change()
    vol20 = ret.rolling(20).std().reindex(index=wk, columns=cols)
    mv = m["total_mv"].reindex(index=wk, columns=cols)

    z = np.load(a.scores, allow_pickle=True)
    s_anch = pd.to_datetime([str(d) for d in z["anchors"]])
    s_cols = [str(c) for c in z["cols"]]
    ka = [i for i, d in enumerate(s_anch) if d in set(wk)]
    kc = [i for i, c in enumerate(s_cols) if c in set(cols)]
    S = pd.DataFrame(z["scores"][np.ix_(ka, kc)], index=s_anch[ka],
                     columns=[s_cols[i] for i in kc])
    log.info("打分 %s | 周锚点 %d | 票池均值 %.0f", S.shape, len(wk),
             base_mask.sum(axis=1).mean())

    def subset(kind: str) -> pd.DataFrame:
        """子样本掩码(逐周截面分位, 无前视)。"""
        if kind == "all":
            return base_mask
        v = vol20 if kind.startswith("vol") else mv
        q = v.rank(axis=1, pct=True)
        sel = q >= 0.70 if kind.startswith("vol") else q <= 0.30
        return base_mask & sel.fillna(False)

    arms = [
        ("1_top6_all", 6, "all"),
        ("2_top3_all", 3, "all"),
        ("3_top6_highvol", 6, "vol"),
        ("4_top6_smallcap", 6, "mv"),
        ("5_top6_vol_small", 6, "vol"),      # 下面叠加 mv 过滤
        ("6_top12_all", 12, "all"),
    ]
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "scores": Path(a.scores).name, "target": TARGET, "margin": MARGIN,
           "arms": {}, "secs": None}
    for tag, top_n, kind in arms:
        mk = subset(kind)
        if tag == "5_top6_vol_small":
            mk = mk & subset("mv")
        df = f1b.run_overlap_portfolio(m, {}, wk, mk, 5, {**cfg, "capital": 1_000_000.0},
                                       pit, 0, score_matrix=S, top_n=top_n, gross=True)
        if df is None:
            log.warning("[%s] 无有效期数", tag)
            continue
        res["arms"][tag] = dist(df["ret"])
        d = res["arms"][tag]
        log.info("[%s] n=%d 周均 %+.3f%% σ%.2f%% | ≥3%%的周 %.1f%% | ≥5%% %.1f%% | ≤−10%% %.1f%%"
                 " | 年化 %+.1f%% MDD %.1f%% | 要周均3%%: %.2f× ⇒ MDD %.0f%% / 年化 %+.0f%%",
                 tag, d["n"], d["mean_pct"], d["std_pct"], 100 * d["ge3_frac"],
                 100 * d["ge5_frac"], 100 * d["le_neg10_frac"], d["ann_pct"], d["mdd_pct"],
                 d["lev_to_mean3x"] or float("nan"), d["mdd_at_lev_pct"] or float("nan"),
                 d["ann_at_lev_pct"] or float("nan"))
    best = max(res["arms"].items(), key=lambda kv: kv[1].get("ge3_frac", 0), default=(None, {}))
    res["verdict"] = {
        "max_ge3_frac_arm": best[0],
        "max_ge3_frac": best[1].get("ge3_frac"),
        "max_mean_pct": max(v.get("mean_pct", -9) for v in res["arms"].values()),
        "any_mean_ge_3": any(v.get("mean_pct", 0) >= 3.0 for v in res["arms"].values()),
    }
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 判决: 最高 ≥3%% 频率 = %.1f%% (%s) | 最高周均 %+.3f%% | 完成 %.1fs → %s ==",
             100 * (res["verdict"]["max_ge3_frac"] or 0), res["verdict"]["max_ge3_frac_arm"],
             res["verdict"]["max_mean_pct"], res["secs"], OUT)


if __name__ == "__main__":
    main()
