"""F16 · 未接线的新数据因子:从"单因子"改测"模型特征"—— 预注册

## 为什么还要测这一条

F3 测过这 17 个因子（moneyflow 资金流 / margin 融资融券 / block_trade 大宗 / 股东户数）的
**单因子周频 IC**：50 个被测因子里只有 4 个基础因子通过预注册判据，新数据 **0 个**。
但 F3 的结论只覆盖"单因子排序"这一种用法，它自己在报告里写明：

> 也未测试它们与 LightGBM 非线性组合后的增量。

LightGBM 可以只用某个特征在**特定区间/特定条件下**（树的分裂）取信息 ——
单因子 IC≈0 与"对树模型无用"是两件事。本实验就测这一条。

## 设计（唯一变量 = 特征集）

    基础臂 : 31 个生产因子(量价/财务/龙虎榜) → 5 日标签 LightGBM(4 种子, 月度重训, 禁运 3)
    扩展臂 : 同样 31 个 + 17 个新数据因子 = 48 个特征, **其余逐字相同**

基准臂直接用已训练存档 `F4_scores_seedA.npz`（同种子同参数），只训扩展臂 ⇒ 省一半算力。
两臂都在**同窗口 491 期(2017-02~2026-09)**上评估，避免 F14 踩过的"窗口不同"陷阱。

## 预注册判据

    Q1 扩展臂的打分 IC(全史/同窗口) 不低于基础臂 −0.002;
    Q2 扩展臂的**组合超额**(W5+M 构型, 同窗口) ≥ 基础臂 +0.5pp;
    Q3 回撤不劣化超过 10%。
三条同时满足 ⇒ 把新数据因子并入 `app/weekly/scores.build_panel`(需另做生产化与测试);
否则记入"新数据的第二种用法也证伪"，与 F3 合并成完整结论。

用法:
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F16_newfactors_as_features.py
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
from app import db                           # noqa: E402
from app.factors import factor_engine as fe  # noqa: E402
from app.factors import flow_factors, holder_factors  # noqa: E402
from app.pit.engine import PITEngine         # noqa: E402
from app.weekly import scores as wsc         # noqa: E402

log = logging.getLogger("f16")
OUT = HERE / "F16_newfactors_as_features_result.json"
CAPITAL = 1_000_000.0


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

    # ---- 基础面板(31 因子) ----
    panel = wsc.build_panel(m, wk, cfg)
    log.info("基础面板 %s", panel["F"].shape)

    # ---- 新数据因子(17 个), 在同一锚点上中性化后拼接 ----
    conn = db.get_conn()
    try:
        ind = fe.industry_map(conn)
    finally:
        conn.close()
    t1 = time.time()
    new = dict(flow_factors.compute_flow_factors(m, dates, include_research=False))
    hold = dict(holder_factors.compute_holder_factors_safe(dates))
    log.info("新数据因子 %d 个 (%.0fs)", len(new) + len(hold), time.time() - t1)
    arrs, names = [panel["F"]], list(panel["names"])
    for nm, df in {**new, **hold}.items():
        sub = fe.neutralize(df.reindex(index=wk, columns=cols), ind)
        arrs.append(sub.to_numpy(dtype=np.float32)[:, :, None])
        names.append(nm)
    Fext = np.concatenate(arrs, axis=2)
    log.info("扩展面板 %s (新增 %d 个特征)", Fext.shape, Fext.shape[2] - panel["F"].shape[2])

    # ---- 只训扩展臂(基础臂复用存档) ----
    ext = dict(panel)
    ext["F"], ext["names"] = Fext, names
    S_ext = wsc.train(ext, cfg, seeds=wsc.SEEDS, horizon=5,
                      progress=lambda i, n, s: log.info("  扩展臂已训练 %d/%d (%.0fs)", i, n, s))
    Wext = pd.DataFrame(S_ext, index=wk, columns=cols)

    z = np.load(HERE / "F4_scores_seedA.npz", allow_pickle=True)
    a5 = pd.to_datetime([str(d) for d in z["anchors"]])
    c5 = [str(c) for c in z["cols"]]
    Wbase = pd.DataFrame(z["scores"], index=a5, columns=c5).reindex(index=wk, columns=cols)

    # ---- 生产月频模型(第三路 die, 与 F14 同口径) ----
    M = wsc.production_score_on(wk, cols)

    lab = f1b.forward_excess_open(m, mask, 5)
    Yr = f1._rank(lab)
    okv = lab.notna().to_numpy()
    ic = {}
    for tag, Sdf in (("基础31", Wbase), ("扩展48", Wext)):
        Xr = f1._rank(Sdf)
        s = pd.Series(f1.row_ic(Xr, Yr, okv & np.isfinite(Xr)), index=wk).dropna()
        ic[tag] = {"n": int(len(s)), "ic": round(float(s.mean()), 4),
                   "t": round(f1b.hac_t(s.to_numpy(), 2), 2),
                   "DEAD": f1b.zone_stat_hac(s, "DEAD", 2),
                   "ACTIVE": f1b.zone_stat_hac(s, "ACTIVE", 2)}
        log.info("[%s] IC %+.4f (t=%.2f) | 死区 %s | 活跃区 %s", tag, ic[tag]["ic"],
                 ic[tag]["t"], ic[tag]["DEAD"], ic[tag]["ACTIVE"])

    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    mask_np = mask.to_numpy()
    arms = {"基础31": Wbase, "扩展48": Wext,
            "基础31+M": wsc._mean_rank(Wbase, M), "扩展48+M": wsc._mean_rank(Wext, M)}
    series = {}
    for tag, Sdf in arms.items():
        df = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, {**cfg, "capital": CAPITAL},
                                       pit, 0, score_matrix=pd.DataFrame(
                                           Sdf, index=wk, columns=cols),
                                       top_n=100, rank_band=8.0, hyst=0.2)
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
        series[tag] = df[["date", "ret", "bench_ret"]].copy()

    # 同窗口(所有臂共有的日期) —— F14 踩过的坑, 这里直接内置
    common = None
    for tag, s in series.items():
        ds = set(pd.to_datetime(s.loc[s["ret"].notna() & s["bench_ret"].notna(), "date"]))
        common = ds if common is None else (common & ds)
    common = sorted(common)
    log.info("=== 同窗口重算: %d 期 (%s ~ %s) ===", len(common),
             common[0].date(), common[-1].date())
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "n_features_base": int(panel["F"].shape[2]), "n_features_ext": int(Fext.shape[2]),
           "common_window": {"n": len(common), "start": str(common[0].date()),
                             "end": str(common[-1].date())}, "ic": ic, "arms": {}, "verdict": {}}
    for tag, s in series.items():
        s2 = s.copy()
        s2["date"] = pd.to_datetime(s2["date"])
        s2 = s2[s2["date"].isin(set(common))].dropna(subset=["ret", "bench_ret"])
        ann_s, ann_b = f1b.ann(s2["ret"], 52.0), f1b.ann(s2["bench_ret"], 52.0)
        nav = (1 + s2["ret"]).cumprod()
        res["arms"][tag] = {"n": int(len(s2)), "ann": round(ann_s, 4),
                            "bench": round(ann_b, 4),
                            "excess_pp": round(100 * (ann_s - ann_b), 2),
                            "mdd": round(float((nav / nav.cummax() - 1).min()), 4)}
        d = res["arms"][tag]
        log.info("  [%s] 年化 %+.2f%% 超额 %+.2fpp | MDD %.1f%%", tag, 100 * d["ann"],
                 d["excess_pp"], 100 * d["mdd"])

    b0, e0 = res["arms"]["基础31+M"], res["arms"]["扩展48+M"]
    res["verdict"] = {
        "ic_ok": bool(ic["扩展48"]["ic"] >= ic["基础31"]["ic"] - 0.002),
        "excess_gain_pp": round(e0["excess_pp"] - b0["excess_pp"], 2),
        "excess_ok": bool(e0["excess_pp"] >= b0["excess_pp"] + 0.5),
        "mdd_ok": bool(e0["mdd"] >= b0["mdd"] * 1.10),
    }
    res["verdict"]["adopt"] = bool(res["verdict"]["ic_ok"] and res["verdict"]["excess_ok"]
                                   and res["verdict"]["mdd_ok"])
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 判决: 采纳新数据因子 = %s (超额 %+.2fpp, IC %s, MDD %s) | %.1fs → %s ==",
             res["verdict"]["adopt"], res["verdict"]["excess_gain_pp"],
             res["verdict"]["ic_ok"], res["verdict"]["mdd_ok"], res["secs"], OUT)


if __name__ == "__main__":
    main()
