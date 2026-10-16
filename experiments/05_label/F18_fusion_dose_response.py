"""F18 · 异构融合的**剂量-反应**检验 —— 按"去相关程度"逐个加 die

## 为什么用剂量-反应而不是"挑最优组合"

F17 台账给出 8 个 KGD 通过、7 个可封装的 die。若直接穷举 2^7 种融合再挑最好的,
那是标准的**多重比较陷阱**(本项目已多次记录: 组合层两轴才能判决)。
故本实验**预先固定加入顺序** = 与在役 die 的**秩相关从低到高**(越不同先加),
逐个累加并报读数:

    A0 = W5 + M(现役)
    A1 = +margin_net_share (ρ=0.047)   ← 最不同
    A2 = +flow             (ρ=0.21)
    A3 = +h10              (ρ=0.44)
    A4 = +head_vt          (ρ=0.64)
    A5 = +snap             (ρ=0.80)
    A6 = +pit              (ρ=0.78)
    A7 = 全部 8 个 KGD 通过者等权

## 预注册判据

    ① 单调性: 超额必须随加入**整体上升**; 出现显著回落(>1pp)即视为"这个 die 不该封"。
    ② 采纳线: 最优臂的超额 ≥ A0 + 0.5pp, 且 MDD 不劣于 A0 的 1.10 倍。
    ③ 融合方式只允许**分位排名平均**(参数最少、最稳健), 不允许加权/门控(已证伪)。
判读: 通过 ⇒ 该组合进 `app/weekly/scores.py` 作第三个 die 组(需另做生产化与测试);
      不通过 ⇒ 记"这批 die 在本口径下无融合增量", 与 F14(单加 M 有增量)并列存档。

用法:
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F18_fusion_dose_response.py
"""
import hashlib
import importlib.util
import json
import logging
import sqlite3
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
f17 = _load("f17", HERE / "F17_die_registry_kgd.py")

from app import config as cfg_mod            # noqa: E402
from app.factors import factor_engine as fe  # noqa: E402
from app.pit.engine import PITEngine         # noqa: E402

log = logging.getLogger("f18")
OUT = HERE / "F18_fusion_dose_response_result.json"
CAPITAL = 1_000_000.0
TOP_N, BAND, HYST = 100, 8.0, 0.2
# 预注册加入顺序 = 与在役的秩相关升序(F17 台账读数)
ORDER = ["margin_net_share", "flow", "h10", "head_vt", "snap", "pit"]


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    pit = PITEngine(m=m, cfg=cfg)
    cols = list(m["close"].columns)
    mask = f1.tradable_frame(pit, wk, cols)

    conn = sqlite3.connect(f"file:{BASE/'data'/'neweco.db'}?mode=ro", uri=True)
    z = np.load(HERE / "F4_scores_seedA.npz", allow_pickle=True)
    a5 = pd.to_datetime([str(d) for d in z["anchors"]])
    c5 = [str(c) for c in z["cols"]]
    base = {"W5": pd.DataFrame(z["scores"], index=a5, columns=c5).reindex(index=wk, columns=cols),
            "M": f17.table_scores(conn, "model_score", wk, cols)}
    dies = {nm: f17.table_scores(conn, f"model_score_die_{nm}", wk, cols) for nm in ORDER}
    conn.close()
    log.info("周锚点 %d | 在役 2 路 + 候选 %d 路", len(wk), len(dies))

    lab = f1b.forward_excess_open(m, mask, 5)
    Yr = f1._rank(lab)
    okv = lab.notna().to_numpy()
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    mask_np = mask.to_numpy()

    def mean_rank(*dfs):
        acc = None
        for d in dfs:
            r = pd.DataFrame(d).rank(axis=1, pct=True)
            acc = r if acc is None else acc + r
        return acc / len(dfs)

    arms, series, ic_stat = {}, {}, {}
    cur = [base["W5"], base["M"]]
    labels = ["A0_W5+M"]
    arms["A0_W5+M"] = mean_rank(*cur)
    for i, nm in enumerate(ORDER, start=1):
        cur = cur + [dies[nm]]
        key = f"A{i}_+{nm}"
        arms[key] = mean_rank(*cur)
        labels.append(key)
    arms["A7_all_pass"] = mean_rank(base["W5"], base["M"], *dies.values())

    for key, Sdf in arms.items():
        Xr = f1._rank(pd.DataFrame(Sdf, index=wk, columns=cols))
        s = pd.Series(f1.row_ic(Xr, Yr, okv & np.isfinite(Xr)), index=wk).dropna()
        ic_stat[key] = {"ic": round(float(s.mean()), 4), "t": round(f1b.hac_t(s.to_numpy(), 2), 2),
                        "DEAD": f1b.zone_stat_hac(s, "DEAD", 2),
                        "ACTIVE": f1b.zone_stat_hac(s, "ACTIVE", 2)}
        df = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, {**cfg, "capital": CAPITAL}, pit, 0,
                                       score_matrix=pd.DataFrame(Sdf, index=wk, columns=cols),
                                       top_n=TOP_N, rank_band=BAND, hyst=HYST)
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
        series[key] = df[["date", "ret", "bench_ret", "turnover"]].copy()

    common = None
    for s in series.values():
        ds = set(pd.to_datetime(s.loc[s["ret"].notna() & s["bench_ret"].notna(), "date"]))
        common = ds if common is None else (common & ds)
    common = sorted(common or [])
    log.info("=== 同窗口 %d 期 (%s ~ %s) ===", len(common),
             common[0].date(), common[-1].date())
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "order": ORDER, "common_window": {"n": len(common), "start": str(common[0].date()),
                                             "end": str(common[-1].date())},
           "arms": {}, "ic": ic_stat, "verdict": {}, "secs": None}
    for key in ["A0_W5+M"] + [f"A{i}_+{nm}" for i, nm in enumerate(ORDER, 1)] + ["A7_all_pass"]:
        s = series.get(key)
        if s is None:
            continue
        s2 = s.copy()
        s2["date"] = pd.to_datetime(s2["date"])
        s2 = s2[s2["date"].isin(set(common))].dropna(subset=["ret", "bench_ret"])
        ann_s, ann_b = f1b.ann(s2["ret"], 52.0), f1b.ann(s2["bench_ret"], 52.0)
        nav = (1 + s2["ret"]).cumprod()
        res["arms"][key] = {"n": int(len(s2)), "excess_pp": round(100 * (ann_s - ann_b), 2),
                            "ann": round(ann_s, 4), "mdd": round(float((nav / nav.cummax() - 1).min()), 4),
                            "turnover": round(float(s2["turnover"].mean()), 3),
                            "ic": ic_stat[key]["ic"]}
        d = res["arms"][key]
        log.info("[%s] 超额 %+.2fpp | 年化 %+.1f%% | MDD %.1f%% | 换手 %.3f | IC %+.4f",
                 key, d["excess_pp"], 100 * d["ann"], 100 * d["mdd"], d["turnover"], d["ic"])

    a0 = res["arms"]["A0_W5+M"]
    best = max(res["arms"].items(), key=lambda kv: kv[1]["excess_pp"])
    res["verdict"] = {
        "baseline_excess_pp": a0["excess_pp"],
        "best_arm": best[0], "best_excess_pp": best[1]["excess_pp"],
        "gain_pp": round(best[1]["excess_pp"] - a0["excess_pp"], 2),
        "mdd_ok": bool(best[1]["mdd"] >= a0["mdd"] * 1.10),
        "adopt": bool(best[1]["excess_pp"] >= a0["excess_pp"] + 0.5 and best[1]["mdd"] >= a0["mdd"] * 1.10),
    }
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 判决: 最优臂 %s 超额 %+.2fpp (基线 %+.2fpp, 增益 %+.2fpp, MDD %s) 采纳=%s | %.1fs → %s ==",
             res["verdict"]["best_arm"], res["verdict"]["best_excess_pp"],
             res["verdict"]["baseline_excess_pp"], res["verdict"]["gain_pp"],
             res["verdict"]["mdd_ok"], res["verdict"]["adopt"], res["secs"], OUT)


if __name__ == "__main__":
    main()
