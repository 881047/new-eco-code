"""C 案：分组子模型 + 等权秩融合 vs 31 联合基线 —— 预注册执行脚本

判据（跑数前冻结）：plans/c-block-fusion-prereg-20260922.md
  - 6 块映射 / 配对双 seed（S1/S2）/ 生产同款训练配置 / 等权秩融合 / F56-F76b-F78 同源研究层口径
  - G0：BASE 研究层年化 ∈ [5.5, 9.5]pp；块模型 ≥95% 锚点
  - 主判据 L1：两套 seed 全部 Δ=ann(C)−ann(BASE) ≥ +1.6pp 且配对 t ≥ 1.6 ⇒ 送组合层；否则不采纳

边界：不改 app/**、不碰 model_score、不换 pin、不写 factor 表；
分数全部落隔离表 model_score_cbase_{s1,s2} / model_score_cblk_{b1..b6}_{s1,s2}。

用法：
  ./.venv/Scripts/python.exe -X utf8 experiments/05_label/C_block_fusion.py            # 全阶段
  ./.venv/Scripts/python.exe -X utf8 experiments/05_label/C_block_fusion.py --stage train
  ./.venv/Scripts/python.exe -X utf8 experiments/05_label/C_block_fusion.py --stage readout,analyze
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfg_mod                                   # noqa: E402
from app import db                                                  # noqa: E402
from app.config import DATA_DIR                                     # noqa: E402
from app.factors import factor_engine as fe                         # noqa: E402
from app.models import trainer                                      # noqa: E402
from app.models.expert import ensure_score_table, zone_of           # noqa: E402
from app.pit.engine import PITEngine                                # noqa: E402
from app.portfolio import fees as fee_mod                           # noqa: E402
from app.weekly import scores as sc                                 # noqa: E402
from app.logger import setup_logger                                 # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "C_block_fusion_result.json"
SERIES_CSV = HERE / "C_block_fusion_series.csv"
log = logging.getLogger("cblock")

# ---- 冻结常量（逐项同预注册） ------------------------------------------------
PROD_BACKTEST_START = "2017-01-01"
TOP_N = 100
HORIZON_READOUT = 21
SELL_REF_DATE = "20260914"
G0_BAND_PP = (5.5, 9.5)          # BASE 研究层年化 sanity 带
L1_GATE_PP = 1.6                 # 主判据 Δ 门限
L1_GATE_T = 1.6
EXPECTED_MIN_FRAC = 0.95         # 块模型锚点下限

BLOCKS = {
    "b1": ["size", "amount_log", "illiq"],
    "b2": ["vol_20", "vol_60", "ret_max20", "turnover_20", "skew_20",
           "beta_250", "turnover_chg"],
    "b3": ["mom_20", "mom_60", "mom_120", "mom_250", "high_52w", "low_52w", "bias_60"],
    "b4": ["rev_5", "bias_20"],
    "b5": ["ep", "bp", "dv_ttm", "roe", "netprofit_yoy", "or_yoy",
           "grossprofit_margin", "eps_yoy", "debt_to_assets"],
    "b6": ["lhb_net20", "lhb_org_net20", "lhb_cnt20"],
}
ALL_31 = sorted({f for v in BLOCKS.values() for f in v})
assert len(ALL_31) == 31, f"分块映射必须覆盖 31 个因子，实际 {len(ALL_31)}"


def _seed_pool(n: int, base: int = 1_000_003, step: int = 7919) -> list:
    prod = set(int(s) for s in trainer.ENSEMBLE_SEEDS)
    out, v = [], base
    while len(out) < n:
        if v not in prod:
            out.append(v)
        v += step
    return out


_POOL = _seed_pool(204)
SEED_SETS = {"s1": _POOL[100:104], "s2": _POOL[200:204]}
assert not (set(SEED_SETS["s1"]) & set(SEED_SETS["s2"])), "S1/S2 必须零重叠"


def _merge_json(patch: dict) -> None:
    prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}

    def rec(dst, src):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                rec(dst[k], v)
            else:
                dst[k] = v

    rec(prev, patch)
    OUT.write_text(json.dumps(prev, ensure_ascii=False, indent=1), encoding="utf-8")


def _tables(sid: str):
    base_t, base_i = f"model_score_cbase_{sid}", f"model_importance_cbase_{sid}"
    blk = {b: (f"model_score_cblk_{b}_{sid}", f"model_importance_cblk_{b}_{sid}")
           for b in BLOCKS}
    return base_t, base_i, blk


def _expected_anchors() -> int:
    # 参照物 = 生产 model_score 的期数（factor 表含月中修复行，不能数它；
    # trainer 对 backtest_start=2017-01-01 产出的锚点数与生产逐位一致）
    with db.conn_ctx() as conn:
        n = conn.execute("SELECT COUNT(DISTINCT trade_date) FROM model_score "
                         "WHERE trade_date>=?", (PROD_BACKTEST_START.replace("-", ""),)
                         ).fetchone()[0]
    return int(n)


# ---- stage: train -----------------------------------------------------------
def stage_train() -> dict:
    expected = _expected_anchors()
    log.info("期望锚点 %d（%s 起）", expected, PROD_BACKTEST_START)

    # 块面板：从生产 factor 表透视（值已中性化，逐位同源；trainer 的 override 契约要求如此）
    t0 = time.time()
    with db.conn_ctx() as conn:
        fac = pd.read_sql("SELECT ts_code, trade_date, factor, value FROM factor", conn)
    panels = {f: fac[fac["factor"] == f].pivot_table(
        index="trade_date", columns="ts_code", values="value") for f in ALL_31}
    log.info("因子面板 %d 个（%.0fs）", len(panels), time.time() - t0)

    retrains = {}
    prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    prev_re = prev.get("train", {}).get("retrains", {})
    for sid, seeds in SEED_SETS.items():
        base_t, base_i, blk = _tables(sid)

        def _have(table):
            with db.conn_ctx() as conn:
                if not db.table_exists(conn, table):
                    return False, 0
                n = conn.execute(
                    f"SELECT COUNT(DISTINCT trade_date) FROM {table}").fetchone()[0]
            return n >= int(expected * EXPECTED_MIN_FRAC), n

        # BASE
        ok, n = _have(base_t)
        tag = f"cbase_{sid}"
        if ok:
            log.info("跳过 %s（已有 %d/%d 锚点）", tag, n, expected)
            retrains[tag] = {"skipped": True, "n_dates": n, "seeds": list(seeds)}
        else:
            ensure_score_table(base_t)
            with db.conn_ctx() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS "
                    f"{base_i} (train_date TEXT, factor TEXT, importance REAL, "
                    "PRIMARY KEY (train_date, factor)) WITHOUT ROWID")
            log.info("== 训练 %s seeds=%s ==", tag, seeds)
            t1 = time.time()
            trainer.run(full=True, score_table=base_t, importance_table=base_i,
                        archive_dir=DATA_DIR / "models" / tag,
                        num_boost_round=trainer.ENSEMBLE_ROUNDS, early_stopping_rounds=0,
                        ensemble_seeds=list(seeds), sample_weight_mode=None,
                        train_months=36, holdout_recent=6,
                        backtest_start=PROD_BACKTEST_START)
            retrains[tag] = {"skipped": False, "seeds": list(seeds),
                             "secs": round(time.time() - t1, 1)}
        # 6 块
        for b, fnames in BLOCKS.items():
            st, it = blk[b]
            ok, n = _have(st)
            tag = f"cblk_{b}_{sid}"
            if ok:
                log.info("跳过 %s（已有 %d/%d 锚点）", tag, n, expected)
                retrains[tag] = {"skipped": True, "n_dates": n, "seeds": list(seeds)}
                continue
            ensure_score_table(st)
            with db.conn_ctx() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS "
                    f"{it} (train_date TEXT, factor TEXT, importance REAL, "
                    "PRIMARY KEY (train_date, factor)) WITHOUT ROWID")
            override = {f: panels[f] for f in fnames}
            log.info("== 训练 %s（%d 因子）seeds=%s ==", tag, len(fnames), seeds)
            t1 = time.time()
            trainer.run(full=True, score_table=st, importance_table=it,
                        archive_dir=DATA_DIR / "models" / tag,
                        num_boost_round=trainer.ENSEMBLE_ROUNDS, early_stopping_rounds=0,
                        ensemble_seeds=list(seeds), sample_weight_mode=None,
                        train_months=36, holdout_recent=6,
                        backtest_start=PROD_BACKTEST_START, factors_override=override)
            retrains[tag] = {"skipped": False, "seeds": list(seeds),
                             "factors": fnames, "secs": round(time.time() - t1, 1)}
        _merge_json({"train": {"expected_anchors": expected, "retrains": retrains}})
    return {"retrains": retrains}


# ---- stage: fuse（等权秩融合） ------------------------------------------------
def _load_scores(table: str) -> pd.DataFrame:
    with db.conn_ctx() as conn:
        df = pd.read_sql(f"SELECT trade_date, ts_code, score FROM {table}", conn)
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    return df.pivot_table(index="trade_date", columns="ts_code",
                          values="score").sort_index()


def stage_fuse() -> dict:
    shapes = {}
    for sid in SEED_SETS:
        _, _, blk = _tables(sid)
        ranks = []
        for b in BLOCKS:
            w = _load_scores(blk[b][0])
            ranks.append(w.rank(axis=1, pct=True))
        f = pd.concat(ranks).groupby(level=0).mean()  # 逐锚点 6 块秩均值
        f = f.sort_index()
        f.to_csv(HERE / f"C_block_fusion_fused_{sid}.csv")
        shapes[sid] = list(f.shape)
        log.info("融合 %s：%s，锚点 %d", sid, f.shape, len(f))
    _merge_json({"fuse": {"method": "per-anchor pct-rank, equal mean across 6 blocks",
                          "files": [f"C_block_fusion_fused_{s}.csv" for s in SEED_SETS]}})
    return {"shape": shapes}


# ---- stage: readout（研究层，F56/F76b/F78 逐字同源） --------------------------
def _top100_series(S, Y, mask_np, sell_rate) -> pd.Series:
    rets, prev = [], None
    for k in range(S.shape[0]):
        y, s = Y[k], S[k]
        ok = mask_np[k] & np.isfinite(s) & np.isfinite(y)
        if ok.sum() < 200:
            rets.append(np.nan)
            continue
        idx = np.argsort(-np.where(ok, s, -np.inf))[:TOP_N]
        w = np.full(len(idx), 1.0 / len(idx))
        gross = float(np.dot(w, y[idx].astype(float)))
        cost = 0.0
        cur = dict(zip(idx, w))
        if prev:
            names = set(cur) | set(prev)
            cost = sum(abs(cur.get(i, 0.0) - prev.get(i, 0.0)) for i in names) * sell_rate
        prev = cur
        rets.append(gross - cost)
    return pd.Series(rets)


def stage_readout() -> dict:
    expected = _expected_anchors()
    wides = {}
    for sid in SEED_SETS:
        base_t, _, _ = _tables(sid)
        b = _load_scores(base_t)
        if len(b) < expected * EXPECTED_MIN_FRAC:
            raise SystemExit(f"{base_t} 只有 {len(b)} 期 ⇒ 先跑 --stage train")
        c = pd.read_csv(HERE / f"C_block_fusion_fused_{sid}.csv",
                        index_col=0, parse_dates=True)
        wides[f"BASE_{sid}"] = b
        wides[f"C_{sid}"] = c
    anchors = pd.DatetimeIndex(sorted(set.intersection(*[set(w.index) for w in wides.values()])))
    cols = sorted(set().union(*[set(w.columns) for w in wides.values()]))
    log.info("共同锚点 %d 期（%s ~ %s）| 列并集 %d", len(anchors),
             anchors[0].date(), anchors[-1].date(), len(cols))

    m = fe.load_market(since=sc.SINCE)
    cfg = cfg_mod.load_config()
    mask = PITEngine(m=m, cfg=cfg).tradable_matrix(anchors, columns=cols).fillna(False)
    Y = sc.forward_excess(m, mask, HORIZON_READOUT)
    sell_rate = fee_mod.effective_rate(25_000.0, "sell", SELL_REF_DATE)
    mask_np = mask.to_numpy(dtype=bool)
    zones = pd.Series(zone_of(anchors), index=anchors)

    series = {}
    ics = {}
    for arm, w in wides.items():
        S = w.reindex(index=anchors, columns=cols).to_numpy(dtype=float)
        series[arm] = pd.Series(_top100_series(S, Y, mask_np, sell_rate).to_numpy(),
                                index=anchors)
        # 副读数 IC（秩相关，生产掩码内）
        ic = []
        for k in range(len(anchors)):
            ok = mask_np[k] & np.isfinite(S[k]) & np.isfinite(Y[k])
            if ok.sum() < 200:
                continue
            ic.append(pd.Series(S[k][ok]).corr(pd.Series(Y[k][ok]), method="spearman"))
        ics[arm] = round(float(np.nanmean(ic)), 4)
    pd.DataFrame(series).to_csv(SERIES_CSV)
    log.info("逐期序列已落盘 %s", SERIES_CSV.name)

    per_arm = {}
    for arm, s in series.items():
        row = {"n": int(s.notna().sum()),
               "ann_pp": round(100 * float(s.mean()) * 12, 4),
               "ic_mean": ics[arm]}
        for seg, sel in (("ACTIVE", zones == "ACTIVE"), ("DEAD", zones == "DEAD")):
            row[f"{seg}_ann_pp"] = round(100 * float(s[sel].mean()) * 12, 4)
        per_arm[arm] = row
        log.info("%s: 全史 %.2fpp | ACTIVE %.2f | DEAD %.2f | IC %.4f",
                 arm, row["ann_pp"], row["ACTIVE_ann_pp"], row["DEAD_ann_pp"],
                 row["ic_mean"])
    _merge_json({"research": {"protocol": {"top_n": TOP_N, "horizon": HORIZON_READOUT,
                                           "sell_rate_bp": round(sell_rate * 1e4, 3),
                                           "anchors": len(anchors),
                                           "columns_union": len(cols)},
                              "per_arm": per_arm}})
    return {"per_arm": per_arm}


# ---- stage: analyze（冻结判据） ----------------------------------------------
def stage_analyze() -> dict:
    prev = json.loads(OUT.read_text(encoding="utf-8"))
    per_arm = prev["research"]["per_arm"]
    df = pd.read_csv(SERIES_CSV, index_col=0, parse_dates=True)

    g0 = {}
    for sid in SEED_SETS:
        a = per_arm[f"BASE_{sid}"]["ann_pp"]
        g0[f"BASE_{sid}"] = {"ann_pp": a, "in_band": bool(G0_BAND_PP[0] <= a <= G0_BAND_PP[1])}
    g0_pass = all(v["in_band"] for v in g0.values())

    pairs = {}
    all_pass = True
    for sid in SEED_SETS:
        d = (df[f"C_{sid}"] - df[f"BASE_{sid}"]).dropna()
        dm = float(d.mean()) * 12 * 100
        se = float(d.std(ddof=1)) / np.sqrt(len(d))
        t_stat = dm / (se * 12 * 100) if se else float("nan")
        pairs[sid] = {"delta_ann_pp": round(dm, 4), "n": int(len(d)),
                      "t_paired": round(t_stat, 3),
                      "pass": bool(dm >= L1_GATE_PP and t_stat >= L1_GATE_T)}
        all_pass &= pairs[sid]["pass"]
    verdict = ("ADOPT_TO_PORTFOLEL_LAYER" if (g0_pass and all_pass)
               else "effect_indistinguishable")
    out = {"g0": g0, "g0_pass": g0_pass, "pairs": pairs,
           "verdict": verdict,
           "note": "L1 门（预注册）：两套 seed 全部 Δ≥+1.6pp 且配对 t≥1.6 才送组合层；"
                   "配对 t 的 SE 只含时间抽样，引用时注明可能虚高 ≤1.20×（F78b 标定）"}
    _merge_json({"analyze": out})
    log.info("判决：%s | G0=%s | %s", verdict, g0_pass,
             {k: (v["delta_ann_pp"], v["t_paired"]) for k, v in pairs.items()})
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="train,fuse,readout,analyze",
                    help="train / fuse / readout / analyze，逗号分隔按序执行")
    args = ap.parse_args()
    setup_logger()
    for s in [x.strip() for x in args.stage.split(",") if x.strip()]:
        log.info("==== stage: %s ====", s)
        r = globals()[f"stage_{s}"]()
        print(json.dumps(r, ensure_ascii=False, indent=1, default=str)[:1500])
    log.info("== 完成 ==")
