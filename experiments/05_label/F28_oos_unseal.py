"""F28 · 周频现役构型的**样本外一次性开封**（C3 等价）+ 全史多重性校正 —— 预注册

## 为什么现在做

`plans/weekly-terminal-plan-20260912.md` §1.1 的 **C3** 是放行判据：

    样本外（2024-01 ~ 2026-08，24 个月，**一次性开封**）年化超额 ≥ +10.0pp

但那一关是给**月频系统**定的 —— 周频袖套从来没有单独做过：F22/F23 只报全窗口（491 期），
F24 报的是**算术**分解与分区间读数。袖套周一就要首次真实执行，这一关必须先补上，
否则"终端能不能用"这个问题在纸面上是没答过的。

## 预注册（写死再看数）

- **臂**：现役构型（2×W5+pit / 100 万 / top100 / 退出带 8× / 持仓加成 0.2σ）、两套种子；
  外加两个**诊断臂**（同构型）：`gross`（零费零滑点）与 `no_gates`（关涨跌停闸门）
  —— 用来把缺口拆成 信号 / 摩擦 / 可交易性 三段（F4 口径）。
- **留出集**：2024-01-01 ~ 2026-08-31（24 个月）；死区 2020-09~2023-12 只作对照。
- **判据**（与 C3 同口径）：留出集**复利年化超额** ≥ **+10.0pp**（基准 = 可交易域等权票池）。
- **报告必带**（§6.2）：窗口 / 基准 / 成本口径 / 样本外标记，以及 HAC t 与**多重性校正后的 p**。
- **一次性**：本实验只跑一次并冻结；此后对构型的任何改动都要在*新的*留出窗上验证
  （不得回头调参再报同一段）。

## 多重性校正

研究预算 = F3 的 33 个独立检验 + F26 的 3 + F27 的 2 = **38**；
另给 100 / 1000 的敏感性（因为**构型本身**还是从 F4/F12/F13 的 ~25 个组合臂里挑出来的，
38 是下限而不是上限）。校正方式：`p_corrected = min(1, 双侧 p × 分母)`。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F28_oos_unseal.py
"""
import hashlib
import importlib.util
import json
import logging
import math
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent
sys.path.insert(0, str(BASE))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


f1 = _load("f1", HERE / "F1_weekly_ic_decay.py")
f1b = _load("f1b", HERE / "F1b_weekly_open_overlap.py")
f17 = _load("f17", HERE / "F17_die_registry_kgd.py")
f22 = _load("f22", HERE / "F22_pair_final_selection.py")

from app import config as cfg_mod             # noqa: E402
from app.factors import factor_engine as fe   # noqa: E402
from app.pit.engine import PITEngine          # noqa: E402

log = logging.getLogger("f28")
OUT = HERE / "F28_oos_unseal_result.json"
CAPITAL, TOP_N, BAND, HYST = 1_000_000.0, 100, 8.0, 0.2
SEED_FILES = {"seedA": "F4_scores_seedA.npz", "seedB": "F4_scores_seedB.npz"}
MIN_NAMES = 200
PER_YEAR = 52.0
OOS = ("2024-01-01", "2026-08-31")            # C3 的留出窗(一次性)
DEAD = ("2020-09-01", "2023-12-31")
C3_THRESHOLD_PP = 10.0
DENOMS = (38, 100, 1000)                      # 研究预算与敏感性


def zone_stats(df: pd.DataFrame, lo: str, hi: str) -> dict:
    s = df[(df["date"] >= lo) & (df["date"] <= hi)].dropna(subset=["ret", "bench_ret"])
    if len(s) < 5:
        return {"n": int(len(s))}
    a = f1b.ann(s["ret"], PER_YEAR)
    b = f1b.ann(s["bench_ret"], PER_YEAR)
    ex = s["ret"] - s["bench_ret"]
    t = f1b.hac_t(ex.to_numpy(), 2)
    nav = (1 + s["ret"]).cumprod()
    return {"n": int(len(s)),
            "ann": round(float(a), 4), "bench_ann": round(float(b), 4),
            "excess_pp": round(100 * (a - b), 2),
            "excess_pp_arith": round(100 * PER_YEAR * float(ex.mean()), 2),
            "t_hac": None if not np.isfinite(t) else round(float(t), 2),
            "mdd": round(float((nav / nav.cummax() - 1).min()), 4),
            "turnover": round(float(s["turnover"].mean()), 3),
            "invested": round(float(s["invested_ratio"].mean()), 4)}


def two_sided_p(t: float) -> float:
    return math.erfc(abs(t) / math.sqrt(2.0))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    cols = list(m["close"].columns)
    pit = PITEngine(m=m, cfg=cfg)
    mask = f1.tradable_frame(pit, wk, cols)
    mask_np = mask.fillna(False).to_numpy()
    conn = sqlite3.connect(f"file:{BASE/'data'/'neweco.db'}?mode=ro", uri=True)
    PITSC = f17.table_scores(conn, "model_score_die_pit", wk, cols)
    conn.close()
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    log.info("交易日 %d | 周锚点 %d | 股票 %d", len(dates), len(wk), len(cols))

    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "oos_window": OOS, "dead_window": DEAD, "c3_threshold_pp": C3_THRESHOLD_PP,
           "denoms": list(DENOMS), "arms": {}, "secs": None}
    for seed, fn in SEED_FILES.items():
        z = np.load(HERE / fn, allow_pickle=True)
        a5 = pd.to_datetime([str(d) for d in z["anchors"]])
        c5 = [str(c) for c in z["cols"]]
        W5 = pd.DataFrame(z["scores"], index=a5, columns=c5).reindex(index=wk, columns=cols)
        S = f22.wr((W5, 2), (PITSC, 1))
        Sdf = pd.DataFrame(S, index=wk, columns=cols)
        arms = {"net": {}, "gross": {"gross": True}, "no_gates": {"no_gates": True}}
        for tag, kw in arms.items():
            if seed != "seedA" and tag != "net":       # 诊断臂只跑 seedA(省机器时间)
                continue
            d = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, {**cfg, "capital": CAPITAL}, pit, 0,
                                          score_matrix=Sdf, top_n=TOP_N, rank_band=BAND,
                                          hyst=HYST, **kw)
            if d is None:
                continue
            bench, prev_exec, prev_ap = [], None, None
            for _, r in d.iterrows():
                e = pos_of[r["date"]]
                if prev_exec is None:
                    bench.append(np.nan)
                else:
                    good = (mask_np[int(prev_ap)] & np.isfinite(op_adj[prev_exec])
                            & np.isfinite(op_adj[e]) & (op_adj[prev_exec] > 0))
                    bench.append(float(np.mean(op_adj[e][good] / op_adj[prev_exec][good] - 1))
                                 if good.sum() >= MIN_NAMES else np.nan)
                prev_exec, prev_ap = e, int(r["anchor_pos"])
            d["bench_ret"] = bench
            d = d.dropna(subset=["ret", "bench_ret"])
            blocks = {"full": ("2010-01-01", "2030-12-31"), "OOS_24m": OOS, "DEAD": DEAD}
            res["arms"].setdefault(seed, {})[tag] = {
                k: zone_stats(d, lo, hi) for k, (lo, hi) in blocks.items()}
            o = res["arms"][seed][tag]["OOS_24m"]
            if o.get("t_hac") is not None:
                p = two_sided_p(o["t_hac"])
                o["p_two_sided"] = round(p, 6)
                o["p_corrected"] = {str(n): round(min(1.0, p * n), 6) for n in DENOMS}
            log.info("[%s|%s] 全史 %+.2fpp | 留出集24m %+.2fpp (t=%s) | 死区 %+.2fpp | MDD(留出) %.1f%%",
                     seed, tag,
                     res["arms"][seed][tag]["full"].get("excess_pp", float("nan")),
                     o.get("excess_pp", float("nan")), o.get("t_hac"),
                     res["arms"][seed][tag]["DEAD"].get("excess_pp", float("nan")),
                     100 * o.get("mdd", float("nan")))

    net_a = res["arms"]["seedA"]["net"]["OOS_24m"]
    net_b = res["arms"]["seedB"]["net"]["OOS_24m"]
    res["verdict"] = {
        "C3_equiv_pass": bool(min(net_a.get("excess_pp", -99), net_b.get("excess_pp", -99))
                              >= C3_THRESHOLD_PP),
        "oos_excess_pp": {"seedA": net_a.get("excess_pp"), "seedB": net_b.get("excess_pp")},
        "p_corrected_max": net_a.get("p_corrected"),
        "note": ("一次性开封: 本判据此后冻结; 改构型必须在新的留出窗上重新验证")}
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== C3 等价判决: 通过=%s | 留出集超额 %s | 校正 p %s | %.1fs → %s ==",
             res["verdict"]["C3_equiv_pass"], res["verdict"]["oos_excess_pp"],
             res["verdict"]["p_corrected_max"], res["secs"], OUT)


if __name__ == "__main__":
    main()
