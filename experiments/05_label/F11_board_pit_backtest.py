"""F11 · 打板的**点时**回测 —— 修正 F10 的前视偏差(本项目最贵的一次自证)

## F10 错在哪(必须先写清楚)

F10 的事件集是「**收盘涨停** 且 盘中开过板」, 买入价取涨停价, 次日开盘卖出,
算出 +3.4%/周、Sharpe 5.8;换成"成交额最小"的子集更是 +11.7%/周、Sharpe 21。

**Sharpe 21 是不可能的**(人类最好记录 Medallion ≈ 2.5~3), 于是继续查:

    打板的买入发生在**盘中**(挂单在涨停价, 价格触板即成交),
    而你在那一刻**不知道它当天会不会封住**。
    F10 只保留"收盘仍涨停"的样本 ⇒ **等于只统计了赢家** = 典型前视偏差。

## 正确的事件定义(点时可得)

    触发 = 当日 **high >= up_limit**(价格触及涨停价, 盘中即可观测)
    买入 = 涨停价(挂在板上的限价单, 触板即成交)
    卖出 = 次日开盘(T+1)
    **不筛收盘**: 触板后没封住(炸板)的样本必须全部留在样本里。

顺带保留一个**诊断拆分**(不能用来选股, 只能用来解释):
    · `封住` = 收盘仍在涨停价
    · `炸板` = 收盘低于涨停价
真实策略必须同时承受这两类 —— 这就是"炸板率"的代价。

## 预注册判据

    Q1 点时口径下, "触板买入→次日开盘卖出"的净收益均值是多少?分封住/炸板。
    Q2 账户级: 每日最多 5 仓、等权、真实费用, 周收益分布与 ≥3% 的周占比。
    Q3 把成交概率 p 计入后, 需要多高的 p 才能让周均 ≥3%?

用法:
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F11_board_pit_backtest.py
"""
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

log = logging.getLogger("f11")
OUT = Path(__file__).resolve().parent / "F11_board_pit_backtest_result.json"
CACHE = BASE / "data" / "weekly" / "touch_events.csv.gz"
SINCE = "20170101"
CAPITAL = 200_000.0
TOP_N = 5
TARGET = 0.03


def load_events(con) -> pd.DataFrame:
    if CACHE.exists():
        df = pd.read_csv(CACHE, dtype={"ts_code": str, "trade_date": str, "pd": str, "nd": str})
        log.info("复用缓存 %s (%d 条)", CACHE, len(df))
        return df
    log.info("扫描「触及涨停」事件(点时口径)…")
    q = f"""
    WITH d AS (
      SELECT ts_code, trade_date, open, high, low, close, pre_close, vol, amount,
             LAG(trade_date) OVER w AS pd,
             LEAD(trade_date) OVER w AS nd,
             LEAD(open)  OVER w AS n_open
      FROM daily WHERE trade_date >= '20161201'
      WINDOW w AS (PARTITION BY ts_code ORDER BY trade_date)
    )
    SELECT d.ts_code, d.trade_date, d.pd, d.nd, d.n_open, d.high, d.low, d.close,
           d.pre_close, l.up_limit, d.amount
    FROM d JOIN stk_limit l ON l.ts_code=d.ts_code AND l.trade_date=d.trade_date
    WHERE d.high >= l.up_limit - 0.001 AND d.trade_date >= '{SINCE}'
    """
    df = pd.read_sql(q, con)
    for c in ("trade_date", "pd"):
        df[c] = df[c].astype(str)
    df.to_csv(CACHE, index=False, compression="gzip")
    log.info("触板事件 %d 条 → %s", len(df), CACHE)
    return df


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    con = sqlite3.connect(f"file:{BASE/'data'/'neweco.db'}?mode=ro", uri=True)
    df = load_events(con)
    sb = pd.read_sql("SELECT ts_code, name, list_date FROM stock_basic", con)
    con.close()

    df = df.merge(sb, on="ts_code", how="left")
    bad = df["ts_code"].str.startswith(("920", "83", "87", "43", "688"))
    td = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    ld = pd.to_datetime(df["list_date"], format="%Y%m%d", errors="coerce")
    young = (td - ld).dt.days < 100
    st = df["name"].astype(str).str.contains("ST|退", na=False)
    df["sealed"] = df["close"] >= df["up_limit"] - 0.001        # 收盘封住 = 事后才知的诊断量
    df["r"] = df["n_open"] / df["up_limit"] - 1.0
    n0 = len(df)
    df = df[~bad & ~young & ~st & (df["r"].abs() <= 0.21)].copy()
    log.info("卫生过滤: %d → %d 条 (触板事件)", n0, len(df))

    fee_p = fee_params()
    df["cost"] = [(trade_cost(CAPITAL / TOP_N, "buy", d, fee_p)
                   + trade_cost(CAPITAL / TOP_N, "sell", nd, fee_p)) / (CAPITAL / TOP_N)
                  for d, nd in zip(df["trade_date"], df["nd"].fillna(df["trade_date"]))]
    df["r_net"] = df["r"] - df["cost"]

    res = {"n_touch": int(len(df)), "sealed_frac": round(float(df["sealed"].mean()), 4),
           "diagnostic": {}, "portfolio": {}, "fill": {}, "secs": None}
    log.info("=== Q1: 点时口径(触板即买) 次日开盘卖 的净收益 ===")
    for tag, sub in (("全部触板", df), ("其中: 封住", df[df["sealed"]]),
                     ("其中: 炸板", df[~df["sealed"]])):
        r = sub["r_net"].dropna()
        res["diagnostic"][tag] = {
            "n": int(len(r)), "frac": round(len(sub) / len(df), 4),
            "mean_pct": round(100 * float(r.mean()), 3),
            "median_pct": round(100 * float(r.median()), 3),
            "win_frac": round(float((r > 0).mean()), 3),
            "p10_pct": round(100 * float(r.quantile(0.10)), 2),
            "p90_pct": round(100 * float(r.quantile(0.90)), 2)}
        d = res["diagnostic"][tag]
        log.info("%-10s 占比 %.1f%% n=%7d 净均值 %+.3f%% 中位 %+.3f%% 胜率 %.1f%% P10 %+.1f%%",
                 tag, 100 * d["frac"], d["n"], d["mean_pct"], d["median_pct"],
                 100 * d["win_frac"], d["p10_pct"])

    # ---- Q2: 账户级(每日按成交额取前 N; 等权; 触板即买) ----
    log.info("=== Q2: 每日成交额最大的 N 只触板股, 次日开盘卖 ===")
    rows = []
    for d, g in df.groupby("trade_date"):
        sel = g.nlargest(TOP_N, "amount")
        rows.append({"trade_date": d, "ret": float(sel["r_net"].mean()) * (len(sel) / TOP_N)})
    daily = pd.DataFrame(rows).set_index("trade_date")["ret"].sort_index()
    nav = (1 + daily).cumprod()
    n = len(daily)
    wk = pd.Series([float((1 + daily.iloc[i:i + 5]).prod() - 1) for i in range(0, n - 4, 5)])
    res["portfolio"] = {
        "n_days": n, "n_weeks": len(wk),
        "daily_mean_pct": round(100 * float(daily.mean()), 3),
        "week_mean_pct": round(100 * float(wk.mean()), 3),
        "week_median_pct": round(100 * float(wk.median()), 3),
        "week_ge3_frac": round(float((wk >= TARGET).mean()), 4),
        "week_worst_pct": round(100 * float(wk.min()), 2),
        "mdd_pct": round(100 * float((nav / nav.cummax() - 1).min()), 1),
        "sharpe": round(float(daily.mean() / daily.std(ddof=1) * np.sqrt(252)), 2),
        "final_nav_x": round(float(nav.iloc[-1]), 2),
    }
    p = res["portfolio"]
    log.info("  周均 %+.3f%% 中位 %+.3f%% | ≥3%% 的周 %.1f%% | MDD %.1f%% Sharpe %.2f | "
             "末期净值 %.2f× | 最差周 %.1f%%",
             p["week_mean_pct"], p["week_median_pct"], 100 * p["week_ge3_frac"], p["mdd_pct"],
             p["sharpe"], p["final_nav_x"], p["week_worst_pct"])

    log.info("=== Q3: 成交概率敏感性 ===")
    for pf in (0.25, 0.5, 0.75, 1.0):
        s = pd.Series([float((1 + daily.iloc[i:i + 5] * pf).prod() - 1)
                       for i in range(0, n - 4, 5)])
        res["fill"][f"p={pf}"] = {"week_mean_pct": round(100 * float(s.mean()), 3),
                                  "ge3_frac": round(float((s >= TARGET).mean()), 4)}
        log.info("  p=%.0f%% ⇒ 周均 %+.3f%% | ≥3%% 的周 %.1f%%", 100 * pf,
                 100 * float(s.mean()), 100 * float((s >= TARGET).mean()))

    res["verdict"] = {
        "all_touch_mean_net_pct": res["diagnostic"]["全部触板"]["mean_pct"],
        "sealed_mean_net_pct": res["diagnostic"]["其中: 封住"]["mean_pct"],
        "failed_mean_net_pct": res["diagnostic"]["其中: 炸板"]["mean_pct"],
        "week_mean_pct": p["week_mean_pct"],
        "reaches_3pct_mean": bool(p["week_mean_pct"] >= 3.0),
        "note": "点时口径; 未计入排队成交概率(见 fill)",
    }
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 判决: 点时(触板即买)净均值 %+.3f%%/日; 封住 %+.3f%% / 炸板 %+.3f%%; "
             "账户周均 %+.3f%% | %.1fs → %s ==",
             res["verdict"]["all_touch_mean_net_pct"], res["verdict"]["sealed_mean_net_pct"],
             res["verdict"]["failed_mean_net_pct"], p["week_mean_pct"], res["secs"], OUT)


if __name__ == "__main__":
    main()
