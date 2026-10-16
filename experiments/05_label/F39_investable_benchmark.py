"""F39 · **可投资基准**对照（"跟直接买指数/ETF 比呢？"）—— 只读，无新判决

## 为什么必须补这一课

此前所有"超额"都是对**等权可交易池**（约 3,700 只等权）算的 —— 那是**研究基准**，
个人买不到（要买就得持有几千只）。用户真正会问的是：

    "这套东西 vs **我真能买到的东西**（指数/ETF），到底赚了什么？"

## 口径（同一窗口、同一起止日，与 F28 完全一致）

  窗口 FULL = 2017-02-06 ~ 2026-09-07（策略的 491 期）；OOS = 2024-01-01 ~ 2026-08-31。

  ① **袖套（现役构型，净口径）**：年化收益 = F28 该区间的 `bench_ann + excess_pp/100`
     —— 注意它**含约 23% 现金**（投入率 0.769），所以与"满仓买指数"不是同结构；
  ② **等权可交易池**：F28 的 `bench_ann`（研究基准）；
  ③ **可投资标的**：中证800 / 沪深300 / 中证500 指数，以及 4 只 ETF
     （512100 中证1000ETF、510500 中证500ETF、159902 中小板ETF、159915 创业板ETF），
     ETF 用 `fund_daily.close × fund_adj.adj_factor`（总收益口径）；
  ④ **同结构调整**：`0.873 × 指数年化 + 0.127 × 1.5%`（把袖套那 12.7% 权益缺口与现金
     收益搬过去）⇒ 这一行才是与袖套**同风险预算**的可投资对照。

**不设判据、不采纳任何东西** —— 这是口径说明，不是新策略。它回答的是
"若用户不跑这套系统、而是买指数/ETF，会拿到什么"。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F39_investable_benchmark.py
"""
import hashlib
import json
import math
import sqlite3
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent
sys.path.insert(0, str(BASE))

OUT = HERE / "F39_investable_benchmark_result.json"
F28 = HERE / "F28_oos_unseal_result.json"
WINDOWS = {"FULL": ("2017-02-06", "2026-09-07"), "OOS": ("2024-01-01", "2026-08-31")}
IDX = {"000906.SH": "中证800", "000300.SH": "沪深300", "000905.SH": "中证500"}
ETF = {"512100.SH": "中证1000ETF", "510500.SH": "中证500ETF",
       "159902.SZ": "中小板ETF", "159915.SZ": "创业板ETF"}
EQUITY_W, CASH_RATE = 0.873, 0.015


def ann(series: pd.Series, dates: pd.Series) -> float:
    if len(series) < 2:
        return float("nan")
    yrs = (pd.Timestamp(dates.iloc[-1]) - pd.Timestamp(dates.iloc[0])).days / 365.25
    return float((series.iloc[-1] / series.iloc[0]) ** (1 / yrs) - 1) if yrs > 0 else float("nan")


def mdd(series: pd.Series) -> float:
    """最大回撤（与袖套/回测同一算法: 净值 / 累积最大 − 1 的最小值）。"""
    s = series.astype(float)
    return float((s / s.cummax() - 1).min())


def main():
    conn = sqlite3.connect(f"file:{BASE/'data'/'neweco.db'}?mode=ro", uri=True)
    f28 = json.loads(F28.read_text(encoding="utf-8"))
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "windows": {k: list(v) for k, v in WINDOWS.items()},
           "equity_weight": EQUITY_W, "cash_rate": CASH_RATE, "tables": {}, "secs": None}
    for wname, (lo, hi) in WINDOWS.items():
        rows = {}
        # ① 袖套与等权池(F28 里两套种子的均值)
        benches, excesses = [], []
        for seed in ("seedA", "seedB"):
            blk = f28["arms"][seed]["net"]["OOS_24m" if wname == "OOS" else "full"]
            benches.append(blk["bench_ann"])
            excesses.append(blk["excess_pp"] / 100.0)
        pool = float(sum(benches) / len(benches))
        sleeve = pool + float(sum(excesses) / len(excesses))
        mdd_sleeve = float(sum(
            f28["arms"][s]["net"]["OOS_24m" if wname == "OOS" else "full"]["mdd"]
            for s in ("seedA", "seedB")) / 2)
        rows["袖套(现役,净,含23%现金)"] = {"ann": round(sleeve, 4), "mdd": round(mdd_sleeve, 4)}
        rows["等权可交易池(研究基准)"] = {"ann": round(pool, 4)}
        # ② 指数
        for code, name in IDX.items():
            df = pd.read_sql("SELECT trade_date, close FROM index_daily WHERE ts_code=? "
                             "AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
                             conn, params=(code, lo, hi))
            a = ann(df["close"], df["trade_date"])
            rows[f"{name}(指数)"] = {"ann": round(a, 4), "mdd": round(mdd(df["close"]), 4)}
            rows[f"{name}(指数×0.873+现金)"] = {
                "ann": round(EQUITY_W * a + (1 - EQUITY_W) * CASH_RATE, 4)}
        # ③ ETF(总收益: close × adj)
        for code, name in ETF.items():
            df = pd.read_sql("SELECT d.trade_date, d.close * COALESCE(a.adj_factor, 1.0) AS px "
                             "FROM fund_daily d LEFT JOIN fund_adj a USING (ts_code, trade_date) "
                             "WHERE d.ts_code=? AND d.trade_date BETWEEN ? AND ? "
                             "ORDER BY d.trade_date", conn, params=(code, lo, hi))
            if len(df) < 100:
                rows[name] = None
                continue
            a = ann(df["px"], df["trade_date"])
            rows[f"{name}(ETF)"] = {"ann": round(a, 4), "mdd": round(mdd(df["px"]), 4)}
            rows[f"{name}(ETF×0.873+现金)"] = {
                "ann": round(EQUITY_W * a + (1 - EQUITY_W) * CASH_RATE, 4)}
        res["tables"][wname] = rows
    conn.close()
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    for wname, rows in res["tables"].items():
        print(f"== {wname} {WINDOWS[wname][0]}~{WINDOWS[wname][1]} ==")
        for k, v in rows.items():
            if v is None:
                print(f"  {k:26s} —")
            elif "mdd" in v:
                print(f"  {k:26s} 年化 {100*v['ann']:+.2f}%  最大回撤 {100*v['mdd']:+.1f}%")
            else:
                print(f"  {k:26s} 年化 {100*v['ann']:+.2f}%")
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
