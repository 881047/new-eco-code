"""F5 · 「模型 alpha 是不是买不到的那部分」—— 涨停/跌停闸门的归因诊断

## 背景（F4 的归因读数）

F4 用同一个 LightGBM 打分跑了四个臂，把「费用」与「可交易性闸门」拆开：

    L0        全口径(费用 + 涨跌停闸门)   超额 −23.87pp
    L0_nofee  免费但保留闸门              超额  −4.41pp   ⇒ 费用值 19.5pp
    L0_nogate 计费但关掉闸门              超额 +15.52pp   ⇒ 闸门值 39.4pp
    L0_gross  两者都关                    超额 +31.95pp

⇒ 缺口 55.8pp 里约 70% 来自闸门，不是费用。若成立，含义是：
**模型的大部分预测力集中在「次日开盘已经一字涨停、根本买不进去」的票上。**

本脚本不靠推理，直接数出来：

    Q1 打分 top-6 里, 有多少比例在执行日开盘已经涨停(买不进)？
    Q2 剔除「开盘涨停」后, 剩下票的 5 日前向超额还有多少（分区间、分分数档）？
    Q3 分数十分位的毛超额是否单调？alpha 集中在哪一档？

判读规则（预注册）：
    · top-6 中「开盘涨停」占比 ≥ 10% 且剔除后毛超额 ≤ 0 ⇒ **alpha 不可交易**成立；
    · 单调成立且最高档剔除后仍显著为正 ⇒ 信号可交易，问题只在组合构造。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F5_limitup_diagnostic.py --since 20140101
"""
import argparse
import hashlib
import importlib.util
import json
import logging
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

from app import config as cfg_mod          # noqa: E402
from app.factors import factor_engine as fe  # noqa: E402
from app.pit.engine import PITEngine       # noqa: E402
from app.portfolio import board as board_mod  # noqa: E402

log = logging.getLogger("f5")
OUT = HERE / "F5_limitup_diagnostic_result.json"
ZONES = f1.ZONES
TOP_N = 6


def zone_of(d):
    for z, (lo, hi) in ZONES.items():
        if pd.Timestamp(lo) <= d <= pd.Timestamp(hi):
            return z
    return "PRE"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="20140101")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since=a.since)
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    pit = PITEngine(m=m, cfg=cfg)
    cols = list(m["close"].columns)
    mask = f1.tradable_frame(pit, wk, cols)
    mask_np = mask.to_numpy()
    wk_pos = {d: i for i, d in enumerate(wk)}
    pos_of = {d: i for i, d in enumerate(dates)}

    z = np.load(HERE / "F4_weekly_lgb_scores.npz", allow_pickle=True)
    s_anchors = pd.to_datetime([str(d) for d in z["anchors"]])
    s_cols = [str(c) for c in z["cols"]]
    Sdf = pd.DataFrame(z["scores"], index=s_anchors, columns=s_cols)
    log.info("打分 %s | 周锚点 %d | 票池均值 %.0f", Sdf.shape, len(wk), mask_np.sum(axis=1).mean())

    op_raw = m["open"].reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pc_raw = m["pre_close"].reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    lab = f1b.forward_excess_open(m, mask, 5)          # 可交易窗口超额(毛)
    lab_np = lab.to_numpy(dtype=np.float64)

    rows = []
    for k, d in enumerate(s_anchors):
        if d not in pos_of or d not in wk_pos:
            continue
        i, kw = pos_of[d], wk_pos[d]
        if i + 6 >= len(dates):
            continue
        sc = Sdf.iloc[k].reindex(cols).to_numpy(dtype=np.float64)
        ok = mask_np[kw]
        valid = ok & np.isfinite(sc) & np.isfinite(lab_np[kw])
        if valid.sum() < 200:
            continue
        e = i + 1
        idx_valid = np.where(valid)[0]
        # 十分位(按分数, 10 = 最高分)
        q = pd.qcut(pd.Series(sc[idx_valid]).rank(method="first"), 10, labels=False) + 1
        dec = np.full(len(cols), np.nan)
        dec[idx_valid] = q.to_numpy()
        # 执行日开盘是否涨停(与生产同源: round2(pre_close) × (1+板块限幅))
        lu = np.full(len(cols), np.nan)
        dstr = dates[e].strftime("%Y%m%d")
        for j in idx_valid:
            pc = pc_raw[e, j]
            if not (np.isfinite(pc) and pc > 0):
                continue
            c = cols[j]
            nm = pit.name_at(c, dates[e]) or ""
            ratio = board_mod.limit_ratio(c, "ST" in nm, dstr, nm, pit.name_at)
            lu[j] = board_mod.round2(board_mod.round2(float(pc)) * (1 + ratio))
        at_limit = np.zeros(len(cols), dtype=bool)
        at_limit[idx_valid] = (np.isfinite(lu[idx_valid])
                               & (op_raw[e, idx_valid] >= lu[idx_valid] - 0.001))

        lab_k = lab_np[kw]
        order = idx_valid[np.argsort(-sc[idx_valid])]
        top = order[:TOP_N]
        trad = top[~at_limit[top]]
        lim = top[at_limit[top]]
        rows.append({
            "date": d, "zone": zone_of(d), "n_valid": int(valid.sum()),
            "top_lim": int(at_limit[top].sum()),
            "top_ret": float(np.nanmean(lab_k[top])) if len(top) else np.nan,
            "top_ret_tradable": float(np.nanmean(lab_k[trad])) if len(trad) else np.nan,
            "top_ret_lim": float(np.nanmean(lab_k[lim])) if len(lim) else np.nan,
            "p10_ret": float(np.nanmean(lab_k[dec == 10])),
            "p9_ret": float(np.nanmean(lab_k[dec == 9])),
            "p1_ret": float(np.nanmean(lab_k[dec == 1])),
            "all_lim_frac": float(at_limit[idx_valid].mean()),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        log.error("无有效锚点")
        return
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "n_anchor": int(len(df)), "since": a.since, "by_zone": {}, "per_period_pct": {}}
    for zname, sub in df.groupby("zone"):
        res["by_zone"][zname] = {
            "n": int(len(sub)),
            "top6_lim_frac": round(float(sub["top_lim"].sum() / (TOP_N * len(sub))), 4),
            "all_lim_frac": round(float(sub["all_lim_frac"].mean()), 4),
            "top6_ret_pct": round(100 * float(sub["top_ret"].mean()), 3),
            "top6_tradable_ret_pct": round(100 * float(sub["top_ret_tradable"].mean()), 3),
            "top6_limit_ret_pct": round(100 * float(sub["top_ret_lim"].mean()), 3)
            if sub["top_ret_lim"].notna().any() else None,
            "decile10_ret_pct": round(100 * float(sub["p10_ret"].mean()), 3),
            "decile9_ret_pct": round(100 * float(sub["p9_ret"].mean()), 3),
            "decile1_ret_pct": round(100 * float(sub["p1_ret"].mean()), 3),
        }
    res["per_period_pct"] = {
        "top6": round(100 * float(df["top_ret"].mean()), 3),
        "top6_tradable": round(100 * float(df["top_ret_tradable"].mean()), 3),
        "decile10": round(100 * float(df["p10_ret"].mean()), 3),
        "decile9": round(100 * float(df["p9_ret"].mean()), 3),
        "decile1": round(100 * float(df["p1_ret"].mean()), 3),
        "top6_lim_frac": round(float(df["top_lim"].sum() / (TOP_N * len(df))), 4),
    }
    res["verdict"] = {
        "alpha_untradable": bool(res["per_period_pct"]["top6_lim_frac"] >= 0.10
                                 and res["per_period_pct"]["top6_tradable"] <= 0),
        "decile_monotone": bool(res["per_period_pct"]["decile10"] > res["per_period_pct"]["decile9"]
                                > res["per_period_pct"]["decile1"]),
    }
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    p = res["per_period_pct"]
    log.info("全样本: top6 %.3f%%/期 | 其中可交易 %.3f%% | 开盘涨停占比 %.1f%% | 十分位10 %.3f%% / 9 %.3f%% / 1 %.3f%%",
             p["top6"], p["top6_tradable"], 100 * p["top6_lim_frac"],
             p["decile10"], p["decile9"], p["decile1"])
    for zname, v in res["by_zone"].items():
        log.info("  [%s] n=%d top6 %.3f%% (可交易 %.3f%% / 涨停档 %.3f%%) 涨停占比 %.1f%% | 十分位10 %.3f%%",
                 zname, v["n"], v["top6_ret_pct"], v["top6_tradable_ret_pct"],
                 v["top6_limit_ret_pct"] if v["top6_limit_ret_pct"] is not None else float("nan"),
                 100 * v["top6_lim_frac"], v["decile10_ret_pct"])
    log.info("判决: alpha 不可交易 = %s | 分档单调 = %s | 完成 %.1fs → %s",
             res["verdict"]["alpha_untradable"], res["verdict"]["decile_monotone"], res["secs"], OUT)


if __name__ == "__main__":
    main()
