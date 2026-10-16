"""F7 · 周收益分布诊断 —— 回答「每 5 个交易日收益 > 3%」这个目标

## 为什么要单独量这个

目标从"年化"改成"**每 5 个交易日 > 3%**"：

    1.03^52 = **4.65 倍/年**（≈ +365%/年）

这比"年化 18%"高一个数量级。判词不能靠直觉，必须用**已实测构型的周收益分布**说话。

## 量什么

对以下三个臂，输出周收益的完整分布（不是只看年化）：

    A  M5 实盘口径   : 100 万 / top100 / 出带 8× / 4 种子 / 真费用 + 涨跌停闸门
    B  M5 毛口径     : 同上但零费用零闸门（上界: 假设什么都买得到、不要钱）
    C  top6 毛口径   : 6 只集中 + 零费用零闸门（历史上周均收益最高的形状）

指标：周均(算术)/中位/标准差/P75/P90/最好/最差、**≥3% 的周占比**、≥1% 占比、
复合年化、以及"要在均值为 3% 需要多少倍杠杆 + 杠杆后回撤"。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F7_weekly_return_distribution.py
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

log = logging.getLogger("f7")
OUT = HERE / "F7_weekly_return_distribution_result.json"
TARGET = 0.03


def stats(ret: pd.Series) -> dict:
    r = ret.dropna()
    if len(r) < 10:
        return {"n": int(len(r))}
    nav = (1 + r).cumprod()
    yrs = len(r) / 52.0
    ann = float(nav.iloc[-1] ** (1 / yrs) - 1)
    mdd = float((nav / nav.cummax() - 1).min())
    mu, sd = float(r.mean()), float(r.std(ddof=1))
    return {
        "n": int(len(r)),
        "mean_pct": round(100 * mu, 3),
        "median_pct": round(100 * float(r.median()), 3),
        "std_pct": round(100 * sd, 3),
        "p75_pct": round(100 * float(r.quantile(0.75)), 3),
        "p90_pct": round(100 * float(r.quantile(0.90)), 3),
        "best_pct": round(100 * float(r.max()), 2),
        "worst_pct": round(100 * float(r.min()), 2),
        "ge_3pct_frac": round(float((r >= TARGET).mean()), 4),
        "ge_1pct_frac": round(float((r >= 0.01).mean()), 4),
        "ann_pct": round(100 * ann, 2),
        "mdd_pct": round(100 * mdd, 2),
        "sharpe": round(mu / sd * math.sqrt(52), 3) if sd > 0 else None,
        "leverage_needed_for_mean_3pct": round(TARGET / mu, 2) if mu > 0 else None,
        "mdd_at_that_leverage_pct": round(100 * min(-1.0, mdd * (TARGET / mu)), 1) if mu > 0 else None,
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
    mask = f1.tradable_frame(pit, wk, cols)

    z = np.load(a.scores, allow_pickle=True)
    s_anch = pd.to_datetime([str(d) for d in z["anchors"]])
    s_cols = [str(c) for c in z["cols"]]
    keep_a = [i for i, d in enumerate(s_anch) if d in set(wk)]
    keep_c = [i for i, c in enumerate(s_cols) if c in set(cols)]
    S = pd.DataFrame(z["scores"][np.ix_(keep_a, keep_c)],
                     index=s_anch[keep_a], columns=[s_cols[i] for i in keep_c])
    log.info("打分 %s | 周锚点 %d | 票池均值 %.0f", S.shape, len(wk), mask.sum(axis=1).mean())

    arms = {
        "A_M5_net": dict(top_n=100, rank_band=8.0, hyst=0.0, gross=False, capital=1_000_000.0),
        "B_M5_gross": dict(top_n=100, rank_band=8.0, hyst=0.0, gross=True, capital=1_000_000.0),
        "C_top6_gross": dict(top_n=6, rank_band=0.0, hyst=0.0, gross=True, capital=1_000_000.0),
    }
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "scores": Path(a.scores).name, "target_per_5d": TARGET,
           "implied_annual_multiple": round((1 + TARGET) ** 52, 2), "arms": {}, "secs": None}
    for tag, kw in arms.items():
        cap = kw.pop("capital")
        df = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, {**cfg, "capital": cap},
                                       pit, 0, score_matrix=S, **kw)
        if df is None:
            log.warning("[%s] 无有效期数", tag)
            continue
        res["arms"][tag] = stats(df["ret"])
        s = res["arms"][tag]
        log.info("[%s] n=%d 周均 %.3f%% 中位 %.3f%% σ %.2f%% | ≥3%% 的周 %.1f%% | ≥1%% %.1f%% "
                 "| 复合年化 %.1f%% MDD %.1f%% | 均值到 3%% 需 %.1f× 杠杆(回撤 %.0f%%)",
                 tag, s["n"], s["mean_pct"], s["median_pct"], s["std_pct"],
                 100 * s["ge_3pct_frac"], 100 * s["ge_1pct_frac"], s["ann_pct"],
                 s["mdd_pct"], s["leverage_needed_for_mean_3pct"] or float("nan"),
                 s["mdd_at_that_leverage_pct"] or float("nan"))
    res["verdict"] = {
        "best_mean_pct": max((v["mean_pct"] for v in res["arms"].values()), default=None),
        "any_arm_mean_ge_3pct": any(v["mean_pct"] >= 3.0 for v in res["arms"].values()),
        "note": "≥3%/5交易日 ⇒ 年化 4.65 倍; 见 arms 的 mean_pct 与 ge_3pct_frac",
    }
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 完成 %.1fs → %s ==", res["secs"], OUT)


if __name__ == "__main__":
    main()
