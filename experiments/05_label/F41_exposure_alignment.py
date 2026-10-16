"""F41 · 敞口对齐：现役满仓（w=1.0）在**留出集**上到底是什么读数 —— 测量，不是搜索

## 为什么必须做

2026-09-13 纸面袖套的敞口按用户目标函数拨到 **w=1.0（满仓，见计划 §2.46）**，但终端的
样本外（F28）与多重比较（F38）读数**都是在 w=0.873 上测的** ⇒ 不补这一刀，终端就是
"跑在满仓、挂着 0.873 的数"，而且看板上那句"样本外 +7.34~8.62pp"会被读成对当前敞口成立。

本实验只做一件事：把 **w=1.0 那条臂在同窗口上的两轴读数**测出来，让终端的运行敞口与它的
证据对齐。

## 口径（与 F25/F28 逐字一致，不新写第二份）

- 臂：**生产的两套种子**（`F4_scores_seedA/seedB`）× `2×W5 + 1×pit`；
  构型 100 万 / top100 / 退出带 8× / 持仓加成 0.2σ；
- **w ∈ {0.873, 1.0} 两档都跑**，其中 w=0.873 是**对照**：它必须复现 F25/F28 的
  +14.91/+14.30pp 与留出集 +7.34/+8.62pp —— 复现不了就说明口径漂了，满仓的数也不许用；
- 窗口：全史（同窗口对齐）/ DEAD(2020-09~2023-12) / **ACTIVE = 留出集(2024-01~2026-08)**；
- 配对检验：满仓减旧敞口的**逐期收益差**，HAC(lag=2)。

## 诚实边界（这次尤其要说清）

- ACTIVE 就是 F28 已开封的留出集 ⇒ 这是**又一次**看它。本实验**不产生新的显著性证据**，
  用途是**对齐**（当前敞口 vs 它的样本外读数），不是"多试几次凑一个能过 C3 的数"；
- F38 的 DSR 是用 w=0.873 的 IR 算的。这里给出满仓的 IR，**但不重算 DSR**（在已开封的窗口上
  重算等于再搜一次）⇒ 多重比较一栏**仍按"未通过"处理**；
- 结论无论好坏都写进 §2.46 与 README，不许只留好看的那一半。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F41_exposure_alignment.py
"""
import hashlib
import importlib.util
import json
import logging
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
f29 = _load("f29", HERE / "F29_tail_weighted_training.py")

from app import config as cfg_mod             # noqa: E402
from app.factors import factor_engine as fe   # noqa: E402
from app.pit.engine import PITEngine          # noqa: E402

log = logging.getLogger("f41")
OUT = HERE / "F41_exposure_alignment_result.json"
CAPITAL, TOP_N, BAND, HYST = 1_000_000.0, 100, 8.0, 0.2
SEED_FILES = {"seedA": "F4_scores_seedA.npz", "seedB": "F4_scores_seedB.npz"}
WEIGHTS = (0.873, 1.0)
FULL = ("2010-01-01", "2030-12-31")
DEAD = ("2020-09-01", "2023-12-31")
ACTIVE = ("2024-01-01", "2026-08-31")
MIN_NAMES = 200
PER_YEAR = 52.0


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
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    conn = sqlite3.connect(f"file:{BASE / 'data' / 'neweco.db'}?mode=ro", uri=True)
    PITSC = f17.table_scores(conn, "model_score_die_pit", wk, cols)
    conn.close()
    log.info("面板就绪 | 锚点 %d | 票池 %d 只", len(wk), len(cols))

    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "weights": list(WEIGHTS), "windows": {"full": FULL, "dead": DEAD, "active": ACTIVE},
           "note": "测量/对齐, 不是搜索: ACTIVE 是 F28 已开封的留出集, 不产生新的显著性证据",
           "arms": {}, "increment": {}, "secs": None}
    series, common = {}, None
    for seed, fn in SEED_FILES.items():
        z = np.load(HERE / fn, allow_pickle=True)
        W5 = pd.DataFrame(z["scores"],
                          index=pd.to_datetime([str(d) for d in z["anchors"]]),
                          columns=[str(c) for c in z["cols"]]).reindex(index=wk, columns=cols)
        Sdf = pd.DataFrame(f22.wr((W5, 2), (PITSC, 1)), index=wk, columns=cols)
        for w in WEIGHTS:
            c = {**cfg, "capital": CAPITAL,
                 "regime_equity_weight": {"aggressive": {"attack": w, "neutral": w, "defense": w}}}
            d = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, c, pit, 0, score_matrix=Sdf,
                                          top_n=TOP_N, rank_band=BAND, hyst=HYST)
            if d is None:
                log.error("[%s|w=%.3f] 无输出", seed, w)
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
            series[(seed, w)] = d[["date", "ret", "bench_ret", "turnover"]].copy()
            ds = set(pd.to_datetime(d.loc[d["ret"].notna() & d["bench_ret"].notna(), "date"]))
            common = ds if common is None else (common & ds)
            log.info("[%s|w=%.3f] 跑完 %d 期", seed, w, len(d))
    common = sorted(common or [])
    log.info("同窗口对齐: %d 期", len(common))

    for (seed, w), s in series.items():
        full = f29._aligned_stats(s, common)
        dead = f29._zone(s, *DEAD)
        active = f29._zone(s, *ACTIVE)
        # **水平值的 t**（不是增量的 t）：终端的报数口径是"超额 X pp, t=Y",
        # 少了它就没法跟 F28 的 t=0.78/0.91 并排看。增量 t 另算(见下)。
        for tag, (lo, hi), dst in (("full", FULL, full), ("active", ACTIVE, active)):
            s2 = s.copy()
            s2["date"] = pd.to_datetime(s2["date"])
            s2 = s2[(s2["date"] >= lo) & (s2["date"] <= hi)].dropna(subset=["ret", "bench_ret"])
            ex = (s2["ret"] - s2["bench_ret"]).to_numpy()
            dst[f"t_hac_{tag}"] = (round(float(f29.f1b.hac_t(ex, 2)), 2)
                                   if len(ex) > 10 else None)
        res["arms"].setdefault(seed, {})[f"w{w:.3f}"] = {
            "full": full, "dead": dead, "active": active}
        log.info("[%s|w=%.3f] 全史 %spp (t=%s, MDD %.1f%%) | DEAD %spp | ACTIVE %spp "
                 "(t=%s, MDD %.1f%%)",
                 seed, w, full.get("excess_pp"), full.get("t_hac_full"),
                 100 * full.get("mdd", float("nan")), dead.get("excess_pp"),
                 active.get("excess_pp"), active.get("t_hac_active"),
                 100 * active.get("mdd", float("nan")))

    for seed in SEED_FILES:
        a, b = series.get((seed, WEIGHTS[0])), series.get((seed, WEIGHTS[-1]))
        if a is None or b is None:
            continue
        d = (b.assign(date=pd.to_datetime(b["date"])).set_index("date")["ret"]
             - a.assign(date=pd.to_datetime(a["date"])).set_index("date")["ret"]).dropna()
        act = d[(d.index >= ACTIVE[0]) & (d.index <= ACTIVE[1])]
        res["increment"][seed] = {
            "t_diff_full": round(float(f29.f1b.hac_t(d.to_numpy(), 2)), 2) if len(d) > 10 else None,
            "active_mean_pp_per_period": round(100 * float(act.mean()), 3),
            "t_diff_active": (round(float(f29.f1b.hac_t(act.to_numpy(), 2)), 2)
                              if len(act) > 10 else None),
            "note": "满仓 − 旧敞口 的逐期差值; t 是 HAC(lag=2)"}
        log.info("[%s] 满仓−旧敞口: 全史 t=%s | 留出集 每期 %+.3fpp, t=%s", seed,
                 res["increment"][seed]["t_diff_full"],
                 res["increment"][seed]["active_mean_pp_per_period"],
                 res["increment"][seed]["t_diff_active"])

    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== F41 完成 %.1fs → %s ==", res["secs"], OUT)


if __name__ == "__main__":
    main()
