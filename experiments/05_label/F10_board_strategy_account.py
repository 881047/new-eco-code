"""F10 · 打板策略的账户级回测(现实可成交域) —— 回答"3%/5 日"能否落到账户上

## 前置(已实测)

- F9 : 涨停收盘 → 次日开盘, 全样本净均值 +1.87%/日(首板)。
- F9b: 剔掉**买不到的一字板**(占 12.8%)后,**开板样本仍 +1.778%**;
       开板·首板 +1.682%/日,**2017~2026 每年为正**(+1.27% ~ +2.33%)。
- 单位核对: `daily.amount` 为**千元**、`vol` 为**手** ⇒ 开板首板当日成交额中位
       **2.2 亿元**(P25 8,101 万) ⇒ 个人账户(10 万级)**容量不是约束**。

## 本脚本回答

    日频"板上买入 → 次日开盘卖出"、每日最多 N 仓、等权、真实费用的账户级回测:
      · 每 5 个交易日(一周)的收益分布是多少?≥3% 的周占多少?
      · 成交概率 p 从 25% → 100% 的敏感性(排队能否成交是唯一无法用日线观测的量);
      · 最大回撤、最差周、逐年表现、以及选股规则(成交额/小市值/随机)的稳健性。

## 预注册口径

- **买入价 = 当日涨停价**(保守:开板日的真实成交价通常**低于**涨停价, 故这是下界);
- **卖出 = 次日开盘**(T+1 允许);费用 = 佣金 max(额×万2.5, 5 元) + 印花税 0.05% + 过户费;
- 只做**首板 + 开板 + 非 ST + 非次新(上市满 60 交易日)**, 每日最多 N 仓, 等权;
- 候选多于 N 时按**当日成交额降序**取前 N(另跑随机/小市值两臂做稳健性);
- 成交概率 p:未成交则当日该仓位收益记 0(资金闲置), 组合层面即收益 × p。

用法:
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F10_board_strategy_account.py
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

log = logging.getLogger("f10")
OUT = Path(__file__).resolve().parent / "F10_board_strategy_account_result.json"
EVENTS = BASE / "data" / "weekly" / "limitup_events.csv.gz"
CAPITAL = 200_000.0
TOP_N = 5
TARGET = 0.03


def load() -> pd.DataFrame:
    df = pd.read_csv(EVENTS, dtype={"ts_code": str, "trade_date": str, "pd": str, "nd": str})
    df["yizi"] = df["low"] >= df["up_limit"] - 0.001
    # 连板数(用 prev_lu 递推)
    s = df.sort_values(["ts_code", "trade_date"])
    b = np.ones(len(s), dtype=int)
    prev_code, run = None, 0
    for i, (code, pl) in enumerate(zip(s["ts_code"], s["prev_lu"])):
        run = run + 1 if (code == prev_code and pl == 1) else 1
        b[i] = run
        prev_code = code
    df["boards"] = pd.Series(b, index=s.index).reindex(df.index)
    return df


def st_codes(con) -> set:
    nm = pd.read_sql("SELECT ts_code, name FROM stock_basic", con)
    return set(nm.loc[nm["name"].astype(str).str.contains("ST|退", na=False), "ts_code"])


def hygiene(df: pd.DataFrame, con) -> pd.DataFrame:
    """数据卫生过滤 —— 这一步是必须的, 不加就会把假收益算进来(2026-09-12 踩到):

    · 排除北交所(920/83/87/43 开头): ±30% 板、流动性差、且生产票池本就排除;
    · 排除科创(688): 生产票池不含;
    · **单日收益夹到 ±21%**: A 股主板/创业/科创的日涨跌幅上限最高 20%,
      实测存在 +390%(000670.SZ 2020-03-19, 复牌/特殊情形)与 −90% 的格子,
      不过滤会把净值复合到 inf(实测 Sharpe 22 = 假);
    · 上市满 100 个自然日以上(次新无涨跌幅/波动异常);
    · ST/退 剔除。
    """
    sb = pd.read_sql("SELECT ts_code, list_date FROM stock_basic", con)
    df = df.merge(sb, on="ts_code", how="left")
    bad_prefix = df["ts_code"].str.startswith(("920", "83", "87", "43", "688"))
    td = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    ld = pd.to_datetime(df["list_date"], format="%Y%m%d", errors="coerce")
    young = (td - ld).dt.days < 100
    df["r_raw"] = df["n_open"] / df["up_limit"] - 1.0
    extreme = df["r_raw"].abs() > 0.21
    df = df[~bad_prefix & ~young & ~extreme].copy()
    log.info("卫生过滤后事件 %d(剔除 北交/科创/次新/ST/|r|>21%% )", len(df))
    return df


def portfolio(df: pd.DataFrame, st: set, p_fill: float, top_n: int,
              rule: str, min_amount_yi: float = 0.0, seed: int = 42) -> pd.Series:
    """返回按交易日的组合净收益序列(等权, 已计费, 已乘成交概率)。"""
    ev = df[(~df["yizi"]) & (df["boards"] == 1) & (~df["ts_code"].isin(st))].copy()
    if min_amount_yi > 0:                       # 成交额下限(千元 → 亿元)
        ev = ev[ev["amount"] >= min_amount_yi * 1e5]
    ev["r_net"] = ev["n_open"] / ev["up_limit"] - 1.0
    fee_p = fee_params()
    ev["cost"] = [(trade_cost(CAPITAL / top_n, "buy", d, fee_p)
                   + trade_cost(CAPITAL / top_n, "sell", nd, fee_p)) / (CAPITAL / top_n)
                  for d, nd in zip(ev["trade_date"], ev["nd"].fillna(ev["trade_date"]))]
    ev["r_net"] -= ev["cost"]
    rng = np.random.default_rng(seed)
    rows = []
    for d, g in ev.groupby("trade_date"):
        if rule == "amount":
            sel = g.nlargest(top_n, "amount")
        elif rule == "small":
            sel = g.nsmallest(top_n, "amount")          # 成交额小 = 偏小盘/不拥挤
        else:
            sel = g.sample(min(top_n, len(g)), random_state=int(rng.integers(1 << 30)))
        rows.append({"trade_date": d, "ret": float(sel["r_net"].mean()) * (len(sel) / top_n)})
    s = pd.DataFrame(rows).set_index("trade_date")["ret"].sort_index()
    return s * p_fill      # 未成交的仓位收益记 0


def weekly_stats(daily: pd.Series) -> dict:
    """按 5 个交易日滚动成"周"收益并给出分布。"""
    if len(daily) < 20:
        return {}
    nav = (1 + daily).cumprod()
    n = len(daily)
    # 每 5 个交易日为一期(不重叠)
    idx = np.arange(0, n - 4, 5)
    wk = pd.Series([float((1 + daily.iloc[i:i + 5]).prod() - 1) for i in idx])
    mdd = float((nav / nav.cummax() - 1).min())
    ann = float(nav.iloc[-1] ** (52 * 5 / n) - 1)
    return {
        "n_days": n, "n_weeks": len(wk),
        "daily_mean_pct": round(100 * float(daily.mean()), 3),
        "week_mean_pct": round(100 * float(wk.mean()), 3),
        "week_median_pct": round(100 * float(wk.median()), 3),
        "week_std_pct": round(100 * float(wk.std(ddof=1)), 2),
        "week_ge_3pct_frac": round(float((wk >= TARGET).mean()), 4),
        "week_ge_5pct_frac": round(float((wk >= 0.05).mean()), 4),
        "week_worst_pct": round(100 * float(wk.min()), 2),
        "week_best_pct": round(100 * float(wk.max()), 2),
        "ann_pct": round(100 * ann, 1), "mdd_pct": round(100 * mdd, 1),
        "sharpe": round(float(daily.mean() / daily.std(ddof=1) * np.sqrt(252)), 2),
        "daily_worst_pct": round(100 * float(daily.min()), 2),
        "daily_p1_pct": round(100 * float(daily.quantile(0.01)), 2),
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    df = load()
    con = sqlite3.connect(f"file:{BASE/'data'/'neweco.db'}?mode=ro", uri=True)
    st = st_codes(con)
    df = hygiene(df, con)
    con.close()
    log.info("事件 %d | 开板首板非ST %d 个交易日样本", len(df), int(
        ((~df["yizi"]) & (df["boards"] == 1) & (~df["ts_code"].isin(st))).sum()))

    res = {"capital": CAPITAL, "top_n": TOP_N, "target": TARGET, "arms": {}, "secs": None}
    # 流动性下限 = 成交概率的现实代理: 成交额越大, 你的排队单越可能成交
    for rule, floor in (("amount", 0.0), ("small", 0.0), ("random", 0.0),
                        ("amount", 0.5), ("amount", 2.0)):
        tag = f"{rule}_amt{floor}"
        daily = portfolio(df, st, 1.0, TOP_N, rule, min_amount_yi=floor)
        st_ = weekly_stats(daily)
        res["arms"][tag] = st_
        log.info("[%-14s p=100%%] 周均 %+.3f%% 中位 %+.3f%% | ≥3%% 的周 %.1f%% | 年化 %+.0f%% "
                 "MDD %.1f%% Sharpe %.2f | 最差周 %.1f%%", tag,
                 st_["week_mean_pct"], st_["week_median_pct"],
                 100 * st_["week_ge_3pct_frac"], st_["ann_pct"], st_["mdd_pct"],
                 st_["sharpe"], st_["week_worst_pct"])

    log.info("=== 成交概率敏感性(amount 规则, 无流动性下限) ===")
    for p in (0.25, 0.357, 0.5, 0.75, 1.0):
        s = weekly_stats(portfolio(df, st, p, TOP_N, "amount", min_amount_yi=0.5))
        res["arms"][f"amount_p{int(p*1000)}"] = s
        log.info("  p=%.1f%% ⇒ 周均 %+.3f%% | ≥3%% 的周 %.1f%% | 年化 %+.0f%% MDD %.1f%%",
                 100 * p, s["week_mean_pct"], 100 * s["week_ge_3pct_frac"], s["ann_pct"],
                 s["mdd_pct"])

    log.info("=== 仓位数量敏感性(p=100%%) ===")
    for n in (3, 5, 10):
        s = weekly_stats(portfolio(df, st, 1.0, n, "amount"))
        res["arms"][f"amount_top{n}"] = s
        log.info("  N=%2d ⇒ 周均 %+.3f%% | ≥3%% 的周 %.1f%% | MDD %.1f%%",
                 n, s["week_mean_pct"], 100 * s["week_ge_3pct_frac"], s["mdd_pct"])

    # 逐年(amount, p=100%)
    daily = portfolio(df, st, 1.0, TOP_N, "amount")
    by_year = {}
    for y, sub in daily.groupby(daily.index.str[:4].rename("y")):
        s = weekly_stats(sub)
        if s:
            by_year[y] = {"week_mean_pct": s["week_mean_pct"],
                          "ge3_frac": s["week_ge_3pct_frac"], "n_weeks": s["n_weeks"]}
            log.info("  %s 周均 %+.3f%% | ≥3%% 的周 %.0f%% (%d 周)", y, s["week_mean_pct"],
                     100 * s["week_ge_3pct_frac"], s["n_weeks"])
    res["by_year"] = by_year

    best = res["arms"]["amount_p1000"]
    res["verdict"] = {
        "week_mean_pct_full_fill": best["week_mean_pct"],
        "ge3_frac_full_fill": best["week_ge_3pct_frac"],
        "reaches_3pct_at_full_fill": bool(best["week_mean_pct"] >= 3.0),
        "min_fill_for_3pct_mean": round(3.0 / best["week_mean_pct"], 3)
        if best["week_mean_pct"] > 0 else None,
    }
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 判决: 满成交周均 %+.3f%%; 周均 ≥3%% 需成交概率 ≥ %.1f%%; 完成 %.1fs → %s ==",
             best["week_mean_pct"], 100 * (res["verdict"]["min_fill_for_3pct_mean"] or 9.99),
             res["secs"], OUT)


if __name__ == "__main__":
    main()
