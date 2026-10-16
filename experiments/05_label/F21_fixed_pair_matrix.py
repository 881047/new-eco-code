"""F21 · 固定配对矩阵 —— 解决「head_vt 到底有没有用」的矛盾（预注册）

## 矛盾的来源

· 2026-09-11 的 `exp_die_stage_b.py` 记录: **head_vt 组合层两窗两轴全否**(年化 −5.81pp / −3.95pp)
  —— 那是**月频 top6** 构造。
· 2026-09-13 F17/F19 在**周频 top100 + 出带 8×** 构造下: head_vt 独立 +4.58pp,
  且动态选择的 D 臂(W5 + 良率 Top2, 16/26 次选中 head_vt) +13.78/+13.11pp 胜现役。

两者都是"组合层"，但构造不同(月频 top6 vs 周频 top100)，所以不必然矛盾 —— 必须实测。
更要紧的是: D 臂的增益可能来自**动态选择的时间运气**, 而不是 head_vt 本身。

## 本实验(去掉动态选择, 只测**固定配对**)

    基准 = W5+M(现役, 各 1/2 分位排名平均)
    对照 = W5 + 每一个候选 die(各 1/2), 以及 W5 单独(1/1)
    两套种子(seedA/seedB)各跑一遍 **同一批臂**。

## 预注册判据

    **采纳某个配对** 需同时满足:
      ① 两套种子上都 ≥ 基准 + 0.5pp;
      ② 两套种子上 MDD 都不劣于基准的 1.10 倍;
      ③ IC 不低于基准。
    若全部不满足 ⇒ **维持 W5+M**, 并把 D 臂的增益记为"动态选择带来的、不可复现为固定配对",
    不再为 die zoo 生产化投入(那条路的收益无法用固定规则兑现)。

用法:
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F21_fixed_pair_matrix.py
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

log = logging.getLogger("f21")
OUT = HERE / "F21_fixed_pair_matrix_result.json"
CAPITAL = 1_000_000.0
TOP_N, BAND, HYST = 100, 8.0, 0.2
PARTNERS = ["M", "head_vt", "snap", "pit", "h10", "margin_net_share", "flow"]
SEED_FILES = {"seedA": "F4_scores_seedA.npz", "seedB": "F4_scores_seedB.npz"}


def mean_rank(*dfs):
    acc = None
    for d in dfs:
        r = pd.DataFrame(d).rank(axis=1, pct=True)
        acc = r if acc is None else acc + r
    return acc / len(dfs)


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
    P = {nm: f17.table_scores(conn, f"model_score_die_{nm}", wk, cols)
         for nm in PARTNERS if nm != "M"}
    P["M"] = f17.table_scores(conn, "model_score", wk, cols)
    conn.close()
    log.info("候选搭档 %d 路载入完成", len(P))

    lab = f1b.forward_excess_open(m, mask, 5)
    Yr = f1._rank(lab)
    okv = lab.notna().to_numpy()
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    mask_np = mask.to_numpy()

    def evaluate(Sdf):
        Xr = f1._rank(Sdf)
        s = pd.Series(f1.row_ic(Xr, Yr, okv & np.isfinite(Xr)), index=wk).dropna()
        ic = round(float(s.mean()), 4) if len(s) else None
        df = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, {**cfg, "capital": CAPITAL}, pit, 0,
                                       score_matrix=pd.DataFrame(Sdf, index=wk, columns=cols),
                                       top_n=TOP_N, rank_band=BAND, hyst=HYST)
        if df is None:
            return None, None, None, ic
        bench, prev_exec, prev_ap = [], None, None
        for _, r in df.iterrows():
            e = pos_of[r["date"]]
            if prev_exec is None or prev_ap is None:
                bench.append(np.nan)
            else:
                good = (mask_np[int(prev_ap)] & np.isfinite(op_adj[prev_exec])
                        & np.isfinite(op_adj[e]) & (op_adj[prev_exec] > 0))
                p0, p1 = op_adj[prev_exec], op_adj[e]
                bench.append(float(np.mean(p1[good] / p0[good] - 1)) if good.sum() >= 200 else np.nan)
            prev_exec, prev_ap = e, int(r["anchor_pos"])
        df["bench_ret"] = bench
        return df[["date", "ret", "bench_ret", "turnover"]], df, None, ic

    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "partners": PARTNERS, "seeds": {}, "verdict": {}, "secs": None}
    for seed, fn in SEED_FILES.items():
        z = np.load(HERE / fn, allow_pickle=True)
        a5 = pd.to_datetime([str(d) for d in z["anchors"]])
        c5 = [str(c) for c in z["cols"]]
        W5 = pd.DataFrame(z["scores"], index=a5, columns=c5).reindex(index=wk, columns=cols)
        arms = {"W5_only": mean_rank(W5)}
        for nm in PARTNERS:
            arms[f"W5+{nm}"] = mean_rank(W5, P[nm])
        series = {}
        for key, Sdf in arms.items():
            s, _df, _x, ic = evaluate(Sdf)
            if s is not None:
                series[key] = (s, ic)
        common = None
        for s, _ in series.values():
            ds = set(pd.to_datetime(s.loc[s["ret"].notna() & s["bench_ret"].notna(), "date"]))
            common = ds if common is None else (common & ds)
        common = sorted(common or [])
        rows = {}
        for key, (s, ic) in series.items():
            s2 = s.copy()
            s2["date"] = pd.to_datetime(s2["date"])
            s2 = s2[s2["date"].isin(set(common))].dropna(subset=["ret", "bench_ret"])
            a_, b_ = f1b.ann(s2["ret"], 52.0), f1b.ann(s2["bench_ret"], 52.0)
            nav = (1 + s2["ret"]).cumprod()
            rows[key] = {"n": int(len(s2)), "excess_pp": round(100 * (a_ - b_), 2),
                         "ann": round(a_, 4), "mdd": round(float((nav / nav.cummax() - 1).min()), 4),
                         "turnover": round(float(s2["turnover"].mean()), 3), "ic": ic}
            log.info("[%s|%s] 超额 %+.2fpp | 年化 %+.1f%% | MDD %.1f%% | IC %s",
                     seed, key, rows[key]["excess_pp"], 100 * rows[key]["ann"],
                     100 * rows[key]["mdd"], ic)
        res["seeds"][seed] = {"common_window": len(common), "arms": rows}

    a = res["seeds"]["seedA"]["arms"]
    b = res["seeds"]["seedB"]["arms"]
    adopt = []
    for key in a:
        if key == "W5+M":
            continue
        if key in b:
            if (a[key]["excess_pp"] >= a["W5+M"]["excess_pp"] + 0.5
                    and b[key]["excess_pp"] >= b["W5+M"]["excess_pp"] + 0.5
                    and a[key]["mdd"] >= a["W5+M"]["mdd"] * 1.10
                    and b[key]["mdd"] >= b["W5+M"]["mdd"] * 1.10
                    and (a[key]["ic"] or 0) >= (a["W5+M"]["ic"] or 0)):
                adopt.append(key)
    res["verdict"] = {"baseline": "W5+M",
                      "baseline_excess": {"seedA": a["W5+M"]["excess_pp"], "seedB": b["W5+M"]["excess_pp"]},
                      "adopt": adopt,
                      "note": "两套种子同时 ≥ 基准+0.5pp 且 MDD 不劣 10% 且 IC 不降, 才采纳"}
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 判决: 可采纳配对 = %s | 基准 W5+M = %+.2f/%+.2fpp | %.1fs → %s ==",
             adopt or "无(维持 W5+M)", a["W5+M"]["excess_pp"], b["W5+M"]["excess_pp"],
             res["secs"], OUT)


if __name__ == "__main__":
    main()
