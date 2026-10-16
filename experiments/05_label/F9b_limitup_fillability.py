"""F9b · 打板的**可成交性**检验 —— 一字板 vs 开板(决定 F9 的巨额收益是不是幻觉)

## 为什么这是全项目最关键的一次检验

F9 算出:首板"板上买 → 次日开盘卖"净均值 **+1.871%/日**、二板 +3.23%、四板 +5.60%,
逐年稳定(2017~2026 每年 +1.4%~+2.5%)。若可成交,满仓周均 +9.36%,
**只要 32% 的成交概率就能达到 3%/周** —— 目标看起来达成了。

**但这里有一个能把结论完全反转的偏差**:F9 把**所有涨停收盘**都当成"买到了"。
实际上 A 股有两种涨停:

    一字板(low == up_limit): 全天封死在涨停价, 排队巨量, 散户单**基本不可能成交**;
    开板   (low <  up_limit): 盘中被砸开过, 涨停价上有真实成交, **可以买到**(需在板上挂单等)。

而"能不能买到"与"次日涨不涨"是**相关**的:一字板往往是最强、次日溢价最高的那批;
开板的往往是资金分歧大、次日容易低开的那批。**这正是 F5 已经暴露过的同一件事**
(模型 top-6 的毛收益几乎全来自"次日开盘已一字涨停"的样本,剔除后为 0)。

## 预注册判据

    Q1 一字板 vs 开板 的事件占比、次日净收益各是多少?
    Q2 只用**开板**样本(现实可成交域):按连板数分组的净收益、胜率、P10。
    Q3 开板样本的 5 仓日频轮动周均收益是多少?能否 ≥3%?
    Q4 容量:开板样本的涨停日成交额分布。

判读:
    · 开板样本仍显著为正且周均 ≥3% ⇒ **打板渠道成立**, 目标在现实口径下可达;
    · 开板样本收益 ≈0 或为负 ⇒ F9 的收益全部来自买不到的一字板 ⇒ **渠道证伪**,
      与 F5 互相印证(这也解释了为什么整套模型都赚不到那部分钱)。

用法:
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F9b_limitup_fillability.py
"""
import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app.portfolio.fees import fee_params, trade_cost  # noqa: E402

log = logging.getLogger("f9b")
OUT = Path(__file__).resolve().parent / "F9b_limitup_fillability_result.json"
CACHE = BASE / "data" / "weekly" / "limitup_events.csv.gz"
SINCE = "20170101"
CLIP = 100_000.0


def load_events(con, since: str) -> pd.DataFrame:
    if CACHE.exists():
        df = pd.read_csv(CACHE, dtype={"ts_code": str, "trade_date": str, "pd": str, "nd": str})
        log.info("复用缓存事件表 %s (%d 条)", CACHE, len(df))
        return df
    log.info("扫描涨停事件…")
    # 注意: 不要在 SQL 里用「相关子查询」判前一日是否涨停 —— SQLite 会对每行重算,
    # 实测退化到 20 分钟不返回(内存 21GB)。改成两次简单查询 + pandas 合并。
    q = f"""
    WITH d AS (
      SELECT ts_code, trade_date, open, high, low, close, pre_close, vol, amount,
             LAG(trade_date) OVER w AS pd,
             LEAD(trade_date) OVER w AS nd,
             LEAD(open)  OVER w AS n_open,
             LEAD(close) OVER w AS n_close
      FROM daily WHERE trade_date >= '20161201'
      WINDOW w AS (PARTITION BY ts_code ORDER BY trade_date)
    )
    SELECT d.ts_code, d.trade_date, d.pd, d.nd, d.n_open, d.n_close, d.low,
           d.close, d.pre_close, l.up_limit, d.vol, d.amount
    FROM d JOIN stk_limit l ON l.ts_code=d.ts_code AND l.trade_date=d.trade_date
    WHERE d.close >= l.up_limit - 0.001 AND d.trade_date >= '{since}'
    """
    df = pd.read_sql(q, con)
    for c in ("trade_date", "pd"):
        df[c] = df[c].astype(str)
    # 前一日是否也涨停: 与自身事件集按 (ts_code, 前一日) 合并
    prev = df[["ts_code", "trade_date"]].rename(columns={"trade_date": "pd"}).assign(prev_lu=1)
    df = df.merge(prev, on=["ts_code", "pd"], how="left")
    df["prev_lu"] = df["prev_lu"].fillna(0).astype(int)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(CACHE, index=False, compression="gzip")
    log.info("涨停事件 %d 条 → 缓存 %s", len(df), CACHE)
    return df


def board_counts(df: pd.DataFrame) -> pd.Series:
    """连板数 = 1 + 前一交易日连板数(prev_lu 由事件集合并得到, 见 load_events)。"""
    s = df.sort_values(["ts_code", "trade_date"])
    n = np.ones(len(s), dtype=int)
    prev_code, run = None, 0
    for i, (code, pl) in enumerate(zip(s["ts_code"], s["prev_lu"])):
        run = run + 1 if (code == prev_code and pl == 1) else 1
        n[i] = run
        prev_code = code
    return pd.Series(n, index=s.index)


def stats(r: pd.Series) -> dict:
    r = r.replace([np.inf, -np.inf], np.nan).dropna()
    if len(r) < 30:
        return {"n": int(len(r))}
    return {"n": int(len(r)), "mean_pct": round(100 * float(r.mean()), 3),
            "median_pct": round(100 * float(r.median()), 3),
            "std_pct": round(100 * float(r.std(ddof=1)), 2),
            "win_frac": round(float((r > 0).mean()), 3),
            "p10_pct": round(100 * float(r.quantile(0.10)), 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=SINCE)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    con = sqlite3.connect(f"file:{BASE/'data'/'neweco.db'}?mode=ro", uri=True)
    df = load_events(con, a.since)
    con.close()

    df["boards"] = board_counts(df)
    fee_p = fee_params()
    df["cost"] = [(trade_cost(CLIP, "buy", d, fee_p) + trade_cost(CLIP, "sell", nd, fee_p)) / CLIP
                  for d, nd in zip(df["trade_date"], df["nd"].fillna(df["trade_date"]))]
    df["r_net"] = df["n_open"] / df["up_limit"] - 1.0 - df["cost"]
    df["yizi"] = df["low"] >= df["up_limit"] - 0.001          # 一字板(买不到)
    df["year"] = df["trade_date"].str[:4]

    res = {"n_events": int(len(df)), "since": a.since, "clip": CLIP,
           "split": {}, "by_boards_open": {}, "by_year_open": {},
           "capacity": {}, "verdict": {}}

    log.info("=== Q1: 一字板 vs 开板 ===")
    for tag, sub in (("一字板(买不到)", df[df["yizi"]]), ("开板(可成交)", df[~df["yizi"]])):
        st = stats(sub["r_net"])
        res["split"][tag] = {"frac": round(len(sub) / len(df), 4), **st}
        log.info("%-14s 占比 %.1f%% n=%6d 次日净均值 %+.3f%% 胜率 %.1f%% P10 %+.1f%%",
                 tag, 100 * len(sub) / len(df), st["n"], st["mean_pct"],
                 100 * st["win_frac"], st["p10_pct"])

    log.info("=== Q2: 只用开板样本(现实可成交), 按连板数 ===")
    op = df[~df["yizi"]]
    for b, sub in op.groupby(op["boards"].clip(upper=4)):
        tag = {1: "首板", 2: "二板", 3: "三板", 4: "四板+"}[b]
        st = stats(sub["r_net"])
        res["by_boards_open"][tag] = st
        log.info("%-5s n=%6d 净均值 %+.3f%% 中位 %+.3f%% 胜率 %.1f%% P10 %+.1f%%",
                 tag, st["n"], st["mean_pct"], st["median_pct"], 100 * st["win_frac"], st["p10_pct"])

    log.info("=== 分年度(开板·首板) ===")
    fo = op[op["boards"] == 1]
    for y, sub in fo.groupby("year"):
        st = stats(sub["r_net"])
        res["by_year_open"][y] = st
        if "mean_pct" in st:
            log.info("  %s n=%5d 净均值 %+.3f%% 胜率 %.1f%%", y, st["n"], st["mean_pct"],
                     100 * st["win_frac"])

    mean_open = float(fo["r_net"].mean())
    wk_full = 5 * mean_open
    res["capacity"] = {"median_amount": float(fo["amount"].median()),
                       "p25_amount": float(fo["amount"].quantile(0.25))}
    log.info("=== Q3: 开板·首板 5 仓日频轮动 ⇒ 周均(满成交) %+.3f%% | 需成交概率 %.1f%% 才到 3%% ===",
             100 * wk_full, 100 * (0.03 / wk_full) if wk_full > 0 else float("inf"))
    log.info("=== Q4: 容量: 开板首板当日成交额 中位 %.0f 元 / P25 %.0f 元 ===",
             res["capacity"]["median_amount"], res["capacity"]["p25_amount"])

    res["verdict"] = {
        "first_board_open_mean_net_pct": round(100 * mean_open, 3),
        "weekly_full_fill_pct": round(100 * wk_full, 3),
        "reaches_3pct": bool(wk_full >= 0.03),
        "required_fill_prob": round(0.03 / wk_full, 3) if wk_full > 0 else None,
        "yizi_frac": res["split"]["一字板(买不到)"]["frac"],
    }
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 判决: 可成交域(开板)首板净均值 %+.3f%%/日 ⇒ 满成交周均 %+.3f%%; 一字板占比 %.1f%% | %.1fs → %s ==",
             100 * mean_open, 100 * wk_full, 100 * res["verdict"]["yizi_frac"], res["secs"], OUT)


if __name__ == "__main__":
    main()
