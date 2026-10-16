# -*- coding: utf-8 -*-
"""黄金 ETF 网格回测（plans/gold-grid-prereg-20261002.md 规格与判据跑前冻结）。
固定锚点 1% 间距 20 格, 底仓 50%, T+0 双向, 双佣金口径, B&H 与货币垫对照。只读。"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("NO_PROXY", "*")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import time

import akshare as ak
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "etf_gold"
OUT = ROOT / "experiments" / "gold_grid_backtest_20261002.json"

SLICE = 10000.0
INIT_POS_PCT = 0.50
GRID_PCT = 0.01
N_GRIDS = 20
GRID_CASH = SLICE * (1 - INIT_POS_PCT) / N_GRIDS
CASH_FEE = 0.02                      # 货币垫年化对照
FEE_FREE5 = (0.0001, 0.0)            # 万1 免五
FEE_STD = (0.00025, 5.0)             # 万2.5 最低5元
START, END = "2019-01-02", "2026-09-30"


def log(m):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def load_data() -> pd.DataFrame:
    DATA.mkdir(parents=True, exist_ok=True)
    cache = DATA / "518880_qfq.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    df = None
    for attempt in range(3):                      # 东财接口瞬断(RemoteDisconnected)重试
        try:
            df = ak.fund_etf_hist_em(symbol="518880", period="daily",
                                     start_date="20190101", end_date="20261001",
                                     adjust="qfq")
            break
        except Exception as e:
            log(f"fund_etf_hist_em 第 {attempt+1} 次失败: {type(e).__name__}, 10s 后重试")
            time.sleep(10)
    if df is None or df.empty:
        # 回退: 新浪源(黄金 ETF 无分红, 不复权≈前复权, 注记)
        log("东财源失败, 回退新浪源")
        df = ak.fund_etf_hist_sina(symbol="sh518880")
        df = df.rename(columns={"date": "日期", "open": "开盘", "high": "最高",
                                "low": "最低", "close": "收盘", "volume": "成交量"})
        df["日期"] = df["日期"].astype(str)
    df.to_parquet(cache, index=False)
    return df


def run_grid(df: pd.DataFrame, fee_rate: float, fee_min: float) -> dict:
    anchor = float(df["收盘"].iloc[0])
    cash = SLICE * (1 - INIT_POS_PCT)
    shares = (SLICE * INIT_POS_PCT) / anchor
    n_trades = n_win = 0
    realized = 0.0
    last_buy = {}                    # 网格层 k → 买入价(算单笔盈亏)
    daily = []
    for _, r in df.iterrows():
        day_high, day_low = float(r["最高"]), float(r["最低"])
        day = r["日期"]
        # 冻结规格实现: 第 k 层 买在 anchor*(1-k*1%), 卖在 anchor*(1+k*1%);
        # 卖出优先(保守现金序)。
        for k in range(1, N_GRIDS + 1):
            sell_trigger = anchor * (1 + k * GRID_PCT)
            if last_buy.get(k) is not None and day_high >= sell_trigger:
                px = min(sell_trigger, day_high)
                fee = max(px * GRID_CASH / anchor * fee_rate, fee_min)
                cash += GRID_CASH - fee
                realized += (px - last_buy[k]) * (GRID_CASH / last_buy[k]) - fee
                n_win += 1 if px > last_buy[k] else 0
                n_trades += 1
                del last_buy[k]
        for k in range(N_GRIDS, 0, -1):
            buy_trigger = anchor * (1 - k * GRID_PCT)
            if last_buy.get(k) is None and k not in last_buy and cash >= GRID_CASH:
                if day_low <= buy_trigger:
                    px = max(buy_trigger, day_low)
                    fee = max(px * GRID_CASH / anchor * fee_rate, fee_min)
                    cash -= GRID_CASH + fee
                    last_buy[k] = px
                    n_trades += 1
        mv = shares * float(r["收盘"]) + sum(
            GRID_CASH * float(r["收盘"]) / last_buy[k] for k in last_buy) + cash
        daily.append({"date": day, "nav": mv})
    # 期末持仓按现价估值
    final = daily[-1]["nav"]
    return {"final_nav": round(final, 2), "n_trades": n_trades,
            "open_layers": len(last_buy),
            "win_rate_per_trade": round(n_win / max(1, n_trades), 4),
            "daily": daily}


def main() -> int:
    df = load_data()
    log(f"数据 {len(df)} 行 {df['日期'].min()}~{df['日期'].max()}")
    df = df[(df["日期"] >= START) & (df["日期"] <= END)].reset_index(drop=True)
    bh_ret = float(df["收盘"].iloc[-1] / df["收盘"].iloc[0] - 1)
    years = (pd.Timestamp(df["日期"].iloc[-1]) - pd.Timestamp(df["日期"].iloc[0])).days / 365.25
    bh_ann = (1 + bh_ret) ** (1 / years) - 1
    bh_dd = float((df["收盘"] / df["收盘"].cummax() - 1).min())

    results = {"asof": datetime.now().isoformat(timespec="seconds"),
               "criteria_file": "plans/gold-grid-prereg-20261002.md",
               "benchmark": {"bh_ann": round(bh_ann, 4), "bh_mdd": round(bh_dd, 4),
                             "cash_ann": CASH_FEE}, "arms": {}}
    for name, (fr, fm) in (("grid_free5", FEE_FREE5), ("grid_std_min5", FEE_STD)):
        res = run_grid(df, fr, fm)
        daily = pd.DataFrame(res.pop("daily"))
        nav = daily["nav"]
        ann = (nav.iloc[-1] / SLICE) ** (1 / years) - 1
        mdd = float((nav / nav.cummax() - 1).min())
        mret = nav.pct_change().dropna()
        res.update({"ann": round(ann, 4), "mdd": round(mdd, 4),
                    "monthly_win_rate": round(float((mret > 0).mean()), 4)})
        results["arms"][name] = res
        log(f"[{name}] 年化 {ann:+.2%} | MDD {mdd:.2%} | 月胜率 "
            f"{res['monthly_win_rate']:.0%} | 单笔胜率 {res['win_rate_per_trade']:.0%} "
            f"| 笔数 {res['n_trades']}")

    g = results["arms"]["grid_free5"]
    d1 = bool(g["ann"] - CASH_FEE >= 0.02)
    d2 = bool(g["win_rate_per_trade"] >= 0.60 and g["monthly_win_rate"] >= 0.65)
    d3 = bool(g["mdd"] <= results["benchmark"]["bh_mdd"] + 0.02)
    verdict = "GO(前提: 用户券商免五)" if (d1 and d2 and d3) else "NO-GO"
    results["criteria"] = {"D1_net_vs_cash_ge2pp": d1, "D2_winrates": d2,
                           "D3_mdd_bound": d3, "verdict": verdict}
    # 分段(信息性)
    for seg, lo, hi in (("2019_2023", "2019-01-01", "2023-12-31"),
                        ("2024_2026", "2024-01-01", "2026-09-30")):
        sub = df[(df["日期"] >= lo) & (df["日期"] <= hi)]
        seg_bh = float(sub["收盘"].iloc[-1] / sub["收盘"].iloc[0] - 1)
        y = (pd.Timestamp(sub["日期"].iloc[-1]) - pd.Timestamp(sub["日期"].iloc[0])).days / 365.25
        results.setdefault("segments", {})[seg] = {
            "bh_ann": round((1 + seg_bh) ** (1 / y) - 1, 4)}
    log(f"判决: {verdict} | D1={d1} D2={d2} D3={d3} | 分段: {results['segments']}")
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    log(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
