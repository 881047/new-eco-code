"""归因补充(2026-09-09): 生产基线的统计强度 + ST 过滤 A/B 的逐月拆解

数据源(均已在库):
  backtest_nav: 20260909_fix0909(生产) / oldm_st(旧模型+过滤) / oldm_nost(旧模型-过滤)
  口径: 月频净值, 115 期(2017-02 ~ 2026-08)
"""
import math
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

conn = sqlite3.connect("data/neweco.db")


def nav(run):
    s = pd.read_sql("SELECT trade_date, nav, sys_nav, bench_nav FROM backtest_nav "
                    "WHERE run_date=? ORDER BY trade_date", conn, params=(run,))
    return s.set_index("trade_date")


def rets(run):
    s = nav(run)["nav"]
    r = s.pct_change()
    r.iloc[0] = s.iloc[0] / 10000.0 - 1  # 首月按 1 万本金起算
    return r


print("=" * 78)
print("一、生产基线 20260909_fix0909 的统计强度(115 个月)")
print("=" * 78)
s = nav("20260909_fix0909")
r = rets("20260909_fix0909")
b = s["bench_nav"].pct_change()
b.iloc[0] = s["bench_nav"].iloc[0] - 1
n = len(r)
mu, sd = r.mean(), r.std(ddof=1)
print(f"月均收益 {mu:+.3%} | 月标准差 {sd:.3%} | 年化 {((1+r).prod())**(12/n)-1:+.2%}")
print(f"月均收益 t 值(对 0) = {mu/(sd/math.sqrt(n)):+.2f}")
d = r - b
print(f"月均超额 {d.mean():+.3%} | 超额 t 值 = {d.mean()/(d.std(ddof=1)/math.sqrt(n)):+.2f} "
      f"(|t|<1.96 即与 0 无统计差异)")
print(f"年化收益的标准误 ≈ {sd*math.sqrt(12)/math.sqrt(n/12):.2%} → 95% 置信区间 "
      f"约 [{(mu-1.96*sd/math.sqrt(n))*12:+.1%}, {(mu+1.96*sd/math.sqrt(n))*12:+.1%}] (粗略线性近似)")
print()
print("分年收益:")
rr = r.copy()
rr.index = pd.to_datetime(rr.index)
bb = b.copy()
bb.index = pd.to_datetime(bb.index)
for y, g in rr.groupby(rr.index.year):
    gb = bb[bb.index.year == y]
    print(f"  {y}: 策略 {(1+g).prod()-1:+7.2%} | 基准 {(1+gb).prod()-1:+7.2%} | "
          f"超额 {(1+g).prod()-(1+gb).prod():+7.2%} | 最差月 {g.min():+6.2%}")
print()
print("收益集中度(全部 115 个月):")
tot = (1 + r).prod() - 1
for k in (1, 3, 5, 10):
    top = r.nlargest(k)
    print(f"  最好的 {k:2d} 个月贡献 {(1+top).prod()-1:+.1%} (总收益 {tot:+.1%})")
print(f"  最差的 5 个月合计 {(1+r.nsmallest(5)).prod()-1:+.1%}")
print()

print("=" * 78)
print("二、ST/退 过滤 A/B(旧模型, 仅过滤开关不同)")
print("=" * 78)
a = rets("oldm_nost")   # 过滤关 = 修复前
c = rets("oldm_st")     # 过滤开 = 修复后
print(f"过滤关(修复前) 年化 {((1+a).prod())**(12/len(a))-1:+.2%} | "
      f"过滤开(修复后) 年化 {((1+c).prod())**(12/len(c))-1:+.2%}")
dl = np.log1p(c) - np.log1p(a)
print(f"逐月 log 差: 均值 {dl.mean():+.3%} | 累计 {dl.sum():+.3f} (= 终值比 {math.exp(dl.sum()):.4f})")
print(f"  差异月数(>0.1pp): {int((dl.abs()>0.001).sum())} / {len(dl)}")
print(f"  t = {dl.mean()/(dl.std(ddof=1)/math.sqrt(len(dl))):+.2f}")
print("  受影响最大的 10 个月:")
for k, v in dl.reindex(dl.abs().nlargest(10).index).sort_values().items():
    print(f"    {k}  修复前 {a[k]:+7.2%} → 修复后 {c[k]:+7.2%}  (Δ{math.expm1(v)*100:+.1f}pp)")
print()
print("两臂 IC 对比(模型排序质量):")
for run in ("oldm_nost", "oldm_st"):
    m = pd.read_sql("SELECT metrics FROM backtest_summary WHERE run_date=?", conn, params=(run,))
    import json
    mm = json.loads(m["metrics"].iloc[0])
    print(f"  {run}: IC {mm['ic_mean']} / ICIR {mm['icir']} / 平均持仓 {mm['avg_stocks']} 只 / "
          f"闲置 {mm['avg_cash_idle']:.1%}")
conn.close()
