# -*- coding: utf-8 -*-
"""第 14 轮改模型:排序目标(lambdarank)vs 回归(plans/lambdarank-prereg-20261002.md
判据跑前冻结)。独立走查,复用 mlattr 已验收的 prepare/month_block(同特征/同标签域/
同 walk-forward/PIT 域);唯一变量 = 目标族(regression → lambdarank)。

双种子集:A=[42,7,123,2024] vs 生产 pin;B=[7,13,99,2024] vs model_score_hw_base_b
(B1 轮回归基线,缺窗补跑)。隔离表 model_score_ltr_a/b。判据 P1/P2/P3 见预注册。
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import importlib.util
import numpy as np
import pandas as pd
import lightgbm as lgb

from app import db
from app import timeutil
from app.backtest import engine as E
from app.models import trainer
from app.models.expert import ensure_score_table

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "lambdarank_ab_20261002.json"

SPEC = {
    "a": {"seeds": [42, 7, 123, 2024], "table": "model_score_ltr_a"},
    "b": {"seeds": [7, 13, 99, 2024], "table": "model_score_ltr_b"},
}
TRAIN_MONTHS, HOLDOUT, N_ROUNDS = 36, 6, 50
DECILES = 10
FULL_WIN = {"start": "20170228", "end": "20260831"}
PROD_WIN = {}
BASELINES = {"a": {"full": "20260928_exe_full", "prod": "20260928_exe"},
             "b": {"full": None, "prod": "20260929_20260929_abhw_ctrl_B"}}


def log(m):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def _load_mlattr():
    spec = importlib.util.spec_from_file_location(
        "mlattr", Path(__file__).resolve().parent.parent / "ml_attribution_horse_race.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fit_lambdarank(train_X, train_y, group, seed: int):
    """冻结超参 = 生产 PARAMS 逐字(objective/metric 换 lambdarank/ndcg)+ 50 轮无早停。"""
    params = dict(objective="lambdarank", metric="ndcg",
                  learning_rate=0.05, num_leaves=31, min_data_in_leaf=200,
                  feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1,
                  lambda_l2=1.0, n_jobs=4, verbose=-1,
                  seed=seed, deterministic=True)
    dtrain = lgb.Dataset(train_X, label=train_y, group=group)
    return lgb.train(params, dtrain, num_boost_round=N_ROUNDS)


def decile_labels(y_blocks: list) -> tuple:
    """逐锚点十分位(1..10, method='first' 确定性) + group 尺寸。"""
    ys, groups = [], []
    for y in y_blocks:
        r = pd.Series(y).rank(method="first")
        dec = pd.qcut(r, DECILES, labels=False).to_numpy() + 1
        ys.append(dec.astype(int))
        groups.append(int(len(y)))
    return np.concatenate(ys), groups


def build_scores(wide, factor_names, md_all, scope, fut, pit, seeds, table: str) -> None:
    ensure_score_table(table)
    out_rows = []
    t0 = time.time()
    pos_of = {d: i for i, d in enumerate(md_all)}
    n_months = 0
    for d in scope:
        i = pos_of[d]
        block = mr.month_block(wide, factor_names, d, fut, pit, need_label=False)
        if block is None:
            continue
        Xv, _y, codes = block
        window = trainer._train_window(md_all, i, TRAIN_MONTHS)
        window = [w for w in window if w in set(md_all)]
        if len(window) < 12:
            continue
        train_months = window[:-HOLDOUT]
        Xb, yb = [], []
        for w in train_months:
            bw = mr.month_block(wide, factor_names, w, fut, pit, need_label=True)
            if bw is not None and len(bw[0]) >= 50:
                Xb.append(bw[0])
                yb.append(bw[1])
        if not Xb or sum(len(b) for b in Xb) < 5000:
            continue
        X_train = np.vstack(Xb)
        y_rank, group = decile_labels(yb)
        per_seed = []
        for s in seeds:
            model = fit_lambdarank(X_train, y_rank, group, s)
            per_seed.append(pd.Series(model.predict(Xv), index=codes).rank(pct=True).to_numpy())
        ens = pd.Series(np.mean(per_seed, axis=0), index=codes)
        pct = ens.rank(pct=True) * 100.0
        d8 = d.strftime("%Y%m%d")
        for c, sc, rv in zip(codes, pct.to_numpy(), ens.to_numpy()):
            out_rows.append((c, d8, float(sc), float(rv)))
        n_months += 1
        if n_months % 12 == 0:
            log(f"  打分 {n_months}/{len(scope)} 月 ({d8}) {time.time()-t0:.0f}s")
    log(f"打分完成 {n_months} 月 {time.time()-t0:.0f}s")
    df = pd.DataFrame(out_rows, columns=["ts_code", "trade_date", "score", "pred_ret"])
    conn = db.get_conn()
    try:
        conn.execute(f"DELETE FROM {table}")
        conn.commit()
        db.upsert_df(conn, df, table)
        conn.commit()
    finally:
        conn.close()
    log(f"{table}: {len(df)} 行 / {df['trade_date'].nunique()} 月")


def baseline_metrics(run_date: str) -> dict:
    conn = db.get_conn()
    try:
        row = pd.read_sql("SELECT metrics FROM backtest_summary WHERE run_date=?",
                          conn, params=(run_date,))
    finally:
        conn.close()
    if row.empty:
        raise RuntimeError(f"库内找不到基准 run {run_date}")
    return json.loads(row["metrics"].iloc[0])


def main() -> int:
    global mr
    mr = _load_mlattr()
    cfg, wide, factor_names, md_all, scope, fut, pit = mr.prepare()
    # 截到生产打分末月(20260831): 生产 model_score 尚无 2026-09 截面, 多一月破坏对齐
    scope = [d for d in scope if d <= pd.Timestamp("2026-08-31")]
    log(f"因子 {len(factor_names)} 个, scope {len(scope)} 月")

    tables = {}
    for sname, sp in SPEC.items():
        log(f"== {sname.upper()} 臂: seeds={sp['seeds']} → {sp['table']} ==")
        build_scores(wide, factor_names, md_all, scope, fut, pit, sp["seeds"], sp["table"])
        tables[sname] = sp["table"]

    # 结构对齐验收
    conn = db.get_conn()
    try:
        for sname, tbl in tables.items():
            cnt = conn.execute(f"SELECT COUNT(*), COUNT(DISTINCT trade_date) "
                               f"FROM {tbl}").fetchone()
            log(f"对齐验收 {tbl}: {cnt[0]} 行 / {cnt[1]} 月 (要求 444312/116)")
            assert cnt[0] == 444312 and cnt[1] == 116, f"{tbl} 结构不对齐 {cnt}"
    finally:
        conn.close()

    # 双窗回测(臂 + B 对照缺窗补跑)
    runs = {}
    for sname, tbl in tables.items():
        for wname, kw in (("full", FULL_WIN), ("prod", PROD_WIN)):
            mtr = E.run_backtest(score_table=tbl, run_tag=f"ltr_{sname}_{wname}", **kw)
            runs[f"{sname}_{wname}"] = mtr
            log(f"[{sname}/{wname}] sys_ann={mtr['sys_ann_return']:+.2%} "
                f"mdd={mtr['sys_max_drawdown']:+.2%} to={mtr.get('avg_turnover')}")
    for wname, kw in (("full", FULL_WIN), ("prod", PROD_WIN)):
        if BASELINES["b"][wname] is None:
            mtr = E.run_backtest(score_table="model_score_hw_base_b",
                                 run_tag=f"ltr_ctrlB_{wname}", **kw)
            BASELINES["b"][wname] = mtr["run_date"]
            runs[f"ctrlB_{wname}"] = mtr

    # 判据 P1/P2/P3
    out = {"asof": datetime.now().isoformat(timespec="seconds"),
           "criteria_file": "plans/lambdarank-prereg-20261002.md", "arms": {}, "verdict": {}}
    deltas = {}
    for sname, bl in BASELINES.items():
        for wname in ("full", "prod"):
            arm = runs[f"{sname}_{wname}"]
            base = baseline_metrics(bl[wname]) if isinstance(bl[wname], str) and \
                not bl[wname].startswith("ltr_ctrlB") else runs[f"ctrlB_{wname}"]
            d = {
                "arm_sys_ann": round(arm["sys_ann_return"], 4),
                "base_sys_ann": round(base["sys_ann_return"], 4),
                "delta_sys_ann_pp": round(100 * (arm["sys_ann_return"] - base["sys_ann_return"]), 2),
                "arm_mdd": round(arm["sys_max_drawdown"], 4),
                "base_mdd": round(base["sys_max_drawdown"], 4),
                "delta_mdd_pp": round(100 * (arm["sys_max_drawdown"] - base["sys_max_drawdown"]), 2),
                "arm_turnover": arm.get("avg_turnover"),
                "base_turnover": base.get("avg_turnover"),
            }
            deltas[f"{sname}_{wname}"] = d
    p1 = all(deltas[f"{s}_prod"]["delta_sys_ann_pp"] >= 2.0 and
             deltas[f"{s}_full"]["delta_sys_ann_pp"] >= 1.0 for s in ("a", "b"))
    same_dir = all(deltas[f"{s}_prod"]["delta_sys_ann_pp"] > 0 for s in ("a", "b"))
    p2 = all(deltas[f"{s}_prod"]["delta_mdd_pp"] > -2.0 and
             deltas[f"{s}_full"]["delta_mdd_pp"] > -3.0 for s in ("a", "b"))
    p3 = all(deltas[f"{s}_prod"]["base_turnover"] is not None and
             (deltas[f"{s}_prod"]["arm_turnover"] or 0) - (deltas[f"{s}_prod"]["base_turnover"] or 0) <= 0.05
             for s in ("a", "b"))
    if p1 and p2 and p3:
        verdict = "PASS(进拍板)"
    elif not same_dir and all(deltas[f"{s}_prod"]["delta_sys_ann_pp"] < 0 for s in ("a", "b")):
        verdict = "REJECT(线结清, MOD-02 缺口了结)"
    else:
        verdict = "INDETERMINATE(收益轴异号或仅一组达标)"
    out["deltas"] = deltas
    out["criteria"] = {"P1_both_seeds_ge_threshold": p1, "P2_mdd_bound": p2,
                       "P3_turnover": p3, "same_direction": same_dir,
                       "verdict": verdict}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    log(f"判决: {verdict}")
    for k, d in deltas.items():
        log(f"  {k}: Δsys={d['delta_sys_ann_pp']:+.2f}pp Δmdd={d['delta_mdd_pp']:+.2f}pp")
    log(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
