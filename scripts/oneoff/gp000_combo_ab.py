# -*- coding: utf-8 -*-
"""gp_000 组合层判决 A/B —— `plans/gp000-combo-prereg-20260926.md` 的执行体。

机制复制自 `scripts/ab_factor_expansion.py`（Phase 2，2026-09-25，判决"49 因子扩充不采纳"），
唯一差异 = 臂 B 的因子集：**31 + gp_000**（被判决的那一版，`data/alpha_pool/gp_000.npz`，
bank.save_values 存档的中性化+PIT 域面板，覆盖 2019-01 起月末；2017-2018 锚点该列全 NaN ⇒
LightGBM 原生 NaN 处理，早期锚点臂 B 退化为与臂 A 等价——预注册 §六.2 已登记）。

臂 A **直接复用 Phase 2 的 OOS 打分缓存** `data/ab_fe_scores_A.parquet`（同 31 因子/同种子
[42,7]/同标签），运行时断言行情数据未前进（daily 最新交易日 == 20260924）⇒ 缓存有效。

判据（预注册 §四，冻结）：采纳建议 = 全史 ΔIC>0 且 eval ΔIC>0 且 全史 Δtop6 毛>0，三条同时。
PASS=POOLED=资格非可用；过判据也只是"采纳建议"，进生产须正式链路 + 重钉 + 拍板。
只读生产表；产物 experiments/gp000_combo_ab_20260926.json。
"""
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.factors.factor_engine import load_market, month_end_dates, industry_map
from app.models import trainer as T

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "neweco.db"
SEEDS = [42, 7]
TRAIN_MONTHS = 36
EVAL_START = "20190722"
TOPK = 6
GP_NPZ = ROOT / "data" / "alpha_pool" / "gp_000.npz"
CACHE_A = ROOT / "data" / "ab_fe_scores_A.parquet"
OUT = ROOT / "experiments" / "gp000_combo_ab_20260926.json"
DATA_FRESH_ASOF = "20260924"          # Phase 2 缓存 A 建于 2026-09-25，行情此后未前进


def main() -> int:
    t0 = time.time()
    m = load_market()
    md_all = month_end_dates(pd.DatetimeIndex(m["close"].index))

    # 缓存有效性门：行情数据自 Phase 2 缓存生成后不得前进（周末窗口内成立）
    conn = sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)
    daily_max = conn.execute("SELECT MAX(trade_date) FROM daily").fetchone()[0]
    print(f"[缓存门] daily 最新 {daily_max}（要求 == {DATA_FRESH_ASOF}）", flush=True)
    if daily_max != DATA_FRESH_ASOF:
        print("[缓存门] 行情已前进 ⇒ 臂 A 缓存作废，重算臂 A", flush=True)
        force_a = True
    else:
        force_a = False

    wide31 = T._load_factors_wide()
    wide31r = wide31.reorder_levels(["factor", "ts_code"], axis=1).sort_index(axis=1)
    names_a = sorted(wide31r.columns.get_level_values("factor").unique())
    print(f"现役宽表 {wide31.shape}", flush=True)

    # gp_000：被判决的那一版（bank.save_values 的 npz 存档）
    d = np.load(GP_NPZ, allow_pickle=True)
    gp_md = pd.DataFrame(d["values"].astype(float),
                         index=[str(x) for x in d["dates"].tolist()],
                         columns=[str(x) for x in d["codes"].tolist()])
    gp_wide = T._wide_from_factors({"gp_000": gp_md}).sort_index(axis=1)
    wide_b = pd.concat([wide31r, gp_wide], axis=1)
    wide_b = wide_b.loc[:, ~wide_b.columns.duplicated()]
    names_b = sorted(wide_b.columns.get_level_values("factor").unique())
    print(f"臂 B 宽表 {wide_b.shape}（gp_000 覆盖 {gp_md.shape[0]} 个月末）", flush=True)

    # 标签（生产同式：21 日复权收益 − 行业均值）
    ac = m["close"] * m["adj"]
    fut = T.forward_return(ac, T.HORIZON)
    labels_wide = fut.loc[md_all]
    ind = industry_map(sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)) \
        if False else None
    con2 = sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)
    inds = industry_map(con2).reindex(labels_wide.columns)
    con2.close()
    labels_neu = labels_wide - labels_wide.T.groupby(inds, sort=False).transform("mean").T

    def walk_forward(wide: pd.DataFrame, names: list, tag: str) -> pd.DataFrame:
        """→ OOS 打分长表（种子间分位排名均值）。Phase 2 同款逐行复制。"""
        oos = []
        for i in range(len(md_all)):
            dt = md_all[i]
            if dt < pd.Timestamp("20170101"):
                continue
            window = T._train_window(md_all, i, TRAIN_MONTHS)
            window = [w for w in window if w in wide.index]
            if len(window) < 12:
                continue
            try:
                X_tr = T._stack(wide, names, window)
                X_tr = T._drop_all_nan_rows(X_tr, f"{dt.date()} {tag} 训练")
                y_tr = labels_neu.loc[window].stack().reindex(X_tr.index)
                mask = y_tr.notna()
                X_tr, y_tr = X_tr.loc[mask], y_tr.loc[mask]
                X_i = T._stack(wide, names, [dt])
                X_i = T._drop_all_nan_rows(X_i, f"{dt.date()} {tag} 推分")
            except KeyError:
                continue
            if len(X_i) < 100:
                continue
            per_seed = []
            for seed in SEEDS:
                p = dict(T.PARAMS)
                p["seed"] = seed
                model = lgb.train(p, lgb.Dataset(X_tr, y_tr),
                                  num_boost_round=T.ENSEMBLE_ROUNDS)
                per_seed.append(pd.Series(model.predict(X_i), index=X_i.index))
            ens = pd.concat(per_seed, axis=1).rank(pct=True).mean(axis=1)
            codes = ens.index.get_level_values(-1).astype(str)
            oos.append(pd.DataFrame({"anchor": dt.strftime("%Y%m%d"),
                                     "ts_code": codes,
                                     "score": ens.to_numpy(float)}))
        return pd.concat(oos, ignore_index=True)

    # 臂 A：缓存复用（同 Phase 2 配置）
    if CACHE_A.exists() and not force_a:
        score_a = pd.read_parquet(CACHE_A)
        print(f"[臂 A] 缓存命中 {len(score_a):,} 行", flush=True)
    else:
        print("== 臂 A（31 基线）walk-forward ==", flush=True)
        score_a = walk_forward(wide31r, names_a, "A")
        score_a.to_parquet(CACHE_A)

    print("== 臂 B（31+gp_000）walk-forward ==", flush=True)
    score_b = walk_forward(wide_b, names_b, "B")
    print(f"OOS 打分：A {len(score_a):,} 行 | B {len(score_b):,} 行", flush=True)

    # 评估（Phase 2 同款：winsorize + 逐月 IC + top6 毛 + 换手）
    def evaluate(score: pd.DataFrame) -> pd.DataFrame:
        y_neu = labels_neu.stack().rename("y_neu").reset_index()
        y_neu.columns = ["anchor", "ts_code", "y_neu"]
        y_neu["anchor"] = y_neu["anchor"].dt.strftime("%Y%m%d")
        y_raw = fut.loc[md_all].stack().rename("y_raw").reset_index()
        y_raw.columns = ["anchor", "ts_code", "y_raw"]
        y_raw["anchor"] = y_raw["anchor"].dt.strftime("%Y%m%d")
        s = score.merge(y_neu, on=["anchor", "ts_code"], how="left") \
                 .merge(y_raw, on=["anchor", "ts_code"], how="left")

        def _w(g):
            g = g.copy()
            lo, hi = g["y_raw"].quantile([0.01, 0.99])
            g["y_raw_w"] = g["y_raw"].clip(lo, hi)
            return g
        s = pd.concat([_w(g) for _, g in s.groupby("anchor", sort=False)],
                      ignore_index=True)
        return s.dropna(subset=["score"])

    def metrics(s: pd.DataFrame, start: str) -> dict:
        s2 = s.loc[s["anchor"] >= start]
        ics, tops, tos = {}, {}, []
        for a_, g in s2.groupby("anchor", sort=False):
            ics[a_] = float(g["score"].rank().corr(g["y_neu"].rank()))
            top = g.nlargest(TOPK, "score")
            v = top["y_raw_w"].dropna()
            if len(v) >= TOPK - 2:
                tops[a_] = float(v.mean())
            tos.append((a_, set(top["ts_code"])))
        ic = pd.Series(ics).sort_index()
        tovr = [len(tos[i][1] & tos[i - 1][1]) / TOPK for i in range(1, len(tos))]
        vals = list(tops.values())
        gross = float(np.nanmean(vals)) * 1e4
        se = float(np.nanstd(vals, ddof=1) / np.sqrt(len(vals))) * 1e4
        return {"n_months": int(len(ic)), "ic": round(float(ic.mean()), 4),
                "ic_t": round(float(ic.mean() / ic.std(ddof=1)), 2),
                "top6_gross_bp": round(gross, 2),
                "top6_gross_t": round(gross / se, 2),
                "turnover": round(1 - float(np.mean(tovr)), 3),
                "ic_series": {k: round(v, 4) for k, v in ic.items()}}

    ev_a, ev_b = evaluate(score_a), evaluate(score_b)
    res = {"A31_full": metrics(ev_a, "20170101"),
           "B32_full": metrics(ev_b, "20170101"),
           "A31_eval": metrics(ev_a.loc[ev_a["anchor"] >= EVAL_START], EVAL_START),
           "B32_eval": metrics(ev_b.loc[ev_b["anchor"] >= EVAL_START], EVAL_START)}
    for tag in ("full", "eval"):
        print(f"\n[{tag}] A31: IC {res[f'A31_{tag}']['ic']:+.4f} (t {res[f'A31_{tag}']['ic_t']:+.2f}) "
              f"| top6 毛 {res[f'A31_{tag}']['top6_gross_bp']:+.1f}bp", flush=True)
        print(f"[{tag}] B32: IC {res[f'B32_{tag}']['ic']:+.4f} (t {res[f'B32_{tag}']['ic_t']:+.2f}) "
              f"| top6 毛 {res[f'B32_{tag}']['top6_gross_bp']:+.1f}bp", flush=True)

    def paired(key_a, key_b):
        ia, ib = res[key_a]["ic_series"], res[key_b]["ic_series"]
        common = sorted(set(ia) & set(ib))
        diff = pd.Series([ib[k] - ia[k] for k in common], index=common)
        t_ = diff.mean() / diff.std(ddof=1) * np.sqrt(len(diff)) if diff.std(ddof=1) else np.nan
        return {"mean": round(float(diff.mean()), 4),
                "t": round(float(t_), 2) if t_ == t_ else None, "n": int(len(diff))}

    res["paired_ic_diff_full"] = paired("A31_full", "B32_full")
    res["paired_ic_diff_eval"] = paired("A31_eval", "B32_eval")

    # 判据（预注册 §四：三条同时）
    d_ic_full = round(res["B32_full"]["ic"] - res["A31_full"]["ic"], 4)
    d_ic_eval = round(res["B32_eval"]["ic"] - res["A31_eval"]["ic"], 4)
    d_top6 = round(res["B32_full"]["top6_gross_bp"] - res["A31_full"]["top6_gross_bp"], 2)
    ok = d_ic_full > 0 and d_ic_eval > 0 and d_top6 > 0
    verdict = {"delta_ic_full": d_ic_full, "delta_ic_eval": d_ic_eval,
               "delta_top6_gross_bp_full": d_top6,
               "verdict": ("过判据（采纳建议）" if ok
                           else "按冻结判据不采纳（与 Phase 2 B80 同款结清语义）")}
    print(f"\n判决: {json.dumps(verdict, ensure_ascii=False)}", flush=True)

    # 诊断：gp_000 与 31 因子的逐月截面 ρ（"增量 vs size"的因子层证据）
    diag = {}
    common_months = sorted(set(gp_md.index.astype(str)) &
                           {d.strftime("%Y%m%d") for d in md_all})
    gp_idx = pd.to_datetime(pd.Index([str(x) for x in d["dates"].tolist()])).strftime("%Y%m%d")
    gp_r = pd.DataFrame(d["values"].astype(float), index=gp_idx,
                        columns=[str(x) for x in d["codes"].tolist()]).rank(axis=1)
    for f in names_a:
        sub = wide31r.xs(f, axis=1, level="factor")
        sub.index = pd.to_datetime(sub.index).strftime("%Y%m%d")
        both = gp_r.reindex(sub.index).dropna(how="all")
        if both.empty:
            diag[f] = {"rho_mean": None, "rho_abs_mean": None}
            continue
        rho = both.corrwith(sub.loc[both.index].rank(axis=1), axis=1)
        diag[f] = {"rho_mean": round(float(rho.mean()), 3),
                   "rho_abs_mean": round(float(rho.abs().mean()), 3)}
    top = sorted(diag.items(), key=lambda kv: -kv[1]["rho_abs_mean"])[:5]
    diag["_top5_abs"] = top
    print(f"[诊断] gp_000 |ρ| 最高: {[(k, v['rho_abs_mean']) for k, v in top]}", flush=True)

    OUT.write_text(json.dumps({
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "prereg": "plans/gp000-combo-prereg-20260926.md",
        "note": "gp_000 组合层判决 A/B（研究层零写库；机制复制自 Phase 2 ab_factor_expansion；"
                "臂 A 复用其 OOS 缓存，臂 B = 31+gp_000(npz 判决版)。",
        "cache_gate": {"daily_max": daily_max, "required": DATA_FRESH_ASOF,
                       "cache_reused": bool(CACHE_A.exists() and not force_a)},
        "seeds": SEEDS, "train_months": TRAIN_MONTHS,
        "results": res, "verdict": verdict, "rho_vs_31factors": diag,
        "runtime_sec": round(time.time() - t0, 1)},
        ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
