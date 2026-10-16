# -*- coding: utf-8 -*-
"""31+2 文本构型 A/B 闭环 runner —— `plans/text-factor-llm-labeling-prereg-20260927.md`
§九 的执行体（复用 nf-AB 机制，不新写管线）。

四臂同进程：{A 控制 = 现行 31（factor 表），B 实验 = 33 = 31 + text_n1m + text_novel3m
（factors_v1.parquet，已按生产口径中性化）} × {SA=[42,7,123,2024], SB=[5,11,77,999]}。
两层连判：打分层（gate.evaluate，Δ DEAD head 双种子集同向 ≥+0.10pp/月）→
组合层（生产路径回测双窗 → gate.verdict_portfolio：Δsys_ann prod 双种子集同向 ≥+1.0pp/年
且 full ≥0、回撤劣化 <2pp、换手 ≤+5pp）。
产物隔离：实验表 nfab2_*/nfimp2_*，工件归档 control/，生产只读。
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

from app import config as cfg_mod
from app import db
from app.alpha import gate
from app.factors import factor_engine as fe
from app.models import expert as ex
from app.models import trainer as T
from app.pit.engine import PITEngine
from app.backtest import engine as E

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "neweco.db"
FV1 = ROOT / "data" / "text_corpus" / "factors_v1.parquet"
OUT = ROOT / "experiments" / "text_factors_ab_20260927.json"
PREREG = "plans/text-factor-llm-labeling-prereg-20260927.md"

SEED_SETS = {"sa": [42, 7, 123, 2024], "sb": [5, 11, 77, 999]}
TEXT_PICK = ["text_n1m", "text_novel3m"]

IMPORTANCE_DDL = """
CREATE TABLE IF NOT EXISTS {t} (
    train_date TEXT, factor TEXT, importance REAL,
    PRIMARY KEY (train_date, factor)
) WITHOUT ROWID
"""


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


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


def build_treatment(ref_wide: pd.DataFrame) -> dict:
    """31 在池 + 2 文本因子（factors_v1 已中性化且对齐参考宇宙）。"""
    wide_r = ref_wide.reorder_levels(["factor", "ts_code"], axis=1).sort_index(axis=1)
    ref_index = pd.to_datetime(ref_wide.index)
    ref_codes = sorted(set(ref_wide.columns.get_level_values("ts_code")))
    trt = {}
    for f in pool31(ref_wide):
        p = wide_r.xs(f, axis=1, level="factor")
        p.index = pd.to_datetime(p.index)
        trt[f] = p
    long = pd.read_parquet(FV1)
    for name in TEXT_PICK:
        sub = long[long["factor"] == name]
        pw = sub.pivot_table(index="trade_date", columns="ts_code",
                             values="value", aggfunc="last")
        pw.index = pd.to_datetime(pw.index)
        pw = pw.reindex(index=ref_index, columns=ref_codes)
        trt[name] = pw
        log(f"文本因子 {name}: 非缺失 {int(pw.notna().to_numpy().sum()):,}")
    return trt


def pool31(ref_wide: pd.DataFrame) -> list:
    return sorted(set(ref_wide.columns.get_level_values("factor")))


def main() -> int:
    t0 = time.time()
    db.init_db()
    ref_wide = T._load_factors_wide()
    with db.conn_ctx() as conn:
        industry = fe.industry_map(conn)

    trt = build_treatment(ref_wide)
    if len(trt) != 33:
        raise SystemExit(f"实验臂因子数 {len(trt)} ≠ 33（31 + 2 文本），中止")

    # ---------- 四臂全量重训 ----------
    runtimes = {}
    for arm, fdict in (("ctrl", None), ("trt", trt)):
        for sname, seeds in SEED_SETS.items():
            table = f"nfab2_{arm}_{sname}"
            ex.ensure_score_table(table)
            ensure_importance_table(f"nfimp2_{arm}_{sname}")
            log(f"开始 {table}（因子 {31 if fdict is None else len(fdict)}，种子 {seeds}）")
            t1 = time.time()
            T.run(train_months=36, backtest_start="2017-01-01", full=True,
                  score_table=table, importance_table=f"nfimp2_{arm}_{sname}",
                  archive_dir=ROOT / "data" / "models" / "control" / table,
                  num_boost_round=T.ENSEMBLE_ROUNDS, early_stopping_rounds=0,
                  ensemble_seeds=seeds, holdout_recent=6,
                  label_neutral="industry", train_start=None,
                  factors_override=fdict)
            runtimes[table] = round(time.time() - t1, 1)
            log(f"{table} 完成 {runtimes[table]:.0f}s")

    # ---------- 打分层判读 ----------
    m = fe.load_market()
    md_all = fe.month_end_dates(pd.DatetimeIndex(m["close"].index))
    md_eval = pd.DatetimeIndex(md_all[md_all >= pd.Timestamp("2019-01-01")])
    zones = gate.zone_of(md_eval)
    fwd = gate.forward_return(m, gate.HORIZON)
    tradable = PITEngine(m=m).tradable_matrix(md_eval)
    conn = sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)

    def score_judge(table: str) -> dict:
        sc = pd.read_sql(f"SELECT trade_date, ts_code, score FROM {table}", conn)
        w = sc.pivot_table(index="trade_date", columns="ts_code", values="score",
                           aggfunc="last")
        w.index = pd.to_datetime(w.index, format="%Y%m%d")
        pm = w.reindex(index=sorted(set(md_eval) & set(w.index)), columns=w.columns)
        pm = pm.where(tradable.reindex(index=md_eval, columns=pm.columns).fillna(False))
        det = gate.evaluate(pm, fwd, md_eval, zones)
        dead, act = det[det.zone == "DEAD"], det[det.zone == "ACTIVE"]
        h_d, h_a = float(dead.head_excess.mean()), float(act.head_excess.mean())
        t_d = float(h_d / (dead.head_excess.std(ddof=1) / np.sqrt(len(dead))))
        return {"dead_head": round(h_d, 4), "dead_t": round(t_d, 2),
                "active_head": round(h_a, 4), "same_sign": bool(h_d * h_a > 0),
                "dead_top6": round(float(dead.top6_excess.mean()), 4)}

    sj = {}
    for arm in ("ctrl", "trt"):
        for s in SEED_SETS:
            sj[f"{arm}_{s}"] = score_judge(f"nfab2_{arm}_{s}")
            log(f"打分层 {arm}_{s}: {sj[f'{arm}_{s}']}")
    conn.close()

    deltas = {s: round(sj[f"trt_{s}"]["dead_head"] - sj[f"ctrl_{s}"]["dead_head"], 4)
              for s in SEED_SETS}
    same_dir = all(v > 0 for v in deltas.values())
    mean_d = float(np.mean(list(deltas.values())))
    score_pass = bool(same_dir and mean_d >= 0.0010)
    log(f"打分层: Δ {deltas} | mean {mean_d:+.4f} | {'PASS' if score_pass else 'FAIL'}")

    # ---------- 组合层（§五 同款：生产路径回测双窗 → verdict_portfolio） ----------
    eval_start = cfg_mod.load_config().get("eval_start")
    runs = {}
    for arm in ("ctrl", "trt"):
        for s in SEED_SETS:
            for wname, start in (("prod", eval_start), ("full", None)):
                tag = f"nfab2_{arm}_{s}_{wname}"
                mm = E.run_backtest(score_table=f"nfab2_{arm}_{s}", run_tag=tag,
                                    start=start)
                runs[f"{arm}_{s}_{wname}"] = {
                    "sys_ann_return": float(mm["sys_ann_return"]),
                    "sys_max_drawdown": float(mm["sys_max_drawdown"]),
                    "avg_turnover": float(mm["avg_turnover"]),
                    "periods": int(mm["periods"])}
                log(f"{tag}: sys_ann {mm['sys_ann_return']:+.2%}, "
                    f"mdd {mm['sys_max_drawdown']:+.2%}, {mm['periods']} 期")

    deltas_p = []
    for s in SEED_SETS:
        deltas_p.append({
            "seed_set": s,
            "sys_ann_prod": round(runs[f"trt_{s}_prod"]["sys_ann_return"]
                                  - runs[f"ctrl_{s}_prod"]["sys_ann_return"], 4),
            "sys_ann_full": round(runs[f"trt_{s}_full"]["sys_ann_return"]
                                  - runs[f"ctrl_{s}_full"]["sys_ann_return"], 4),
            "sys_mdd_prod": round(runs[f"trt_{s}_prod"]["sys_max_drawdown"]
                                  - runs[f"ctrl_{s}_prod"]["sys_max_drawdown"], 4),
            "sys_mdd_full": round(runs[f"trt_{s}_full"]["sys_max_drawdown"]
                                  - runs[f"ctrl_{s}_full"]["sys_max_drawdown"], 4),
            "turnover": round(runs[f"trt_{s}_prod"]["avg_turnover"]
                              - runs[f"ctrl_{s}_prod"]["avg_turnover"], 4)})
    pv = gate.verdict_portfolio(deltas_p)
    log(f"组合层判决: {pv}")

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"), "prereg": PREREG,
           "runtime_sec": round(time.time() - t0, 1), "arm_runtimes": runtimes,
           "score_layer": {"readouts": sj, "deltas": deltas, "mean_delta": round(mean_d, 4),
                           "PASS": score_pass},
           "portfolio_layer": {"runs": runs, "deltas": deltas_p, "verdict": pv},
           "closure": ("文本因子线闭环：打分层 "
                       f"{'PASS' if score_pass else 'FAIL'} + 组合层 {pv['verdict']} ⇒ "
                       + ("采纳包交用户拍板" if score_pass and pv["verdict"] == "PASS"
                          else "线结清登记（N 预算 +5）"))}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    log(f"闭环: {out['closure']}")
    log(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
