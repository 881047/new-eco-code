# -*- coding: utf-8 -*-
"""剂量稳健性补验 —— `plans/text-factor-llm-labeling-prereg-20260927.md` §十一 的执行体。

六次全量重训：D1=31+text_n1m、D2=31+text_novel3m（各 × SA）、31 × SC=[3,17,53,97]、
31+2 × SC。剂量判据：D1/D2 prod Δsys_ann ≥ 0 且回撤劣化 <2pp；31+2 在 SC 上
Δsys_ann_prod > 0。全部 PASS ⇒ 采纳包。产物 experiments/text_factors_dose_20260927.json。
"""
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from app import config as cfg_mod
from app import db
from app.backtest import engine as E
from app.factors import factor_engine as fe
from app.models import expert as ex
from app.models import trainer as T

ROOT = Path(__file__).resolve().parents[2]
FV1 = ROOT / "data" / "text_corpus" / "factors_v1.parquet"
OUT = ROOT / "experiments" / "text_factors_dose_20260927.json"
PREREG = "plans/text-factor-llm-labeling-prereg-20260927.md"

SC = [3, 17, 53, 97]
SA = [42, 7, 123, 2024]


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def ensure_importance_table(name: str) -> None:
    import re
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError(f"非法表名: {name}")
    conn = db.get_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS {t} (train_date TEXT, factor TEXT, "
            "importance REAL, PRIMARY KEY (train_date, factor)) WITHOUT ROWID".format(t=name))
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    t0 = time.time()
    db.init_db()
    ref_wide = T._load_factors_wide()
    wide_r = ref_wide.reorder_levels(["factor", "ts_code"], axis=1).sort_index(axis=1)
    ref_index = pd.to_datetime(ref_wide.index)
    ref_codes = sorted(set(ref_wide.columns.get_level_values("ts_code")))

    long = pd.read_parquet(FV1)
    text_panels = {}
    for name in ("text_n1m", "text_novel3m"):
        sub = long[long["factor"] == name]
        pw = sub.pivot_table(index="trade_date", columns="ts_code",
                             values="value", aggfunc="last")
        pw.index = pd.to_datetime(pw.index)
        text_panels[name] = pw.reindex(index=ref_index, columns=ref_codes)

    # 6 个训练构型: (表名, 附加文本因子列表, 种子集)
    configs = [
        ("tfab_d1_sa", ["text_n1m"], SA),
        ("tfab_d1_sc", ["text_n1m"], SC),
        ("tfab_d2_sa", ["text_novel3m"], SA),
        ("tfab_d2_sc", ["text_novel3m"], SC),
        ("tfab_base_sc", [], SC),
        ("tfab_p2_sc", ["text_n1m", "text_novel3m"], SC),
    ]
    runtimes = {}
    for table, extra, seeds in configs:
        trt = {}
        wide_r = ref_wide.reorder_levels(["factor", "ts_code"], axis=1).sort_index(axis=1)
        for f in sorted(set(ref_wide.columns.get_level_values("factor"))):
            pp = wide_r.xs(f, axis=1, level="factor")
            pp.index = pd.to_datetime(pp.index)
            trt[f] = pp
        for name in extra:
            trt[name] = text_panels[name]
        ex.ensure_score_table(table)
        ensure_importance_table(f"tfab_imp_{table}")
        log(f"开始 {table}（因子 {len(trt)}，种子 {seeds}）")
        t1 = time.time()
        T.run(train_months=36, backtest_start="2017-01-01", full=True,
              score_table=table, importance_table=f"tfab_imp_{table}",
              archive_dir=ROOT / "data" / "models" / "control" / table,
              num_boost_round=T.ENSEMBLE_ROUNDS, early_stopping_rounds=0,
              ensemble_seeds=seeds, holdout_recent=6,
              label_neutral="industry", train_start=None,
              factors_override=trt)
        runtimes[table] = round(time.time() - t1, 1)
        log(f"{table} 完成 {runtimes[table]:.0f}s")

    # ---------- 组合层读数（prod/full 双窗） ----------
    eval_start = cfg_mod.load_config().get("eval_start")
    runs = {}
    for table, _extra, _seeds in configs:
        for wname, start in (("prod", eval_start), ("full", None)):
            mm = E.run_backtest(score_table=table, run_tag=f"tfab_{table}_{wname}",
                                start=start)
            runs[f"{table}_{wname}"] = {
                "sys_ann_return": round(float(mm["sys_ann_return"]), 4),
                "sys_max_drawdown": round(float(mm["sys_max_drawdown"]), 4),
                "avg_turnover": round(float(mm["avg_turnover"]), 4),
                "periods": int(mm["periods"])}
            log(f"{table}_{wname}: sys_ann {mm['sys_ann_return']:+.2%}, "
                f"mdd {mm['sys_max_drawdown']:+.2%}")

    # 对照: 31×SA 复用 nfab2_ctrl_sa（同日同环境已训）；31×SC = tfab_base_sc
    def delta(table, base_table):
        d = {}
        for w in ("prod", "full"):
            d[f"sys_ann_{w}"] = round(runs[f"{table}_{w}"]["sys_ann_return"]
                                      - runs[f"{base_table}_{w}"]["sys_ann_return"], 4)
            d[f"sys_mdd_{w}"] = round(runs[f"{table}_{w}"]["sys_max_drawdown"]
                                      - runs[f"{base_table}_{w}"]["sys_max_drawdown"], 4)
        d["turnover"] = round(runs[f"{table}_prod"]["avg_turnover"]
                              - runs[f"{base_table}_prod"]["avg_turnover"], 4)
        return d

    deltas = {"d1_vs_31sa": delta("tfab_d1_sa", "nfab2_ctrl_sa"),
              "d2_vs_31sa": delta("tfab_d2_sa", "nfab2_ctrl_sa"),
              "p2_vs_31sc": delta("tfab_p2_sc", "tfab_base_sc")}
    dose_ok = (deltas["d1_vs_31sa"]["sys_ann_prod"] >= 0
               and deltas["d1_vs_31sa"]["sys_mdd_prod"] > -0.02
               and deltas["d2_vs_31sa"]["sys_ann_prod"] >= 0
               and deltas["d2_vs_31sa"]["sys_mdd_prod"] > -0.02)
    sc_ok = deltas["p2_vs_31sc"]["sys_ann_prod"] > 0
    adopt_package = bool(dose_ok and sc_ok)
    out = {"ran_at": datetime.now().isoformat(timespec="seconds"), "prereg": PREREG,
           "runtime_sec": round(time.time() - t0, 1), "arm_runtimes": runtimes,
           "runs": runs, "deltas": deltas,
           "dose_ok": dose_ok, "sc_third_seed_ok": sc_ok,
           "ADOPT_PACKAGE": adopt_package,
           "closure": ("剂量稳健 + 第三种子方向确认 ⇒ 采纳包成立（工程落地另立预注册）"
                       if adopt_package else "剂量/第三种子不达标 ⇒ 采纳撤回，线结清")}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    log(f"剂量判据: dose_ok {dose_ok} | sc_ok {sc_ok} | ADOPT_PACKAGE {adopt_package}")
    log(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
