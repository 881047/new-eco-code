# -*- coding: utf-8 -*-
"""lambdarank REJECT 判决的对抗性自查（2026-10-02，用户问"有没有可能被错误判负"）。
只读。检验四种假 REJECT 机制:
  T1 信号有效性: ltr 分数的月度 Spearman IC vs 21 日前瞻(对照生产 IC≈0.099)
  T2 头部质量: ltr top6 的 21 日超额(对照回归 top6 ≈+2%/月, rankens≈0)
  T3 与 pin 的序相关(应低相关——学习目标不同;若≈1 则实现错误)
  T4 欠训练探针: 最近锚点重训 1 个 seed, 500 轮+早停 vs 冻结 50 轮的 top6 超额差
产物 experiments/lambdarank_diagnose_20261002.json
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import importlib.util
import numpy as np
import pandas as pd
import lightgbm as lgb

from app import db
from app.backtest import engine as E
from app.factors.factor_engine import load_market
from app.models import trainer

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "lambdarank_diagnose_20261002.json"


def log(m):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def _load_mlattr():
    spec = importlib.util.spec_from_file_location(
        "mlattr", Path(__file__).resolve().parents[1] / "ml_attribution_horse_race.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    mr = _load_mlattr()
    cfg, wide, factor_names, md_all, scope, fut, pit = mr.prepare()
    scope = [d for d in scope if d <= pd.Timestamp("2026-08-31")]
    m = load_market()
    ac = m["close"] * m["adj"]
    fwd = trainer.forward_return(ac, trainer.HORIZON)
    pool_mean = fwd.mean(axis=1)

    conn = db.get_conn()
    try:
        tables = {"ltr_a": "model_score_ltr_a", "ltr_b": "model_score_ltr_b",
                  "pin": "model_score"}
        scores = {}
        for name, tbl in tables.items():
            scores[name] = pd.read_sql(f"SELECT trade_date, ts_code, score FROM {tbl}", conn)
    finally:
        conn.close()

    ics = {k: {} for k in scores}
    top6 = {k: {} for k in scores}
    overlap = {}
    for d, g in scores["pin"].groupby("trade_date"):
        t = pd.Timestamp(d)
        if t not in fwd.index:
            continue
        f = fwd.loc[t].dropna()
        pm = float(pool_mean.loc[t])
        base = g.set_index("ts_code")["score"].reindex(f.index)
        pin_rank = base.rank(pct=True)
        for name in ("ltr_a", "ltr_b", "pin"):
            s = scores[name]
            gg = s[s["trade_date"] == d].set_index("ts_code")["score"].reindex(f.index)
            pair = pd.concat([gg, f], axis=1).dropna()
            if len(pair) < 100:
                continue
            ics[name][d] = float(pair.iloc[:, 0].corr(pair.iloc[:, 1], method="spearman"))
            top = gg.dropna().nlargest(6)
            r6 = f.reindex(top.index).dropna()
            top6[name][d] = float(r6.mean() - pm) if len(r6) >= 4 else np.nan
            if name == "ltr_a":
                overlap[d] = float(pd.concat([gg.rank(pct=True), pin_rank], axis=1).dropna()
                                    .corr().iloc[0, 1])

    def summ(dct):
        s = pd.Series(dct).dropna()
        return {"n": int(len(s)), "ic_mean": round(float(s.mean()), 4),
                "ic_ir": round(float(s.mean() / s.std()), 3) if s.std() > 0 else None,
                "ic_pos_rate": round(float((s > 0).mean()), 3)}

    out = {"asof": datetime.now().isoformat(timespec="seconds"),
           "T1_ic": {k: summ(v) for k, v in ics.items()},
           "T2_top6_monthly_excess": {k: summ(v) for k, v in top6.items()},
           "T3_rank_corr_with_pin": summ(overlap)}

    # T4 欠训练探针: 最近锚点(20260831), seed=42, 50 轮 vs 500 轮+早停(ndcg@最后12月)
    i = md_all.get_loc(pd.Timestamp("2026-08-31"))
    window = trainer._train_window(md_all, i, 36)
    window = [w for w in window if w in set(md_all)]
    train_months = window[:-6]
    Xb, yb = [], []
    for w in train_months:
        bw = mr.month_block(wide, factor_names, w, fut, pit, need_label=True)
        if bw is not None and len(bw[0]) >= 50:
            Xb.append(bw[0])
            yb.append(bw[1])
    X_train = np.vstack(Xb)
    ys, groups = [], []
    for y in yb:
        r = pd.Series(y).rank(method="first")
        ys.append(pd.qcut(r, 10, labels=False).to_numpy() + 1)
        groups.append(int(len(y)))
    y_rank = np.concatenate(ys)
    block = mr.month_block(wide, factor_names, pd.Timestamp("2026-08-31"), fut, pit,
                           need_label=False)
    Xv, _yv, codes = block
    f = fwd.loc[pd.Timestamp("2026-08-31")].dropna()
    pm = float(pool_mean.loc[pd.Timestamp("2026-08-31")])
    probe = {}
    for name, rounds in (("frozen_50", 50), ("long_500_earlystop", 500)):
        params = dict(objective="lambdarank", metric="ndcg", learning_rate=0.05,
                      num_leaves=31, min_data_in_leaf=200, feature_fraction=0.8,
                      bagging_fraction=0.8, bagging_freq=1, lambda_l2=1.0,
                      n_jobs=4, verbose=-1, seed=42, deterministic=True)
        dtrain = lgb.Dataset(X_train, label=y_rank, group=groups)
        if name == "long_500_earlystop":
            n_val = groups[-12]
            cut = sum(groups[:-12])
            dval = lgb.Dataset(X_train[cut:], label=y_rank[cut:],
                               group=groups[-12:], reference=dtrain)
            model = lgb.train(params, dtrain, num_boost_round=rounds,
                              valid_sets=[dval], callbacks=[lgb.early_stopping(50)])
        else:
            model = lgb.train(params, dtrain, num_boost_round=rounds)
        pred = pd.Series(model.predict(Xv), index=codes)
        top6v = pred.dropna().nlargest(6)
        r6 = f.reindex(top6v.index).dropna()
        probe[name] = {"top6_excess": round(float(r6.mean() - pm), 4) if len(r6) >= 4 else None,
                       "ic": round(float(pd.concat([pred, f], axis=1).dropna()
                                          .corr(method="spearman").iloc[0, 1]), 4),
                       "best_iter": int(model.best_iteration or rounds)}
    out["T4_undertraining_probe"] = probe
    log(f"T1 IC: {out['T1_ic']}")
    log(f"T2 top6 月超额: {out['T2_top6_monthly_excess']}")
    log(f"T3 与 pin 序相关: {out['T3_rank_corr_with_pin']}")
    log(f"T4 欠训练探针: {probe}")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    log(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
