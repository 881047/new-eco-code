# -*- coding: utf-8 -*-
"""新过线因子构型全量重训 A/B —— `plans/nf-fullretrain-prereg-20260926.md` 的执行体。

设计（预注册 §一/§二，跑数前锁定）:
  四臂同进程 = {A 控制臂: 现行生产 31 因子(factor 表原路径), B 实验臂: 58 因子
  = 保留池过档 8 + 新加入 bank 过档 50} × {种子集 SA=[42,7,123,2024](生产),
  SB=[5,11,77,999](第二套, 双种子集同向铁律)}。共同参数 = 生产口径
  (full 重训 / 36 月滚动 / 2017 评估起 / 50 轮无早停 / holdout_recent=6 /
  标签行业中性化 / horizon 21)。

机制与守卫:
  · B 臂走 `trainer.run(factors_override=...)`(生产路径一字不经过;
    verify_d1_replica.py 已证 override ≡ factor 表口径)。
  · 50 个 npz 面板(2019-01 起, 93 锚点)按 factors_override docstring 要求先做
    与 factor 表写入口径一致的 `fe.neutralize`(去极值 2%/98% → 行业中性化 →
    z-score); F35 稀疏守卫: sparse_ratio < 0.02 改走 `neutralize_sparse`。
  · 对齐守卫: npz reindex 到 31 因子宽表的(日期 × 股票)宇宙, 非缺失保留 < 60% 即中止;
    neutralize 前完成列集对齐(float32/行数陷阱, 见 factor_engine docstring)。

产物隔离: 实验表 nfab_{ctrl,trt}_{sa,sb} + nfimp_{...}; 模型工件归档
data/models/control/nfab_*; 生产 model_score/model_importance/ens 一字不动
(不写生产表, 无需 ONEOFF_CONFIRM)。判读 = 预注册 §三(DEAD head 双种子集同向
+ 增量均值 ≥ +0.10pp/月 + B 臂 DEAD t 均 ≥ 2.0), 与 diag_gate_calibration 同法
(gate.evaluate + PIT 域, md_eval ≥ 2019-01-01)。
产物: experiments/nf_fullretrain_ab_20260926.json。
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
from app.alpha import gate
from app.factors import factor_engine as fe
from app.factors.factor_engine import load_market, month_end_dates
from app.models import expert as ex
from app.models import trainer as T
from app.pit.engine import PITEngine

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "neweco.db"
POOL = ROOT / "data" / "alpha_pool"
RETEST = ROOT / "experiments" / "single_factor_retest_20260926.json"
OUT = ROOT / "experiments" / "nf_fullretrain_ab_20260926.json"
PREREG = "plans/nf-fullretrain-prereg-20260926.md"

SEED_SETS = {"sa": [42, 7, 123, 2024], "sb": [5, 11, 77, 999]}

#: 与 app/db.py 的 model_importance 同 schema(trainer 只 DELETE 不建表, 实验表须先建)
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


def build_treatment(ref_wide: pd.DataFrame, industry: pd.Series) -> tuple[dict, dict]:
    """→ ({因子名: 面板}, 审计)。8 保留取自 factor 表宽表(已中性化, 不再处理);
    50 新过线取自 npz → reindex 参考宇宙 → F35 稀疏守卫 → 生产口径 neutralize。"""
    d = json.loads(RETEST.read_text(encoding="utf-8"))
    pool_pass = sorted(r["factor"] for r in d["pool"] if r.get("PASS_single"))
    bank_pass = list(d["bank"]["pass_list"])
    log(f"过档名单: 池 {len(pool_pass)} / bank {len(bank_pass)}")

    ref_index = pd.to_datetime(ref_wide.index)
    ref_codes = sorted(set(ref_wide.columns.get_level_values("ts_code")))
    wide_r = ref_wide.reorder_levels(["factor", "ts_code"], axis=1).sort_index(axis=1)

    trt: dict[str, pd.DataFrame] = {}
    for f in pool_pass:
        p = wide_r.xs(f, axis=1, level="factor")
        p.index = pd.to_datetime(p.index)
        trt[f] = p

    audit, missing = [], []
    for aid in bank_pass:
        npz = POOL / f"{aid}.npz"
        if not npz.exists():
            missing.append(aid)
            audit.append({"alpha_id": aid, "note": "无 npz 面板, 剔除出实验臂"})
            continue
        z = np.load(npz, allow_pickle=True)
        pw = pd.DataFrame(
            z["values"].astype(float),
            index=pd.to_datetime([str(x) for x in z["dates"].tolist()]),
            columns=[str(x) for x in z["codes"].tolist()],
        )
        nn_before = int(pw.notna().to_numpy().sum())
        pw = pw.reindex(index=ref_index, columns=ref_codes)
        nn_after = int(pw.notna().to_numpy().sum())
        retain = (nn_after / nn_before) if nn_before else 0.0
        if retain < 0.60:
            raise SystemExit(f"对齐守卫中止: {aid} reindex 后非缺失保留 {retain:.0%} < 60%")
        sr = float(fe.sparse_ratio(pw))
        if sr < fe.SPARSE_NONZERO_RATIO:
            trt[aid] = fe.neutralize_sparse(pw, industry)
            mode = "neutralize_sparse(F35)"
        else:
            trt[aid] = fe.neutralize(pw, industry)
            mode = "neutralize"
        audit.append({"alpha_id": aid, "nonnull_before": nn_before,
                      "nonnull_after": nn_after, "retain": round(retain, 3),
                      "sparse_ratio": round(sr, 4), "neutralize_mode": mode})
    log(f"实验臂因子数 {len(trt)} | 无面板剔除 {missing or '无'}")
    return trt, {"kept_pool": pool_pass, "bank_missing": missing,
                 "n_treatment": len(trt), "bank_audit": audit}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge-only", action="store_true",
                    help="跳过四臂训练, 只重跑判读(评分表已在库时用)")
    args = ap.parse_args()

    t_start = time.time()
    db.init_db()
    ref_wide = T._load_factors_wide()
    log(f"参考宽表: {ref_wide.shape[0]} 期 × {ref_wide.shape[1] // 31 if ref_wide.shape[1] else 0} 因子宽表")

    with db.conn_ctx() as conn:
        industry = fe.industry_map(conn)
    trt, trt_audit = build_treatment(ref_wide, industry)
    if len(trt) != 58:
        raise SystemExit(f"实验臂因子数 {len(trt)} ≠ 58(8 保留 + 50 新过线), 中止核对")

    runtimes = {}
    if not args.judge_only:
        # ---------- 四臂全量重训(同进程顺序, 同日同环境) ----------
        for arm, fdict in (("ctrl", None), ("trt", trt)):
            for sname, seeds in SEED_SETS.items():
                table = f"nfab_{arm}_{sname}"
                ex.ensure_score_table(table)
                ensure_importance_table(f"nfimp_{arm}_{sname}")
                log(f"开始 {table}(因子 {31 if fdict is None else len(fdict)} 个, 种子 {seeds})")
                t0 = time.time()
                T.run(train_months=36, backtest_start="2017-01-01", full=True,
                      score_table=table, importance_table=f"nfimp_{arm}_{sname}",
                      archive_dir=ROOT / "data" / "models" / "control" / table,
                      num_boost_round=T.ENSEMBLE_ROUNDS, early_stopping_rounds=0,
                      ensemble_seeds=seeds, holdout_recent=6,
                      label_neutral="industry", train_start=None,
                      factors_override=fdict)
                runtimes[table] = round(time.time() - t0, 1)
                log(f"{table} 完成, 用时 {runtimes[table]:.0f}s")

    # ---------- 判读(预注册 §三; 与 diag_gate_calibration 同法同窗) ----------
    m = fe.load_market()
    md_all = month_end_dates(pd.DatetimeIndex(m["close"].index))
    md_eval = pd.DatetimeIndex(md_all[md_all >= pd.Timestamp("2019-01-01")])
    zones = gate.zone_of(md_eval)
    fwd = gate.forward_return(m, gate.HORIZON)
    tradable = PITEngine(m=m).tradable_matrix(md_eval)

    def judge(table: str) -> dict:
        conn = sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)
        sc = pd.read_sql(f"SELECT trade_date, ts_code, score FROM {table}", conn)
        conn.close()
        w = sc.pivot_table(index="trade_date", columns="ts_code", values="score",
                           aggfunc="last")
        w.index = pd.to_datetime(w.index, format="%Y%m%d")
        pm = w.reindex(index=sorted(set(md_eval) & set(w.index)),
                       columns=w.columns)
        pm = pm.where(tradable.reindex(index=md_eval, columns=pm.columns).fillna(False))
        det = gate.evaluate(pm, fwd, md_eval, zones)
        if det.empty:
            return {"table": table, "note": "无有效截面"}
        dead, act = det[det.zone == "DEAD"], det[det.zone == "ACTIVE"]
        h_d, h_a = float(dead.head_excess.mean()), float(act.head_excess.mean())
        t_d = float(h_d / (dead.head_excess.std(ddof=1) / np.sqrt(len(dead))))
        return {"table": table, "dead_head": round(h_d, 4), "dead_t": round(t_d, 2),
                "active_head": round(h_a, 4), "same_sign": bool(h_d * h_a > 0),
                "dead_top6": round(float(dead.top6_excess.mean()), 4),
                "n_anchor": int(len(det))}

    rows = {f"{arm}_{sname}": judge(f"nfab_{arm}_{sname}")
            for arm in ("ctrl", "trt") for sname in SEED_SETS}
    for r in rows.values():
        log(str(r))

    delta = {s: round(rows[f"trt_{s}"]["dead_head"] - rows[f"ctrl_{s}"]["dead_head"], 4)
             for s in SEED_SETS if "dead_head" in rows[f"trt_{s}"] and "dead_head" in rows[f"ctrl_{s}"]}
    same_dir = len(delta) == 2 and all(v > 0 for v in delta.values())
    mean_delta = float(np.mean(list(delta.values()))) if delta else float("nan")
    sig = all(rows[f"trt_{s}"].get("dead_t", -9) >= 2.0 for s in SEED_SETS)
    adopt_candidate = bool(same_dir and mean_delta >= 0.0010 and sig)
    verdict = {"delta_dead_head": delta, "mean_delta": round(mean_delta, 4),
               "c1_双种子集同向": same_dir, "c2_增量均值≥+0.10pp": bool(mean_delta >= 0.0010),
               "c3_B臂DEAD_t均≥2": sig, "ADOPT_CANDIDATE": adopt_candidate}
    log(f"判据: {verdict}")

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"),
           "prereg": PREREG, "runtime_sec": round(time.time() - t_start, 1),
           "arm_runtimes_sec": runtimes, "treatment_audit": trt_audit,
           "judge": rows, "verdict": verdict}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    log(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
