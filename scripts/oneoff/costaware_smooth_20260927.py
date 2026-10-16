# -*- coding: utf-8 -*-
"""成本感知评分平滑（λ-收缩）A/B —— `plans/costaware-smooth-prereg-20260927.md` 的执行体。

机制（§一 冻结）: 生产 model_score 逐月秩百分位 → S_used(t) = (1−λ)p_t + λ·S_used(t−1)
（EMA 收缩期间分数变化 = 换手正则的一阶实现），λ ∈ {0.3, 0.5} → 隔离表
slv_smooth30/slv_smooth50 → 生产引擎双窗回测（含 chase 闸，两臂同配置）。
判据（§二 冻结）: P1 换手 ≤ 对照×0.80（降 ≥20%）/ P2 excess_ann ≥ 对照−1.5pp（双窗）/
P3 MDD 劣化 ≤2pp（双窗）。判决词机械：存在 λ 全过 ⇒ PASS（进拍板，取换手最低 λ）；
否则 REJECT（线结清）。对照 = pin 20260927_excl5/_full 在册读数。
只读生产 model_score；只写自建隔离表。产物 experiments/costaware_smooth_20260927.json。
"""
import json
import sqlite3
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from app import config as cfg_mod
from app.backtest.engine import run_backtest

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "costaware_smooth_20260927.json"
PREREG = "plans/costaware-smooth-prereg-20260927.md"
LAMBDAS = {"smooth30": 0.3, "smooth50": 0.5}
TURN_CUT = 0.80          # P1: 换手 ≤ 对照 × 0.80
ANN_NONINF = -0.015      # P2: excess_ann ≥ 对照 − 1.5pp
MDD_TOL = -0.02          # P3: MDD 劣化 ≤ 2pp
WINDOWS = {"prod": True, "full": False}   # prod = eval_start 起; full = 全史

# 对照 = pin 20260927_excl5/_full 在册读数（experiments/volscale_overlay 的 base 同源,
# 这里直接读 backtest_summary 的注册口径, 避免二次计算漂移）
PIN = {"prod": {"turnover": 0.8865, "excess_ann": 0.2230, "max_drawdown": -0.2584},
       "full": {"turnover": 0.8778, "excess_ann": 0.1107, "max_drawdown": -0.4336}}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def build_smooth_table(conn, lam: float, table: str) -> int:
    """读 model_score 长表 → 逐月秩百分位 → λ-EMA → 写隔离表。返回行数。"""
    df = pd.read_sql("SELECT trade_date, ts_code, score FROM model_score ORDER BY trade_date",
                     conn)
    w = df.pivot_table(index="trade_date", columns="ts_code", values="score", aggfunc="last")
    w.index = w.index.astype(str)
    p = w.rank(axis=1, pct=True)                     # 逐月秩百分位 ∈ (0,1]
    used = p.copy()
    prev = None
    for d in used.index:
        if prev is None:
            used.loc[d] = p.loc[d]
        else:
            pv = prev.reindex(used.columns)
            # 链断(当月或上月缺行) ⇒ 以当月新分重启——保住全宇宙, 平滑只在连续月对上生效
            blend = (1 - lam) * p.loc[d] + lam * pv
            used.loc[d] = blend.where(p.loc[d].notna() & pv.notna(), p.loc[d])
        prev = used.loc[d]
    long = used.stack().rename("score").reset_index()
    long.columns = ["trade_date", "ts_code", "score"]
    long = long.dropna(subset=["score"])
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ("
                 "ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL, "
                 "PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID")
    conn.execute(f"DELETE FROM {table}")
    conn.commit()
    long.assign(pred_ret=np.nan)[["ts_code", "trade_date", "score", "pred_ret"]] \
        .to_sql(table, conn, if_exists="append", index=False)
    n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return int(n)


def main() -> int:
    t0 = time.time()
    cfg = cfg_mod.load_config()
    eval_start = cfg.get("eval_start")
    conn = sqlite3.connect(str(ROOT / "data" / "neweco.db"))
    runs = {"pin_20260927_excl5": PIN}   # 对照（在册读数, 引擎同尺）
    try:
        for tname, lam in LAMBDAS.items():
            n = build_smooth_table(conn, lam, tname)
            log(f"隔离表 {tname} (λ={lam}): {n} 行")
    finally:
        conn.close()

    for tname in LAMBDAS:
        for wname, use_start in WINDOWS.items():
            tag = f"slvsm_{tname}_{wname}"
            m = run_backtest(score_table=tname, run_tag=tag,
                             start=(eval_start if use_start else None))
            runs[f"{tname}_{wname}"] = {
                "ann_return": round(float(m["ann_return"]), 4),
                "excess_ann": round(float(m["excess_ann"]), 4),
                "max_drawdown": round(float(m["max_drawdown"]), 4),
                "avg_turnover": round(float(m["avg_turnover"]), 4),
                "ic_mean": round(float(m["ic_mean"]), 4),
                "win_rate": round(float(m["win_rate"]), 4),
                "periods": int(m["periods"])}
            log(f"  {tag:24s} excess {m['excess_ann']:+.4f} mdd {m['max_drawdown']:+.4f} "
                f"to {m['avg_turnover']:.4f} ic {m['ic_mean']:+.4f}")

    verdicts = {}
    for tname, lam in LAMBDAS.items():
        checks = {}
        for w in ("prod", "full"):
            r = runs[f"{tname}_{w}"]
            c = PIN[w]
            checks[f"p1_turnover_{w}"] = bool(r["avg_turnover"] <= c["turnover"] * TURN_CUT)
            checks[f"p2_excess_{w}"] = bool(r["excess_ann"] >= c["excess_ann"] + ANN_NONINF)
            checks[f"p3_mdd_{w}"] = bool(r["max_drawdown"] >= c["max_drawdown"] + MDD_TOL)
        passed = all(checks.values())
        verdicts[tname] = {"lambda": lam, "checks": checks, "PASS_screen": passed}
        log(f"λ={lam}: {'PASS' if passed else 'REJECT'} {checks}")

    passing = {t: v for t, v in verdicts.items() if v["PASS_screen"]}
    if passing:
        best = min(passing, key=lambda t: runs[f"{t}_prod"]["avg_turnover"])
        verdict = f"PASS（进拍板，取 {best}，换手最低）"
    else:
        verdict = "REJECT（线结清，登记勿重测）"

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"), "prereg": PREREG,
           "control_pin": "20260927_excl5", "lambdas": LAMBDAS,
           "criteria": {"p1": f"换手 ≤ 对照×{TURN_CUT}", "p2": f"excess ≥ 对照{ANN_NONINF:+.3f}",
                        "p3": f"MDD 劣化 ≤2pp"},
           "runs": runs, "verdicts": verdicts,
           "verdict": verdict,
           "note": "确定性变换无种子噪声; PASS 后生产接线(config score_smooth_lambda DEFAULT 关"
                   "+重钉)须拍板; 生产 model_score 一字未动",
           "runtime_sec": round(time.time() - t0, 1)}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"判决: {verdict}")
    print(f"产物: {OUT}  ({time.time() - t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
