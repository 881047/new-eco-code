"""F37 · 贪婪预算 + 单票权重上限（**登记过但从未跑过**的中间路线）—— 预注册

## 出处

`plans/weekly-terminal-plan-20260912.md` 早期版本（"可负担性"那节）写着：

    "**下一步（中间路线，尚未做）**：贪婪预算 + 单票权重上限（如不超过 nav×w/top_n 的 1.5 倍）"

F23 当时只测了"价格上限/顺延"，**贪婪预算本身**（`greedy_budget=True`）测出来更糟
（持仓 100→62 只、回撤 −43%→−55.7%）；带**单票上限**的中间路线一直没跑。

## 为什么现在值得跑

F24 把实施缺口拆开：**现金拖累 −6.92pp/年**是最大单项。F34 证明"改宽度"只能拿回 2.8pp，
因为剩下的钱是**整手零头**。而贪婪预算换的是另一种机制：

    A0 现状：每只预算 = nav×w/top_n 固定；买不起一手的票 ⇒ 名额空置、钱留现金
    G      ：每只预算 = **剩余权益预算 / 剩余名额** ⇒ 买不起的票自动把钱让给后面的票
    G+cap  ：再加"单票不超过 1.25/1.5 × 等权预算"的上限 ⇒ 不让钱堆到少数几只上

**与 F25 的敞口刻度不同**（这点必须说清）：F25 是**提高风险预算**（w 从 0.873 提到 1.0）；
这里 w 仍是 0.873 —— 只是把**已经宣布的 87.3% 权益预算真正投出去**。

## 臂（其余全部冻结：2×W5+pit / 100 万 / top100 / 带 8× / 加成 0.2σ / 两套种子）

    A0 现状 | G1 贪婪+上限1.5× | G2 贪婪+上限1.25× | G3 纯贪婪（复现 F23 的负结果）

## 预注册判据（与 F34 同一套三轴，便于横向比较）

    采纳 = 两套种子同时：① **投入率 ≥ 现役 + 5pp**（本实验的目的）；
                        ② 全史超额 ≥ 现役；③ MDD 不劣于现役 × 1.10。
    多个臂满足 ⇒ 取两套种子超额均值最高者。

留出集（2024-01~2026-08）与死区一并报出，**不作判据**（已一次性开封）。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F37_greedy_budget_cap.py
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

log = logging.getLogger("f37")
OUT = HERE / "F37_greedy_budget_cap_result.json"
CAPITAL, TOP_N, BAND, HYST = 1_000_000.0, 100, 8.0, 0.2
SEED_FILES = {"seedA": "F4_scores_seedA.npz", "seedB": "F4_scores_seedB.npz"}
ARMS = {"A0_现状": {},
        "G1_贪婪+上限1.5x": {"greedy_budget": True, "budget_cap_mult": 1.5},
        "G2_贪婪+上限1.25x": {"greedy_budget": True, "budget_cap_mult": 1.25},
        "G3_纯贪婪": {"greedy_budget": True}}
MIN_NAMES = 200
PER_YEAR = 52.0
FULL = ("2010-01-01", "2030-12-31")
OOS = ("2024-01-01", "2026-08-31")
DEAD = ("2020-09-01", "2023-12-31")


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

    def zone(df, lo, hi):
        s = df[(df["date"] >= lo) & (df["date"] <= hi)].dropna(subset=["ret", "bench_ret"])
        if len(s) < 5:
            return {"n": int(len(s))}
        a, b = f1b.ann(s["ret"], PER_YEAR), f1b.ann(s["bench_ret"], PER_YEAR)
        nav = (1 + s["ret"]).cumprod()
        return {"n": int(len(s)), "excess_pp": round(100 * (a - b), 2),
                "mdd": round(float((nav / nav.cummax() - 1).min()), 4)}

    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "arms": {k: v for k, v in ARMS.items()}, "windows": {"full": FULL, "oos": OOS},
           "results": {}, "verdict": {}, "secs": None}
    for seed, fn in SEED_FILES.items():
        z = np.load(HERE / fn, allow_pickle=True)
        a5 = pd.to_datetime([str(d) for d in z["anchors"]])
        c5 = [str(c) for c in z["cols"]]
        W5 = pd.DataFrame(z["scores"], index=a5, columns=c5).reindex(index=wk, columns=cols)
        Sdf = pd.DataFrame(f22.wr((W5, 2), (PITSC, 1)), index=wk, columns=cols)
        for tag, kw in ARMS.items():
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
            r = {"full": zone(d, *FULL), "OOS": zone(d, *OOS), "DEAD": zone(d, *DEAD),
                 "invested": round(float(d["invested_ratio"].mean()), 4),
                 "names": round(float(d["n"].mean()), 1),
                 "turnover": round(float(d["turnover"].mean()), 3)}
            res["results"].setdefault(seed, {})[tag] = r
            log.info("[%s|%s] 全史 %+.2fpp (MDD %.1f%%) | **投入率 %.3f** (持仓 %.0f 只) "
                     "| 换手 %.3f | 留出集 %+.2fpp | 死区 %+.2fpp",
                     seed, tag, r["full"]["excess_pp"], 100 * r["full"]["mdd"], r["invested"],
                     r["names"], r["turnover"], r["OOS"]["excess_pp"], r["DEAD"]["excess_pp"])

    cands = []
    for tag in ARMS:
        if tag == "A0_现状":
            continue
        ok = True
        for seed in SEED_FILES:
            b = res["results"][seed]["A0_现状"]
            c = res["results"][seed].get(tag)
            if c is None or not (c["invested"] >= b["invested"] + 0.05
                                 and c["full"]["excess_pp"] >= b["full"]["excess_pp"]
                                 and c["full"]["mdd"] >= b["full"]["mdd"] * 1.10):
                ok = False
        if ok:
            cands.append((tag, float(np.mean([res["results"][s][tag]["full"]["excess_pp"]
                                              for s in SEED_FILES]))))
    cands.sort(key=lambda kv: -kv[1])
    res["verdict"] = {"adopt": cands[0][0] if cands else None,
                      "candidates": [[k, round(v, 2)] for k, v in cands],
                      "criteria": "两套种子同时: 投入率≥现役+5pp 且 全史超额≥现役 且 MDD≥现役×1.10",
                      "note": "w 不变(仍是 0.873) —— 这只是把已宣布的权益预算真正投出去, 不是提高风险预算"}
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 判决: 采纳=%s | 候选=%s | %.1fs → %s ==",
             res["verdict"]["adopt"] or "维持 A0_现状", res["verdict"]["candidates"],
             res["secs"], OUT)


if __name__ == "__main__":
    main()
