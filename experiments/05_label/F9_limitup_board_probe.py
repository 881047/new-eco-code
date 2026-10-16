"""F9 · 涨停板"打板"隔日策略探针 —— 最后一条未被实测的渠道

## 为什么测这个

F5 的归因说了一件事:周频模型的 top-6 毛收益**几乎全部来自「次日开盘已一字涨停」的样本**
(那 3.7% 的样本平均 +20~39%/期)。这暗示一个从没被本项目测过的渠道:
**不是"买不到涨停",而是"就在涨停板上买"** —— 即 A 股散户常说的**打板**。

它是唯一一个在既有数据下能严格检验、且理论上可能产生"每 5 日 3%"的渠道:

    涨停价买入(排队成交) → 次日卖出(T+1 允许) → 赚"次日溢价"

## 数据与口径(全部本地、可复现)

- **涨停识别**:`stk_limit.up_limit`(交易所公布值, E5 实验已验证与真值位级一致),
  判据 `close >= up_limit - 0.001`;
- **买入价** = `up_limit`(在板上排队,成交价即涨停价);**卖出** = 次日开盘 或 次日收盘;
- **连板数**:按该股自己的交易日序列,向前连续涨停的天数(1=首板);
- **费用**:佣金 max(额×万2.5, 5 元) + 卖出印花税 0.05% + 过户费 0.001%×2;
- **成交概率 p**:日线**无法**观测排队是否轮到你 ⇒ 结果按 p ∈ {0.25, 0.5, 0.75, 1.0}
  做**敏感性**报告(未成交则收益记 0,但仍占一个仓位名额);
- **T+1**:买入当日不可卖,故卖出只能是次日及以后 ✓ 本探针只用次日。

## 预注册判据

    Q1 打板的次日"板上→次日开盘"平均收益(毛)是多少?分首板/二板/三板以上、分年度。
    Q2 计入费用后,一个每日 5 仓、日频轮动的打板组合,**周均收益**能否 ≥3%?
    Q3 若不能,达到 3%/周 需要多少成交概率 p(或需要多大的次日溢价)?
    Q4 容量:一只票当天在涨停价附近能成交多少钱(用成交额近似),组合体量多大就不可行?

判读:Q2 为否 ⇒ **打板渠道也不能达成目标**(记录为第 5 条被证伪的路径)。

用法:
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F9_limitup_board_probe.py
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

log = logging.getLogger("f9")
OUT = Path(__file__).resolve().parent / "F9_limitup_board_probe_result.json"
SINCE = "20170101"          # stk_limit 覆盖起点
CLIP = 100_000.0            # 单笔名义额(费用与容量口径)


def load_events(con) -> pd.DataFrame:
    """涨停事件 + 次日开盘/收盘 + 前一日是否也涨停(连板判定) + 当日成交额。"""
    log.info("扫描涨停事件(2017 起, %s)…", SINCE)
    q = f"""
    WITH d AS (
      SELECT ts_code, trade_date, open, close, pre_close, vol, amount,
             LAG(trade_date) OVER w AS pd,
             LEAD(trade_date) OVER w AS nd,
             LEAD(open)  OVER w AS n_open,
             LEAD(close) OVER w AS n_close
      FROM daily WHERE trade_date >= '20161201'
      WINDOW w AS (PARTITION BY ts_code ORDER BY trade_date)
    ),
    lu AS (
      SELECT d.ts_code, d.trade_date
      FROM d JOIN stk_limit l ON l.ts_code=d.ts_code AND l.trade_date=d.trade_date
      WHERE d.close >= l.up_limit - 0.001
    )
    SELECT d.ts_code, d.trade_date, d.pd, d.nd, d.n_open, d.n_close,
           d.close, d.pre_close, l.up_limit, d.vol, d.amount,
           (SELECT 1 FROM lu p WHERE p.ts_code=d.ts_code AND p.trade_date=d.pd) AS prev_lu
    FROM d JOIN stk_limit l ON l.ts_code=d.ts_code AND l.trade_date=d.trade_date
    WHERE d.close >= l.up_limit - 0.001 AND d.trade_date >= '{SINCE}'
    """
    df = pd.read_sql(q, con)
    df["trade_date"] = df["trade_date"].astype(str)
    df["pd"] = df["pd"].astype(str)
    log.info("涨停事件 %d 条 (%s ~ %s)", len(df), df["trade_date"].min(), df["trade_date"].max())
    return df


def board_counts(df: pd.DataFrame) -> pd.Series:
    """连板数: 前一日也涨停则累加(按个股交易日序, 序列里只有涨停日, 用 pd 判定是否连续)。"""
    df = df.sort_values(["ts_code", "trade_date"])
    n = np.ones(len(df), dtype=int)
    prev_code, prev_date, run = None, None, 0
    for i, (code, d, p) in enumerate(zip(df["ts_code"], df["trade_date"], df["pd"])):
        if code != prev_code:
            run = 1
        elif p == prev_date:          # 前一交易日也在事件序列里 ⇒ 连板
            run += 1
        else:
            run = 1
        n[i] = run
        prev_code, prev_date = code, d
    return pd.Series(n, index=df.index)


def stats(r: pd.Series, label: str) -> dict:
    r = r.replace([np.inf, -np.inf], np.nan).dropna()
    if len(r) < 50:
        return {"n": int(len(r))}
    return {"n": int(len(r)), "mean_pct": round(100 * float(r.mean()), 3),
            "median_pct": round(100 * float(r.median()), 3),
            "std_pct": round(100 * float(r.std(ddof=1)), 2),
            "win_frac": round(float((r > 0).mean()), 3),
            "p90_pct": round(100 * float(r.quantile(0.90)), 2),
            "p10_pct": round(100 * float(r.quantile(0.10)), 2),
            "worst_pct": round(100 * float(r.min()), 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=SINCE)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    con = sqlite3.connect(f"file:{BASE/'data'/'neweco.db'}?mode=ro", uri=True)
    df = load_events(con)
    con.close()
    if df.empty:
        log.error("无涨停事件")
        return

    df["boards"] = board_counts(df)
    df["cost"] = [(_cost := trade_cost(CLIP, "buy", d, fee_params())
                   + trade_cost(CLIP, "sell", nd, fee_params())) / CLIP
                  for d, nd in zip(df["trade_date"], df["nd"].fillna(df["trade_date"]))]
    df["r_open"] = df["n_open"] / df["up_limit"] - 1.0          # 板上买 → 次日开盘卖
    df["r_close"] = df["n_close"] / df["up_limit"] - 1.0        # 板上买 → 次日收盘卖
    df["r_open_net"] = df["r_open"] - df["cost"]
    df["year"] = df["trade_date"].str[:4]

    res = {"n_events": int(len(df)), "clip": CLIP, "since": a.since,
           "by_boards": {}, "by_year": {}, "fill_sensitivity": {}, "verdict": {}}
    log.info("=== Q1: 次日收益(毛/净, 按连板数) ===")
    for b, sub in df.groupby(df["boards"].clip(upper=4)):
        tag = {1: "首板", 2: "二板", 3: "三板", 4: "四板+"}[b]
        s_raw = stats(sub["r_open"], tag)
        s_net = stats(sub["r_open_net"], tag)
        res["by_boards"][tag] = {"raw": s_raw, "net": s_net,
                                 "close_exit_net": stats(sub["r_close"] - sub["cost"], tag)}
        log.info("%-5s n=%6d 毛均值 %+.3f%% (中位 %+.3f%%) 净均值 %+.3f%% | 胜率 %.1f%% | "
                 "P90 %+.1f%% / P10 %+.1f%%", tag, s_raw["n"], s_raw["mean_pct"],
                 s_raw["median_pct"], s_net["mean_pct"], 100 * s_raw["win_frac"],
                 s_raw["p90_pct"], s_raw["p10_pct"])

    log.info("=== 分年度(首板, 净) ===")
    for y, sub in df[df["boards"] == 1].groupby("year"):
        st = stats(sub["r_open_net"], y)
        res["by_year"][y] = st
        if "mean_pct" in st:
            log.info("  %s n=%5d 净均值 %+.3f%% 胜率 %.1f%%", y, st["n"], st["mean_pct"],
                     100 * st["win_frac"])

    # ---- Q2/Q3: 每日 5 仓、日频轮动的周均收益(含成交概率敏感性) ----
    first = df[df["boards"] == 1]
    mean_net = float(first["r_open_net"].mean())
    log.info("=== Q2/Q3: 首板 5 仓日频轮动 → 周均(5 次)= %.3f%% × 成交概率 ===", 100 * 5 * mean_net)
    for p in (0.25, 0.5, 0.75, 1.0):
        wk = 5 * mean_net * p
        res["fill_sensitivity"][f"p={p}"] = {"weekly_mean_pct": round(100 * wk, 3),
                                             "reaches_3pct": bool(wk >= 0.03)}
        log.info("  成交概率 %.0f%% ⇒ 周均 %+.3f%% (目标 3%%: %s)", 100 * p, 100 * wk,
                 "达标" if wk >= 0.03 else "不达标")

    cap = float(df["amount"].median())
    res["capacity"] = {"median_amount_cny": cap,
                       "note": "涨停当日成交额中位数; 打板资金占其中一小部分才不显著冲击"}
    log.info("=== Q4: 容量(涨停日成交额中位数)= %.0f 元 ===", cap)
    res["verdict"] = {
        "first_board_mean_net_pct": round(100 * mean_net, 3),
        "weekly_at_full_fill_pct": round(100 * 5 * mean_net, 3),
        "reaches_3pct_at_full_fill": bool(5 * mean_net >= 0.03),
        "required_p_for_3pct": (round(0.03 / (5 * mean_net), 3) if mean_net > 0 else None),
    }
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 判决: 首板净均值 %+.3f%%/日 ⇒ 满成交周均 %+.3f%%; 要 3%%/周 需成交概率 %.1f%% | %.1fs → %s ==",
             100 * mean_net, 100 * 5 * mean_net,
             100 * (res["verdict"]["required_p_for_3pct"] or 9.99), res["secs"], OUT)


if __name__ == "__main__":
    main()
