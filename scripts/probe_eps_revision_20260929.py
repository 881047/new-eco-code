# -*- coding: utf-8 -*-
"""O4 探针：东财研报 EPS 预测修正动量（FY2026/FY2027 同财年跨报告修正）。

语义判定（2026-09-29，见 05 号台账）：research_report_em 的 fy1_eps/fy2_eps/fy3_eps
按**采集时点（2026-09）的财年**重映射 —— fy1=FY2026 预测（2024-01 起报告有值）、
fy2=FY2027 预测（2025-01 起）、fy3=FY2028（2026 起）。证据：茅台 2024-04 报告
fy1_eps≈93.9（当年对 FY2026 的乐观预测）→ 2026-08 报告 ≈68.1（连续 miss 后下修，
FY2025 实际 65.66 吻合）；fy2 非空行年份分布（2025:13731 / 2026:7284）与三年
预测窗位置完全自洽。⇒ 修正动量只能在**同一财年内跨报告**构造（天然同财年，无
跨年含义漂移）。

候选（探针 3 个，正式分母以预注册实测为准）：
  rev_fy1_1m / rev_fy1_3m：F(t)-F(t-W) / |F(t-W)|，F=截至当日最新 FY2026 预测存量
  rev_fy2_3m：FY2027 预测同款（锚点更少）
口径与门禁家族一致：fe.neutralize_sparse、PIT tradable、gate.evaluate（ACTIVE 段）。
数据窗限制：fy1 2024-01 起 ⇒ 只覆盖 ACTIVE zone（2024-01..2026-08），DEAD 段
结构性无数据 ⇒ 判决只能出 ACTIVE 单窗读数（预注册冻结）。
只读零写库。
"""
import sys
import warnings
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from app.alpha import gate
from app import db as app_db
from app.factors import factor_engine as fe
from app.factors.factor_engine import load_market, month_end_dates
from app.pit.engine import PITEngine

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = {"1m": 21, "3m": 63}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_reports() -> pd.DataFrame:
    with app_db.conn_ctx() as conn:
        df = pd.read_sql(
            "SELECT ts_code, report_date, fy1_eps, fy2_eps FROM research_report_em "
            "WHERE fy1_eps IS NOT NULL", conn)
    df["t"] = pd.to_datetime(df["report_date"], format="%Y%m%d")
    return df


def stock_matrix(m, df, col):
    """逐日存量矩阵 (ndays × ncodes)：报告日写入值，交易日轴 ffill。"""
    td = pd.DatetimeIndex(m["close"].index)
    codes = list(m["close"].columns)
    V = np.full((len(td), len(codes)), np.nan, dtype=np.float32)
    td_ix = {d: i for i, d in enumerate(td)}
    code_ix = {c: i for i, c in enumerate(codes)}
    sub = df.dropna(subset=[col])
    for t, c, v in zip(sub["t"], sub["ts_code"], sub[col]):
        i, j = td_ix.get(t), code_ix.get(c)
        if i is not None and j is not None:
            V[i, j] = v
    out = pd.DataFrame(V, index=td, columns=codes).ffill()
    # 重置：ffill 后首个非 NaN 之前的 NaN 保持 NaN（ffill 不会填头部，天然满足）
    return out


def main() -> int:
    t0 = datetime.now()
    m = load_market()
    md_all = month_end_dates(pd.DatetimeIndex(m["close"].index))
    md_eval = pd.DatetimeIndex([d for d in md_all if d >= pd.Timestamp("2019-01-01")])
    zones = gate.zone_of(md_eval)
    fwd = gate.forward_return(m, gate.HORIZON)
    pit = PITEngine(m=m)
    tradable = pit.tradable_matrix(md_eval)
    with app_db.conn_ctx() as conn:
        industry = fe.industry_map(conn)

    rep = load_reports()
    log(f"研报行 {len(rep):,}（fy1 非空）| 股票 {rep.ts_code.nunique()}")
    M1 = stock_matrix(m, rep, "fy1_eps")   # FY2026 预测存量
    M2 = stock_matrix(m, rep, "fy2_eps")   # FY2027 预测存量

    cands = {}
    for wname, W in WINDOWS.items():
        base = M1.shift(W)
        rev = (M1 - base) / base.abs().replace(0, np.nan)
        cands[f"rev_fy1_{wname}"] = rev
    base2 = M2.shift(WINDOWS["3m"])
    cands["rev_fy2_3m"] = (M2 - base2) / base2.abs().replace(0, np.nan)
    log(f"候选 {len(cands)} 个 ({(datetime.now()-t0).total_seconds():.0f}s)")

    rows = []
    for name, panel in cands.items():
        P = panel.reindex(md_eval)
        # 存量覆盖（ACTIVE 段锚点）
        z_act = zones == "ACTIVE"
        cov = P[z_act].notna().sum(axis=1)
        sub = fe.neutralize_sparse(P, industry) if fe.sparse_ratio(P) < fe.SPARSE_NONZERO_RATIO \
            else fe.neutralize(P, industry)
        pm = sub.where(tradable.reindex(index=md_eval, columns=P.columns).fillna(False))
        det = gate.evaluate(pm, fwd, md_eval, zones)
        if det.empty:
            rows.append({"factor": name, "note": "无有效截面"})
            continue
        act = det[det.zone == "ACTIVE"]
        dead = det[det.zone == "DEAD"]
        h_a = float(act.head_excess.mean())
        t_a = float(h_a / (act.head_excess.std(ddof=1) / np.sqrt(len(act)))) if len(act) > 2 else np.nan
        rows.append({
            "factor": name,
            "anchors_ACTIVE": int(len(act)), "anchors_DEAD": int(len(dead)),
            "median_names_ACTIVE": int(act.n_names.median()) if "n_names" in act else None,
            "coverage_median": int(cov.median()) if len(cov) else 0,
            "ACTIVE_head": round(h_a, 4), "ACTIVE_head_t": round(t_a, 2),
            "ACTIVE_top6": round(float(act.top6_excess.mean()), 4) if len(act) else None,
        })
        log(f"  {name:14s} 锚点A={len(act)} 覆盖中位={rows[-1]['coverage_median']} "
            f"ACTIVE head {h_a:+.4f} t {t_a:+.2f}")

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"),
           "semantics": "fy1=FY2026/fy2=FY2027 预测（采集时点重映射，05 号台账 44 补）",
           "rows": rows}
    print(pd.DataFrame(rows).to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
