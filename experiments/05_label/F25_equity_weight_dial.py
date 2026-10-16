"""F25 · 权益权重刻度：那 23% 闲置现金该不该投出去 —— 预注册

## 由来（全部是 F24 实测，491 期、两套种子）

F24 把「等权持仓口径」的超额 **+22.15pp/年（算术）** 拆成
风格 +11.50 / 行业 +1.27 / 个股 +9.37，然后一段 **实施缺口 −9.39pp/年**
把它压到引擎净值口径 +12.75pp/年。缺口里最大的单项是

    **现金拖累 ≈ −7.1pp/年**（等权口径）—— 基准是**满仓**等权池，而袖套平均只投出 76.9%。

那 76.9% 分两层：① 从月频系统继承的 `regime_equity_weight.aggressive.attack = 0.873`；
② 整手粒度/可负担性把 87.3% 又磨到 76.9%（F23 已测，价格上限/顺延都只挪 +0.2pp）。

## 本实验只回答一个问题

    「把权益权重从 0.873 提到 0.95 / 1.00，超额与回撤各变成多少？」

**这不是 alpha 实验，是风险预算刻度**：多投出的那部分现金本身不产生 alpha。
而且因为基准是满仓等权池，持有现金在指标上**必然**扣分 —— 这是口径事实，
不是策略缺陷；反过来也**不许**靠改基准（改成"87.3% 池 + 现金"）来美化读数。

## 臂（其余构型完全冻结：100 万 / top100 / 退出带 8× / 持仓加成 0.2σ / 2×W5+pit / 两套种子）

    W873（现役） / W950 / W1000

## 预注册判据

**不自动采纳任何一档** —— 敞口是 §9 里要用户拍板的决策点。本实验交付三列读数：
超额（算术 + 复利）/ MDD / 超额÷|MDD| / 投入率 / 换手，并**实测验证**"超额对权益权重近似线性"
这个说法（回归系数 + R²），而不是靠嘴说。另记录投入率的**逐年结构**：
它决定"新账户第一年"的现金拖累比回测均值更重还是更轻。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F25_equity_weight_dial.py
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

from app import config as cfg_mod             # noqa: E402
from app.factors import factor_engine as fe   # noqa: E402
from app.pit.engine import PITEngine          # noqa: E402

log = logging.getLogger("f25")
OUT = HERE / "F25_equity_weight_dial_result.json"
CAPITAL, TOP_N, BAND, HYST = 1_000_000.0, 100, 8.0, 0.2
SEED_FILES = {"seedA": "F4_scores_seedA.npz", "seedB": "F4_scores_seedB.npz"}
WEIGHTS = (0.873, 0.95, 1.0)
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
    log.info("交易日 %d | 周锚点 %d | 股票 %d", len(dates), len(wk), len(cols))
    conn = sqlite3.connect(f"file:{BASE/'data'/'neweco.db'}?mode=ro", uri=True)
    PITSC = f17.table_scores(conn, "model_score_die_pit", wk, cols)
    conn.close()
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}

    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "config": {"capital": CAPITAL, "top_n": TOP_N, "band": BAND, "hyst": HYST,
                      "weights": list(WEIGHTS)},
           "seeds": {}, "linearity": {}, "secs": None}
    per_weight = {w: [] for w in WEIGHTS}          # (seed, arithmetic excess) 用于线性检验

    for seed, fn in SEED_FILES.items():
        z = np.load(HERE / fn, allow_pickle=True)
        a5 = pd.to_datetime([str(d) for d in z["anchors"]])
        c5 = [str(c) for c in z["cols"]]
        W5 = pd.DataFrame(z["scores"], index=a5, columns=c5).reindex(index=wk, columns=cols)
        S = f22.wr((W5, 2), (PITSC, 1))
        Sdf = pd.DataFrame(S, index=wk, columns=cols)
        rows = {}
        for w in WEIGHTS:
            c = {**cfg, "capital": CAPITAL,
                 "regime_equity_weight": {"aggressive": {"attack": w, "neutral": w, "defense": w}}}
            d = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, c, pit, 0, score_matrix=Sdf,
                                          top_n=TOP_N, rank_band=BAND, hyst=HYST)
            if d is None:
                log.error("[%s|W%.3f] 无输出", seed, w)
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
            nav = (1 + d.loc[ok, "ret"]).cumprod()
            a_ = f1b.ann(d.loc[ok, "ret"], PER_YEAR)
            b_ = f1b.ann(d.loc[ok, "bench_ret"], PER_YEAR)
            ex_arith = 100 * PER_YEAR * float((d.loc[ok, "ret"] - d.loc[ok, "bench_ret"]).mean())
            mdd = float((nav / nav.cummax() - 1).min())
            by_year = (d.loc[ok].assign(y=pd.to_datetime(d.loc[ok, "date"]).dt.year)
                       .groupby("y")["invested_ratio"].mean().round(4).to_dict())
            rows[f"W{int(round(w * 1000))}"] = {
                "n": int(ok.sum()), "ann": round(a_, 4), "bench_ann": round(b_, 4),
                "excess_pp_ann": round(100 * (a_ - b_), 2), "excess_pp_arith": round(ex_arith, 2),
                "mdd": round(mdd, 4), "excess_over_mdd": round(ex_arith / abs(100 * mdd), 3),
                "invested": round(float(d["invested_ratio"].mean()), 4),
                "turnover": round(float(d["turnover"].mean()), 3),
                "invested_by_year": by_year}
            per_weight[w].append(ex_arith)
            log.info("[%s|W%.3f] 超额(复利) %+.2fpp | (算术) %+.2fpp | MDD %.1f%% | 超额/|MDD| %.3f "
                     "| 投入率 %.3f | 换手 %.3f",
                     seed, w, rows[f"W{int(round(w * 1000))}"]["excess_pp_ann"], ex_arith,
                     100 * mdd, rows[f"W{int(round(w * 1000))}"]["excess_over_mdd"],
                     rows[f"W{int(round(w * 1000))}"]["invested"],
                     rows[f"W{int(round(w * 1000))}"]["turnover"])
        res["seeds"][seed] = rows

    # 线性检验: 超额 = a + b × 权重（用两套种子 × 三档 = 6 个点）
    xs = np.array([w for w in WEIGHTS for _ in per_weight[w]], dtype=float)
    ys = np.array([v for w in WEIGHTS for v in per_weight[w]], dtype=float)
    if len(xs) >= 3:
        b, a = np.polyfit(xs, ys, 1)
        pred = a + b * xs
        ss_res = float(((ys - pred) ** 2).sum())
        ss_tot = float(((ys - ys.mean()) ** 2).sum())
        res["linearity"] = {"slope_pp_per_unit_weight": round(float(b), 2),
                            "intercept_pp": round(float(a), 2),
                            "r2": round(1 - ss_res / ss_tot, 4) if ss_tot > 0 else None,
                            "n_points": int(len(xs))}
        log.info("线性检验: 超额 ≈ %+.2f + %+.2f×权重 (R²=%.4f)",
                 a, b, res["linearity"]["r2"])
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 完成 %.1fs → %s ==", res["secs"], OUT)


if __name__ == "__main__":
    main()
