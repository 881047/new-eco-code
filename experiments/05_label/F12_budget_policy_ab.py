"""F12 · 预算策略 A/B:解决「新账户只投出 55%」—— 预注册

## 背景(实跑暴露的缺口)

用 100 万本金出周频方案(锚点 20260911, top100/band8)时:

    74 只成交 / 26 只被丢弃 —— 原因**全部是"一手都买不起"**
    (每只预算 = 100万×0.873/100 = 8,730 元, 而 A 股最小 100 股 ⇒ 价格 >87 元的票直接出局)
    ⇒ 实际投入率只有 **55%**(回测里账户从 100 万滚到 1200 万, 这个约束会自然消失,
       但**新开的账户享受不到**)。

## 本 A/B 检验两个机制(同一份打分, 只改预算规则)

    A0 现状      : 每只固定预算 = 净值×0.873/top_n; 买不起就跳过(钱留现金)
    A1 贪婪预算  : 每只预算 = **剩余权益预算 / 剩余名额**(买不起的票让出的钱分给后面的票)
    A2 顺延候选  : 目标名单买不起的名额, 由**排名更靠后**的票补上(持仓上限不变)
    A3 两者并用

## 预注册判据

    Q1 投入率(持仓市值/净值)能否从 55% 抬到 ≥80%?
    Q2 全史超额(净口径, 对可交易域等权)是否**不劣于** A0(−0.5pp 容差)?
    Q3 换手/回撤是否不劣化超过 10%?

判读:Q1 达标且 Q2/Q3 不劣 ⇒ 采纳为终端默认预算规则(并同步进 plan.py);
      否则保留现状并把读数写进计划文档(不默认开启)。

用法:
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F12_budget_policy_ab.py
"""
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

from app import config as cfg_mod            # noqa: E402
from app.factors import factor_engine as fe  # noqa: E402
from app.pit.engine import PITEngine         # noqa: E402

log = logging.getLogger("f12")
OUT = HERE / "F12_budget_policy_ab_result.json"
SINCE = "20140101"
CAPITAL = 1_000_000.0
TOP_N = 100
BAND = 8.0
HYST = 0.2


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since=SINCE)
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
    log.info("打分 %s | 周锚点 %d", S.shape, len(wk))

    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    mask_np = mask.to_numpy()

    arms = [("A0_现状", dict(greedy_budget=False, fill_through=False)),
            ("A1_贪婪预算", dict(greedy_budget=True, fill_through=False)),
            ("A2_顺延候选", dict(greedy_budget=False, fill_through=True)),
            ("A3_两者并用", dict(greedy_budget=True, fill_through=True)),
            # 中间路线(2026-09-12 追加): 贪婪 + 顺延 + **单票预算上限** ⇒ 多买几只而不超配
            ("A4_贪婪顺延_上限1.0", dict(greedy_budget=True, fill_through=True,
                                        budget_cap_mult=1.0)),
            ("A5_贪婪顺延_上限1.5", dict(greedy_budget=True, fill_through=True,
                                        budget_cap_mult=1.5)),
            ("A6_贪婪顺延_上限2.0", dict(greedy_budget=True, fill_through=True,
                                        budget_cap_mult=2.0))]
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "capital": CAPITAL, "top_n": TOP_N, "band": BAND, "arms": {}, "secs": None}
    for tag, kw in arms:
        df = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, {**cfg, "capital": CAPITAL},
                                       pit, 0, score_matrix=S, top_n=TOP_N,
                                       rank_band=BAND, hyst=HYST, **kw)
        if df is None:
            continue
        op_b = np.full((len(df), ), np.nan)
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
        okr = df["ret"].notna() & df["bench_ret"].notna()
        af, bf = f1b.ann(df.loc[okr, "ret"], 52.0), f1b.ann(df.loc[okr, "bench_ret"], 52.0)
        nav = (1 + df.loc[okr, "ret"]).cumprod()
        res["arms"][tag] = {
            "n": int(okr.sum()), "ann": round(af, 4), "bench": round(bf, 4),
            "excess_pp": round(100 * (af - bf), 2),
            "turnover": round(float(df.loc[okr, "turnover"].mean()), 3),
            "invested_ratio": round(float(df.loc[okr, "invested_ratio"].mean()), 4),
            "avg_names": round(float(df.loc[okr, "n"].mean()), 1),
            "mdd": round(float((nav / nav.cummax() - 1).min()), 4),
        }
        d = res["arms"][tag]
        log.info("[%s] 年化 %+.2f%% 基准 %+.2f%% ⇒ 超额 %+.2fpp | 投入率 %.1f%% | 均持仓 %.0f 只 "
                 "| 换手 %.3f MDD %.1f%%", tag, 100 * d["ann"], 100 * d["bench"],
                 d["excess_pp"], 100 * d["invested_ratio"], d["avg_names"], d["turnover"],
                 100 * d["mdd"])

    a0, best = res["arms"]["A0_现状"], None
    for tag in ("A1_贪婪预算", "A2_顺延候选", "A3_两者并用",
                "A4_贪婪顺延_上限1.0", "A5_贪婪顺延_上限1.5", "A6_贪婪顺延_上限2.0"):
        d = res["arms"].get(tag)
        if d and d["invested_ratio"] >= 0.85 and d["excess_pp"] >= a0["excess_pp"] - 0.2:
            # 追加回撤约束(预注册 Q3): 不得比基线劣化超过 10%(相对)
            if d["mdd"] >= a0["mdd"] * 1.10:
                best = tag
    res["verdict"] = {"baseline_invested": a0["invested_ratio"],
                      "adopt": best,
                      "note": "采纳条件: 投入率 ≥80% 且 超额不劣于基线 −0.5pp"}
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 判决: 采纳 = %s (基线投入率 %.1f%%) | %.1fs → %s ==",
             best or "维持现状", 100 * a0["invested_ratio"], res["secs"], OUT)


if __name__ == "__main__":
    main()
