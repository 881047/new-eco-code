# -*- coding: utf-8 -*-
"""S2 PBO/CSCV 诊断器 —— 度量"构型实验选择过程本身"的过拟合概率。

出处：Bailey, Borwein, López de Prado & Zhu, "The Probability of Backtest
Overfitting", Journal of Computational Finance (2017) 的 CSCV（组合对称交叉验证）。
**这是诊断器不是策略**：read-only，只读 backtest_nav 的实验臂，不改任何生产配置。

## 本仓适配（plans/return-stability-deep-analysis-20260929.md §5.1，跑数前冻结）

1. **近重复臂去重**：库内 run 大量是重钉/复验/diag 同构重跑——在近重复臂之间做 IS
   选择 = 抛硬币，PBO 被机械推向 0.5。规则：全史月频臂按 run_date 升序贪心去重，
   与任何已保留臂的月度系统线收益相关系数 >= 0.9995 的后续臂丢弃。
2. **决策轴**：sys_nav（系统线）月度收益——本仓 A/B 判决轴是 Δsys_ann。
3. **IS 选择指标** = 训练块上年化几何收益（主判）；Sharpe 口径为敏感性副读数。
   OOS 相对秩 ω → logits λ = logit(ω)；PBO = P(λ<0)。S=16 块，C(16,8)=12870 组合。

判读（冻结）：PBO < 0.2 健康 / 0.2~0.5 部分过拟合 / > 0.5 IS 选择不携带 OOS 信息。

用法：
    .venv/Scripts/python.exe scripts/pbo_cscv_diagnostic.py            # 跑并落 experiments/
    .venv/Scripts/python.exe scripts/pbo_cscv_diagnostic.py --dry-run  # 只打印臂集合
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import math
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from app import db

log = logging.getLogger(__name__)

BASE = Path(__file__).resolve().parent.parent
OUT_PATH = BASE / "experiments" / "pbo_cscv_diagnostic_20260929.json"

# 冻结口径（§5.1）
FULL_WINDOW = (pd.Timestamp("2017-02-28"), pd.Timestamp("2026-08-31"))
FULL_POINTS = 115
DEDUP_CORR = 0.9995
N_BLOCKS = 16
PBO_BANDS = (0.2, 0.5)


def load_monthly_matrix(conn) -> tuple[pd.Index, dict[str, pd.Series]]:
    """全史月频臂 → (月索引, {run_date: sys_nav 月度收益 Series})。"""
    df = pd.read_sql("SELECT run_date, trade_date, sys_nav FROM backtest_nav", conn)
    out = {}
    for run, g in df.groupby("run_date"):
        g = g.sort_values("trade_date")
        if len(g) != FULL_POINTS:
            continue
        idx = pd.to_datetime(g["trade_date"], format="%Y%m%d")
        if idx.iloc[0] != FULL_WINDOW[0] or idx.iloc[-1] != FULL_WINDOW[1]:
            continue
        nav = pd.Series(g["sys_nav"].to_numpy(dtype=float), index=idx)
        if nav.isna().any() or (nav <= 0).any():
            continue
        ret = nav.pct_change().dropna()
        if len(ret) != FULL_POINTS - 1 or ret.isna().any():
            continue
        out[run] = ret
    return sorted(out)[0].__class__(sorted(set().union(*[s.index for s in out.values()]))), out


def dedupe_arms(rets: dict[str, pd.Series]) -> tuple[list[str], list[str]]:
    """贪心去重（§5.1 规则）：run_date 升序，与已保留臂相关 >= 0.9995 的丢弃。"""
    order = sorted(rets)
    kept: list[str] = []
    dropped: list[str] = []
    mat = pd.DataFrame(rets)                      # 列 = run_date
    for run in order:
        if any(abs(mat[run].corr(mat[k])) >= DEDUP_CORR for k in kept):
            dropped.append(run)
        else:
            kept.append(run)
    return kept, dropped


def _ann_return(r: np.ndarray) -> float:
    """月度收益 → 年化几何（月数非 0 时）。"""
    if len(r) == 0:
        return float("nan")
    total = float(np.prod(1.0 + r))
    if total <= 0:
        return -1.0
    return total ** (12.0 / len(r)) - 1.0


def _sharpe(r: np.ndarray) -> float:
    if len(r) < 2:
        return float("nan")
    sd = float(np.std(r, ddof=1))
    return float(np.mean(r)) / sd * math.sqrt(12.0) if sd > 0 else float("nan")


def cscv(mat: pd.DataFrame, metric: str = "ann") -> dict:
    """CSCV 主循环。mat: (T×N) 月度收益，列 = 臂。返回 PBO 与 λ 分布。"""
    T, N = mat.shape
    S = N_BLOCKS
    if T % S != 0:
        raise ValueError(f"T={T} 不能被 S={S} 整除")
    block = T // S
    R = mat.to_numpy(dtype=float)

    def _metric(r: np.ndarray) -> float:
        return _ann_return(r) if metric == "ann" else _sharpe(r)

    lambdas = []
    omegas = []
    combos = list(itertools.combinations(range(S), S // 2))
    for train_blocks in combos:
        train_rows = np.concatenate([np.arange(b * block, (b + 1) * block)
                                     for b in train_blocks])
        test_rows = np.setdiff1d(np.arange(T), train_rows)
        is_perf = np.array([_metric(R[train_rows, j]) for j in range(N)])
        best = int(np.argmax(is_perf))
        oos_perf = np.array([_metric(R[test_rows, j]) for j in range(N)])
        # OOS 相对秩 ∈ (0,1]：1 = IS 最优臂 OOS 最好
        order = np.argsort(oos_perf)
        rank = int(np.where(order == best)[0][0]) + 1
        omega = rank / N
        omegas.append(omega)
        omega = min(max(omega, 1e-6), 1 - 1e-6)
        lambdas.append(math.log(omega / (1 - omega)))
    lambdas = np.array(lambdas)
    pbo = float((lambdas < 0).mean())
    return {"pbo": pbo, "n_combos": len(lambdas),
            "lambda_mean": float(lambdas.mean()),
            "lambda_p10": float(np.percentile(lambdas, 10)),
            "lambda_p90": float(np.percentile(lambdas, 90)),
            "omega_below_median": float((np.array(omegas) <= 0.5).mean())}


def verdict_of(pbo: float) -> str:
    lo, hi = PBO_BANDS
    if pbo < lo:
        return "HEALTHY(选择过程健康)"
    if pbo <= hi:
        return "PARTIAL(部分过拟合, forward 预期应再打折)"
    return "FALSE(按 IS 挑最优不携带 OOS 信息 ⇒ 未来构型实验须带 OOS 延迟确认期)"


def run(dry_run: bool = False) -> dict:
    conn = db.get_conn()
    try:
        _, rets = load_monthly_matrix(conn)
    finally:
        conn.close()
    log.info("全史月频臂 %d 个(115 点窗口)", len(rets))
    kept, dropped = dedupe_arms(rets)
    log.info("去重后保留 %d 个真实构型臂(近重复 corr>=%.4f 丢弃 %d 个)",
             len(kept), DEDUP_CORR, len(dropped))
    if dry_run:
        for k in kept:
            print("  KEEP", k)
        return {"kept": kept, "dropped": dropped}
    mat = pd.DataFrame({k: rets[k] for k in kept}).dropna(axis=1, how="any")
    # 任何臂缺月都会让矩阵不齐——去重后仍要求所有臂共享同一月索引
    assert mat.shape[0] == FULL_POINTS - 1, mat.shape
    # T=114 不被 S=16 整除(执行细节, 判据不变): 截取最近 112 个月(丢最早 2 个月)
    # ⇒ S=16 / block=7 / C(16,8)=12870 保持冻结设计
    mat = mat.iloc[-(N_BLOCKS * ((FULL_POINTS - 1) // N_BLOCKS)):]
    main = cscv(mat, metric="ann")
    sens = cscv(mat, metric="sharpe")
    out = {
        "asof": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "criteria": {"window": [str(FULL_WINDOW[0].date()), str(FULL_WINDOW[1].date())],
                     "n_points": FULL_POINTS, "dedup_corr": DEDUP_CORR,
                     "n_blocks": N_BLOCKS, "is_metric": "annualized-geometric(sys_nav)",
                     "axis": "sys_nav monthly returns",
                     "trim_note": "T=114→112(丢最早2个月)以整除 S=16"},
        "n_arms_raw": len(rets), "n_arms_kept": len(kept),
        "n_arms_dropped": len(dropped),
        "kept_arms": kept, "dropped_arms": dropped,
        "pbo_main_ann": main, "pbo_sensitivity_sharpe": sens,
        "verdict": verdict_of(main["pbo"]),
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("PBO=%.3f (%s) | 敏感性(Sharpe 口径)=%.3f → %s",
             main["pbo"], verdict_of(main["pbo"]), sens["pbo"], OUT_PATH.name)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    res = run(dry_run=args.dry_run)
    if not args.dry_run:
        print(json.dumps({k: v for k, v in res.items()
                          if k in ("n_arms_raw", "n_arms_kept", "n_arms_dropped",
                                   "pbo_main_ann", "pbo_sensitivity_sharpe", "verdict")},
                         ensure_ascii=False, indent=1))
