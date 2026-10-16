"""F17 · die 台账(KGD + 去相关)—— 芯片式异构的第一个正经交付

## 为什么先做这个

方案 `plans/abstract-sleeping-rainbow.md` 把"芯片式异构"的前提写得很硬:

    · die 必须按**信息源**划分(不是按算法族) —— 否则只是同一信号做方差缩减;
    · 每个 die 必须**单独**过 KGD(自己就能跑赢基准)才允许被封装;
    · 封装前必须查**两两相关性**: 与已有 die 秩相关 > 0.95 的, 直接退役;
    · "按近期表现加权/复活"已被 R1 实验证伪 ⇒ 只许退役, 不许加权。

库里已经躺着 2026-09-11 那批实验产生的 **13 个 die 打分表**(model_score_die_*)。
此前它们只是"跑过的一次实验"; 本脚本把它们变成**资产台账**:

    对每个 die, 在同一批周锚点上算①覆盖率 ②周频 IC(死区/活跃区) ③独立组合超额(净)
    ④与在役 die 的秩相关 ⑤KGD 判决

## 判据(预注册)

    KGD 通过 = 死区与活跃区 IC **同号** 且 独立组合(100万/top100/带8×/真费用)超额 > 0
    可封装 = KGD 通过 且 与**所有**在役 die 的 |秩相关| < 0.95
    否则 = 退役候选(写进台账, 不改生产)

用法:
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F17_die_registry_kgd.py
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

from app import config as cfg_mod            # noqa: E402
from app.factors import factor_engine as fe  # noqa: E402
from app.pit.engine import PITEngine         # noqa: E402

log = logging.getLogger("f17")
OUT = HERE / "F17_die_registry_kgd_result.json"
CAPITAL = 1_000_000.0
TOP_N, BAND = 100, 8.0

# 在役 die(周频袖套当前默认) = 5 日标签 LightGBM(W5) + 生产月频模型(M)
INCUMBENT = ("W5", "M")


def table_scores(conn, table: str, anchors: pd.DatetimeIndex, cols) -> pd.DataFrame:
    """月频打分表 → 周锚点(**只用 ≤ 锚点的最近一期**, 无前视)。"""
    df = pd.read_sql(f"SELECT ts_code, trade_date, score FROM {table}", conn)
    if df.empty:
        return pd.DataFrame(index=anchors, columns=cols, dtype=float)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    piv = df.pivot_table(index="trade_date", columns="ts_code", values="score").sort_index()
    return piv.reindex(index=piv.index.union(anchors)).ffill().reindex(index=anchors, columns=cols)


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
    log.info("周锚点 %d | 票池均值 %.0f", len(wk), mask.sum(axis=1).mean())

    conn = sqlite3.connect(f"file:{BASE/'data'/'neweco.db'}?mode=ro", uri=True)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'model_score_die_%' "
        "AND name NOT LIKE '%smoke%' AND name NOT LIKE '%run1%' ORDER BY name")]
    log.info("候选 die 表 %d 个: %s", len(tables), tables)

    # 在役两路: W5(存档) + M(生产表)
    z = np.load(HERE / "F4_scores_seedA.npz", allow_pickle=True)
    a5 = pd.to_datetime([str(d) for d in z["anchors"]])
    c5 = [str(c) for c in z["cols"]]
    scores = {"W5": pd.DataFrame(z["scores"], index=a5, columns=c5).reindex(index=wk, columns=cols),
              "M": table_scores(conn, "model_score", wk, cols)}
    for t in tables:
        nm = t.replace("model_score_die_", "")
        scores[nm] = table_scores(conn, t, wk, cols)
    conn.close()
    log.info("载入 %d 路打分", len(scores))

    lab = f1b.forward_excess_open(m, mask, 5)
    Yr = f1._rank(lab)
    okv = lab.notna().to_numpy()
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    mask_np = mask.to_numpy()

    rows, series = {}, {}
    for nm, Sdf in scores.items():
        cov = float(Sdf.notna().to_numpy().mean())
        Xr = f1._rank(Sdf)
        ic = pd.Series(f1.row_ic(Xr, Yr, okv & np.isfinite(Xr)), index=wk).dropna()
        ic_stats = {"n": int(len(ic)), "ic": round(float(ic.mean()), 4) if len(ic) else None,
                    "DEAD": f1b.zone_stat_hac(ic, "DEAD", 2) if len(ic) else None,
                    "ACTIVE": f1b.zone_stat_hac(ic, "ACTIVE", 2) if len(ic) else None}
        df = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, {**cfg, "capital": CAPITAL},
                                       pit, 0, score_matrix=pd.DataFrame(Sdf, index=wk, columns=cols),
                                       top_n=TOP_N, rank_band=BAND, hyst=0.2)
        if df is not None:
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
            series[nm] = df[["date", "ret", "bench_ret"]].copy()
        rows[nm] = {"coverage": round(cov, 3), "ic": ic_stats, "n_periods": int(len(df)) if df is not None else 0}
        d = rows[nm]
        log.info("[%s] 覆盖 %.0f%% | IC %s (死区 %s / 活跃 %s) | 期数 %d",
                 nm, 100 * cov, d["ic"]["ic"],
                 (d["ic"]["DEAD"] or {}).get("ic"), (d["ic"]["ACTIVE"] or {}).get("ic"),
                 d["n_periods"])

    # ---- 同窗口(所有 die 共有)上算组合超额 + KGD 判决 ----
    common = None
    for nm, s in series.items():
        if len(s) < 50:
            continue
        ds = set(pd.to_datetime(s.loc[s["ret"].notna() & s["bench_ret"].notna(), "date"]))
        common = ds if common is None else (common & ds)
    common = sorted(common or [])
    log.info("=== 同窗口 %d 期 (%s ~ %s) ===", len(common),
             common[0].date() if common else "-", common[-1].date() if common else "-")
    for nm, s in series.items():
        s2 = s.copy()
        s2["date"] = pd.to_datetime(s2["date"])
        s2 = s2[s2["date"].isin(set(common))].dropna(subset=["ret", "bench_ret"])
        if len(s2) < 50:
            rows[nm]["skipped"] = "同窗口期数不足"
            continue
        ann_s, ann_b = f1b.ann(s2["ret"], 52.0), f1b.ann(s2["bench_ret"], 52.0)
        nav = (1 + s2["ret"]).cumprod()
        rows[nm].update({"excess_pp": round(100 * (ann_s - ann_b), 2),
                         "ann": round(ann_s, 4), "mdd": round(float((nav / nav.cummax() - 1).min()), 4)})

    # ---- 两两秩相关(锚点×股票的全体格子; 只比较"都有分"的格子) ----
    names = [n for n in scores if rows.get(n, {}).get("n_periods", 0) > 0]
    corr = {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            A, B = scores[a], scores[b]
            both = A.notna() & B.notna()
            if int(both.to_numpy().sum()) < 5000:
                continue
            ra = A.where(both).rank(axis=1, pct=True)
            rb = B.where(both).rank(axis=1, pct=True)
            v = float(np.nanmean([ra.iloc[j].corr(rb.iloc[j]) for j in range(len(ra))]))
            corr[f"{a}|{b}"] = round(v, 3)
    for nm in names:
        inc = [abs(corr[k]) for k in corr
               if nm in k.split("|") and any(x in k.split("|") for x in INCUMBENT)]
        rows[nm]["max_abs_corr_with_incumbent"] = round(max(inc), 3) if inc else None

    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "incumbent": list(INCUMBENT), "n_dies": len(names),
           "common_window": {"n": len(common),
                             "start": str(common[0].date()) if common else None,
                             "end": str(common[-1].date()) if common else None},
           "dies": rows, "pairwise_corr": corr, "verdict": {}, "secs": None}
    kgd, fusable = [], []
    for nm, d in rows.items():
        z1, z2 = d.get("ic", {}).get("DEAD"), d.get("ic", {}).get("ACTIVE")
        if not z1 or not z2 or z1.get("ic") is None or z2.get("ic") is None:
            continue
        same_sign = (z1["ic"] > 0) == (z2["ic"] > 0)
        passed = bool(same_sign and (d.get("excess_pp") or -9) > 0)
        d["kgd_pass"] = passed
        if passed:
            kgd.append(nm)
            mc = d.get("max_abs_corr_with_incumbent")
            if mc is None or mc < 0.95:
                fusable.append(nm)
        log.info("[%s] 同窗口超额 %+.2fpp MDD %.1f%% | 与在役最大|ρ| %s | KGD %s%s",
                 nm, d.get("excess_pp") or 0.0, 100 * (d.get("mdd") or 0.0),
                 d.get("max_abs_corr_with_incumbent"), "PASS" if passed else "FAIL",
                 "  ★可封装" if nm in fusable else "")
    res["verdict"] = {"kgd_pass": sorted(kgd), "fusable_candidates": sorted(fusable),
                      "note": "可封装 = KGD 通过 且 与在役 die |ρ|<0.95; 本脚本只出台账, 不接线生产"}
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 台账完成: KGD 通过 %d 个, 其中可封装 %d 个 | %.1fs → %s ==",
             len(kgd), len(fusable), res["secs"], OUT)


if __name__ == "__main__":
    main()
