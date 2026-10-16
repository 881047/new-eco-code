# -*- coding: utf-8 -*-
"""26+2 vs 26 文本因子增量 A/B 训练体 —— `plans/txt2-excl-ab-prereg-20260927.md` 的执行体。

对照 = excl_min_sa/sb（26 因子，今训已验证 = 现生产逐位复现，excl_pool_train 产物）。
处理 = txt2_trt_sa/sb：26 因子面板（与 excl_min 同源同构）+ text_n1m + text_novel3m
（文本线 factors_v1.parquet 留档原值 → reindex 月锚+参考宇宙 → fe.neutralize 生产写入口径，
稀疏守卫沿 nf 先例）= 28 因子。双种子集 sa/sb 同日同进程，参数与 excl_ctrl 逐字一致。
隔离表 txt2_trt_*/txt2imp_*，生产表只读。判决由 portfolio_admission_gate.py 另做
（--ctrl excl_min --cand txt2_trt --tag txt2pead）。产物 experiments/txt2_excl_train_20260927.json。
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app import db
from app.factors import factor_engine as fe
from app.models import expert as ex
from app.models import trainer as T

ROOT = Path(__file__).resolve().parents[2]
RETEST = ROOT / "experiments" / "single_factor_retest_s5_20260927.json"
FACTORS_V1 = ROOT / "data" / "text_corpus" / "factors_v1.parquet"
OUT = ROOT / "experiments" / "txt2_excl_train_20260927.json"
PREREG = "plans/txt2-excl-ab-prereg-20260927.md"
SEED_SETS = {"sa": [42, 7, 123, 2024], "sb": [5, 11, 77, 999]}
TEXT_FACTORS = ["text_n1m", "text_novel3m"]

IMPORTANCE_DDL = """
CREATE TABLE IF NOT EXISTS {t} (
    train_date TEXT, factor TEXT, importance REAL,
    PRIMARY KEY (train_date, factor)
) WITHOUT ROWID
"""


def ensure_importance_table(name: str) -> None:
    import re
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError(f"非法表名: {name}")
    conn = db.get_conn()
    try:
        conn.execute(IMPORTANCE_DDL.format(t=name))
        conn.commit()
    finally:
        conn.close()


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def build_txt2_dict(ref_wide) -> tuple:
    """→ (28 因子面板 dict, audit)。26 = 31 − S5 判否 5（与 excl_min 同源同构）；
    +text_n1m/text_novel3m（factors_v1 原值 → reindex → neutralize 稀疏守卫）。"""
    d = json.loads(RETEST.read_text(encoding="utf-8"))
    pool_rows = [r for r in d["pool"]["rows"] if r.get("s1_head") is not None]
    s5_flipped = sorted(r["factor"] for r in pool_rows if r.get("s5_half_life") is False)
    all31 = sorted(set(ref_wide.columns.get_level_values("factor")))
    keep26 = sorted(set(all31) - set(s5_flipped))
    wide_r = ref_wide.reorder_levels(["factor", "ts_code"], axis=1).sort_index(axis=1)
    if len(keep26) != 26:
        raise SystemExit(f"26 因子基座派生异常: {len(keep26)}")

    fdict = {}
    for f in keep26:
        sub = wide_r.xs(f, axis=1, level="factor")
        sub.index = pd.to_datetime(sub.index)
        fdict[f] = sub

    # 文本面板: 长表 → 宽 → reindex 参考宇宙(锚点×股票) → 中性化(稀疏守卫)
    long = pd.read_parquet(FACTORS_V1)
    long = long[long["factor"].isin(TEXT_FACTORS)]
    ref_index = pd.to_datetime(ref_wide.index)
    ref_codes = sorted(set(ref_wide.columns.get_level_values("ts_code")))
    for f in TEXT_FACTORS:
        sub = long[long["factor"] == f].pivot_table(
            index="trade_date", columns="ts_code", values="value", aggfunc="last")
        sub.index = pd.to_datetime(sub.index)
        sub = sub.reindex(index=ref_index, columns=ref_codes)
        nn_before = int(sub.notna().to_numpy().sum())
        sr = float(fe.sparse_ratio(sub))
        if sr < fe.SPARSE_NONZERO_RATIO:
            sub = fe.neutralize_sparse(sub, industry_map)
            mode = "neutralize_sparse(F35)"
        else:
            sub = fe.neutralize(sub, industry_map)
            mode = "neutralize"
        fdict[f] = sub
        log(f"  文本因子 {f}: 稀疏比 {sr:.4f} ⇒ {mode}")
    audit = {"n_base": len(keep26), "s5_flipped": s5_flipped, "n_total": len(fdict)}
    if len(fdict) != 28:
        raise SystemExit(f"处理臂因子数 {len(fdict)} ≠ 28, 中止核对")
    return fdict, audit


industry_map = None


def main() -> int:
    global industry_map
    t0 = time.time()
    db.init_db()
    ref_wide = T._load_factors_wide()
    with db.conn_ctx() as conn:
        industry_map = fe.industry_map(conn)
    fdict, audit = build_txt2_dict(ref_wide)
    log(f"处理臂 28 因子就绪")

    runtimes = {}
    for sname, seeds in SEED_SETS.items():
        table = f"txt2_trt_{sname}"
        ex.ensure_score_table(table)
        ensure_importance_table(f"txt2imp_{sname}")
        log(f"开始 {table}（因子 {len(fdict)} 个, 种子 {seeds}）")
        t1 = time.time()
        T.run(train_months=36, backtest_start="2017-01-01", full=True,
              score_table=table, importance_table=f"txt2imp_{sname}",
              archive_dir=ROOT / "data" / "models" / "control" / table,
              num_boost_round=T.ENSEMBLE_ROUNDS, early_stopping_rounds=0,
              ensemble_seeds=seeds, holdout_recent=6,
              label_neutral="industry", train_start=None,
              factors_override=fdict)
        runtimes[table] = round(time.time() - t1, 1)
        log(f"{table} 完成, 用时 {runtimes[table]:.0f}s")

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"), "prereg": PREREG,
           "runtime_sec": round(time.time() - t0, 1), "arm_runtimes_sec": runtimes,
           "audit": audit,
           "control": "excl_min_sa/sb（excl_pool_train 产物, 26 因子, prod 逐位复现现生产）",
           "next": "portfolio_admission_gate.py --ctrl excl_min --cand txt2_trt --tag txt2pead"}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"产物: {OUT}  ({time.time() - t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
