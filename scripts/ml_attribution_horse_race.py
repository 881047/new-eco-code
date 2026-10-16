# -*- coding: utf-8 -*-
"""模型归因赛马 —— "是不是模型的问题"的机械回答。

plans/model-rebuild-options-20260929.md §1（判据跑数前冻结）：
同特征（生产 26 因子消费侧口径）/同标签（21 日行业中性超额, delist_last_price=True）/
同 walk-forward（36 月窗、近端 6 月 holdout 不参训、PIT 可交易域、北交排除）下，
三个新打分臂 vs 生产 LightGBM pin（库内既有 run，不重跑）：

    ridge    岭回归闭式解（标准化特征, λ=1.0 冻结, NaN→0=均值中性）
    rankens  无学习: 26 因子当月截面 pct-rank 等权均值
    random   固定种子均匀随机分（管线 sanity 地板）

打分写隔离表 model_score_mlattr_*（ensure_score_table 契约, 重跑幂等），
引擎 run_backtest(score_table=) 双窗换表回测（09-27 确立的组合层 A/B 通道）。
read-only for production: 不碰 model_score/pin/任何生产配置。

用法:
    .venv/Scripts/python.exe scripts/ml_attribution_horse_race.py            # 全流程
    .venv/Scripts/python.exe scripts/ml_attribution_horse_race.py --scores-only
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from app import config as cfg_mod
from app import db
from app import timeutil
from app.backtest import engine as E
from app.factors.factor_engine import load_market, month_end_dates
from app.models import trainer
from app.models.expert import ensure_score_table
from app.pit.engine import PITEngine

log = logging.getLogger(__name__)

BASE = Path(__file__).resolve().parent.parent
OUT_PATH = BASE / "experiments" / "ml_attribution_20260929.json"

# 冻结口径（§1）
TRAIN_MONTHS = 36          # 与生产 trainer 同窗
HOLDOUT = 6                # 近端留出不参训（与生产同口径）
RIDGE_LAMBDA = 1.0         # 冻结不调参（标准化特征尺度）
TOPN_PORT = 6
SEED = 42
FULL_WIN = ("20170228", "20260831")
PROD_WIN = (None, None)    # 走 config eval_start（与 pin 20260928_exe 同窗）
WINDOWS = [("full", FULL_WIN), ("prod", PROD_WIN)]
ARMS = ("ridge", "rankens", "random")
TABLES = {a: f"model_score_mlattr_{a}" for a in ARMS}
LGBM_RUNS = {"full": "20260928_exe_full", "prod": "20260928_exe"}
SEED_NOISE_PP = 3.5        # 既有实测种子噪声容差
SANITY_GAP_PP = 5.0


# ---------------------------------------------------------------- 数据准备
def prepare():
    cfg = cfg_mod.load_config()
    excl = set(cfg.get("factor_exclusions") or [])
    wide = trainer._apply_exclusions(trainer._load_factors_wide(), excl)
    factor_names = sorted(wide.columns.get_level_values("factor").unique())
    log.info("因子 %d 个（生产消费侧口径）", len(factor_names))

    m = load_market()
    dates = pd.to_datetime(m["close"].index)
    # ⚠️ md_all 必须用**全历史**(2014 起, 与生产 trainer 同口径): 训练窗要吃到
    # 2014-2016 的数据; 只对 >=2017 的月份写分(评估域)。若把 md_all 截到 2017,
    # 早期臂的训练窗被砍 ⇒ 系统性削弱 ridge ⇒ 赛马不公平(偏向 V2)。
    md_all = month_end_dates(dates, today=timeutil.today())
    md_all = md_all[md_all >= pd.Timestamp("2014-01-01")]
    scope = md_all[md_all >= pd.Timestamp("2017-01-01")]
    ac = m["close"] * m["adj"]
    fut = trainer.forward_return(ac, trainer.HORIZON)
    with db.conn_ctx() as conn:
        inds = trainer.industry_map(conn)
    inds = inds.reindex(fut.columns)
    # 标签行业中性化（与生产标签同口径；pandas 3: 须 axis=0 按行广播族注意——
    # 这里是 DataFrame-DataFrame 对齐, 无坑）
    fut = fut - fut.T.groupby(inds, sort=False).transform("mean").T
    pit = PITEngine(m, cfg=cfg)
    return cfg, wide, factor_names, md_all, scope, fut, pit


def month_block(wide, factor_names, d, fut, pit, need_label: bool = True):
    """单月截面块: (X 矩阵 [mask 后], y 向量, codes)。

    need_label=True(训练月): mask = 标签非 NaN ∧ PIT 可交易 ∧ 非全 NaN 特征行;
    need_label=False(**打分月**): 标签不参与 —— 打分不需要前瞻标签, 只有训练月需要。
    ⚠️ 首版把两者耦合 ⇒ 最后一个月(无前瞻标签)整月缺分、引擎评估窗短 2 期, 赛马
    两臂的对照窗与基准不再逐月对齐(2026-09-29 实测修正)。
    """
    if d not in wide.index:
        return None
    row = wide.loc[d]
    Xd = row.unstack("factor").reindex(columns=factor_names)
    y = fut.loc[d].reindex(Xd.index)
    ok = pit.tradable_mask(d, allow_bse=False).reindex(Xd.index).fillna(False).astype(bool)
    if need_label:
        good = y.notna().to_numpy() & ok.to_numpy()
    else:
        good = ok.to_numpy()
    Xv = Xd.to_numpy(dtype="float64")[good]
    keep = np.isfinite(Xv).any(axis=1)
    if keep.sum() < 50:
        return None
    yv = y.to_numpy(dtype="float64")[good][keep] if need_label \
        else np.full(int(keep.sum()), np.nan)
    # codes 必须与 good 过滤对齐(2026-10-02 审查修正): 原写 Xd.index[ok][keep] ——
    # 训练月 ok ⊇ good(存在"可交易但标签 NaN"的行时两者不等), 布尔索引长度错位。
    # 实际首跑两者逐月恰好相等未爆, 属潜在错位; 打分月 good=ok 语义不变。
    return (Xv[keep], yv, Xd.index[good][keep])


# ---------------------------------------------------------------- 臂实现
def fit_ridge(blocks, n_factors: int):
    """岭回归闭式解: β = (X'X/n + λI)^-1 · X'y/n, 特征按训练集标准化, NaN→0。"""
    X = np.vstack([b[0] for b in blocks])
    y = np.concatenate([b[1] for b in blocks])
    mu = np.nanmean(np.where(np.isfinite(X), X, np.nan), axis=0)
    sd = np.nanstd(np.where(np.isfinite(X), X, np.nan), axis=0)
    sd = np.where(np.isfinite(sd) & (sd > 1e-12), sd, 1.0)
    Z = np.where(np.isfinite(X), (X - mu) / sd, 0.0)
    n = len(Z)
    A = (Z.T @ Z) / n + RIDGE_LAMBDA * np.eye(n_factors)
    b = (Z.T @ y) / n
    return {"beta": np.linalg.solve(A, b), "mu": mu, "sd": sd}


def ridge_predict(model, Xv: np.ndarray) -> np.ndarray:
    Z = np.where(np.isfinite(Xv), (Xv - model["mu"]) / model["sd"], 0.0)
    return Z @ model["beta"]


def rankens_score(Xv: np.ndarray) -> np.ndarray:
    """无学习: 每因子截面 pct-rank(等权均值)。NaN 因子不计入该股均值(有效因子数<3 ⇒ NaN)。"""
    X = np.where(np.isfinite(Xv), Xv, np.nan)
    rk = pd.DataFrame(X).rank(pct=True).to_numpy()
    with np.errstate(invalid="ignore"):
        m = np.nanmean(rk, axis=1)
    cnt = np.isfinite(rk).sum(axis=1)
    return np.where(cnt >= 3, m, np.nan)


# ---------------------------------------------------------------- 主流程
def build_scores(wide, factor_names, md_all, scope, fut, pit, dry_run=False):
    """逐月一次扫描(仅 scope 内月份写分, 训练窗用全历史 md_all): 各臂打分落隔离表。"""
    for tbl in TABLES.values():
        ensure_score_table(tbl)
    out_rows = {a: [] for a in ARMS}
    n_months = 0
    t0 = time.time()
    pos_of = {d: i for i, d in enumerate(md_all)}
    for d in scope:
        i = pos_of[d]
        block = month_block(wide, factor_names, d, fut, pit, need_label=False)
        if block is None:
            continue
        Xv, _y, codes = block
        # --- rankens / random: 只需当月截面
        rk = rankens_score(Xv)
        rng = np.random.RandomState(SEED + i)
        rnd = rng.random_sample(len(codes))
        # --- ridge: walk-forward 窗口
        window = trainer._train_window(md_all, i, TRAIN_MONTHS)
        window = [w for w in window if w in set(md_all)]
        if len(window) >= 12:
            # 近端 HOLDOUT 个月不参训(与生产同口径)
            train_months = window[:-HOLDOUT] if len(window) > HOLDOUT else window
            blocks = []
            for w in train_months:
                bw = month_block(wide, factor_names, w, fut, pit, need_label=True)
                if bw is not None and len(bw[0]) >= 50:
                    blocks.append(bw)
            if sum(len(b[0]) for b in blocks) >= 5000:
                model = fit_ridge(blocks, len(factor_names))
                pred = ridge_predict(model, Xv)
            else:
                pred = None
        else:
            pred = None
        d8 = d.strftime("%Y%m%d")
        # score = 月内 pct-rank ×100(与生产同尺, 引擎只看序); pred_ret = 原始分
        for arm, raw in (("ridge", pred), ("rankens", rk), ("random", rnd)):
            if raw is None or np.all(~np.isfinite(raw)):
                continue
            s = pd.Series(raw, index=codes)
            pct = s.rank(pct=True) * 100.0
            for c, sc, rv in zip(codes, pct.to_numpy(), np.asarray(raw, dtype=float)):
                out_rows[arm].append((c, d8, float(sc), float(rv)))
        n_months += 1
        if n_months % 24 == 0:
            log.info("扫描 %d/%d 月 (%s), %.0fs", n_months, len(md_all), d8, time.time() - t0)
    log.info("月度扫描完成: %d 月, %.0fs", n_months, time.time() - t0)
    if dry_run:
        return {a: len(v) for a, v in out_rows.items()}
    # 幂等落库
    for arm, rows in out_rows.items():
        df = pd.DataFrame(rows, columns=["ts_code", "trade_date", "score", "pred_ret"])
        conn = db.get_conn()
        try:
            conn.execute(f"DELETE FROM {TABLES[arm]}")
            conn.commit()
            db.upsert_df(conn, df, TABLES[arm])
            conn.commit()
        finally:
            conn.close()
        log.info("%s: %d 行 / %d 月", TABLES[arm], len(df), df["trade_date"].nunique())
    return {a: len(v) for a, v in out_rows.items()}


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


def main(scores_only: bool = False):
    cfg, wide, factor_names, md_all, scope, fut, pit = prepare()
    counts = build_scores(wide, factor_names, md_all, scope, fut, pit,
                          dry_run=scores_only)
    if scores_only:
        print(json.dumps(counts, ensure_ascii=False))
        return
    out = {"asof": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "criteria_file": "plans/model-rebuild-options-20260929.md §1",
           "arms": {}, "baseline": {}}
    for wname, (start, end) in WINDOWS:
        kw = {} if start is None else {"start": start, "end": end}
        for arm in ARMS:
            t0 = time.time()
            mtr = E.run_backtest(score_table=TABLES[arm], run_tag=f"mlattr_{arm}_{wname}",
                                 **kw)
            out["arms"][f"{arm}_{wname}"] = {
                "run_date": mtr["run_date"], "periods": mtr["periods"],
                "ann_return": mtr["ann_return"], "max_drawdown": mtr["max_drawdown"],
                "sys_ann_return": mtr["sys_ann_return"],
                "sys_max_drawdown": mtr["sys_max_drawdown"],
                "sys_mdd_daily": mtr.get("sys_mdd_daily"),
                "avg_cash_idle": mtr.get("avg_cash_idle"),
                "avg_stocks": mtr.get("avg_stocks"),
                "seconds": round(time.time() - t0, 1)}
            log.info("[%s/%s] sys_ann=%.2f%% (%.0fs)", arm, wname,
                     100 * mtr["sys_ann_return"], time.time() - t0)
        out["baseline"][wname] = baseline_metrics(LGBM_RUNS[wname])
    # ---- 判据（§1 冻结）
    for wname, _ in WINDOWS:
        base = out["baseline"][wname]["sys_ann_return"]
        arm_ann = {a: out["arms"][f"{a}_{wname}"]["sys_ann_return"] for a in ARMS}
        gaps = {a: 100 * (base - arm_ann[a]) for a in ARMS}      # LGBM − 臂 (pp)
        rnd_ok = gaps["random"] >= SANITY_GAP_PP
        v1 = (gaps["ridge"] <= SEED_NOISE_PP and gaps["rankens"] <= SEED_NOISE_PP
              and rnd_ok)
        v2 = max(gaps["ridge"], gaps["rankens"]) > SEED_NOISE_PP and rnd_ok
        out[f"verdict_{wname}"] = {
            "lgbm_sys_ann_pct": round(100 * base, 2),
            "gaps_pp": {a: round(gaps[a], 2) for a in ARMS},
            "sanity_random_ok": bool(rnd_ok),
            "verdict": ("V1_ML_无增量(向简单重构可提请拍板)" if v1
                        else "V2_ML_有真实排序增量(重构目标=数据/结构)" if v2
                        else "V3_混合(逐臂细读)")}
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("判决: %s", json.dumps({k: v for k, v in out.items()
                                     if k.startswith("verdict")}, ensure_ascii=False))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main(scores_only="--scores-only" in sys.argv)
