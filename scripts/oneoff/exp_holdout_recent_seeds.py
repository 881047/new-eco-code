"""holdout_recent 两套种子复验 —— MOD-05 采纳决策的稳健性扩展(2026-09-16, 预注册)

背景
----
exp_holdout_recent.py（种子集 A=[42,7,123,2024]，生产现役）判决：ho3 过三轴判据
（P1 +9.91pp、全期 +3.86pp、回撤反好 3.02pp），采纳待用户拍板。用户拍板：
**先做两套种子复验，双种子同向增益才重新考虑采纳**。本脚本是该指示的预注册落地。

设计
----
只训 **ho6 与 ho3 两臂**（ho0 在 A 判决已不过且不参与 ho3-vs-ho6 对照，省一半算力），
种子集 B=[11,37,101,777]、C=[5,53,208,1999]（互不重叠、与 A 不重叠）；
其余参数与 exp_holdout_recent v2 逐字一致（train_months=36 / train_start=None /
backtest_start='2019-07-01' / 50 轮无早停 / holdout 显式传参 / 同日同快照）。

事前指定的判定规则(跑之前写死, 不许事后改)
------------------------------------------
对每个种子集 s ∈ {b, c} 独立算三轴（表名小写）：ΔP1 = P1(ho3_s) − P1(ho6_s)（风险匹配池子同
exp_holdout_recent：V=L·(pool/pool₀)+(1−L)，L=1−idle）；Δfull = sys_ann(ho3_s) −
sys_ann(ho6_s)；mdd_worse = sys_mdd(ho6_s) − sys_mdd(ho3_s)。
  · **复验通过**当且仅当：b、c **两套种子集分别**满足
    ΔP1 ≥ +3.0pp 且 Δfull ≥ −2.0pp 且 mdd_worse ≤ +0.5pp（与 A 同判据，一字不改）
  · b、c 全过 ⇒ 连同 A（已过）= **三套种子同向** ⇒ 维持「采纳 holdout=3」建议，
    是否进生产仍由用户拍板（改默认 + 全量重训 + 重钉 pin）
  · 任一套不过 ⇒ **复验不通过** ⇒ 撤回采纳建议，维持 holdout_recent=6 并在台账登记
    「单快照增益不可跨种子稳健」
  · 对照臂 ho6_B / ho6_C 与生产 pin 的绝对差**只作快照漂移读数登记**（v2 语义，
    不作废——两臂同日同快照，臂间差不受影响）

安全
----
实验表 model_score_ho{6,3}_s{B,C}，不写 model_score / lgb_latest / backtest 之外的
任何生产表。模型归档 data/models/ens_ho*_s*（gitignore）。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from app import config as cfg_mod
from app import db
from app.backtest import engine as E
from app.logger import setup_logger
from app.models.trainer import ENSEMBLE_ROUNDS, run as train_run

setup_logger()
db.init_db()

# 表名小写（trainer 校验 [a-z][a-z0-9_]*）；展示层自行大写
SEED_SETS = {"b": [11, 37, 101, 777], "c": [5, 53, 208, 1999]}
ARMS = [6, 3]                 # holdout_recent；6 = 对照（生产默认）
BT_START = "2019-07-01"
EVAL_START = "20190722"
CAP = 20000.0
P1 = ("2020-09-01", "2023-12-31")
CRIT = {"p1_gain": 0.03, "full_drop_max": -0.02, "mdd_worse_max": 0.005}   # 与 A 一字不改

_orig = cfg_mod.load_config


def _cap(*a, **k):
    c = _orig(*a, **k)
    c["capital"] = CAP
    return c


mode = sys.argv[1] if len(sys.argv) > 1 else "all"
t0 = time.time()

if mode in ("train", "all"):
    for sname, seeds in SEED_SETS.items():
        for ho in ARMS:
            st = f"model_score_ho{ho}_s{sname}"
            it = f"model_importance_ho{ho}_s{sname}"
            conn = db.get_conn()
            conn.execute(f"CREATE TABLE IF NOT EXISTS {st} ("
                         "ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL, "
                         "PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID")
            conn.execute(f"CREATE TABLE IF NOT EXISTS {it} ("
                         "train_date TEXT, factor TEXT, importance REAL, "
                         "PRIMARY KEY (train_date, factor)) WITHOUT ROWID")
            conn.commit()
            n_have = pd.read_sql(f"SELECT COUNT(DISTINCT trade_date) n FROM {st}", conn).iloc[0, 0]
            conn.close()
            print("[{:>5.0f}s] === ho={} seeds={} (已有 {} 月) ===".format(
                time.time() - t0, ho, sname, n_have), flush=True)
            arch = Path(__file__).resolve().parents[2] / "data" / "models" / f"ens_ho{ho}_s{sname}"
            arch.mkdir(parents=True, exist_ok=True)
            r = train_run(train_months=36, train_start=None, backtest_start=BT_START,
                          full=False, score_table=st, importance_table=it, archive_dir=arch,
                          num_boost_round=ENSEMBLE_ROUNDS, early_stopping_rounds=0,
                          ensemble_seeds=seeds, holdout_recent=ho)
            print("[{:>5.0f}s] ho={} seeds={} 完成: {}".format(
                time.time() - t0, ho, sname, json.dumps(r, ensure_ascii=False, default=str)), flush=True)

if mode in ("bt", "all"):
    conn = db.get_conn()
    p = pd.read_sql("SELECT trade_date, close FROM pool_index", conn)
    conn.close()
    p["trade_date"] = pd.to_datetime(p["trade_date"])
    pool = p.set_index("trade_date")["close"].sort_index()

    cfg_mod.load_config = _cap
    rows = []
    try:
        for sname in SEED_SETS:
            for ho in ARMS:
                st = f"model_score_ho{ho}_s{sname}"
                m = E.run_backtest(score_table=st, run_tag=f"ho{ho}s{sname}", start=EVAL_START)
                rows.append({"seeds": sname, "ho": ho, "run": m["run_date"],
                             "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
                             "idle": m["avg_cash_idle"], "ic": m["ic_mean"]})
                print("[{:>5.0f}s] {}".format(time.time() - t0,
                      json.dumps(rows[-1], ensure_ascii=False)), flush=True)
    finally:
        cfg_mod.load_config = _orig

    segs = {}
    for r in rows:
        conn = db.get_conn()
        d = pd.read_sql("SELECT trade_date, sys_nav FROM backtest_nav WHERE run_date=? "
                        "ORDER BY trade_date", conn, params=(r["run"],))
        conn.close()
        d["trade_date"] = pd.to_datetime(d["trade_date"])
        nav = d.set_index("trade_date")["sys_nav"]
        L = 1.0 - r["idle"]
        V = L * (pool.reindex(nav.index) / pool.reindex(nav.index).iloc[0]) + (1 - L)
        V = V / V.iloc[0]
        rs, rv = nav.pct_change().dropna(), V.pct_change().dropna()
        mm = (rs.index >= P1[0]) & (rs.index <= P1[1])
        n = int(mm.sum())
        segs[(r["seeds"], r["ho"])] = ((1 + rs[mm]).prod() ** (12 / n) - 1)

    print("\n" + "=" * 88)
    print("holdout 两套种子复验 —— 判据与 A 集一字不改：ΔP1≥+3.0pp 且 Δfull≥−2.0pp 且 mdd_worse≤+0.5pp")
    print("=" * 88)
    verdicts = {}
    for sname in SEED_SETS:
        ctl = next(r for r in rows if r["seeds"] == sname and r["ho"] == 6)
        arm = next(r for r in rows if r["seeds"] == sname and r["ho"] == 3)
        d1 = segs[(sname, 3)] - segs[(sname, 6)]
        df_all = arm["sys_ann"] - ctl["sys_ann"]
        mdd_worse = ctl["sys_mdd"] - arm["sys_mdd"]
        passed = (d1 >= CRIT["p1_gain"] and df_all >= CRIT["full_drop_max"]
                  and mdd_worse <= CRIT["mdd_worse_max"])
        verdicts[sname] = {"d_p1": round(d1, 4), "d_full": round(df_all, 4),
                           "mdd_worse": round(mdd_worse, 4), "passed": bool(passed)}
        print("种子集 {}: ΔP1 {:+.2f}pp  Δfull {:+.2f}pp  mdd_worse {:+.2f}pp  ⇒ {}".format(
            sname, d1 * 100, df_all * 100, mdd_worse * 100, "过" if passed else "不过"))
    all_pass = all(v["passed"] for v in verdicts.values())
    print("\n总判决: {}".format(
        "复验通过（b/c 双过，连同 A 集三套种子同向）⇒ 维持「采纳 holdout=3」建议，进生产仍由用户拍板"
        if all_pass else
        "复验不通过（" + "/".join(k for k, v in verdicts.items() if not v["passed"]) +
        " 未过）⇒ 撤回采纳建议，维持 holdout_recent=6"))

    out = (Path(__file__).resolve().parents[2] / "experiments" / "05_label"
           / "exp_holdout_recent_seeds_result.json")
    out.write_text(json.dumps({
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "purpose": "MOD-05 采纳决策的种子稳健性扩展（用户拍板：双种子同向才重新考虑采纳）",
        "criteria": CRIT, "seed_sets": SEED_SETS,
        "rows": rows, "verdicts": verdicts, "all_pass": bool(all_pass),
        "note": "对照臂与 pin 的绝对差只作快照漂移读数（exp_holdout_recent v2 语义）",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print("结果已写", out)
