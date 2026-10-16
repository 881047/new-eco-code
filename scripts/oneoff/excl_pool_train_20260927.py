# -*- coding: utf-8 -*-
"""池剔除 A/B 训练体 —— `plans/excl-pool-ab-prereg-20260927.md` 的执行体。

六臂同进程同日同环境全量重训（参数与 nf 先例 fullretrain_nf_ab_20260926.py 逐字一致）:
  excl_ctrl_{sa,sb} = 31 现行（factor 表原路径, factors_override=None）
  excl_min_{sa,sb}  = 31 − {S5 官方翻除的池成员}（机械读 single_factor_retest_s5_20260927.json）
  excl_gate_{sa,sb} = 仅 S1-S5 过档池成员（同上机械读）
双种子集 sa=[42,7,123,2024]/sb=[5,11,77,999]；B 臂走 factors_override=子集面板
（面板取自 factor 表宽表、已中性化、不再处理——与 nf 臂"8 保留"同法）。
隔离表 excl_*/exclimp_*，模型归档 data/models/control/excl_*；生产表全程只读。
训练完成后由 portfolio_admission_gate.py --ctrl excl_ctrl --cand excl_min/excl_gate
做组合层判决（另立 padm_ 产物）。产物 experiments/excl_pool_train_20260927.json。
"""
import json
import sqlite3
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
OUT = ROOT / "experiments" / "excl_pool_train_20260927.json"
PREREG = "plans/excl-pool-ab-prereg-20260927.md"
SEED_SETS = {"sa": [42, 7, 123, 2024], "sb": [5, 11, 77, 999]}

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


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def build_arms(ref_wide) -> tuple[dict, dict, dict]:
    """→ (min_dict, gate_dict, audit)。机械派生自官方 S1-S5 重测产物, 不手工挑拣。"""
    d = json.loads(RETEST.read_text(encoding="utf-8"))
    pool_rows = [r for r in d["pool"]["rows"] if r.get("s1_head") is not None]
    s5_flipped = sorted(r["factor"] for r in pool_rows if r.get("s5_half_life") is False)
    gate_keep = sorted(r["factor"] for r in pool_rows if r.get("PASS_single"))
    all31 = sorted(set(ref_wide.columns.get_level_values("factor")))
    min_keep = sorted(set(all31) - set(s5_flipped))
    wide_r = ref_wide.reorder_levels(["factor", "ts_code"], axis=1).sort_index(axis=1)

    def panels(names: list[str]) -> dict:
        out = {}
        for f in names:
            p = wide_r.xs(f, axis=1, level="factor")
            p.index = pd.to_datetime(p.index)
            out[f] = p
        return out

    audit = {"all31": all31, "s5_flipped": s5_flipped, "gate_keep": gate_keep,
             "min_keep_n": len(min_keep), "gate_keep_n": len(gate_keep)}
    log(f"臂派生: 31 → min {len(min_keep)}（翻除 {s5_flipped}）/ gate {len(gate_keep)}")
    if len(min_keep) != 26 or len(gate_keep) != 6:
        raise SystemExit(f"臂尺寸与预注册不符: min={len(min_keep)}(期望26) "
                         f"gate={len(gate_keep)}(期望6), 中止核对")
    return panels(min_keep), panels(gate_keep), audit


def main() -> int:
    t_start = time.time()
    db.init_db()
    ref_wide = T._load_factors_wide()
    log(f"参考宽表: {ref_wide.shape[0]} 期 × {ref_wide.shape[1]} 列")
    min_dict, gate_dict, audit = build_arms(ref_wide)

    runtimes = {}
    for arm, fdict in (("ctrl", None), ("min", min_dict), ("gate", gate_dict)):
        for sname, seeds in SEED_SETS.items():
            table = f"excl_{arm}_{sname}"
            ex.ensure_score_table(table)
            ensure_importance_table(f"exclimp_{arm}_{sname}")
            log(f"开始 {table}（因子 {31 if fdict is None else len(fdict)} 个, 种子 {seeds}）")
            t0 = time.time()
            T.run(train_months=36, backtest_start="2017-01-01", full=True,
                  score_table=table, importance_table=f"exclimp_{arm}_{sname}",
                  archive_dir=ROOT / "data" / "models" / "control" / table,
                  num_boost_round=T.ENSEMBLE_ROUNDS, early_stopping_rounds=0,
                  ensemble_seeds=seeds, holdout_recent=6,
                  label_neutral="industry", train_start=None,
                  factors_override=fdict)
            runtimes[table] = round(time.time() - t0, 1)
            log(f"{table} 完成, 用时 {runtimes[table]:.0f}s")

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"),
           "prereg": PREREG, "runtime_sec": round(time.time() - t_start, 1),
           "arm_runtimes_sec": runtimes, "audit": audit,
           "next": "portfolio_admission_gate.py --ctrl excl_ctrl --cand excl_min --tag exclmin"
                   " / --cand excl_gate --tag exclgate"}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"产物: {OUT}  ({time.time() - t_start:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
