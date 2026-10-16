# -*- coding: utf-8 -*-
"""差距瀑布分解 —— "量化程序差在哪里"的机械回答（2026-10-02，用户指令"继续审查分析"）。

只读零写库。把"程序实现的超额"与"模型 α 本体"之间的落差逐层量化：

  L0 原始 α 本体      = top6(按 model_score, 不做任何域过滤) 21 日前瞻收益 − 全截面均值
  L1 PIT 域后 α       = top6(tradable_mask 域内) − 域内均值          ⇒ L0−L1 = 域过滤损耗
  L2 回测系统线超额   = sys_ann − bench_ann（backtest_summary 现役 pin 读数）
                      ⇒ L1−L2 = 实现损耗, 其中可量化子项:
                         · 现金闲置拖累 ≈ avg_cash_idle × (sys_ann − 现金收益 2%)
                         · 交易成本     ≈ avg_turnover × 12 × 2 × 0.003（单边月换手×双边费率）
                         · 其余 = 执行/口径差(t+1 开盘成交 vs 月末收盘前瞻、持仓窗 vs 21 日、整手)
  L3 纸面实盘         = paper_nav 实现收益 − 同窗 bench_nav（窗口过短只报累计, 不年化）

判读纪律：每层损耗给的是**量级归因**不是精确会计（L0/L1 用 21 日前瞻口径, L2 是月频
持有实现口径, 两者持仓窗不同）——结论只取"哪层漏得最多"这一级排序。
产物 experiments/gap_waterfall_20261002.json。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app import config as cfg_mod
from app import db
from app.factors.factor_engine import load_market, month_end_dates
from app.models import trainer
from app.pit.engine import PITEngine

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "gap_waterfall_20261002.json"
WINDOWS = {"prod": "20190731", "full": "20170126"}
TOPN = 6
CASH_RATE = 0.02          # 逆回购/货币口径, 拖累量级用
HORIZON = 21


def main() -> int:
    cfg = cfg_mod.load_config()
    excl = set(cfg.get("factor_exclusions") or [])
    wide = trainer._apply_exclusions(trainer._load_factors_wide(), excl)
    m = load_market()
    ac = m["close"] * m["adj"]
    fwd = trainer.forward_return(ac, HORIZON)
    pit = PITEngine(m, cfg=cfg)
    # 基准同窗前瞻(与 fwd 同一交易日轴按位置对齐): L1_index = top6 − 中证800 21 日
    # 前瞻收益, 与回测满仓超额同基准 ⇒ 瀑布逐层可比(截面等权池 ≠ 市值加权指数,
    # 两者之差在小盘年份可达数 pp, 不可混用)。
    bench = m.get("bench")
    td_index = pd.DatetimeIndex(m["close"].index)
    bench_pos = {d: i for i, d in enumerate(td_index)}

    conn = db.get_conn()
    try:
        ms = pd.read_sql("SELECT trade_date, ts_code, score FROM model_score", conn)
        summary = {}
        for run in ("20260928_exe", "20260928_exe_full"):
            row = pd.read_sql("SELECT metrics FROM backtest_summary WHERE run_date=?",
                              conn, params=(run,))
            summary[run] = json.loads(row["metrics"].iloc[0]) if not row.empty else {}
    finally:
        conn.close()
    if ms.empty:
        raise RuntimeError("model_score 为空")
    ms["d"] = pd.to_datetime(ms["trade_date"], format="%Y%m%d")

    out = {"asof": datetime.now().isoformat(timespec="seconds"),
           "note": "量级归因非精确会计; L0/L1=21 日前瞻口径, L2=月频持有实现口径",
           "windows": {}}

    for wname, start in WINDOWS.items():
        md = month_end_dates(pd.DatetimeIndex(m["close"].index))
        anchors = [d for d in md if pd.Timestamp(start) <= d <= pd.Timestamp("2026-08-31")]
        rows = []
        for d in anchors:
            g = ms[ms["d"] == d].dropna(subset=["score"])
            if len(g) < 100 or d not in fwd.index:
                continue
            f = fwd.loc[d].dropna().rename("fwd")
            ok = pit.tradable_mask(d, allow_bse=False).reindex(g["ts_code"]) \
                .fillna(False).astype(bool)
            sc = g.set_index("ts_code")["score"].reindex(f.index)
            pair = pd.concat([sc, f], axis=1).dropna()
            if len(pair) < 100:
                continue
            pool_all = float(pair.iloc[:, 1].mean())
            top6_raw = pair.sort_values("score", ascending=False).head(TOPN)
            l0 = float(top6_raw["fwd"].mean() - pool_all)
            in_dom = pair[pair.index.isin(ok[ok].index)]
            if len(in_dom) < 50:
                continue
            pool_dom = float(in_dom.iloc[:, 1].mean())
            top6_dom = in_dom.sort_values("score", ascending=False).head(TOPN)
            l1 = float(top6_dom["fwd"].mean() - pool_dom)
            n_dom6 = len(top6_dom)
            l1_idx = None
            if bench is not None:
                i = bench_pos.get(d)
                if i is not None and i + HORIZON < len(bench):
                    b_fwd = float(bench.iloc[i + HORIZON] / bench.iloc[i] - 1.0)
                    l1_idx = float(top6_dom["fwd"].mean() - b_fwd)
            rows.append({"date": d.strftime("%Y%m%d"), "l0": l0, "l1": l1,
                         "l1_idx": l1_idx,
                         "n_scored": int(len(pair)), "n_dom": int(len(in_dom)),
                         "n_dom_top6": int(n_dom6)})
        dfm = pd.DataFrame(rows)
        met = summary.get("20260928_exe" if wname == "prod" else "20260928_exe_full", {})
        l0_ann = float(dfm["l0"].mean() * 12)
        l1_ann = float(dfm["l1"].mean() * 12)
        l1_idx_ann = float(dfm["l1_idx"].dropna().mean() * 12) \
            if dfm["l1_idx"].notna().any() else None
        sys_ann = met.get("sys_ann_return")
        bench_ann = met.get("bench_ann")
        full_ann = met.get("ann_return")              # 满仓线
        l2 = (sys_ann - bench_ann) if sys_ann and bench_ann else None
        full_excess = (full_ann - bench_ann) if full_ann and bench_ann else None
        idle = met.get("avg_cash_idle")
        to = met.get("avg_turnover")
        cost_drag = to * 12 * met.get("cost", 0.003) if to else None
        out["windows"][wname] = {
            "n_anchors": int(len(dfm)),
            "L0_raw_alpha_vs_pool_ann": round(l0_ann, 4),
            "L1_domain_alpha_vs_pool_ann": round(l1_ann, 4),
            "domain_filter_loss": round(l0_ann - l1_ann, 4),
            "L1_index_alpha_ann": round(l1_idx_ann, 4) if l1_idx_ann is not None else None,
            "full_deploy_excess_ann": round(full_excess, 4) if full_excess else None,
            "machinery_loss_ideal_to_full": round(l1_idx_ann - full_excess, 4)
                if l1_idx_ann is not None and full_excess else None,
            "sys_excess_ann": round(l2, 4) if l2 else None,
            "cash_buffer_discount_full_to_sys": round(full_excess - l2, 4)
                if full_excess and l2 else None,
            "engine_metrics": {"avg_cash_idle": idle, "avg_stocks": met.get("avg_stocks"),
                               "avg_turnover": to, "cost": met.get("cost"),
                               "implied_cost_drag_ann": round(cost_drag, 4)
                               if cost_drag else None,
                               "ic_mean": met.get("ic_mean"), "icir": met.get("icir")},
            "monthly": dfm.to_dict("records"),
        }
        print(f"[{wname}] L1_idx={l1_idx_ann:+.1%} 满仓超额={full_excess:+.1%} "
              f"机械损耗={l1_idx_ann-full_excess:+.1%} | 系统超额={l2:+.1%} "
              f"现金缓冲折扣={full_excess-l2:+.1%} | 域过滤={l0_ann-l1_ann:+.1%} "
              f"成本拖累(参考)={cost_drag:+.1%}")

    # L3 纸面实盘(窗口过短, 只报累计)
    conn = db.get_conn()
    try:
        nav = pd.read_sql("SELECT trade_date, nav, bench_nav FROM paper_nav "
                          "ORDER BY trade_date", conn)
    finally:
        conn.close()
    if len(nav) >= 2:
        r_nav = float(nav["nav"].iloc[-1] / nav["nav"].iloc[0] - 1)
        b0 = nav["bench_nav"].dropna()
        r_bench = float(nav["bench_nav"].iloc[-1] / nav["bench_nav"].iloc[0] - 1) \
            if nav["bench_nav"].iloc[0] else None
        out["paper"] = {"window": [str(nav["trade_date"].iloc[0]),
                                   str(nav["trade_date"].iloc[-1])],
                        "n_days": int(len(nav)),
                        "nav_ret": round(r_nav, 4),
                        "bench_ret": round(r_bench, 4) if r_bench is not None else None,
                        "excess": round(r_nav - r_bench, 4)
                        if r_bench is not None else None,
                        "note": "12 个交易日, 量级不做年化推断"}
        print(f"[paper] {out['paper']['window']} ret={r_nav:+.2%} "
              f"bench={r_bench:+.2%} excess={r_nav - r_bench:+.2%}")

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
