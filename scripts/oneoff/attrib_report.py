"""归因报告(2026-09-09): 回测年化从 38.51% 跌到 5.94% 的逐项拆解

数据源: backtest_nav / backtest_summary (库内已存在的实验 run)
口径: 月频净值 115 期 (2017-02 ~ 2026-08), 引擎 nav 初值 1.0, 首行即首月收益
  → 首月收益 = nav[0] - 1 (不是 nav[0]/10000 - 1)
"""
import json
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
    """月度收益; 首月以 nav 初值 1.0 为基准"""
    s = nav(run)["nav"]
    r = s.pct_change()
    r.iloc[0] = s.iloc[0] - 1.0
    return r


def ann(r):
    return (1 + r).prod() ** (12 / len(r)) - 1


def summ(run):
    m = pd.read_sql("SELECT metrics FROM backtest_summary WHERE run_date=?", conn, params=(run,))
    return json.loads(m["metrics"].iloc[0])


def boot(dl, n=20000, seed=7):
    rng = np.random.default_rng(seed)
    x = dl.values
    s = rng.choice(x, size=(n, len(x)), replace=True).sum(axis=1)
    return np.percentile(s, [5, 50, 95])


def compare(name, run_a, run_b, note=""):
    """run_a = 修复前, run_b = 修复后"""
    a, b = rets(run_a), rets(run_b)
    dl = np.log1p(b) - np.log1p(a)
    n = len(dl)
    t = dl.mean() / (dl.std(ddof=1) / math.sqrt(n))
    q = boot(dl)
    print(f"\n--- {name} {note}")
    print(f"  年化 {ann(a):+.2%} → {ann(b):+.2%}  (Δ{ann(b)-ann(a):+.2f}pp)")
    print(f"  累计 log 差 {dl.sum():+.4f} (= 终值比 {math.exp(dl.sum()):.4f}) | 月均 {dl.mean():+.3%} | "
          f"sd {dl.std(ddof=1):.2%} | t = {t:+.2f}")
    verdict = "显著为负" if q[2] < 0 else ("显著为正" if q[0] > 0 else "含 0, 与噪声不可区分")
    print(f"  自助法 5%/50%/95% 分位 [{q[0]:+.3f} / {q[1]:+.3f} / {q[2]:+.3f}] → {verdict}")
    print(f"  差异月数(>0.1pp): {int((dl.abs() > 0.001).sum())} / {n}")
    big = dl.reindex(dl.abs().nlargest(8).index).sort_values()
    print("  影响最大的 8 个月:")
    for k, v in big.items():
        print(f"    {k}  前 {a[k]:+7.2%} → 后 {b[k]:+7.2%}  (Δ{math.expm1(v)*100:+.1f}pp)")
    return dl


print("=" * 80)
print("零、修复链条(全部 run 已在库)")
print("=" * 80)
chain = [
    ("20260906_round15", "round15 旧基线"),
    ("20260907_fix5", "fix5"),
    ("20260908_20260908_fullmarket", "0908 全市场 PIT(含 ST 过滤)"),
    ("20260909_oldm_nost", "A/B 旧模型·ST过滤关"),
    ("20260909_oldm_st", "A/B 旧模型·ST过滤开"),
    ("20260909_ab0_ctrl", "ab0 对照组"),
    ("20260909_ab1_ann", "ab1 +①披露日顺延"),
    ("20260909_ab2_ann_delist", "ab2 +③退市/口径"),
    ("20260909_fix0909", "fix0909 生产基线"),
]
print(f"{'run':34s} {'年化':>8s} {'超额':>8s} {'MDD':>8s} {'日频MDD':>9s} {'夏普':>7s} {'IC':>7s} {'持仓':>6s} {'闲置':>7s}")
for run, label in chain:
    m = summ(run)
    print(f"{run:34s} {m['ann_return']:>7.2%} {m['excess_ann']:>7.2%} {m['max_drawdown']:>7.2%} "
          f"{m['mdd_daily']:>8.2%} {m['sharpe']:>7.3f} {m['ic_mean']:>7.4f} {m['avg_stocks']:>6.2f} "
          f"{m['avg_cash_idle']:>6.1%}   {label}")

print()
print("=" * 80)
print("一、生产基线 20260909_fix0909 的统计强度(115 个月)")
print("=" * 80)
s = nav("20260909_fix0909")
r = rets("20260909_fix0909")
b = s["bench_nav"].pct_change()
b.iloc[0] = s["bench_nav"].iloc[0] - 1.0
n = len(r)
mu, sd = r.mean(), r.std(ddof=1)
print(f"月均 {mu:+.3%} | 月 sd {sd:.3%} | 年化 {ann(r):+.2%} (几何) | 算术年化 {mu*12:+.2%}")
print(f"月均 t 值(对 0) = {mu/(sd/math.sqrt(n)):+.2f}  → 与 0 无统计差异(<1.96)" if abs(mu/(sd/math.sqrt(n))) < 1.96
      else f"月均 t 值(对 0) = {mu/(sd/math.sqrt(n)):+.2f}")
d = r - b
print(f"月均超额 {d.mean():+.3%} | 超额 t = {d.mean()/(d.std(ddof=1)/math.sqrt(n)):+.2f}")
print(f"年化标准误 ≈ {sd*math.sqrt(12)/math.sqrt(n/12):.2%} → 年化 95% 区间约 "
      f"[{(mu-1.96*sd/math.sqrt(n))*12:+.1%}, {(mu+1.96*sd/math.sqrt(n))*12:+.1%}] (线性近似)")
print("\n分年:")
rr = r.copy(); rr.index = pd.to_datetime(rr.index)
bb = b.copy(); bb.index = pd.to_datetime(bb.index)
for y in sorted(set(rr.index.year)):
    g = rr[rr.index.year == y]; gb = bb[bb.index.year == y]
    print(f"  {y}: 策略 {(1+g).prod()-1:+7.2%} | 基准 {(1+gb).prod()-1:+7.2%} | "
          f"超额 {(1+g).prod()-(1+gb).prod():+7.2%} | 最差月 {g.min():+6.2%} | 胜月 {int((g>0).sum())}/{len(g)}")
print(f"\n集中度(全部 {n} 个月, 总收益 {(1+r).prod()-1:+.1%}):")
for k in (1, 3, 5, 10):
    print(f"  最好 {k:2d} 个月 {(1+r.nlargest(k)).prod()-1:+8.1%} | 最差 {k:2d} 个月 {(1+r.nsmallest(k)).prod()-1:+8.1%}")
print(f"  去掉最好的 5 个月后年化: {((1+r.drop(r.nlargest(5).index)).prod()**(12/(n-5))-1):+.2%}")
print(f"  去掉最差的 5 个月后年化: {((1+r.drop(r.nsmallest(5).index)).prod()**(12/(n-5))-1):+.2%}")

print()
print("=" * 80)
print("二、ST/退 PIT 过滤 A/B(旧模型, 唯一开关差异) —— 最大单项")
print("=" * 80)
compare("ST/退 过滤 关→开", "20260909_oldm_nost", "20260909_oldm_st",
        "(关=修复前口径, 可买 ST/退)")

print()
print("=" * 80)
print("三、① 财务披露日顺延 A/B(ab0→ab1)")
print("=" * 80)
compare("① 披露日顺延 关→开", "20260909_ab0_ctrl", "20260909_ab1_ann")

print()
print("=" * 80)
print("四、③ 退市/口径 A/B(ab1→ab2)")
print("=" * 80)
compare("③ 退市处理 关→开", "20260909_ab1_ann", "20260909_ab2_ann_delist")

print()
print("=" * 80)
print("五、合计校验")
print("=" * 80)
a0, a2 = rets("20260909_oldm_nost"), rets("20260909_fix0909")
print(f"旧口径(可买ST/退, 未修①③) 年化 {ann(a0):+.2%} → 生产基线 {ann(a2):+.2%}")
print(f"累计 log 差 {np.log1p(a2).sum()-np.log1p(a0).sum():+.4f} = 终值比 {math.exp(np.log1p(a2).sum()-np.log1p(a0).sum()):.4f}")
conn.close()
