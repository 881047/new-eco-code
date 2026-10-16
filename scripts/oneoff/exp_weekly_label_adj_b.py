"""周频标签「复权 vs 未复权」的**训练 A/B**（阶段 B，2026-09-14）

判据**已预注册**在 `scripts/oneoff/exp_weekly_label_adj.py` 的 docstring 里（阶段 A 已跑）：
采用复权标签 ⇔ ① 留出集(2024-01~2026-08)超额 ≥ 未复权臂 − 0.5pp（两套种子均值）；
② 全史 MDD 不差于未复权臂 + 2pp；③ 逐锚点 rank IC 均值 ≥ 未复权臂（严格不降）。
三条任一不满足 ⇒ **不采纳**（维持未复权，登记为"已知且被测试钉住的口径"）。

## 只换标签，其它一律不动

- 特征 / 锚点 / 可交易掩码 / 训练窗 / EMBARGO / params / 种子：**两臂完全共享**
  （只调一次 `F4.build_dataset()`，两臂同一份 `F`）；
- **RAW 臂 = 生产在跑的那份 W5 存档**（`experiments/05_label/F4_scores_seed{A,B}.npz`，逐位不动）；
- **ADJ 臂** = 同一份数据集，只把喂给 `forward_excess_open` 的 `m["open"]` 换成 `open × adj`
  再跑 `F4.train_scores()`（它只从 `ds["_m"]` 取标签 —— 这就是 F4 自带的干净接缝）；
- 组合与模拟**照抄 F52 的生产口径**：`S = wr(2×W5, 1×pit)`、top100 / 退出带 8× / hold_bonus=0 /
  1,000,000 本金 / `F1b.run_overlap_portfolio`；基准 = 可交易池等权（复权开盘，F52 同式）。

## 口径纪律（避免两把尺混用）

- **收益率读数（超额 / MDD / 换手 / 投入率）由模拟器与真实价格决定 ⇒ 两臂同一把尺**，直接可比；
- **IC 报 2×2**（训练标签 × 评估标签），**主对照 = "as-deployed" 配对**：(raw,raw) vs (adj,adj)
  —— 即"真按那一套训练+评估会看到什么"；跨配对只作诊断（拿 raw 尺去评 adj 臂会被系统性扣分，
  那不是决策问题、是度量错配）。

## 跑法与产物

    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\exp_weekly_label_adj_b.py --seed A
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\exp_weekly_label_adj_b.py --seed AB

产物（`experiments/10_label_adj_ab/`）：`AB_result.json`（读数 + 逐条判据）、
`AB_series_{seed}_{arm}.json`（逐期序列，便于复核）、`W5adj_{seed}.npz`（ADJ 臂打分，可复算、不入 git）。
**不写任何生产产物**（生产打分 / 方案 / 账本一律不碰）。
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

EXP = BASE / "experiments" / "05_label"
OUTDIR = BASE / "experiments" / "10_label_adj_ab"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 顺序有讲究：先 F4（训练），再 F52（它自己把 f1/f1b/f17/f22 注册进 sys.modules，
# 并暴露 zone_stats / FULL / ACTIVE / MIN_NAMES —— 判定口径直接复用，不重写第二套）
f4 = _load("f4", EXP / "F4_weekly_lgb.py")
f52 = _load("f52", EXP / "F52_live_vs_research_shape.py")
f1b, f17, f22 = f52.f1b, f52.f17, f52.f22

from app import config as cfg_mod              # noqa: E402
from app.factors import factor_engine as fe    # noqa: E402

log = logging.getLogger("label_adj_b")

CAPITAL, TOP_N, BAND = 1_000_000.0, 100, 8.0
SEEDS = ("seedA", "seedB")
W5_FILE = {"seedA": EXP / "F4_scores_seedA.npz", "seedB": EXP / "F4_scores_seedB.npz"}
SEED_NUM = {"seedA": 42, "seedB": 7}          # 与 F4.SEEDS 前两个一致
CRIT_EXCESS_PP, CRIT_MDD_PP = 0.5, 2.0        # 预注册判据 ① ② 的容差


def _w5(path: Path, wk, cols) -> pd.DataFrame:
    z = np.load(path, allow_pickle=True)
    return pd.DataFrame(z["scores"],
                        index=pd.to_datetime([str(d) for d in z["anchors"]]),
                        columns=[str(c) for c in z["cols"]]).reindex(index=wk, columns=cols)


def _bench(m, d, mask, dates, cols) -> list:
    """基准 = 可交易池等权（锚点 → 执行日的**复权**开盘），逐字照 F52。"""
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
                         if good.sum() >= f52.MIN_NAMES else np.nan)
        prev_exec, prev_ap = e, int(r["anchor_pos"])
    return bench


def run_arm(m, wk, cols, mask, pit, Sdf, cfg):
    """一套打分 → 生产构型的逐期序列（F52 同口径）。"""
    c = {**cfg, "capital": CAPITAL,
         "regime_equity_weight": {"aggressive": {"attack": 1.0, "neutral": 1.0,
                                                 "defense": 1.0}}}
    d = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, c, pit, 0, score_matrix=Sdf,
                                  top_n=TOP_N, rank_band=BAND, hyst=0.0)
    if d is None:
        return None
    d = d.rename(columns={"n": "n_positions"})
    d["bench_ret"] = _bench(m, d, mask, pd.to_datetime(m["close"].index), cols)
    return d


def ic_series(ds, S, yardstick_m) -> pd.Series:
    """逐锚点 IC；`yardstick_m` 决定"用哪把尺评"（`f4.score_ic` 只从 ds["_m"] 取标签）。"""
    d = {"wk": ds["wk"], "mask": ds["mask"], "cols": ds["cols"], "_m": yardstick_m, "_h": 5}
    return f4.score_ic(d, S)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", default="A", choices=["A", "AB"])
    ap.add_argument("--retrain-raw", action="store_true",
                    help="RAW 臂也**用当前面板重训**（对照组）：排除'档案是 09-12 训的、"
                         "而 ADJ 是今天训的'这个数据版本混杂 —— 主口径仍是冻结档案")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    m_adj = dict(m)
    m_adj["open"] = m["open"] * m["adj"]           # 唯一改动：标签用的开盘价带上复权
    ds = f4.build_dataset(m, cfg)                  # 特征/掩码/锚点（两臂共享，只建一次）
    ds["_m"], ds["_h"] = m, 5
    wk, cols, mask, pit = ds["wk"], ds["cols"], ds["mask"], ds["pit"]
    log.info("面板就绪 | 锚点 %d | 票池 %d | 因子 %d | %.0fs",
             len(wk), len(cols), len(ds["names"]), time.time() - t0)

    conn = sqlite3.connect(f"file:{BASE / 'data' / 'neweco.db'}?mode=ro", uri=True)
    PITSC = f17.table_scores(conn, "model_score_die_pit", wk, cols)
    conn.close()

    OUTDIR.mkdir(parents=True, exist_ok=True)
    seeds = SEEDS[:1] if a.seed == "A" else SEEDS
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "question": "周频训练标签从裸 open 换成 open×adj —— 会不会更好？",
           "criteria": {"excess_pp_tol": CRIT_EXCESS_PP, "mdd_pp_tol": CRIT_MDD_PP,
                        "ic": "adj ≥ raw（严格不降）"},
           "design": {"RAW": "F4_scores_{seed}.npz（生产在跑的 W5）",
                      "ADJ": "同一份 build_dataset + 同一 params/种子，只换标签",
                      "shape": "top100 / band 8× / hold_bonus 0 / 1,000,000",
                      "blend": "2×W5 + 1×pit（f22.wr）"},
           "windows": {"full": list(f52.FULL), "active": list(f52.ACTIVE)},
           "results": {}, "ic": {}, "criteria_eval": {}, "verdict": {}, "secs": None}

    # ---- ADJ 臂训练（RAW 臂直接用生产存档，不重训）----
    ds_adj = dict(ds)
    ds_adj["_m"] = m_adj
    for seed in seeds:
        W5_raw = _w5(W5_FILE[seed], wk, cols)
        log.info("[%s] 训练 ADJ 臂（seed=%s）…", seed, SEED_NUM[seed])
        t1 = time.time()
        W5_adj = f4.train_scores(ds_adj, cfg, seeds=[SEED_NUM[seed]],
                                 scores_file=OUTDIR / f"W5adj_{seed}.npz")
        W5_adj = pd.DataFrame(W5_adj, index=wk, columns=cols)
        log.info("[%s] ADJ 训练完成 %.0fs", seed, time.time() - t1)
        arms = [("RAW", W5_raw), ("ADJ", W5_adj)]
        if a.retrain_raw:
            log.info("[%s] 重训 RAW 臂（同面板同种子，只换标签 —— 版本混杂对照）…", seed)
            t2 = time.time()
            W5_rt = f4.train_scores(ds, cfg, seeds=[SEED_NUM[seed]],
                                    scores_file=OUTDIR / f"W5raw_retrain_{seed}.npz")
            arms.append(("RAW_retrain", pd.DataFrame(W5_rt, index=wk, columns=cols)))
            log.info("[%s] RAW_retrain 完成 %.0fs", seed, time.time() - t2)
        res["results"][seed] = {}
        res["ic"][seed] = {}
        for arm, W5 in arms:
            Sdf = pd.DataFrame(f22.wr((pd.DataFrame(W5), 2), (PITSC, 1)),
                               index=wk, columns=cols)
            d = run_arm(m, wk, cols, mask, pit, Sdf, cfg)
            if d is None:
                res["results"][seed][arm] = {"error": "模拟无输出"}
                continue
            res["results"][seed][arm] = {"full": f52.zone_stats(d, *f52.FULL),
                                         "active": f52.zone_stats(d, *f52.ACTIVE)}
            d[["date", "ret", "bench_ret", "turnover", "invested_ratio",
               "n_positions"]].to_json(
                OUTDIR / f"AB_series_{seed}_{arm}.json", orient="records",
                force_ascii=False, indent=1)
            # IC 2×2：四格都算，主对照是 (raw,raw) vs (adj,adj)
            for yname, ym in (("raw", m), ("adj", m_adj)):
                ic = ic_series(ds, W5.values, ym)
                res["ic"][seed][f"{arm}_{yname}"] = {
                    "mean": round(float(ic.mean()), 5), "n": int(ic.notna().sum())}
            f = res["results"][seed][arm]["full"]
            ac = res["results"][seed][arm]["active"]
            log.info("[%s|%s] full 超额 %spp / MDD %s | 留出集 超额 %spp / MDD %s",
                     seed, arm, f.get("excess_pp"), f.get("mdd"),
                     ac.get("excess_pp"), ac.get("mdd"))

    # ---- 判据（逐条 + 两套种子，全部满足才算通过）----
    for seed in seeds:
        raw = res["results"][seed].get("RAW") or {}
        adj = res["results"][seed].get("ADJ") or {}
        if not raw.get("full") or not adj.get("full"):
            res["criteria_eval"][seed] = {"error": "缺读数"}
            continue
        d_ex = adj["active"].get("excess_pp", np.nan) - raw["active"].get("excess_pp", np.nan)
        d_md = (adj["full"].get("mdd", np.nan) - raw["full"].get("mdd", np.nan)) * 100
        ic_raw = res["ic"][seed]["RAW_raw"]["mean"]
        ic_adj = res["ic"][seed]["ADJ_adj"]["mean"]
        res["criteria_eval"][seed] = {
            "d_holdout_excess_pp": round(float(d_ex), 2),
            "d_full_mdd_pp": round(float(d_md), 2),
            "ic_raw_arm": ic_raw, "ic_adj_arm": ic_adj,
            "d_ic": round(float(ic_adj - ic_raw), 5),
            "c_excess": bool(d_ex >= -CRIT_EXCESS_PP),
            "c_mdd": bool(d_md >= -CRIT_MDD_PP),
            "c_ic": bool(ic_adj >= ic_raw),
        }
        res["criteria_eval"][seed]["PASS"] = all(
            res["criteria_eval"][seed][k] for k in ("c_excess", "c_mdd", "c_ic"))
    ok = bool(res["criteria_eval"]) and all(
        v.get("PASS") for v in res["criteria_eval"].values())
    # ---- 稳健性对照（**不计入预注册判决**）：同面板重训的 RAW 臂 ----
    if a.retrain_raw:
        rob = {}
        for seed in seeds:
            raw = res["results"][seed].get("RAW_retrain") or {}
            adj = res["results"][seed].get("ADJ") or {}
            if not raw.get("full") or not adj.get("full"):
                rob[seed] = {"error": "缺读数"}
                continue
            rob[seed] = {
                "d_holdout_excess_pp": round(
                    adj["active"].get("excess_pp", np.nan)
                    - raw["active"].get("excess_pp", np.nan), 2),
                "d_full_excess_pp": round(
                    adj["full"].get("excess_pp", np.nan)
                    - raw["full"].get("excess_pp", np.nan), 2),
                "d_full_mdd_pp": round((adj["full"].get("mdd", np.nan)
                                        - raw["full"].get("mdd", np.nan)) * 100, 2),
                "ic_raw": res["ic"][seed]["RAW_retrain_raw"]["mean"],
                "ic_adj": res["ic"][seed]["ADJ_adj"]["mean"],
            }
            rob[seed]["d_ic"] = round(rob[seed]["ic_adj"] - rob[seed]["ic_raw"], 5)
        res["robustness_vintage_matched"] = rob
    res["verdict"] = {
        "adopt_adjusted_label": bool(ok),
        "conclusion": ("三条判据全部成立 ⇒ **建议采纳复权标签**（下一步：换 pin + 重钉生产打分 + 更新 KB）"
                       if ok else
                       "至少一条不成立 ⇒ **不采纳**（维持未复权；登记为'已知且被测试钉住的口径'）"),
        "note": "两套种子全部满足才算通过（预注册写死）；跨配对 IC 只作诊断、不参与判定"}
    res["secs"] = round(time.time() - t0, 1)
    (OUTDIR / "AB_result.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps({"criteria_eval": res["criteria_eval"], "verdict": res["verdict"]},
                     ensure_ascii=False, indent=1))
    log.info("写入 %s（%.0fs）", OUTDIR / "AB_result.json", res["secs"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
