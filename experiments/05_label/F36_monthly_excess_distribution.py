"""F36 · **月度口径**的超额分布（用户的原始提问就是这个尺度）—— 描述性，无新判决

## 为什么单独做这一件

用户的目标原话是"**周频调仓月收益尽可能的超额**"，而此前所有读数都是**年化**（+14.6pp 等）。
年化数字有两个问题：① 它是复利外推，直觉上难判断；② 容易被读成"每年稳定 +14.6%"。
本实验把同一条净值曲线换成**月度**表达，给的是分布而不是单点：
中位/均值/正超额月占比/最差最好月 —— **口径不变、不新增任何可调参数、不做任何新判决**。

## 臂与窗口

现役构型（2×W5+pit / 100 万 / top100 / 带 8× / 加成 0.2σ），两套种子；
全史（491 周 ≈ 115 个月）、留出集（2024-01~2026-08，**32 个月**）、死区（2020-09~2023-12）。

> ⚠️ 标签修正（2026-09-13）：此前 §1.1/C3、§2.28、README 都把这段留出窗写成"24 个月"，
> 实际是 **32 个月**（12+12+8）。**数字不受影响**（窗口日期写对了），但标签是错的。
> F28 的脚本/JSON 是**冻结证据**不动（其键名 `OOS_24m` 属历史遗留，已在计划文档里注明），
> 本脚本用 `OOS_32m`。

## 必须一起读的三件事（否则月度数字又是自欺）

1. **样本极小**：全史才 113 个月、留出集只有 24 个月 ⇒ 留出集的"月均超额"标准误约
   `σ_月/√24 ≈ 2.4/4.9 ≈ 0.5pp`（**按月**），年化放大 √12 倍 ⇒ 与 §2.28 的 ±20~25pp 一致；
2. **月内路径被抹掉**：同一个年化数字，可以是"每月稳稳 +1%"也可以是"三个月 +8%、其余 −2%"，
   所以必须同时给**命中率**与**最差月**；
3. **口径**：超额 = 组合月度收益（周频 NAV 按自然月复利）× 同月**可交易域等权票池**收益之比 − 1；
   与 F22/F28/F31 完全同源（同一引擎、同一基准、同一费用）。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F36_monthly_excess_distribution.py
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


f29 = _load("f29", HERE / "F29_tail_weighted_training.py")
f17 = _load("f17", HERE / "F17_die_registry_kgd.py")
f22 = _load("f22", HERE / "F22_pair_final_selection.py")
f1, f1b = f29.f1, f29.f1b

from app import config as cfg_mod             # noqa: E402
from app.factors import factor_engine as fe   # noqa: E402
from app.pit.engine import PITEngine          # noqa: E402

log = logging.getLogger("f36")
OUT = HERE / "F36_monthly_excess_distribution_result.json"
SEED_FILES = {"seedA": "F4_scores_seedA.npz", "seedB": "F4_scores_seedB.npz"}
WINDOWS = {"FULL": ("2010-01-01", "2030-12-31"), "OOS_32m": ("2024-01-01", "2026-08-31"),
           "DEAD": ("2020-09-01", "2023-12-31")}


def to_monthly(df: pd.DataFrame) -> pd.DataFrame:
    """周频 (ret, bench_ret) → 自然月复利收益。"""
    d = df.dropna(subset=["ret", "bench_ret"]).copy()
    d["date"] = pd.to_datetime(d["date"])
    d["m"] = d["date"].dt.to_period("M")
    g = d.groupby("m").agg(ret=("ret", lambda s: float((1 + s).prod() - 1)),
                           bench=("bench_ret", lambda s: float((1 + s).prod() - 1)),
                           weeks=("ret", "size"))
    g = g[g["weeks"] >= 2]                       # 半月以下的月不参与(边界月)
    g["excess"] = g["ret"] - g["bench"]
    return g


def stats(g: pd.DataFrame) -> dict:
    if len(g) < 3:
        return {"n": int(len(g))}
    e = g["excess"]
    ann = float(np.prod(1 + g["ret"]) ** (12 / len(g)) - 1)
    ann_b = float(np.prod(1 + g["bench"]) ** (12 / len(g)) - 1)
    return {"n": int(len(g)),
            "mean_pp": round(100 * float(e.mean()), 2),
            "median_pp": round(100 * float(e.median()), 2),
            "hit": round(float((e > 0).mean()), 3),
            "sd_pp": round(100 * float(e.std()), 2),
            "worst_pp": round(100 * float(e.min()), 2),
            "best_pp": round(100 * float(e.max()), 2),
            "ann_excess_pp": round(100 * (ann - ann_b), 2),
            "avg_ret_pp": round(100 * float(g["ret"].mean()), 2),
            "avg_bench_pp": round(100 * float(g["bench"].mean()), 2)}


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
    conn = sqlite3.connect(f"file:{BASE/'data'/'neweco.db'}?mode=ro", uri=True)
    PITSC = f17.table_scores(conn, "model_score_die_pit", wk, cols)
    conn.close()
    log.info("周锚点 %d | 股票 %d", len(wk), len(cols))

    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "windows": {k: list(v) for k, v in WINDOWS.items()}, "seeds": {}, "secs": None}
    for seed, fn in SEED_FILES.items():
        z = np.load(HERE / fn, allow_pickle=True)
        a5 = pd.to_datetime([str(d) for d in z["anchors"]])
        c5 = [str(c) for c in z["cols"]]
        W5 = pd.DataFrame(z["scores"], index=a5, columns=c5).reindex(index=wk, columns=cols)
        Sdf = pd.DataFrame(f22.wr((W5, 2), (PITSC, 1)), index=wk, columns=cols)
        s = f29._run_series(m, wk, mask, pit, cfg, pos_of, mask_np, op_adj, Sdf)
        mon = to_monthly(s)
        res["seeds"][seed] = {}
        for wname, (lo, hi) in WINDOWS.items():
            g = mon[(mon.index.to_timestamp() >= lo) & (mon.index.to_timestamp() <= hi)]
            res["seeds"][seed][wname] = stats(g)
            st = res["seeds"][seed][wname]
            if st.get("n", 0) >= 3:
                log.info("[%s|%s] %d 个月 | 月均超额 %+.2fpp(中位 %+.2f) | 正超额月 %.0f%% "
                         "| 月σ %.2fpp | 最差 %+.2f / 最好 %+.2f | 年化超额 %+.2fpp",
                         seed, wname, st["n"], st["mean_pp"], st["median_pp"], 100 * st["hit"],
                         st["sd_pp"], st["worst_pp"], st["best_pp"], st["ann_excess_pp"])
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 月度口径完成 | %.1fs → %s ==", res["secs"], OUT)


if __name__ == "__main__":
    main()
