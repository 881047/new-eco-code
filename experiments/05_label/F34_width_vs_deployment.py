"""F34 · 构型宽度 vs 资金投放（整手粒度那条杠杆）—— 预注册

## 为什么做这一件

F24 把实施缺口拆开：**−9.02pp/年**，其中 **现金拖累 −6.92pp** 是最大单项。现金从哪来？
两层：① 权益权重 0.873（继承月频，**用户决策**，见 F25）；② **整手粒度** ——
1M / top100 下每只预算 `1M×0.873/100 = 8,730 元`：一只 50 元的票只能买 1 手（5,000 元，
利用率 57%），一只 100 元的票直接买不起 ⇒ 实测平均只投出 **76.9%**。

**宽度直接决定每只预算** ⇒ 是唯一还没测过的"结构性"投放杠杆（F23 测的是价格上限/顺延，
F4 测的是 20 万账户的宽度，F25 测的是权益权重本身）。

## 臂（其余全部冻结：现役 2×W5+pit 融合分 / 1M / 退出带 8× / 持仓加成 0.2σ / 两套种子）

    top_n ∈ {50, 100（现役）, 150, 200}     退出带倍数固定 8×（"窄进宽出"的比例不变）

## 预注册判据（**组合两轴 + 投放率**，三轴同时）

    采纳 = 两套种子上同时：
      ① **投入率 ≥ 现役 + 5pp**（这是本实验的目的；F24 说现金拖累值 −6.9pp，5pp 是它的量级）；
      ② 全史超额 ≥ 现役；
      ③ MDD 不劣于现役 × 1.10。
    多个臂满足 ⇒ 取两套种子超额均值最高者。

## 诚实声明

- 留出集（2024-01~2026-08）读数一并报出，但**不作判据**（F28 已一次性开封）；
- 宽度**同时**改变两件事：每只预算（→ 投放率）与分散度（→ 特质风险）。所以判据必须两轴都看 ——
  只看投放率会把人引向"少买几只、把钱堆上去"，那正是 F23 里被否掉的贪婪预算；
- 本实验不动信号、不动费用、不动撮合。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F34_width_vs_deployment.py
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

log = logging.getLogger("f34")
OUT = HERE / "F34_width_vs_deployment_result.json"
CAPITAL, BAND, HYST = 1_000_000.0, 8.0, 0.2
WIDTHS = (50, 100, 150, 200)
BASE_WIDTH = 100
SEED_FILES = {"seedA": "F4_scores_seedA.npz", "seedB": "F4_scores_seedB.npz"}
MIN_NAMES = 200
PER_YEAR = 52.0
FULL = ("2010-01-01", "2030-12-31")
OOS = ("2024-01-01", "2026-08-31")


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
           "widths": list(WIDTHS), "band": BAND, "hyst": HYST, "capital": CAPITAL,
           "arms": {}, "verdict": {}, "secs": None}
    for seed, fn in SEED_FILES.items():
        z = np.load(HERE / fn, allow_pickle=True)
        a5 = pd.to_datetime([str(d) for d in z["anchors"]])
        c5 = [str(c) for c in z["cols"]]
        W5 = pd.DataFrame(z["scores"], index=a5, columns=c5).reindex(index=wk, columns=cols)
        Sdf = pd.DataFrame(f22.wr((W5, 2), (PITSC, 1)), index=wk, columns=cols)
        for tn in WIDTHS:
            c = {**cfg, "capital": CAPITAL}
            d = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, c, pit, 0, score_matrix=Sdf,
                                          top_n=tn, rank_band=BAND, hyst=HYST)
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
            ok = d["ret"].notna() & d["bench_ret"].notna()
            a_, b_ = f1b.ann(d.loc[ok, "ret"], PER_YEAR), f1b.ann(d.loc[ok, "bench_ret"], PER_YEAR)
            nav = (1 + d.loc[ok, "ret"]).cumprod()
            oo = d[(d["date"] >= OOS[0]) & (d["date"] <= OOS[1])].dropna(
                subset=["ret", "bench_ret"])
            ao, bo = f1b.ann(oo["ret"], PER_YEAR), f1b.ann(oo["bench_ret"], PER_YEAR)
            res["arms"].setdefault(seed, {})[f"top{tn}"] = {
                "n": int(ok.sum()),
                "excess_pp": round(100 * (a_ - b_), 2),
                "mdd": round(float((nav / nav.cummax() - 1).min()), 4),
                "invested": round(float(d["invested_ratio"].mean()), 4),
                "names": round(float(d["n"].mean()), 1),
                "turnover": round(float(d["turnover"].mean()), 3),
                "oos_excess_pp": round(100 * (ao - bo), 2)}
            r = res["arms"][seed][f"top{tn}"]
            log.info("[%s|top%-3d] 全史 %+.2fpp (MDD %.1f%%) | **投入率 %.3f** (持仓 %.0f 只) "
                     "| 换手 %.3f | 留出集 %+.2fpp",
                     seed, tn, r["excess_pp"], 100 * r["mdd"], r["invested"], r["names"],
                     r["turnover"], r["oos_excess_pp"])

    cands = []
    for tn in WIDTHS:
        if tn == BASE_WIDTH:
            continue
        ok = True
        for seed in SEED_FILES:
            b = res["arms"][seed][f"top{BASE_WIDTH}"]
            c = res["arms"][seed][f"top{tn}"]
            if not (c["invested"] >= b["invested"] + 0.05
                    and c["excess_pp"] >= b["excess_pp"]
                    and c["mdd"] >= b["mdd"] * 1.10):
                ok = False
        if ok:
            cands.append((f"top{tn}",
                          float(np.mean([res["arms"][s][f"top{tn}"]["excess_pp"]
                                         for s in SEED_FILES]))))
    cands.sort(key=lambda kv: -kv[1])
    res["verdict"] = {"adopt": cands[0][0] if cands else None,
                      "candidates": [[k, round(v, 2)] for k, v in cands],
                      "criteria": "两套种子同时: 投入率≥现役+5pp 且 全史超额≥现役 且 MDD≥现役×1.10",
                      "note": "投产率是机制、不是拟合; 但宽度同时改变分散度 ⇒ 两轴必须同时看"}
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 判决: 采纳=%s | 候选=%s | %.1fs → %s ==",
             res["verdict"]["adopt"] or "维持 top100", res["verdict"]["candidates"],
             res["secs"], OUT)


if __name__ == "__main__":
    main()
