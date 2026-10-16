"""F13 · 敞口漂移检验:已报的 +18.4%/年 里有多少是"策略"、多少是"敞口偷偷变大"

## 为什么要查

回测器的两条规则(与已验证构型一致):
    · **留存持仓从不再平衡**(不调股数, 只按名单变化交易);
    · **现金从不回补**(每周只买"新面孔", 不把已涨上去的持仓卖回目标权重)。

组合长期跑赢现金 ⇒ 持仓市值占比(投入率)会随净值增长**逐年漂移**。
若末年敞口显著高于首年, 那么对外口径"恒定 87.3% 权益仓位"在长期回测里就不成立,
已报的 +18.4%/年 里就混进了**敞口扩张**带来的收益(那不是 alpha, 是加仓)。

## 本脚本输出

    逐年: 投入率均值 / 现金-净值比 / 组合年化 / 基准年化 / 超额
    并给出判决: 漂移是否显著(末年 vs 首年 > 10pp)

用法:
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F13_exposure_drift_check.py
"""
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

from app import config as cfg_mod            # noqa: E402
from app.factors import factor_engine as fe  # noqa: E402
from app.pit.engine import PITEngine         # noqa: E402

log = logging.getLogger("f13")
OUT = HERE / "F13_exposure_drift_check_result.json"
CAPITAL = 1_000_000.0


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    pit = PITEngine(m=m, cfg=cfg)
    cols = list(m["close"].columns)
    mask = f1.tradable_frame(pit, wk, cols)
    z = np.load(HERE / "F4_scores_seedA.npz", allow_pickle=True)
    anch = pd.to_datetime([str(d) for d in z["anchors"]])
    scols = [str(c) for c in z["cols"]]
    ka = [i for i, d in enumerate(anch) if d in set(wk)]
    kc = [i for i, c in enumerate(scols) if c in set(cols)]
    S = pd.DataFrame(z["scores"][np.ix_(ka, kc)], index=anch[ka],
                     columns=[scols[i] for i in kc])

    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    mask_np = mask.to_numpy()

    arms = {"A0_现状": dict(greedy_budget=False, fill_through=False),
            "A5_上限1.5": dict(greedy_budget=True, fill_through=True, budget_cap_mult=1.5)}
    res = {"capital": CAPITAL, "arms": {}, "secs": None}
    for tag, kw in arms.items():
        df = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, {**cfg, "capital": CAPITAL},
                                       pit, 0, score_matrix=S, top_n=100, rank_band=8.0,
                                       hyst=0.2, **kw)
        if df is None:
            continue
        bench, prev_exec, prev_ap = [], None, None
        for _, r in df.iterrows():
            e = pos_of[r["date"]]
            if prev_exec is None or prev_ap is None:
                bench.append(np.nan)
            else:
                good = (mask_np[int(prev_ap)] & np.isfinite(op_adj[prev_exec])
                        & np.isfinite(op_adj[e]) & (op_adj[prev_exec] > 0))
                p0, p1 = op_adj[prev_exec], op_adj[e]
                bench.append(float(np.mean(p1[good] / p0[good] - 1)) if good.sum() >= 200
                             else np.nan)
            prev_exec, prev_ap = e, int(r["anchor_pos"])
        df["bench_ret"] = bench
        df["year"] = df["date"].dt.strftime("%Y")
        log.info("=== %s: 逐年投入率与收益 ===", tag)
        rows = {}
        for y, sub in df.groupby("year"):
            if len(sub) < 10:
                continue
            okr = sub["ret"].notna() & sub["bench_ret"].notna()
            a = f1b.ann(sub.loc[okr, "ret"], 52.0)
            b = f1b.ann(sub.loc[okr, "bench_ret"], 52.0)
            rows[y] = {"n": int(len(sub)),
                       "invested": round(float(sub["invested_ratio"].mean()), 4),
                       "cash_over_nav": round(float((sub["cash"] / sub["nav"]).mean()), 4),
                       "ann": round(a, 4), "bench": round(b, 4),
                       "excess_pp": round(100 * (a - b), 2),
                       "nav_end": round(float(sub["nav"].iloc[-1]), 0)}
            log.info("  %s n=%3d 投入率 %.1f%% | 现金/净值 %.1f%% | 组合 %+.1f%% 基准 %+.1f%% "
                     "⇒ 超额 %+.1fpp | 期末净值 %.0f", y, rows[y]["n"],
                     100 * rows[y]["invested"], 100 * rows[y]["cash_over_nav"],
                     100 * rows[y]["ann"], 100 * rows[y]["bench"], rows[y]["excess_pp"],
                     rows[y]["nav_end"])
        res["arms"][tag] = rows
        yrs = sorted(rows)
        if len(yrs) >= 3:
            first, last = rows[yrs[0]]["invested"], rows[yrs[-1]]["invested"]
            res.setdefault("verdict", {})[tag] = {
                "first_year": yrs[0], "last_year": yrs[-1],
                "invested_first": first, "invested_last": last,
                "drift_pp": round(100 * (last - first), 1),
                "drift_significant": bool((last - first) > 0.10),
            }
            log.info("  ⇒ 投入率漂移 %s: %.1f%% → %.1f%% (%+.1fpp) 显著=%s",
                     tag, 100 * first, 100 * last, 100 * (last - first),
                     res["verdict"][tag]["drift_significant"])
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 完成 %.1fs → %s ==", res["secs"], OUT)


if __name__ == "__main__":
    main()
