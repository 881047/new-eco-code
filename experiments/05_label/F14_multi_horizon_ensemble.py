"""F14 · 多周期标签集成(5 日 + 21 日 + 生产月频模型) —— 预注册

## 动机

项目自己的异构原则说:**误差去相关要靠"口粮不同", 不是"算法不同"**。
目前的周频终端只有**一个 die**:5 日标签的 LightGBM(4 种子)。
而历史上"4 种子"只是同一信号的方差缩减, 不是去相关。

这里有三个**真正不同**的 die:

    W5  : 5 日标签 LightGBM(周锚点, 月度重训, 4 种子) —— 现有终端
    W21 : 21 日标签 LightGBM(**同样本周锚点面板**, 只把标签换成一个月) —— 新
    M   : 生产 `model_score`(月度截面、21 日标签、生产训练器) —— 现成, 前向填充到周锚点

注意 W5 与 M 都可能"看起来像", 但它们的**训练样本频率、标签窗口、特征时点**都不同
(W5: 每周截面×5日标签; M: 每月截面×21日标签), 所以是真异构候选。

## 预注册判据

    基准臂 = W5(现终端)在 M5 构型(100 万/top100/出带8×/滞回0.2)下的全史超额。
    Q1 任一集成臂(W5+W21 / W5+M / 三者)的全史超额 ≥ 基准 +1.0pp?
    Q2 且其回撤不劣于基准 10% 以内?
    Q3 且其打分 IC(全史)不低于 W5?

判读:三条同时满足 ⇒ 采纳该集成进 `app/weekly/scores.py` 并重跑终端;
      否则记录读数、维持单 die(并说明"多周期集成在本数据上无增益")。

用法:
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F14_multi_horizon_ensemble.py
"""
import hashlib
import importlib.util
import json
import logging
import sqlite3
import sys
import time
import argparse
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
from app import db                           # noqa: E402
from app.factors import factor_engine as fe  # noqa: E402
from app.pit.engine import PITEngine         # noqa: E402
from app.weekly import scores as wsc         # noqa: E402

log = logging.getLogger("f14")
OUT = HERE / "F14_multi_horizon_ensemble_result.json"
CAPITAL = 1_000_000.0
SEEDS = (42, 7, 123, 2024)


def rank_df(S: np.ndarray, index, columns) -> pd.DataFrame:
    return pd.DataFrame(S, index=index, columns=columns)


def mean_rank(*dfs) -> pd.DataFrame:
    """分位排名平均(各 die 先各自做逐行分位, 再平均) —— 与生产"分位排名均值"同口径。"""
    acc, n = None, 0
    for d in dfs:
        r = d.rank(axis=1, pct=True)
        acc = r if acc is None else acc + r
        n += 1
    return acc / n


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    ap = argparse.ArgumentParser()
    ap.add_argument("--scores", default=str(HERE / "F4_scores_seedA.npz"),
                    help="W5 打分存档(默认 seedA)")
    ap.add_argument("--seeds", default="42,7,123,2024",
                    help="本次训练 W21 用的种子(确认检验时换成另一组)")
    ap.add_argument("--out", default=None, help="结果文件名")
    ap.add_argument("--skip-w21", action="store_true",
                    help="不训练 W21(只对比 W5 / M / W5+M) —— 用于快速复算已采纳构型")
    a = ap.parse_args()
    seeds = tuple(int(x) for x in a.seeds.split(",") if x.strip())
    out_path = Path(a.out) if a.out else OUT
    cfg = cfg_mod.load_config()
    m = fe.load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    pit = PITEngine(m=m, cfg=cfg)
    cols = list(m["close"].columns)
    mask = f1.tradable_frame(pit, wk, cols)
    log.info("周锚点 %d | 票池均值 %.0f", len(wk), mask.sum(axis=1).mean())

    # ---- die 1: W5(复用已训练存档) ----
    z5 = np.load(a.scores, allow_pickle=True)
    a5 = pd.to_datetime([str(d) for d in z5["anchors"]])
    c5 = [str(c) for c in z5["cols"]]
    W5 = pd.DataFrame(z5["scores"], index=a5, columns=c5).reindex(index=wk, columns=cols)
    log.info("W5 载入 %s", W5.shape)

    panel = wsc.build_panel(m, wk, cfg)
    # ---- die 2: W21(本次训练; --skip-w21 时不训练, 只对比已采纳的 W5+M) ----
    if a.skip_w21:
        W21 = None
        log.info("--skip-w21: 跳过 W21 训练")
    else:
        t1 = time.time()
        S21 = wsc.train(panel, cfg, seeds=seeds, horizon=21,
                        progress=lambda i, n, s: log.info("  W21 已训练 %d/%d (%.0fs)", i, n, s))
        W21 = rank_df(S21, wk, cols)
        log.info("W21 训练完成 %.0fs", time.time() - t1)

    # ---- die 3: 生产月频 model_score, 前向填充到周锚点(只用 ≤ 锚点的最近一期) ----
    con = sqlite3.connect(f"file:{BASE/'data'/'neweco.db'}?mode=ro", uri=True)
    ms = pd.read_sql("SELECT ts_code, trade_date, score FROM model_score", con)
    con.close()
    ms["trade_date"] = pd.to_datetime(ms["trade_date"])
    piv = ms.pivot_table(index="trade_date", columns="ts_code", values="score").sort_index()
    M = piv.reindex(index=piv.index.union(wk)).ffill().reindex(index=wk, columns=cols)
    log.info("生产 model_score 对齐到周锚点 %s (覆盖 %.0f%%)",
             M.shape, 100 * M.notna().mean().mean())

    # ---- 集成臂 ----
    arms = {"W5": W5, "M": M, "W5+M": mean_rank(W5, M)}
    if W21 is not None:
        arms.update({"W21": W21, "W5+W21": mean_rank(W5, W21),
                     "W5+W21+M": mean_rank(W5, W21, M)})
        arms = {k: arms[k] for k in ("W5", "W21", "M", "W5+W21", "W5+M", "W5+W21+M")}
    ic = {}
    lab = f1b.forward_excess_open(m, mask, 5)
    Yr = f1._rank(lab)
    okv = lab.notna().to_numpy()
    for tag, Sdf in arms.items():
        Xr = f1._rank(Sdf.reindex(index=wk, columns=cols))
        s = pd.Series(f1.row_ic(Xr, Yr, okv & np.isfinite(Xr)), index=wk).dropna()
        ic[tag] = {"n": int(len(s)), "ic": round(float(s.mean()), 4),
                   "t": round(f1b.hac_t(s.to_numpy(), 2), 2),
                   "DEAD": f1b.zone_stat_hac(s, "DEAD", 2),
                   "ACTIVE": f1b.zone_stat_hac(s, "ACTIVE", 2)}
        log.info("[%s] 打分 IC 全史 %+.4f (t=%.2f) | 死区 %s | 活跃区 %s", tag,
                 ic[tag]["ic"], ic[tag]["t"], ic[tag]["DEAD"], ic[tag]["ACTIVE"])

    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    mask_np = mask.to_numpy()
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "capital": CAPITAL, "ic": ic, "arms": {}, "secs": None}
    series = {}
    for tag, Sdf in arms.items():
        df = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, {**cfg, "capital": CAPITAL},
                                       pit, 0, score_matrix=Sdf, top_n=100, rank_band=8.0,
                                       hyst=0.2)
        if df is None:
            continue
        bench, prev_exec, prev_ap = [], None, None
        for _, r in df.iterrows():
            e = pos_of[r["date"]]
            if prev_exec is None or prev_ap is None:
                bench.append(np.nan)
            else:
                good = (mask_np[int(prev_ap)] & np.isfinite(op_adj[prev_exec])
                        & np.isfinite(op_adj[e]) & (op_adj[prev_exec] > 0))
                p0, p1 = op_adj[prev_exec], op_adj[e]
                bench.append(float(np.mean(p1[good] / p0[good] - 1)) if good.sum() >= 200
                             else np.nan)
            prev_exec, prev_ap = e, int(r["anchor_pos"])
        df["bench_ret"] = bench
        series[tag] = df[["date", "ret", "bench_ret", "turnover", "invested_ratio"]].copy()
        okr = df["ret"].notna() & df["bench_ret"].notna()
        af, bf = f1b.ann(df.loc[okr, "ret"], 52.0), f1b.ann(df.loc[okr, "bench_ret"], 52.0)
        nav = (1 + df.loc[okr, "ret"]).cumprod()
        res["arms"][tag] = {
            "n": int(okr.sum()), "ann": round(af, 4), "bench": round(bf, 4),
            "excess_pp": round(100 * (af - bf), 2),
            "turnover": round(float(df.loc[okr, "turnover"].mean()), 3),
            "invested_ratio": round(float(df.loc[okr, "invested_ratio"].mean()), 4),
            "mdd": round(float((nav / nav.cummax() - 1).min()), 4)}
        d = res["arms"][tag]
        log.info("[%s] 年化 %+.2f%% 超额 %+.2fpp | MDD %.1f%% | 换手 %.3f | 投入率 %.1f%%",
                 tag, 100 * d["ann"], d["excess_pp"], 100 * d["mdd"], d["turnover"],
                 100 * d["invested_ratio"])

    # ---- 同窗口重算(2026-09-12 修正): 含 M 的臂只从 2017 起有分, 与前者的**评估窗口不同**,
    #      直接比"超额/回撤"是拿两段不同的历史对比。这里在**所有臂的交集日期**上重算一遍。
    common = None
    for tag, s in series.items():
        ds = set(pd.to_datetime(s.loc[s["ret"].notna() & s["bench_ret"].notna(), "date"]))
        common = ds if common is None else (common & ds)
    common = sorted(common)
    log.info("=== 同窗口重算: %d 期(所有臂共有), %s ~ %s ===",
             len(common), common[0].date() if common else "-", common[-1].date() if common else "-")
    res["common_window"] = {"n": len(common),
                            "start": str(common[0].date()) if common else None,
                            "end": str(common[-1].date()) if common else None, "arms": {}}
    for tag, s in series.items():
        s2 = s.copy()
        s2["date"] = pd.to_datetime(s2["date"])
        s2 = s2[s2["date"].isin(set(common))].dropna(subset=["ret", "bench_ret"])
        if len(s2) < 50:
            continue
        # 注意: 不要把局部变量命名为 `a`/`args` —— 会遮蔽 argparse 的 namespace(实测踩过)
        ann_s = f1b.ann(s2["ret"], 52.0)
        ann_b = f1b.ann(s2["bench_ret"], 52.0)
        nav = (1 + s2["ret"]).cumprod()
        res["common_window"]["arms"][tag] = {
            "n": int(len(s2)), "ann": round(ann_s, 4), "bench": round(ann_b, 4),
            "excess_pp": round(100 * (ann_s - ann_b), 2),
            "mdd": round(float((nav / nav.cummax() - 1).min()), 4),
            "turnover": round(float(s2["turnover"].mean()), 3)}
        d = res["common_window"]["arms"][tag]
        log.info("  [同窗口 %s] 年化 %+.2f%% 基准 %+.2f%% ⇒ 超额 %+.2fpp | MDD %.1f%%",
                 tag, 100 * d["ann"], 100 * d["bench"], d["excess_pp"], 100 * d["mdd"])

    base = res["arms"]["W5"]
    best = None
    for tag in [t for t in ("W21", "M", "W5+W21", "W5+M", "W5+W21+M") if t in res["arms"]]:
        d = res["arms"].get(tag)
        if not d:
            continue
        if (d["excess_pp"] >= base["excess_pp"] + 1.0 and d["mdd"] >= base["mdd"] * 1.10
                and ic[tag]["ic"] >= ic["W5"]["ic"]):
            best = tag
    res["verdict"] = {"baseline_W5_excess_pp": base["excess_pp"], "adopt": best,
                      "note": "采纳条件: 超额 ≥ 基准+1pp 且 回撤不劣 10% 且 IC 不降"}
    res["secs"] = round(time.time() - t0, 1)
    res["seeds"] = list(seeds)
    res["w5_scores"] = Path(a.scores).name
    out_path.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")
    log.info("== 判决: 采纳 = %s (基准 W5 超额 %+.2fpp) | %.1fs → %s ==",
             best or "维持单 die", base["excess_pp"], res["secs"], out_path)


if __name__ == "__main__":
    main()
