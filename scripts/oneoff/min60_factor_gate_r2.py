# -*- coding: utf-8 -*-
"""min60 异构口粮 · 变换族门禁轮 r2 —— `plans/min60-gate-r2-prereg-20260926.md` 的执行体。

数据: `data/min60_derived/daily.parquet`（x1=尾盘成交额占比, x2=尾盘收益,
x3=收盘对日VWAP-1; 覆盖 2020-01-02~2026-09-16, 全市场 5,444 只——预注册 §一 审计）。
净化(r4 冻结阈值): x1∉[0,1]→NaN, |x2|>0.21→NaN, |x3|>0.15→NaN。

12 条变换面板（冻结清单见预注册 §二）经 Factory → ZooProposer(panels) → gate 六判据
→ alpha_bank。判据与出口声明全在预注册: PASS=POOLED=资格非可用; **0 PASS 是基准预期**;
任何 PASS 的组合层判决另立预注册（第一问固定 = 增量 vs rev_5/size）。
只读生产表; 产物 experiments/min60_factor_gate_r2_20260926.json。
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app.alpha.factory import Factory, PreRegistration
from app.alpha.factory import ZooProposer
from app.logger import setup_logger

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "data" / "min60_derived" / "daily.parquet"
OUT = ROOT / "experiments" / "min60_factor_gate_r2_20260926.json"
SAN_X1, SAN_X2, SAN_X3 = (0.0, 1.0), 0.21, 0.15        # 预注册 §二 冻结


def main() -> int:
    setup_logger()
    t0 = time.time()
    d = pd.read_parquet(SRC)
    print(f"源面板 {len(d):,} 行 × {d['ts_code'].nunique()} 只 "
          f"({d['date'].min()}~{d['date'].max()})", flush=True)
    d["x1"] = d["x1"].where(d["x1"].between(*SAN_X1))
    d["x2"] = d["x2"].where(d["x2"].abs() <= SAN_X2)
    d["x3"] = d["x3"].where(d["x3"].abs() <= SAN_X3)
    piv = {c: d.pivot(index="date", columns="ts_code", values=c) for c in ("x1", "x2", "x3")}
    for c in piv:
        piv[c].index = pd.to_datetime(piv[c].index).sort_values()
    del d

    x1, x2, x3 = piv["x1"], piv["x2"], piv["x3"]
    roll = lambda df, n, how: getattr(df.rolling(n), how)()
    panels = {
        "m60_lvs20": roll(x1, 20, "mean"),
        "m60_lvs_chg": roll(x1, 5, "mean") - roll(x1, 20, "mean"),
        "m60_lr5": roll(x2, 5, "sum"),
        "m60_lr10": roll(x2, 10, "sum"),
        "m60_lr20": roll(x2, 20, "sum"),
        "m60_lr5n": -roll(x2, 5, "sum"),
        "m60_lr20n": -roll(x2, 20, "sum"),
        "m60_cv5": roll(x3, 5, "mean"),
        "m60_cv10": roll(x3, 10, "mean"),
        "m60_cv20": roll(x3, 20, "mean"),
        "m60_cv5n": -roll(x3, 5, "mean"),
        "m60_lvsxlr": roll(x1, 20, "mean") * roll(x2, 5, "sum"),
    }
    assert len(panels) == 12, len(panels)
    panels = {k: v.dropna(how="all") for k, v in panels.items()}
    for k, v in panels.items():
        print(f"  {k:12s} {v.shape}  非空行 {int(v.notna().any(axis=1).sum())}", flush=True)

    intent = ("min60 异构口粮变换族 r2: 触发条件① 正式行使, 12 条冻结变换过 gate 六判据; "
              "先验 = 日值基列反向显著且低于/贴 head 门槛 ⇒ 0 PASS 为基准预期; "
              "成功定义 = 全部求值入册 + 判决如实登记 (plans/min60-gate-r2-prereg-20260926.md §三)")
    f = Factory(PreRegistration(intent=intent))
    f.add(ZooProposer(label="min60", panels=panels,
                      panel_meta={k: {"info_source": "min60", "source": str(SRC),
                                      "name": k} for k in panels}))
    denom = f.declare()
    print(f"[预注册] 分母 {denom}", flush=True)

    records = f.run(save=True)
    recs = records.replace({np.nan: None}).to_dict("records") if len(records) else []
    n_pass = sum(1 for r in recs if r.get("PASS"))
    out = {
        "ran_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "prereg": "plans/min60-gate-r2-prereg-20260926.md",
        "denominator": denom, "n_records": len(recs), "n_pass": n_pass,
        "n_duplicates": len(f.duplicates), "duplicates": f.duplicates,
        "sanity": {"x1": SAN_X1, "x2": SAN_X2, "x3": SAN_X3},
        "records": recs,
        "exit_note": "PASS=POOLED=资格非可用; 0 PASS ⇒ min60 变换族结清(原始列已结清)",
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    cols = [c for c in ("alpha_id", "DEAD_head", "DEAD_head_t", "ACTIVE_head",
                        "p_adj", "PASS") if records is not None and len(records)
            and c in records.columns]
    print(f"\n产物: {OUT}  ({time.time() - t0:.0f}s)")
    if len(records):
        print(records[cols].to_string(index=False))
    print(f"[读数] 求值 {len(recs)} | PASS {n_pass} | 撞车 {len(f.duplicates)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
