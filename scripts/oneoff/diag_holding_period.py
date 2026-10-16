"""持有期经济性诊断(2026-09-09 深夜): 换手/成本 vs alpha 衰减

背景: 生产月频换手 91%/月, 单边成本 0.3%+滑点 0.1%(1 万本金下最低佣金≈0.31%/笔),
      → 每月成本 ≈ 0.91 × 0.8% ≈ 0.73%, 年化 ≈ 8.7%, 比策略净年化(5.94%)还大。
      所以「年化上去」的第一杠杆可能不是选股, 而是**持有期**。

本脚本不改模型、不重训, 只回答三个问题:
  Q1 alpha 随持有期怎么衰减? (h=1/2/3 个月的 top-n 绝对收益与行业相对超额)
  Q2 名字持久性: 相邻月 top-n 重叠率(决定降频后换手能降多少)
  Q3 理想化组合(等权、月/双月/季调仓、含成本)的净年化与回撤对比

成本口径与引擎一致: 只对「新进名字」收双边 2×(0.3%+0.1%)=0.8%。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app import db
from app.logger import setup_logger
from app.factors.factor_engine import load_market, month_end_dates, industry_map

setup_logger()
db.init_db()

COST = 0.003 + 0.001          # 单边成本+滑点(1 万口径)
H = [1, 2, 3]                 # 持有月数
NS = [3, 6, 12]

m = load_market(since="2016-01-01")
ac = m["close"] * m["adj"]
dates = pd.to_datetime(m["close"].index)
md_all = month_end_dates(dates)

conn = db.get_conn()
inds = industry_map(conn)
sc = pd.read_sql("SELECT ts_code, trade_date, score FROM model_score", conn)
conn.close()
sc["trade_date"] = pd.to_datetime(sc["trade_date"])
wide = sc.pivot_table(index="trade_date", columns="ts_code", values="score").sort_index()
md = md_all[md_all.isin(wide.index)]
print(f"评分月 {len(md)} 个: {md[0].date()} → {md[-1].date()} | 截面中位 {int(wide.notna().sum(axis=1).median())} 只")

# 绝对前瞻收益(任意持有期): 月末 t → 第 h 个月末的绝对收益
# 注意: 位置必须在 **月末序列 md** 上取, 不能在全部交易日上取(否则 h 变成交易日)
px = ac.reindex(index=dates, columns=wide.columns)
md_pos = {d: i for i, d in enumerate(md)}


def fwd(t, h_months):
    """从 t 起持有 h_months 个自然月(月末→月末)的绝对收益"""
    i, j = md_pos[t], md_pos[t] + h_months
    if j >= len(md):
        return None
    return px.loc[md[j]] / px.loc[md[i]] - 1


def topn(t, n):
    s = wide.loc[t].dropna()
    return s.nlargest(n).index


print()
print("=" * 100)
print("Q1/Q2: alpha 衰减 + 名字持久性(行业相对超额=绝对收益减行业均值)")
print("=" * 100)
# 预先算好每个月的绝对收益面板
fwd_abs = {h: pd.DataFrame({d: fwd(d, h) for d in md}).T for h in H}
for h in H:
    fwd_abs[h] = fwd_abs[h].dropna(how="all")

# 行业相对
fwd_rel = {}
for h in H:
    f = fwd_abs[h]
    inds_h = inds.reindex(f.columns)
    fwd_rel[h] = f - f.T.groupby(inds_h, sort=False).transform("mean").T

print(f"{'n':>3} {'h(月)':>6} {'月均绝对':>10} {'月均超额':>10} {'折月超额':>10} {'IC':>8}")
for n in NS:
    for h in H:
        ab, rel, ic = [], [], []
        for d in md:
            if d not in fwd_abs[h].index:
                continue
            codes = topn(d, n)
            r_abs = fwd_abs[h].loc[d].reindex(codes)
            r_rel = fwd_rel[h].loc[d].reindex(codes)
            ab.append(r_abs.mean())
            rel.append(r_rel.mean())
            lab = fwd_rel[h].loc[d]
            s = wide.loc[d].dropna()
            common = s.index.intersection(lab.dropna().index)
            if len(common) >= 100:
                ic.append(s.reindex(common).corr(lab.reindex(common), method="spearman"))
        ab, rel = np.array(ab), np.array(rel)
        print(f"{n:>3} {h:>6} {ab.mean()/h:>9.2%} {rel.mean()/h:>9.2%} "
              f"{rel.mean()/h:>9.2%} {np.mean(ic):>8.4f}")

print()
print("名字持久性(top-n 相邻月重叠率):")
for n in NS:
    sel = {d: set(topn(d, n)) for d in md}
    for lag in (1, 2, 3):
        ov = [len(sel[md[i]] & sel[md[i - lag]]) / n for i in range(lag, len(md))]
        print(f"  n={n:>2} lag={lag}月: 重叠 {np.mean(ov):>6.1%} → 换手 {1-np.mean(ov):>6.1%}")

print()
print("=" * 100)
print(f"Q3: 理想化组合(等权/月末调仓/成本 {COST*2:.1%} 双边仅收新名字) 净年化与回撤")
print("=" * 100)
print(f"{'n':>3} {'持有':>5} {'年化':>9} {'回撤':>9} {'夏普':>7} {'月换手':>7} {'年成本':>8}")
for n in NS:
    for h in H:
        # 相位平均: 每 h 个月调仓, 相位 p=0..h-1 各跑一条, 取平均
        anns, mdds, shs, tos, costs = [], [], [], [], []
        for p in range(h):
            nav, rets, turn = 1.0, [], []
            prev = None
            i = p
            while i < len(md):
                d = md[i]
                if d not in fwd_abs[h].index:
                    break
                codes = list(topn(d, n))
                r = fwd_abs[h].loc[d].reindex(codes).mean()
                if np.isnan(r):
                    break
                to = 1.0 if prev is None else 1 - len(set(prev) & set(codes)) / n
                net = r - to * COST * 2
                nav *= (1 + net)
                rets.append(net)
                turn.append(to)
                prev = codes
                i += h
            if len(rets) < 20:
                continue
            rs = pd.Series(rets)
            months = h * len(rs)
            anns.append(nav ** (12 / months) - 1)
            navs = (1 + rs).cumprod()
            mdds.append(float((navs / navs.cummax() - 1).min()))
            shs.append(rs.mean() / rs.std(ddof=1) * np.sqrt(12 / h))
            tos.append(np.mean(turn))
            costs.append(np.mean(turn) * COST * 2 * 12 / h)
        if anns:
            print(f"{n:>3} {h:>4}月 {np.mean(anns):>+8.2%} {np.mean(mdds):>8.2%} "
                  f"{np.mean(shs):>7.2f} {np.mean(tos):>6.1%} {np.mean(costs):>7.2%}")

print()
print("参考: 全宇宙等权(市场基准)月度路径")
bench = fwd_abs[1].mean(axis=1)
bnav = (1 + bench).cumprod()
print(f"  年化 {bnav.iloc[-1] ** (12/len(bench)) - 1:+.2%} | 回撤 "
      f"{float((bnav/bnav.cummax()-1).min()):.2%} | 月均 {bench.mean():+.2%}")
