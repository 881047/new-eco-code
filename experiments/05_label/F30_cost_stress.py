"""F30 · 成本压力臂（§5 W-A2 / 验收 C4）—— 预注册

## 为什么做这一件

§5 的 **W-A2** 与 §1.1 的 **C4** 都要求："费率 0 / 1× / 1.5× / 2× / 3×；**1.5× 下超额仍 > 0**；
报告盈亏平衡费率"。这条**从来没在周频袖套上跑过** —— 而 F28 已经告诉我们近端只剩 +7.3~8.6pp，
且近端摩擦约 1.5pp ⇒ "成本再涨 50% 会不会把超额吃光" 是个必须回答的问题。

## 臂（现役构型冻结：2×W5+pit / 100 万 / top100 / 带 8× / 加成 0.2σ / 两套种子）

    k ∈ {0, 1, 1.5, 2, 3}   同时把四项费用与滑点乘 k：
      佣金率、单笔最低佣金、印花税(当期档)、过户费、`paper.slip`
    （k=0 = 零费用零滑点诊断臂；k=1 = 生产口径）

## 预注册判据

    ① 1.5× 臂的**全史超额 > 0**（W-A2 原文）；
    ② 报告**盈亏平衡倍率** k*（全史超额 = 0 的内插点）；
    ③ 留出集（2024-01~2026-08）各档一并报出（F28 已开封，只作读数不作判据）。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F30_cost_stress.py
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


# 复用 F29 的 harness(跑组合 + 接基准 + 对齐统计)与 F22 的融合口径 —— 不写第二份实现
f29 = _load("f29", HERE / "F29_tail_weighted_training.py")
f17 = _load("f17", HERE / "F17_die_registry_kgd.py")
f22 = _load("f22", HERE / "F22_pair_final_selection.py")
f1 = f29.f1

from app import config as cfg_mod             # noqa: E402
from app.factors import factor_engine as fe   # noqa: E402
from app.pit.engine import PITEngine          # noqa: E402

log = logging.getLogger("f30")
OUT = HERE / "F30_cost_stress_result.json"
SEED_FILES = {"seedA": "F4_scores_seedA.npz", "seedB": "F4_scores_seedB.npz"}
MULTS = (0.0, 1.0, 1.5, 2.0, 3.0)
FULL = ("2010-01-01", "2030-12-31")
OOS = ("2024-01-01", "2026-08-31")
DEAD = ("2020-09-01", "2023-12-31")


def stress_cfg(cfg: dict, k: float) -> dict:
    """把四项费用与滑点同乘 k —— 只改成本口径, 不动任何策略参数。"""
    fees = dict((cfg.get("fees") or {}))
    base = {"commission_rate": 0.00025, "min_commission": 5.0, "stamp_tax": 0.0005,
            "stamp_tax_before": 0.001, "transfer_fee": 0.00001}
    out = {name: k * float(fees.get(name, dv)) for name, dv in base.items()}
    paper = dict(cfg.get("paper") or {})
    paper["slip"] = k * float(paper.get("slip", 0.001))
    return {**cfg, "fees": out, "paper": paper}


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
    log.info("交易日 %d | 周锚点 %d | 股票 %d", len(dates), len(wk), len(cols))

    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "mults": list(MULTS), "windows": {"full": FULL, "oos": OOS, "dead": DEAD},
           "arms": {}, "secs": None}
    for seed, fn in SEED_FILES.items():
        z = np.load(HERE / fn, allow_pickle=True)
        a5 = pd.to_datetime([str(d) for d in z["anchors"]])
        c5 = [str(c) for c in z["cols"]]
        W5 = pd.DataFrame(z["scores"], index=a5, columns=c5).reindex(index=wk, columns=cols)
        Sdf = pd.DataFrame(f22.wr((W5, 2), (PITSC, 1)), index=wk, columns=cols)
        for k in MULTS:
            c = stress_cfg(cfg, k)
            s = f29._run_series(m, wk, mask, pit, c, pos_of, mask_np, op_adj, Sdf)
            r = {"full": f29._zone(s, *FULL), "OOS": f29._zone(s, *OOS),
                 "DEAD": f29._zone(s, *DEAD)}
            res["arms"].setdefault(seed, {})[f"k{k:g}"] = r
            log.info("[%s|k=%g] 全史 %+.2fpp (MDD %.1f%%, 换手 %.3f) | 留出集 %+.2fpp | 死区 %+.2fpp",
                     seed, k, r["full"]["excess_pp"], 100 * r["full"]["mdd"],
                     r["full"]["turnover"], r["OOS"]["excess_pp"], r["DEAD"]["excess_pp"])

    # 盈亏平衡倍率(全史超额=0 的内插) —— 只对 seedA/seedB 各自算, 再取均值
    breaks = {}
    for seed in SEED_FILES:
        pts = [(k, res["arms"][seed][f"k{k:g}"]["full"]["excess_pp"]) for k in MULTS]
        pts = [(k, e) for k, e in pts if e is not None]
        b = None
        for (k0, e0), (k1, e1) in zip(pts, pts[1:]):
            if e0 == 0 or (e0 > 0) != (e1 > 0):
                b = k0 if e0 == 0 else k0 + (k1 - k0) * e0 / (e0 - e1)
                break
        breaks[seed] = None if b is None else round(float(b), 2)
    res["breakeven_mult"] = breaks
    k15 = [res["arms"][s]["k1.5"]["full"]["excess_pp"] for s in SEED_FILES]
    res["verdict"] = {"W_A2_pass_1p5x_positive": bool(min(k15) > 0),
                      "excess_at_1p5x": {s: res["arms"][s]["k1.5"]["full"]["excess_pp"]
                                         for s in SEED_FILES},
                      "breakeven_mult": breaks,
                      "C4_10pp_at_1p5x": bool(min(
                          res["arms"][s]["k1.5"]["full"]["excess_pp"] for s in SEED_FILES) >= 10.0)}
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== W-A2: 1.5× 下超额仍>0 = %s | 盈亏平衡倍率 %s | C4(1.5× 下 ≥10pp) = %s | %.1fs → %s ==",
             res["verdict"]["W_A2_pass_1p5x_positive"], breaks,
             res["verdict"]["C4_10pp_at_1p5x"], res["secs"], OUT)


if __name__ == "__main__":
    main()
