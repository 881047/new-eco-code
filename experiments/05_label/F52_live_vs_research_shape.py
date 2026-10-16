"""F52 · **研究构型 vs 生产构型**：100 万档的 `hold_bonus` 校准（2026-09-13）

## 为什么做（先摆已核实的事实）

- 生产 `plan.pick_shape(1_000_000)` ⇒ **(top_n=100, band=8.0, hold_bonus=0.0)**；
  拟执行方案 `data/weekly/plan_20260911.json` 里也只有 `hold_bonus=0.0`（2026-09-13 实读）。
- 而 100 万档的**研究**脚本几乎都传 `hyst=0.2`：F13/F14/F16/F17/F20/F21/F22/F23/F24/F25/F28/
  F29/F33/F34/F37/F38/F41（逐文件核过）；F7 用的是 `hyst=0.0`。
- 方案里给用户看的 `expectation`（超额 17.65pp / MDD −36.9% / 换手 0.092，留出集 +10.38/+11.80pp）
  来源是 **F25/F41** ⇒ 那组路演数字描述的是"带 0.2σ 持仓加成"的构型，**不是生产在跑的构型**。

## 判据（跑数前冻结；这是**单次校准**，不是搜索 ⇒ 不增加多重比较负担）

- 臂：`A0_生产构型(hyst=0.0)` 与 `A1_研究构型(hyst=0.2)`；其余全同
  （1,000,000 / top100 / 出带 8× / w=1.0 / 2×W5+pit / 同一段 491 期 / 两套种子）；
- 窗口：full（2010-01-01~2030-12-31 截断到实际 491 期）+ ACTIVE（2024-01~2026-08，
  **F28 已开封** ⇒ 只作方向，不产生新的显著性证据）；
- 判读（两套种子同时满足才算"无实质差异"）：
    |Δ超额| ≤ **1.0pp** 且 |ΔMDD| ≤ **2.0pp**  ⇒ 研究读数可继续引用（但 02/05 必须加注"含 hold_bonus=0.2"）；
    否则 ⇒ **以生产构型读数为准**，改 `plan.py` 的 DIAL_POINTS/EXPECTATION 与 KB 02/05 的数字，
           并把差异如实登记（研究构型降换手 ⇒ 差异方向预期是"研究读数偏乐观"）。

## 顺带（补 14 号第 9 条的另一半）

把**逐期序列**落盘（date / ret / bench_ret / turnover / invested_ratio / n）⇒ 之后第三方体检的
"逐期持仓数""逐期投入率"两条也能算，不用再重跑模拟器。

用法：
    D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F52_live_vs_research_shape.py [--seed A|AB]
"""
import argparse
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
    sys.modules[name] = mod            # @dataclass 需要 cls.__module__ 可查
    spec.loader.exec_module(mod)
    return mod


f1 = _load("f1", HERE / "F1_weekly_ic_decay.py")
f1b = _load("f1b", HERE / "F1b_weekly_open_overlap.py")
f17 = _load("f17", HERE / "F17_die_registry_kgd.py")
f22 = _load("f22", HERE / "F22_pair_final_selection.py")

from app import config as cfg_mod              # noqa: E402
from app.factors import factor_engine as fe    # noqa: E402
from app.pit.engine import PITEngine           # noqa: E402

log = logging.getLogger("f52")
OUT = HERE / "F52_live_vs_research_shape_result.json"
CAPITAL, TOP_N, BAND = 1_000_000.0, 100, 8.0
ARMS = {"A0_生产构型(hyst=0.0)": 0.0, "A1_研究构型(hyst=0.2)": 0.2}
SEEDS = ("seedA", "seedB")
SEED_FILE = {"seedA": "F4_scores_seedA.npz", "seedB": "F4_scores_seedB.npz"}
FULL = ("2010-01-01", "2030-12-31")
ACTIVE = ("2024-01-01", "2026-08-31")
MIN_NAMES = 200
PER_YEAR = 52.0
TOL_EXCESS_PP, TOL_MDD_PP = 1.0, 2.0


def zone_stats(d: pd.DataFrame, lo: str, hi: str) -> dict:
    s = d[(d["date"] >= lo) & (d["date"] <= hi)].dropna(subset=["ret", "bench_ret"])
    if len(s) < 2:
        return {"n": int(len(s))}
    a, b = f1b.ann(s["ret"], PER_YEAR), f1b.ann(s["bench_ret"], PER_YEAR)
    nav = s["nav"]
    return {"n": int(len(s)), "excess_pp": round(100 * (a - b), 2),
            "mdd": round(float((nav / nav.cummax() - 1).min()), 4),
            "turnover": round(float(s["turnover"].mean()), 3),
            "invested": round(float(s["invested_ratio"].mean()), 4),
            "names": round(float(s["n_positions"].mean()), 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", default="A", choices=["A", "AB"], help="A=只跑 seedA(先筛)")
    a = ap.parse_args()
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
    conn = sqlite3.connect(f"file:{BASE / 'data' / 'neweco.db'}?mode=ro", uri=True)
    PITSC = f17.table_scores(conn, "model_score_die_pit", wk, cols)
    conn.close()
    log.info("面板就绪 | 锚点 %d | 票池 %d 只 | %.0fs", len(wk), len(cols), time.time() - t0)

    seeds = SEEDS[:1] if a.seed == "A" else SEEDS
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "question": "生产构型(hold_bonus=0.0) 与 研究构型(hyst=0.2) 在 100 万档差多少？",
           "tolerance": {"excess_pp": TOL_EXCESS_PP, "mdd_pp": TOL_MDD_PP},
           "arms": list(ARMS), "seeds": list(seeds),
           "windows": {"full": FULL, "active": ACTIVE},
           "note": "单次校准, 不是搜索; ACTIVE 是 F28 已开封的留出集 ⇒ 只作方向",
           "results": {}, "verdict": {}, "secs": None}
    for seed in seeds:
        z = np.load(HERE / SEED_FILE[seed], allow_pickle=True)
        W5 = pd.DataFrame(z["scores"],
                          index=pd.to_datetime([str(d) for d in z["anchors"]]),
                          columns=[str(c) for c in z["cols"]]).reindex(index=wk, columns=cols)
        Sdf = pd.DataFrame(f22.wr((W5, 2), (PITSC, 1)), index=wk, columns=cols)
        res["results"][seed] = {}
        for arm, hyst in ARMS.items():
            c = {**cfg, "capital": CAPITAL,
                 "regime_equity_weight": {"aggressive": {"attack": 1.0, "neutral": 1.0,
                                                         "defense": 1.0}}}
            d = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, c, pit, 0, score_matrix=Sdf,
                                          top_n=TOP_N, rank_band=BAND, hyst=hyst)
            if d is None:
                log.error("[%s|%s] 无输出", seed, arm)
                continue
            d = d.rename(columns={"n": "n_positions"})
            # 基准：与 F41 同口径（可交易池等权、锚点→执行日复权开盘）
            op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
            pos_of = {x: i for i, x in enumerate(dates)}
            mask_np = mask.fillna(False).to_numpy()
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
            res["results"][seed][arm] = {"full": zone_stats(d, *FULL),
                                         "active": zone_stats(d, *ACTIVE)}
            # 逐期序列落盘（补 14 号第 9 条的另一半：逐期投入率/持仓数）
            ser = d[["date", "ret", "bench_ret", "turnover", "invested_ratio", "n_positions"]]
            ser.to_json(HERE / f"F52_series_{seed}_{arm.split('_')[0]}.json",
                        orient="records", force_ascii=False, indent=1)
            f = res["results"][seed][arm]["full"]
            log.info("[%s|%s] full: 超额 %spp | MDD %s | 换手 %s | 投入 %s | 持仓 %s 只",
                     seed, arm, f.get("excess_pp"), f.get("mdd"), f.get("turnover"),
                     f.get("invested"), f.get("names"))

    # 判读：两套种子同时满足阈值才算"无实质差异"
    ok_all, deltas = True, {}
    arm_live, arm_research = list(ARMS)[0], list(ARMS)[1]
    for seed in seeds:
        r = res["results"].get(seed) or {}
        a0, a1 = r.get(arm_live), r.get(arm_research)
        if not a0 or not a1:
            ok_all = False
            continue
        de = abs(a1["full"]["excess_pp"] - a0["full"]["excess_pp"])
        dm = abs(a1["full"]["mdd"] - a0["full"]["mdd"]) * 100
        deltas[seed] = {"delta_excess_pp": round(de, 2), "delta_mdd_pp": round(dm, 2),
                        "within_tolerance": bool(de <= TOL_EXCESS_PP and dm <= TOL_MDD_PP)}
        ok_all = ok_all and deltas[seed]["within_tolerance"]
    res["deltas"] = deltas
    res["verdict"] = {
        "within_tolerance": bool(ok_all),
        "conclusion": ("两构型在容差内 ⇒ 研究读数可继续引用（02/05 需加注'含 hold_bonus=0.2'）"
                       if ok_all else
                       "**超出容差** ⇒ 以生产构型(A0)读数为准，改 plan.py 的 DIAL_POINTS/EXPECTATION 与 KB 02/05"),
        "note": "A0 = 生产在跑的构型（hold_bonus=0.0）；A1 = F25/F41 那批研究用的构型（hyst=0.2）"}
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"deltas": deltas, "verdict": res["verdict"]}, ensure_ascii=False, indent=1))
    print(f"写入 {OUT.name}（{res['secs']}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
