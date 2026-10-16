# -*- coding: utf-8 -*-
"""F1 月末行冻结预注册的两项实验（plans/f1-monthend-freeze-prereg-20260929.md，判据已冻结）。

实验一（根因复现）：用 09-22 备份的 stock_basic 行业映射 + 相同原始行情 + 日频链同款
**单行中性化路径** 重算 20260831 截面量价/lhb 因子，与备份 factor 表 8/31 行逐位对比。
  E1 过（≥95% 位级相等）⇒ 根因链从"指纹推断"升级为完全复现。

实验二（决策影响）：生产 lgb_latest_s*.txt 集成（不重训）对两版 8/31 特征打分——
  版本 A = 现库（漂移后）、版本 B = 09-22 备份（漂移前）。
  E2a：top6 完全一致且秩相关 ≥0.999 ⇒ 漂移未影响过月度方案；
  E2b：top6 任一不同 ⇒ 漂移已在实际改变月度方案。

只读（备份 ATTACH ro；生产库只读；不写任何表）。特征口径：与生产因子表同源
（26 因子消费口径由模型 feature_name 决定，factor 表保持 31 全量）。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app import db
from app import timeutil
from app.factors.factor_engine import (compute_lhb_factors, compute_price_factors,
                                       load_market, neutralize)
from app.logger import setup_logger

setup_logger()

BACKUP = Path(__file__).resolve().parents[2] / "data" / "backup" / "neweco_20260922.db"
OUT = Path(__file__).resolve().parents[2] / "experiments" / "05_label" / "f1_monthend_freeze_exp_0929.json"
MD = "20260831"
LOOKBACK_DAYS = 300        # daily_engine 同款窗口
PRICE_FACTORS = {"rev_5", "mom_20", "mom_60", "mom_120", "mom_250", "vol_20", "vol_60",
                 "skew_20", "ret_max20", "turnover_20", "turnover_chg", "amount_log",
                 "illiq", "bias_20", "bias_60", "high_52w", "low_52w", "beta_250",
                 "size", "ep", "bp", "dv_ttm"}
LHB_FACTORS = {"lhb_net20", "lhb_org_net20", "lhb_cnt20"}


def window_cutoff(conn) -> str:
    """daily_engine 同款: 评分日往回 300 个交易日的窗口起点。"""
    row = conn.execute(
        "SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date<='20260831' "
        "ORDER BY cal_date DESC LIMIT 1 OFFSET ?", (LOOKBACK_DAYS,)).fetchone()
    return row[0] if row else "20130101"


def recompute(conn, industry: pd.Series) -> pd.DataFrame:
    """日频链同款单行路径重算 8/31 的量价+lhb 因子（中性化后长表）。"""
    cutoff = window_cutoff(conn)
    m = load_market(since=cutoff)
    dates = pd.to_datetime(m["close"].index)
    price_f = compute_price_factors(m)
    for name in price_f:
        price_f[name].index = dates
    lhb_f = compute_lhb_factors(dates)
    out = []
    d = pd.Timestamp(MD)
    for name, df in {**price_f, **lhb_f}.items():
        neu = neutralize(df.loc[[d]], industry)          # 单行 float32 路径
        s = neu.loc[d].dropna()
        out.append(pd.DataFrame({"ts_code": s.index, "factor": name,
                                 "value": s.values.astype(float)}))
    return pd.concat(out, ignore_index=True)


def exp1(conn) -> dict:
    # 09-22 备份的行业映射(当日快照) —— 漂移的"第一现场"输入
    ind_bak = pd.read_sql("SELECT ts_code, industry FROM bak.stock_basic", conn)
    ind_bak = ind_bak.set_index("ts_code")["industry"].fillna("未知")
    recom = recompute(conn, ind_bak)
    bak = pd.read_sql(
        "SELECT ts_code, factor, value FROM bak.factor WHERE trade_date=?", conn, params=(MD,))
    keep = set(PRICE_FACTORS) | set(LHB_FACTORS)
    bak = bak[bak["factor"].isin(keep)]
    merged = recom.merge(bak, on=["ts_code", "factor"], suffixes=("_rc", "_bak"), how="inner")
    a = merged["value_rc"].to_numpy(dtype=float)
    b = merged["value_bak"].to_numpy(dtype=float)
    exact = a == b
    rate = float(exact.mean()) if len(a) else 0.0
    by_factor = merged.groupby("factor").apply(
        lambda g: float((g["value_rc"].to_numpy() == g["value_bak"].to_numpy()).mean()),
        include_groups=False).round(4).to_dict()
    return {"n_compared": int(len(a)), "exact_rate": round(rate, 6),
            "by_factor_exact": by_factor,
            "verdict_E1": "PASS(根因完全复现)" if rate >= 0.95 else "FAIL(另有漂移源)"}


def _score(features: pd.DataFrame, feat_names: list, models) -> pd.Series:
    """daily_score 同款打分(不写库): 集成分位秩均值 ×100。features: index=ts_code。"""
    missing = [f for f in feat_names if f not in features.columns]
    if missing:
        raise RuntimeError(f"缺列: {missing}")
    Xm = features.reindex(columns=feat_names)
    pct = []
    for m in models:
        p = m.predict(Xm.astype(np.float32))
        pct.append(pd.Series(p, index=features.index).rank(pct=True))
    return (sum(pct) / len(pct)) * 100


def exp2(conn) -> dict:
    from app.models.daily_score import ENS_DIR, ENS_PREFIX, _model_files
    files = _model_files()
    import lightgbm as lgb
    models = [lgb.Booster(model_file=str(f)) for f in files]
    feat_names = models[0].feature_name()
    ver = {}
    for tag, table in (("A_cur", "main"), ("B_0922", "bak")):
        fac = pd.read_sql(f"SELECT ts_code, factor, value FROM {table}.factor WHERE trade_date=?",
                          conn, params=(MD,))
        X = fac.pivot(index="ts_code", columns="factor", values="value")
        # 可交易域(两版相同): PIT 域 —— 用生产口径六层
        from app.factors.factor_engine import load_market as _lm
        from app.pit.engine import PITEngine
        m = _lm()
        pit = PITEngine(m)
        ok = pit.tradable_mask(pd.Timestamp(MD), allow_bse=False)
        X = X[X.index.isin(ok[ok].index)]
        ver[tag] = _score(X, feat_names, models)
    common = ver["A_cur"].index.intersection(ver["B_0922"].index)
    sa, sb = ver["A_cur"].reindex(common), ver["B_0922"].reindex(common)
    top6_a = set(sa.sort_values(ascending=False).head(6).index)
    top6_b = set(sb.sort_values(ascending=False).head(6).index)
    rho = float(sa.corr(sb, method="spearman"))
    return {"n_scored": int(len(common)), "top6_A": sorted(top6_a), "top6_B": sorted(top6_b),
            "top6_identical": top6_a == top6_b, "spearman": round(rho, 6),
            "verdict_E2": "E2a_决策无影响" if top6_a == top6_b and rho >= 0.999
                          else "E2b_决策有影响"}


def main():
    if not BACKUP.exists():
        print("备份库不存在:", BACKUP)
        return 2
    conn = db.get_conn()
    try:
        conn.execute("PRAGMA query_only=1")
        conn.execute(f"ATTACH DATABASE '{BACKUP.as_posix()}' AS bak")
        r1 = exp1(conn)
        print("实验一(根因复现):", json.dumps(r1, ensure_ascii=False, indent=1))
        r2 = exp2(conn)
        print("实验二(决策影响):", json.dumps(r2, ensure_ascii=False, indent=1))
    finally:
        conn.close()
    OUT.write_text(json.dumps({
        "asof": timeutil.stamp(), "prereg": "plans/f1-monthend-freeze-prereg-20260929.md",
        "exp1": r1, "exp2": r2}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n结果已写", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
