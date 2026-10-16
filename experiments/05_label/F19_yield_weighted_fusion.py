"""F19 · 良率加权融合 vs 等权融合 vs 现役 —— 芯片式异构的最后一环

## F18 暴露的问题

等权排名平均对 die 数量**不是尺度无关的**: 现役 W5+M 两路 = 各占 1/2, 加入 6 个候选 die 后
W5 只剩 1/8 权重 ⇒ 超额从 +12.02pp 掉到 +3.27~6.31pp。**那是稀释, 不是分散**。
而 M 之所以有效(F14/F15), 是因为它**自己就有 +4.14pp** 且与 W5 互补。

## 本实验: 按"良率"定权(芯片术语 = 良率分级), 而不是按近期表现追涨

    · 权重 ∝ 各 die **在过去 104 周**的独立 IC(只用过去 ⇒ 无前视), 负值截断为 0;
    · 每 26 周重估一次权重(避免每周抖动); 单 die 权重上限 50%(防单点支配);
    · 对照臂: (a) 现役 W5+M (b) 全 die 等权 (c) 良率加权 (d) 良率加权+只保留权重>0 的 die
    · **禁止**按近期"组合表现"加权(已被 R1 证伪: 那是追涨杀跌); 这里用的是 **IC**(信号质量),
      两者不同 —— 但本实验仍把它当待证伪对象, 不预设它有效。

## 预注册判据

    ① 良率加权的同窗口超额 ≥ 现役 + 0.5pp;
    ② 其 MDD 不劣于现役的 1.10 倍;
    ③ 且必须**优于全 die 等权**(否则增益只是"少稀释", 不是"会定权")。
三条同时满足 ⇒ 采纳为终端第三组 die 的融合方式(需另做生产化+测试);
否则记"良率加权在本 die 集上无增量", 与 F18 合并成完整结论。

用法:
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F19_yield_weighted_fusion.py
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
f17 = _load("f17", HERE / "F17_die_registry_kgd.py")

from app import config as cfg_mod            # noqa: E402
from app.factors import factor_engine as fe  # noqa: E402
from app.pit.engine import PITEngine         # noqa: E402

log = logging.getLogger("f19")
OUT = HERE / "F19_yield_weighted_fusion_result.json"
CAPITAL = 1_000_000.0
TOP_N, BAND, HYST = 100, 8.0, 0.2
TRAIL, REFIT, CAP = 104, 26, 0.5        # 104 周回看 / 26 周重估 / 单 die 权重上限
DIES = ["margin_net_share", "flow", "h10", "head_vt", "snap", "pit"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scores", default=str(HERE / "F4_scores_seedA.npz"),
                    help="W5 打分存档(确认检验时换另一套种子)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
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
    z = np.load(args.scores, allow_pickle=True)
    a5 = pd.to_datetime([str(d) for d in z["anchors"]])
    c5 = [str(c) for c in z["cols"]]
    S = {"W5": pd.DataFrame(z["scores"], index=a5, columns=c5).reindex(index=wk, columns=cols),
         "M": f17.table_scores(conn, "model_score", wk, cols)}
    for nm in DIES:
        S[nm] = f17.table_scores(conn, f"model_score_die_{nm}", wk, cols)
    conn.close()
    log.info("周锚点 %d | 参与融合 %d 路: %s", len(wk), len(S), list(S))

    lab = f1b.forward_excess_open(m, mask, 5)
    Yr = f1._rank(lab)
    okv = lab.notna().to_numpy()
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    mask_np = mask.to_numpy()

    # 逐 die 的周频 IC 时序(用于滚动定权) + 分位排名面板
    R = {}
    for nm, Sdf in S.items():
        Xr = f1._rank(Sdf)
        R[nm] = pd.DataFrame(Xr, index=wk, columns=cols).rank(axis=1, pct=True)
    ics = {}
    for nm, Sdf in S.items():
        Xr = f1._rank(Sdf)
        ics[nm] = pd.Series(f1.row_ic(Xr, Yr, okv & np.isfinite(Xr)), index=wk)
    ic_df = pd.DataFrame(ics)

    def fuse(weights_by_time, require_all: bool = True) -> pd.DataFrame:
        """分位排名加权融合。

        ⚠️ 口径(2026-09-13 修正): **缺失 die 不得当成 0 分**。原实现用 `r.fillna(0)`,
        会让 2015-2016(那时只有 W5 有分)也算"有 8 路融合", 窗口被撑到 606 期,
        与 F14/F15 的 491 期口径不一致 ⇒ 两次结果的超额不可直接比较。
        现改为: 只对**该锚点上真有分的 die**归一化权重; 全缺则整行 NaN(该锚点跳过)。
        """
        out = pd.DataFrame(np.nan, index=wk, columns=cols)
        for j, d in enumerate(wk):
            w = weights_by_time(d)
            acc, tot = None, 0.0
            for nm, ww in w.items():
                if ww <= 0:
                    continue
                r = R[nm].iloc[j]
                if not np.isfinite(r.to_numpy()).any():
                    if require_all:
                        acc, tot = None, 0.0       # 任一在役 die 缺分 ⇒ 该锚点整体不出分
                        break
                    continue                       # 否则: 让它缺席, 权重按可得者归一
                acc = (r * ww) if acc is None else (acc + r * ww)
                tot += ww
            out.iloc[j] = (acc / tot) if (acc is not None and tot > 0) else np.nan
        return out

    def equal_w(_d):
        return {nm: 1.0 for nm in S}

    # 良率权重: 每 REFIT 周重估一次, 用**过去 TRAIL 周**的 IC 均值(负值截断), 上限 50%
    cache = {}
    weight_log = []          # 记录每次重估的权重向量(诊断"到底选了哪几路")

    def yield_w(d):
        key = wk.get_loc(d) // REFIT
        if key in cache:
            return cache[key]
        j = wk.get_loc(d)
        lo = max(0, j - TRAIL)
        past = ic_df.iloc[lo:j]
        raw = {nm: max(0.0, float(past[nm].mean())) if past[nm].notna().sum() >= 20 else 0.0
               for nm in S}
        tot = sum(raw.values())
        w = {nm: (v / tot if tot > 0 else 0.0) for nm, v in raw.items()}
        w = {nm: min(v, CAP) for nm, v in w.items()}
        s = sum(w.values())
        w = {nm: (v / s if s > 0 else 1.0 / len(S)) for nm, v in w.items()}
        cache[key] = w
        weight_log.append({"at_anchor": d.strftime("%Y%m%d"), "weights": {k: round(v, 4) for k, v in w.items()}})
        return w

    arms = {"A_现役W5+M": None, "B_全die等权": fuse(equal_w), "C_良率加权": fuse(yield_w)}
    arms["A_现役W5+M"] = None  # 由 W5/M 两路单独构成, 下面统一算

    def two_die(_d):
        return {"W5": 0.5, "M": 0.5}

    arms["A_现役W5+M"] = fuse(two_die)
    # D: 良率加权但只用 W5+M+权重最高的 2 路(更保守的候选)
    arms["D_良率加权+Top2"] = fuse(lambda d: dict(sorted(
        yield_w(d).items(), key=lambda kv: -kv[1])[:2]))

    series, ic_stat = {}, {}
    for key, Sdf in arms.items():
        Xr = f1._rank(Sdf)
        s = pd.Series(f1.row_ic(Xr, Yr, okv & np.isfinite(Xr)), index=wk).dropna()
        ic_stat[key] = {"ic": round(float(s.mean()), 4)}
        df = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, {**cfg, "capital": CAPITAL}, pit, 0,
                                       score_matrix=Sdf, top_n=TOP_N, rank_band=BAND, hyst=HYST)
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
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "w5_scores": Path(args.scores).name,
           "trail": TRAIL, "refit": REFIT, "cap": CAP, "common_window": len(common),
           "weight_log": weight_log,
           "arms": {}, "verdict": {}, "secs": None}
    for key, s in series.items():
        s2 = s.copy()
        s2["date"] = pd.to_datetime(s2["date"])
        s2 = s2[s2["date"].isin(set(common))].dropna(subset=["ret", "bench_ret"])
        a_, b_ = f1b.ann(s2["ret"], 52.0), f1b.ann(s2["bench_ret"], 52.0)
        nav = (1 + s2["ret"]).cumprod()
        res["arms"][key] = {"n": int(len(s2)), "excess_pp": round(100 * (a_ - b_), 2),
                            "ann": round(a_, 4), "mdd": round(float((nav / nav.cummax() - 1).min()), 4),
                            "turnover": round(float(s2["turnover"].mean()), 3),
                            "ic": ic_stat[key]["ic"]}
        d = res["arms"][key]
        log.info("[%s] 超额 %+.2fpp | 年化 %+.1f%% | MDD %.1f%% | 换手 %.3f | IC %+.4f",
                 key, d["excess_pp"], 100 * d["ann"], 100 * d["mdd"], d["turnover"], d["ic"])

    a0, cy, eq = res["arms"]["A_现役W5+M"], res["arms"]["C_良率加权"], res["arms"]["B_全die等权"]
    res["verdict"] = {
        "baseline_pp": a0["excess_pp"], "yield_pp": cy["excess_pp"], "equal_pp": eq["excess_pp"],
        "beat_baseline": bool(cy["excess_pp"] >= a0["excess_pp"] + 0.5),
        "mdd_ok": bool(cy["mdd"] >= a0["mdd"] * 1.10),
        "beat_equal": bool(cy["excess_pp"] > eq["excess_pp"]),
    }
    res["verdict"]["adopt"] = bool(res["verdict"]["beat_baseline"] and res["verdict"]["mdd_ok"]
                                   and res["verdict"]["beat_equal"])
    res["secs"] = round(time.time() - t0, 1)
    out_path = Path(args.out) if args.out else OUT
    out_path.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 判决: 良率加权 %+.2fpp vs 现役 %+.2fpp vs 等权 %+.2fpp ⇒ 采纳=%s | %.1fs → %s ==",
             cy["excess_pp"], a0["excess_pp"], eq["excess_pp"], res["verdict"]["adopt"],
             res["secs"], out_path)


if __name__ == "__main__":
    main()
