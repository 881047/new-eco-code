"""回撤/年化归因诊断(2026-09-10) —— 回答「回撤这么大、年化不高, 是不是模型不靠谱」

不做任何假设, 只用已 pin 基线的 backtest_nav_daily(含 bench_nav=中证800)拆三件事:
  1. 最大回撤发生在哪几段、多深、和指数自己跌的时间是否重合(重合度 = beta 成分)
  2. 日频回归: 系统线/满仓线 对 中证800 的 beta、R²、年化 alpha
  3. 分年: 指数收益 / 策略收益 / 超额, 看 alpha 是「常年略负」还是「个别年极端」
  4. t 统计量: 超额收益的均值/标准误 —— 9.7 年样本下能区分「有 alpha」和「没 alpha」吗
只读, 不写库。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

RUN = sys.argv[1] if len(sys.argv) > 1 else "20260910_fees0910"
BASE = Path(__file__).resolve().parents[2]

conn = sqlite3.connect(str(BASE / "data" / "neweco.db"))
d = pd.read_sql(
    "SELECT trade_date, nav, sys_nav, bench_nav FROM backtest_nav_daily "
    "WHERE run_date=? ORDER BY trade_date", conn, params=(RUN,))
m = pd.read_sql(
    "SELECT trade_date, nav, sys_nav, bench_nav FROM backtest_nav "
    "WHERE run_date=? ORDER BY trade_date", conn, params=(RUN,))
summ = pd.read_sql("SELECT metrics FROM backtest_summary WHERE run_date=?",
                   conn, params=(RUN,))
conn.close()

for df in (d, m):
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    for c in ("nav", "sys_nav", "bench_nav"):
        df[c] = pd.to_numeric(df[c], errors="coerce")

print("=" * 96)
print("run = {}   日频 {} 行  {} → {}".format(
    RUN, len(d), d["trade_date"].iloc[0].date(), d["trade_date"].iloc[-1].date()))
if len(summ):
    import json
    mt = json.loads(summ["metrics"].iloc[0])
    print("summary: 满仓 ann={:.2%} mdd={:.2%} | 系统 ann={:.2%} mdd={:.2%}(日频 {:.2%})".format(
        mt["ann_return"], mt["max_drawdown"], mt["sys_ann_return"],
        mt["sys_max_drawdown"], mt["sys_mdd_daily"]))
print("=" * 96)


def dd_series(s):
    return s / s.cummax() - 1.0


def episodes(s, thresh=-0.10):
    """回撤事件: 从跌穿 thresh 到回本为一段, 记录峰值日/谷底日/深度/回本日。"""
    dd = dd_series(s)
    eps, start = [], None
    for dt, v in dd.items():
        if start is None and v < thresh:
            start = dt
        elif start is not None and v >= -1e-12:
            seg = dd.loc[start:dt]
            tr = seg.idxmin()
            eps.append({"peak": s.loc[:tr].idxmax(), "trough": tr, "recover": dt,
                        "depth": float(seg.min())})
            start = None
    if start is not None:
        seg = dd.loc[start:]
        tr = seg.idxmin()
        eps.append({"peak": s.loc[:tr].idxmax(), "trough": tr, "recover": None,
                    "depth": float(seg.min())})
    return sorted(eps, key=lambda e: e["depth"])


print("\n【1】最大回撤事件(阈值 -10%)")
for name, col in (("满仓线", "nav"), ("系统线", "sys_nav"), ("中证800", "bench_nav")):
    s = d.set_index("trade_date")[col].dropna()
    eps = episodes(s)
    print("\n  {}  年化波动 {:.1%}".format(
        name, s.pct_change().std() * np.sqrt(252)))
    for e in eps[:4]:
        rec = e["recover"].date() if e["recover"] is not None else "未回本"
        span = ((e["trough"] - e["peak"]).days, (e["recover"] - e["peak"]).days
                if e["recover"] is not None else None)
        print("    {:>7.2%}  {:} → {:} (回本 {:})   峰→谷 {:>4d} 天, 峰→回本 {}".format(
            e["depth"], e["peak"].date(), e["trough"].date(), rec,
            span[0], "{} 天".format(span[1]) if span[1] else "—"))

print("\n【2】对中证800 的日频回归(超额 = 策略 - beta×指数)")
for name, col in (("满仓线", "nav"), ("系统线", "sys_nav")):
    x = d[["bench_nav", col]].dropna()
    rb = x["bench_nav"].pct_change().dropna()
    rs = x[col].pct_change().dropna()
    idx = rb.index.intersection(rs.index)
    rb, rs = rb.loc[idx], rs.loc[idx]
    beta, alpha = np.polyfit(rb, rs, 1)
    resid = rs - (alpha + beta * rb)
    r2 = 1 - resid.var() / rs.var()
    t_alpha = alpha / (resid.std(ddof=2) / np.sqrt(len(rs)))
    ann_a = (1 + alpha) ** 252 - 1
    print("  {}: beta={:.2f}  R²={:.2f}  年化 alpha={:+.2%}  (t={:.2f}, n={})".format(
        name, beta, r2, ann_a, t_alpha, len(rs)))
    print("      策略年化波动 {:.1%} / 指数 {:.1%} / 残差 {:.1%}".format(
        rs.std() * np.sqrt(252), rb.std() * np.sqrt(252), resid.std() * np.sqrt(252)))
    # beta 复刻线: 只吃 beta、不吃 alpha, 看它自己的最大回撤
    syn = (1 + beta * rb).cumprod()
    print("      同 beta 的「纯指数」复刻线最大回撤 {:.2%}  ← 纯 beta 成分".format(
        float(dd_series(syn).min())))

print("\n【3】分年收益")
d["y"] = d["trade_date"].dt.year
print("    {:>6} {:>10} {:>10} {:>10} {:>10}".format("年", "中证800", "满仓线", "系统线", "系统超额"))
for y, g in d.groupby("y"):
    if len(g) < 20:
        continue
    r = {c: g[c].iloc[-1] / g[c].iloc[0] - 1 for c in ("bench_nav", "nav", "sys_nav")}
    print("    {:>6} {:>+10.2%} {:>+10.2%} {:>+10.2%} {:>+10.2%}".format(
        y, r["bench_nav"], r["nav"], r["sys_nav"], r["sys_nav"] - r["bench_nav"]))

print("\n【4】能不能区分「有 alpha」和「没 alpha」")
for name, col in (("满仓线", "nav"), ("系统线", "sys_nav")):
    # 月频(与调仓周期一致), 超额对指数, t = 均值/标准误
    mm = m[["trade_date", "bench_nav", col]].dropna().set_index("trade_date")
    ex = mm[col].pct_change().dropna() - mm["bench_nav"].pct_change().dropna()
    n = len(ex)
    t = ex.mean() / (ex.std(ddof=1) / np.sqrt(n))
    ann_ex = ex.mean() * 12
    se_ann = ex.std(ddof=1) / np.sqrt(n) * 12
    print("  {} 月频超额: 均值 {:+.2%}/年, 标准误 ±{:.2%}, t={:.2f}, n={} 期 → "
          "95% 区间 [{:+.1%}, {:+.1%}]".format(
              name, ann_ex, se_ann, t, n, ann_ex - 1.96 * se_ann, ann_ex + 1.96 * se_ann))
