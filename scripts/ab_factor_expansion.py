r"""Phase 2 · 因子扩充 A/B（研究层，零写库）—— 回答"把以前挖的因子加进去会更好吗"。

跑法（cwd = 仓根，~40-60 分钟）：
    .venv\\Scripts\\python.exe -X utf8 -m scripts.ab_factor_expansion

## 设计（两臂同种子/同窗口/同标签，唯一差异 = 因子集）

- **臂 A（基线）**：现役 31 因子（`factor` 表，trainer 同源）；
- **臂 B（扩充）**：31 + 49 个 alpha_pool 筛查存活因子（`screen_alpha_pool` 2026-09-25）；
- 训练/推分：**复刻 trainer.run 的生产行为**——walk-forward 窗口 `_train_window(·, 36)`、
  标签 `forward_return(21, 退市回退)` + **行业中性**（labels − 同行业均值，生产默认）、
  LGBM `PARAMS` 50 轮 × 种子 [42, 7]（生产 4 种子的子集，两臂同种子 ⇒ 配对成立）、
  推分 = 种子间分位排名均值；
- 评估（OOS，2019-07 起与 pin 评估窗可比 + 全史两套）：逐月 rank IC（对行业中性标签）、
  top-6 篮子**原始** 21 日收益（生产口径）、top-6 换手、配对差（B−A）t 值。

⚠️ 研究层：**不写 model_score / 不碰生产表**；改造判据仍须预注册后走正式 A/B。
"""
import argparse
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb

from app.factors.factor_engine import load_market, month_end_dates, industry_map
from app.models import trainer as T

DB = r"data/neweco.db"
SEEDS = [42, 7]
TRAIN_MONTHS = 36
EVAL_START = "20190722"
TOPK = 6
POOL_DIR = Path(__file__).resolve().parents[1] / "data" / "alpha_pool"
SCREEN = Path(__file__).resolve().parents[1] / "data" / "alpha_pool_screen_20260925.json"


def survivors_of() -> list:
    d = json.loads(SCREEN.read_text(encoding="utf-8"))
    return d["survivors"]


def load_survivor_wides(names: list) -> pd.DataFrame:
    """→ 存活因子的 trainer 同构宽表（columns = (factor, ts_code) MultiIndex）。"""
    parts = {}
    for n in names:
        p = POOL_DIR / f"{n}.npz"
        d = np.load(p, allow_pickle=True)
        dates = [str(x) for x in d["dates"].tolist()]
        codes = [str(x) for x in d["codes"].tolist()]
        parts[n] = pd.DataFrame(d["values"].astype(float), index=dates, columns=codes)
    wide = T._wide_from_factors(parts)                   # columns=(factor, ts_code)
    return wide


def main(refresh: bool = False):
    t0 = time.time()
    print("== ① 市场与因子面板 ==", flush=True)
    m = load_market()
    md_all = month_end_dates(pd.DatetimeIndex(m["close"].index))
    wide31 = T._load_factors_wide()
    print(f"现役宽表 {wide31.shape}", flush=True)

    surv = survivors_of()
    print(f"存活因子 {len(surv)} 个（screen 2026-09-25）", flush=True)
    wide49 = load_survivor_wides(surv)
    wide31r = wide31.reorder_levels(["factor", "ts_code"], axis=1).sort_index(axis=1)
    wide_ext = pd.concat([wide31r, wide49.sort_index(axis=1)], axis=1)
    wide_ext = wide_ext.loc[:, ~wide_ext.columns.duplicated()]
    print(f"扩充宽表 {wide_ext.shape}", flush=True)

    print("== ② 标签（forward_return 21d + 行业中性，生产同式）==", flush=True)
    ac = (m["close"] * m["adj"])
    fut = T.forward_return(ac, T.HORIZON)
    labels_wide = fut.loc[md_all]
    conn = sqlite3.connect(DB)
    inds = industry_map(conn).reindex(labels_wide.columns)
    conn.close()
    labels_neu = labels_wide - labels_wide.T.groupby(inds, sort=False).transform("mean").T

    names_a = sorted(wide31r.columns.get_level_values("factor").unique())
    names_b = sorted(wide_ext.columns.get_level_values("factor").unique())

    def walk_forward(wide: pd.DataFrame, names: list, tag: str) -> pd.DataFrame:
        """→ OOS 打分长表 [anchor, ts_code, score]（种子间分位排名均值，生产同式）。"""
        oos = []
        for i in range(len(md_all)):
            d = md_all[i]
            if d < pd.Timestamp("20170101"):
                continue
            window = T._train_window(md_all, i, TRAIN_MONTHS)
            window = [w for w in window if w in wide.index]
            if len(window) < 12:
                continue
            try:
                X_tr = T._stack(wide, names, window)
                X_tr = T._drop_all_nan_rows(X_tr, f"{d.date()} {tag} 训练")
                y_tr = labels_neu.loc[window].stack().reindex(X_tr.index)
                mask = y_tr.notna()
                X_tr, y_tr = X_tr.loc[mask], y_tr.loc[mask]
                X_i = T._stack(wide, names, [d])
                X_i = T._drop_all_nan_rows(X_i, f"{d.date()} {tag} 推分")
            except KeyError:
                continue
            if len(X_i) < 100:
                continue
            per_seed = []
            for seed in SEEDS:
                p = dict(T.PARAMS)
                p["seed"] = seed
                model = lgb.train(p, lgb.Dataset(X_tr, y_tr), num_boost_round=T.ENSEMBLE_ROUNDS)
                pr = model.predict(X_i)
                per_seed.append(pd.Series(pr, index=X_i.index))
            # 种子间分位排名均值（生产同式）
            ens = pd.concat(per_seed, axis=1).rank(pct=True).mean(axis=1)
            codes = ens.index.get_level_values(-1).astype(str)
            oos.append(pd.DataFrame({"anchor": d.strftime("%Y%m%d"),
                                     "ts_code": codes,
                                     "score": ens.to_numpy(float)}))
        return pd.concat(oos, ignore_index=True)

    cache_a = Path("data") / "ab_fe_scores_A.parquet"
    cache_b = Path("data") / "ab_fe_scores_B.parquet"
    if cache_a.exists() and cache_b.exists() and not refresh:
        score_a = pd.read_parquet(cache_a)
        score_b = pd.read_parquet(cache_b)
        print(f"OOS 打分缓存命中：A {len(score_a):,} / B {len(score_b):,} 行")
    else:
        print("== ③ 臂 A（31 基线）walk-forward ==", flush=True)
        score_a = walk_forward(wide31r, names_a, "A")
        print("== ④ 臂 B（31+49 扩充）walk-forward ==", flush=True)
        score_b = walk_forward(wide_ext, names_b, "B")
        score_a.to_parquet(cache_a)
        score_b.to_parquet(cache_b)
    print(f"OOS 打分：A {len(score_a):,} 行｜B {len(score_b):,} 行", flush=True)

    print("== ⑤ OOS 评估 ==", flush=True)
    def evaluate(score: pd.DataFrame) -> pd.DataFrame:
        y_neu = labels_neu.stack().rename("y_neu").reset_index()
        y_neu.columns = ["anchor", "ts_code", "y_neu"]
        y_neu["anchor"] = y_neu["anchor"].dt.strftime("%Y%m%d")
        y_raw = fut.loc[md_all].stack().rename("y_raw").reset_index()
        y_raw.columns = ["anchor", "ts_code", "y_raw"]
        y_raw["anchor"] = y_raw["anchor"].dt.strftime("%Y%m%d")
        s = score.merge(y_neu, on=["anchor", "ts_code"], how="left") \
                 .merge(y_raw, on=["anchor", "ts_code"], how="left")

        # 逐月 1%/99% winsorize：y_raw 含极端标签（长期停牌 ffill 等造成数百倍离群），
        # 均值口径的篮子收益必须去极值；IC 为秩口径天然稳健
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
        ics, tops = {}, {}
        tos = []
        for a_, g in s2.groupby("anchor", sort=False):
            ics[a_] = float(g["score"].rank().corr(g["y_neu"].rank()))
            top = g.nlargest(TOPK, "score")
            v = top["y_raw_w"].dropna()
            # ⚠️ 末月前向窗口越出数据末端 ⇒ 标签 NaN 属"未来未知"，该月不计入毛 α
            if len(v) >= TOPK - 2:
                tops[a_] = float(v.mean())
            tos.append((a_, set(top["ts_code"])))
        ic = pd.Series(ics).sort_index()
        tovr = []
        for i in range(1, len(tos)):
            tovr.append(len(tos[i][1] & tos[i - 1][1]) / TOPK)
        vals = list(tops.values())
        gross = float(np.nanmean(vals)) * 1e4
        se = float(np.nanstd(vals, ddof=1) / np.sqrt(len(vals))) * 1e4
        return {"n_months": int(len(ic)), "ic": round(float(ic.mean()), 4),
                "ic_t": round(float(ic.mean() / ic.std(ddof=1)), 2),
                "top6_gross_bp": round(gross, 2), "top6_gross_t": round(gross / se, 2),
                "turnover": round(1 - float(np.mean(tovr)), 3),
                "ic_series": {k: round(v, 4) for k, v in ic.items()}}

    ev_a, ev_b = evaluate(score_a), evaluate(score_b)
    res = {"A31_full": metrics(ev_a, "20170101"),
           "B80_full": metrics(ev_b, "20170101"),
           "A31_eval": metrics(ev_a.loc[ev_a["anchor"] >= EVAL_START], EVAL_START),
           "B80_eval": metrics(ev_b.loc[ev_b["anchor"] >= EVAL_START], EVAL_START)}
    for tag in ("full", "eval"):
        print(f"\n[{tag}] A31: IC {res[f'A31_{tag}']['ic']:+.4f} (t {res[f'A31_{tag}']['ic_t']:+.2f})｜"
              f"top6 毛 {res[f'A31_{tag}']['top6_gross_bp']:+.1f}bp｜换手 {res[f'A31_{tag}']['turnover']}")
        print(f"[{tag}] B80: IC {res[f'B80_{tag}']['ic']:+.4f} (t {res[f'B80_{tag}']['ic_t']:+.2f})｜"
              f"top6 毛 {res[f'B80_{tag}']['top6_gross_bp']:+.1f}bp｜换手 {res[f'B80_{tag}']['turnover']}")

    # 配对差（B−A，逐月 IC）
    def paired(key_a, key_b):
        ia = res[key_a]["ic_series"]
        ib = res[key_b]["ic_series"]
        common = sorted(set(ia) & set(ib))
        diff = pd.Series([ib[k] - ia[k] for k in common], index=common)
        t_ = diff.mean() / diff.std(ddof=1) * np.sqrt(len(diff)) if diff.std(ddof=1) else np.nan
        return {"mean": round(float(diff.mean()), 4),
                "t": round(float(t_), 2) if t_ == t_ else None, "n": int(len(diff))}
    res["paired_ic_diff_eval"] = paired("A31_eval", "B80_eval")
    res["paired_ic_diff_full"] = paired("A31_full", "B80_full")
    print(f"\n配对 IC 差（B−A，eval 窗）：{res['paired_ic_diff_eval']}")
    print(f"配对 IC 差（B−A，全史）：{res['paired_ic_diff_full']}")

    q = Path("data") / f"ab_factor_expansion_{datetime.now().strftime('%Y%m%d')}.json"
    q.write_text(json.dumps({
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "Phase 2 重训 A/B（研究层，零写库）：臂 A = 现役 31；臂 B = 31+49 存活因子。"
                "同种子/同窗口/同标签，唯一差异 = 因子集。种子 [42,7] 为生产 4 种子的子集"
                "（运行时裁剪，两臂一致故配对成立）。",
        "survivors": surv, "seeds": SEEDS, "train_months": TRAIN_MONTHS,
        "results": res, "runtime_sec": round(time.time() - t0, 1)},
        ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"读数入库：{q}")


if __name__ == "__main__":
    _ap = argparse.ArgumentParser()
    _ap.add_argument("--refresh", action="store_true")
    _args = _ap.parse_args()
    main(refresh=_args.refresh)
