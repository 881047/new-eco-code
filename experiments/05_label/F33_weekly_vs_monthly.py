"""F33 · 周频 vs 月频两轴（§5 W-A6）—— 预注册

## 为什么做这一件

§5 的 **W-A6** 是清单里最后一条没在当前 die 上跑过的实验：**调仓频率本身值不值**。
它有两个必须回答的现实动机：
  1. **成本**（F30）：周频换手 0.09/周，费+滑点吃掉 2.2pp/年（17.13 毛 → 14.91 净）；
     月频换手天然低 ~4 倍 ⇒ 若信号不衰减那么快，月频可能**净超额更高**；
  2. **信号**（F24/F32b）：毛超额一半以上是风格，而风格暴露的持续性以**月**为单位，
     5 日标签+周频调仓可能只是把同一份暴露**付了四次费**。

## 三个臂（同一套分数构件：`2×die + 1×pit` 的 2:1 融合，与线上一致）

| 臂 | 打分 | 锚点 | 标签 horizon | 年化因子 |
|---|---|---|---|---|
| `W5w` | W5（5 日标签，**复用 F29 存档**） | 周锚点 | 5 | 52 |
| `W5m` | 同上（**同一份分数**，只换调仓节奏 ⇒ 隔离"频率"变量） | 月锚点(取 ≤ 月末的最近周打分) | 5 | 12 |
| `M21` | 用 **21 日标签重新训练**（月锚点、月末重训、禁运 3） | 月锚点 | 21 | 12 |

融合里的第二路 `pit` 用生产表 `model_score_die_pit` 在**各自锚点**上取（同一份数据）。
构型冻结：100 万 / top100 / 退出带 8× / 持仓加成 0.2σ / 两套种子 SA、SB。

**频率相关的训练窗（事前写死）**：训练窗是按**月**计的常量 —— 生产 36 个月在周锚点下是
~156 个锚点，但**月锚点下只有 36 个 < 训练循环的 40 锚点硬门槛**（实测：0 个模型）。
⇒ 月频臂用 **48 个月**（48 个锚点，刚过门槛，且仍是"4 年历史"这个量级），周频臂沿用生产 36 个月。
这是频率本身的固有差异，不是调参；两臂都报出实际训练到的模型数。

## 预注册判据（"月频是否应当取代周频"）

    采纳"改月频" = 两套种子上同时：
      ① 月频**全史超额** ≥ 周频；
      ② 月频 **MDD** 不劣于周频（即 mdd_month ≥ mdd_week，负数更大者胜）；
      ③ 至少一轴的改善超过噪声门槛：超额 ≥ +2.0pp **或** MDD 改善 ≥ 5pp。
    （月频观测数只有 1/4，噪声更大 ⇒ 门槛按此放大；另报配对差值的 HAC t 作诊断。）

判决只针对**频率**，不改变其它任何参数；`M21` 若胜出，仍需在新留出窗上确认（§2.28 已开封）。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F33_weekly_vs_monthly.py
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
from app.weekly import scores as wsc          # noqa: E402

log = logging.getLogger("f33")
OUT = HERE / "F33_weekly_vs_monthly_result.json"
CAPITAL, TOP_N, BAND, HYST = 1_000_000.0, 100, 8.0, 0.2
MIN_NAMES = 200
SETS = {"SA": (42, 7, 123, 2024), "SB": (43, 8, 124, 2025)}
FULL = ("2010-01-01", "2030-12-31")
OOS = ("2024-01-01", "2026-08-31")


def run_arm(m, anchors, mask, pit, cfg, cols, pos_of, op_adj, Sdf, horizon, per_year):
    """跑一条臂并接上**决策日票池**基准, 用该频率的年化因子统计。"""
    mask_np = mask.fillna(False).to_numpy()
    df = f1b.run_overlap_portfolio(m, {}, anchors, mask, horizon, {**cfg, "capital": CAPITAL},
                                   pit, 0, score_matrix=Sdf, top_n=TOP_N, rank_band=BAND,
                                   hyst=HYST)
    if df is None:
        return None
    bench, prev_exec, prev_ap = [], None, None
    for _, r in df.iterrows():
        e = pos_of[r["date"]]
        if prev_exec is None:
            bench.append(np.nan)
        else:
            good = (mask_np[int(prev_ap)] & np.isfinite(op_adj[prev_exec])
                    & np.isfinite(op_adj[e]) & (op_adj[prev_exec] > 0))
            bench.append(float(np.mean(op_adj[e][good] / op_adj[prev_exec][good] - 1))
                         if good.sum() >= MIN_NAMES else np.nan)
        prev_exec, prev_ap = e, int(r["anchor_pos"])
    df["bench_ret"] = bench
    return df


def zone(df, lo, hi, per_year):
    s = df[(df["date"] >= lo) & (df["date"] <= hi)].dropna(subset=["ret", "bench_ret"])
    if len(s) < 5:
        return {"n": int(len(s))}
    a, b = f1b.ann(s["ret"], per_year), f1b.ann(s["bench_ret"], per_year)
    nav = (1 + s["ret"]).cumprod()
    ex = s["ret"] - s["bench_ret"]
    t = f1b.hac_t(ex.to_numpy(), 2)
    return {"n": int(len(s)), "excess_pp": round(100 * (a - b), 2),
            "mdd": round(float((nav / nav.cummax() - 1).min()), 4),
            "turnover": round(float(s["turnover"].mean()), 3),
            "inv": round(float(s["invested_ratio"].mean()), 4),
            "t": None if not np.isfinite(t) else round(float(t), 2)}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    wsc.N_JOBS = 24
    m = fe.load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    mt = fe.month_end_dates(dates)
    cols = list(m["close"].columns)
    pit = PITEngine(m=m, cfg=cfg)
    mask_w = f1.tradable_frame(pit, wk, cols)
    mask_m = f1.tradable_frame(pit, mt, cols)
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    conn = sqlite3.connect(f"file:{BASE/'data'/'neweco.db'}?mode=ro", uri=True)
    PIT_w = f17.table_scores(conn, "model_score_die_pit", wk, cols)
    PIT_m = f17.table_scores(conn, "model_score_die_pit", mt, cols)
    conn.close()
    log.info("周锚点 %d | 月锚点 %d | 股票 %d", len(wk), len(mt), len(cols))

    panel_m = None
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "sets": {k: list(v) for k, v in SETS.items()}, "arms": {}, "verdict": {}, "secs": None}
    for set_name in SETS:
        # --- W5w / W5m: 复用 F29 的 W5 存档(同一面板、同种子集) ---
        z = np.load(HERE / f"F29_scores_{set_name}_A0_base.npz", allow_pickle=True)
        W5 = pd.DataFrame(np.asarray(z["scores"], dtype=np.float32),
                          index=pd.to_datetime([str(d) for d in z["anchors"]]),
                          columns=[str(c) for c in z["cols"]]).reindex(index=wk, columns=cols)
        # W5m: 月锚点上取"最近一个已完成的周打分"(不前视)
        W5m = W5.reindex(index=wk).ffill().reindex(index=mt, method="ffill")
        W5m.index = mt
        # --- M21: 21 日标签重训(月锚点) ---
        if panel_m is None:
            panel_m = wsc.build_panel(m, mt, cfg)
            log.info("月频面板 %s", panel_m["F"].shape)
        p21 = HERE / f"F33_scores_{set_name}_M21.npz"
        if p21.exists():
            zz = np.load(p21, allow_pickle=True)
            S21 = np.asarray(zz["scores"], dtype=np.float32)
            good = int(np.isfinite(S21).any(axis=1).sum()) if S21.size else 0
            if good < 20:                    # 空/全 NaN 的存档绝不复用(第一版曾留下 0 模型的产物)
                log.warning("  [%s|M21] 存档只有 %d 个有效锚点 ⇒ 重训", set_name, good)
                p21.unlink()
        if not p21.exists():
            old_tm = wsc.TRAIN_MONTHS
            wsc.TRAIN_MONTHS = 48        # 月锚点: 36 个月只有 36 个锚点 < 40 的硬门槛
            S21 = wsc.train(panel_m, cfg, seeds=SETS[set_name], horizon=21,
                            progress=lambda i, n, s: log.info("  [%s|M21] %d/%d (%.0fs)",
                                                              set_name, i, n, s))
            wsc.TRAIN_MONTHS = old_tm
            np.savez_compressed(p21, scores=S21, anchors=panel_m["anchors"].to_numpy(), cols=cols)
        else:
            log.info("  [%s|M21] 复用已训练存档", set_name)
        M21 = pd.DataFrame(S21, index=mt, columns=cols)

        arms = {
            "W5w": (wk, mask_w, 5, 52.0, pd.DataFrame(f22.wr((W5, 2), (PIT_w, 1)), index=wk, columns=cols)),
            "W5m": (mt, mask_m, 5, 12.0, pd.DataFrame(f22.wr((W5m, 2), (PIT_m, 1)), index=mt, columns=cols)),
            "M21": (mt, mask_m, 21, 12.0, pd.DataFrame(f22.wr((M21, 2), (PIT_m, 1)), index=mt, columns=cols)),
        }
        for tag, (anch, msk, h, py, Sdf) in arms.items():
            d = run_arm(m, anch, msk, pit, cfg, cols, pos_of, op_adj, Sdf, h, py)
            if d is None:
                continue
            r = {"full": zone(d, *FULL, py), "OOS": zone(d, *OOS, py),
                 "n_per_year": py, "horizon": h}
            res["arms"].setdefault(set_name, {})[tag] = r
            log.info("[%s|%s] 全史 %+.2fpp (t=%s, MDD %.1f%%, 换手 %.3f) | 留出集 %+.2fpp",
                     set_name, tag, r["full"]["excess_pp"], r["full"]["t"],
                     100 * r["full"]["mdd"], r["full"]["turnover"], r["OOS"]["excess_pp"])

    # ---- 预注册判据: 月频是否应当取代周频 ----
    verdict = {}
    for set_name in SETS:
        w = res["arms"][set_name]["W5w"]["full"]
        m_ = res["arms"][set_name]["M21"]["full"]
        d_ex = m_["excess_pp"] - w["excess_pp"]
        d_mdd = 100 * (m_["mdd"] - w["mdd"])          # 负得更少 ⇒ 正
        verdict[set_name] = {"excess_delta_pp": round(d_ex, 2), "mdd_delta_pp": round(d_mdd, 2),
                             "pass": bool(d_ex >= 0 and d_mdd >= 0
                                          and (d_ex >= 2.0 or d_mdd >= 5.0))}
    res["verdict"] = {"monthly_beats_weekly": bool(all(v["pass"] for v in verdict.values())),
                      "per_set": verdict,
                      "criteria": "两套种子同时: 月频超额≥周频 且 MDD不劣 且 至少一轴改善(超额≥2pp 或 MDD≥5pp)"}
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== W-A6 判决: 月频取代周频 = %s | %s | %.1fs → %s ==",
             res["verdict"]["monthly_beats_weekly"], verdict, res["secs"], OUT)


if __name__ == "__main__":
    main()
