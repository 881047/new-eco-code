"""回测基准那一腿用的是**价格指数**还是**全收益**：量级实测（2026-09-19，一次性；**只读**）

## 为什么

`app/backtest/engine.py:603-605` 的基准腿取 `m["bench"]`，而 `m["bench"]` 是
`index_daily.close`（`factor_engine.py:180-181`，代码 = `benchmark_code()` = 000906.SH 中证800）。
指数行情表只有**价格指数**（不含分红再投资），而持仓那一腿按 `close × adj_factor` 计价
（`engine.py:341`）= **全收益**口径（分红按除权日计入）。两条腿不同尺 ⇒
`excess_ann = 策略年化 − 基准年化` 被系统性抬高一个"基准股息率"的量级。

这件事对本项目不是修辞问题：README/02 号现行的全史与留出集超额是**判决口径**，
而用户的目标线是"周频超额 ≥ +10pp/年"。抬高量若接近 1pp 就占掉目标的 10%。

## 口径（跑数前写死，不做判决）

- 抬高量下界 = 中证800 成分股的**市值加权股息率**（`index_weight` ∩ `daily_basic.dv_ttm`），
  逐月末算一次再按年取均值 —— 这正是价格指数漏掉的那块收益；
  ⚠️ `dv_ttm` 是"过去 12 个月实际派发"，与指数编制用的"预期股息率"略有出入 ⇒ 当作一阶估计。
- 抬高量上界 = 同一批成分股市值加权 `dv_ttm` 的**年复合**口径（`Π(1+y_m)`），
  因为分红再投资本身还会再吃一份收益 ⇒ 复权版本必然 ≥ 算术版本。
- 覆盖域只有 `index_weight` 的 20190131~20260831（早于 2019 无权重可得）⇒
  报告必须写明"这段窗口的估计"，不许外推到全史。
- 现行读数**只引用** `backtest_summary` 最近几行（不重跑回测、不动金标准）。
  ⚠️ 该表里绝大多数行是**实验臂**的 run（`20260919_abg_*` 这种），不是生产 pin；
  生产超额读数一律以 08 号 pin 台账为准 ⇒ 本脚本只把"抬高量"算出来，
  由引用者自己除以**自己那把尺上的** excess_ann 才是占比。

## 纪律

**这不是判决、不改任何代码/口径。** 改基准 = 改回测成交与标签之外的第三条口径 ⇒
按 01 号要走 A/B + 预注册 + 重钉 pin。本脚本只把"要不要立项"的量级问题回答掉。
引用本脚本任何数字时必须连"一阶估计 / 2019~2026 覆盖域 / 未做 A/B"一起引用。

运行：`D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\audit_bench_total_return_gap.py`
产物：`logs/audit_bench_total_return_gap.json`（自己的文件，不写任何生产表）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

OUT = BASE / "logs" / "audit_bench_total_return_gap.json"
INDEX_START = "20190131"          # index_weight 覆盖起点（实测）


def _ro():
    con = sqlite3.connect(f"file:{BASE / 'data' / 'neweco.db'}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=1")
    return con


def bench_price_annualized(con, code: str) -> dict:
    """价格指数自身的年化（用来把"抬高量"换算成超额读数的百分比）。"""
    s = pd.read_sql("SELECT trade_date, close FROM index_daily WHERE ts_code=? "
                    "AND trade_date>=? ORDER BY trade_date", con, params=(code, INDEX_START))
    if len(s) < 20:
        return {}
    d0, d1 = s["trade_date"].iloc[0], s["trade_date"].iloc[-1]
    tot = float(s["close"].iloc[-1] / s["close"].iloc[0] - 1.0)
    years = (pd.Timestamp(d1) - pd.Timestamp(d0)).days / 365.25
    ann = (1.0 + tot) ** (1.0 / years) - 1.0 if years > 0 else None
    return {"code": code, "from": d0, "to": d1, "years": round(years, 2),
            "price_total": round(tot, 4), "price_ann": round(ann, 4)}


def dividend_yield_by_month(con, code: str) -> pd.Series:
    """逐月末：指数成分股的市值加权 dv_ttm（%）。缺权重的月份自动跳过。"""
    months = [r[0] for r in con.execute(
        "SELECT DISTINCT trade_date FROM index_weight WHERE index_code=? AND trade_date>=?"
        " ORDER BY trade_date", (code, INDEX_START))]
    out = {}
    for d in months:
        row = con.execute(
            "SELECT SUM(b.dv_ttm * w.weight) / SUM(w.weight) "
            "FROM index_weight w JOIN daily_basic b "
            "  ON b.ts_code = w.con_code AND b.trade_date = w.trade_date "
            "WHERE w.index_code=? AND w.trade_date=? AND b.dv_ttm IS NOT NULL AND w.weight>0",
            (code, d)).fetchone()
        if row and row[0] is not None:
            out[d] = float(row[0])
    return pd.Series(out, dtype=float).sort_index()


def recent_summary_rows(con, n: int = 5) -> list:
    """最近 n 行回测读数（含实验臂，仅供"抬高量占比"心算；生产 pin 以 08 号为准）。"""
    rows = con.execute("SELECT run_date, metrics FROM backtest_summary ORDER BY rowid DESC "
                       "LIMIT ?", (n,)).fetchall()
    out = []
    for run_date, blob in rows:
        try:
            mp = json.loads(blob) if blob else {}
        except json.JSONDecodeError:
            mp = {}
        out.append({"run_date": run_date, "start": mp.get("start"), "end": mp.get("end"),
                    "periods": mp.get("periods"), "ann_return": mp.get("ann_return"),
                    "bench_ann": mp.get("bench_ann"), "excess_ann": mp.get("excess_ann")})
    return out


def main() -> int:
    from app.config import benchmark_code
    code = benchmark_code()
    con = _ro()
    try:
        recent = recent_summary_rows(con)
        px = bench_price_annualized(con, code)
        y = dividend_yield_by_month(con, code)
    finally:
        con.close()

    if y.empty:
        print("index_weight ∩ daily_basic.dv_ttm 全空 —— 无法估计，先看数据覆盖")
        return 1
    yearly = y.groupby([k[:4] for k in y.index]).mean()   # 每年平均月度股息率(百分点/年)
    # dv_ttm 是**年化**股息率(%) ⇒ 月度切片是 y/12；再按年复利回来才是"分红再投资"的上界。
    # (直接把年化值当月度收益复利会把 2.2% 算成 30% —— 第一版就这么错过，量级一看就知道不对)
    compound = (1.0 + y / 1200.0).prod() ** (12.0 / len(y)) - 1.0
    arith_pp = float(yearly.mean())
    res = {
        "benchmark_code": code,
        "coverage": {"from": str(y.index.min()), "to": str(y.index.max()), "n_months": int(len(y))},
        "recent_backtest_summary_rows": recent,
        "bench_price_index": px,
        "bench_dividend_yield_pp_per_year": {
            "arith_mean_over_years": round(arith_pp, 3),
            "min_year": round(float(yearly.min()), 3),
            "max_year": round(float(yearly.max()), 3),
            "compound_upper_bound": round(float(compound) * 100.0, 3),
            "by_year": {k: round(float(v), 3) for k, v in yearly.items()},
        },
        "how_to_read": (
            "抬高量(百分点/年) = 上面的股息率：`excess_ann` 里被基准少算的那一块。"
            "占比 = 抬高量 ÷ |excess_ann×100| —— 分母请用 08 号 pin 台账上的生产读数，"
            "recent_backtest_summary_rows 只是同表的最近几行(多为实验臂)。"
            "注意策略腿的 adj_factor 把**个股**分红全额计入了，所以这不是双边抵消的误差。"),
        "not_a_verdict": "阶段 A 量级实测：未做 A/B、未改口径、未重钉 pin。",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"基准 {code} 价格指数年化({px.get('from')}~{px.get('to')}): "
          f"{(px.get('price_ann') or float('nan')) * 100:.2f}%")
    print(f"成分股市值加权股息率: 算术 {arith_pp:.2f}pp/年"
          f"（年度区间 {float(yearly.min()):.2f}~{float(yearly.max()):.2f}）、"
          f"复权上界 {float(compound) * 100:.2f}pp/年")
    print("⇒ 这就是 excess_ann 被抬高的量级（基准只算价格、持仓腿算全收益）")
    for r in recent[:3]:
        if r.get("excess_ann") is not None:
            print("   {:<28} excess_ann={:+.2f}pp/年 ⇒ 抬高量占 {:.0f}%".format(
                str(r["run_date"])[:28], r["excess_ann"] * 100.0,
                arith_pp / abs(r["excess_ann"] * 100.0) * 100.0))
    print(f"产物: {OUT.relative_to(BASE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
