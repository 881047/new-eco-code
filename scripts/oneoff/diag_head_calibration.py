"""头部校准诊断 + 免重训廉价候选(2026-09-09 深夜, 用户"继续挖改进方向")

动机: 生产 IC 0.0859 / ICIR 0.735 是**全市场平均排序质量**, 但组合只买 3~6 只头部
      (avg_stocks 3.26)。若头部按分数的实现超额并不优于中段, 说明头部噪声大 ——
      该优先改标签/目标函数, 而不是继续堆因子。本脚本不重训, 纯评估现有生产推分。

口径: 标签 = 未来 21 交易日复权收益 - 行业均值(与 trainer 完全同口径, 含 ③ 退市/停牌
      最后有效价回退); 评分宇宙 = model_score 实际行(生产已做 ST/退 PIT、科创/北交过滤)。

输出:
  D1 十分位单调性(月均超额 / 中位 / 分年)
  D2 头部 top3 / top6 / top12 / 全体 的实现超额(等权, 含 t 值)
  D3 头部集中度: top-n 剔除最好一只后的超额(彩票依赖度)
  D4 廉价候选(免重训): 2 期 / 3 期分数均值(信号平滑)的头部超额对照
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app import db
from app.logger import setup_logger
from app.factors.factor_engine import load_market, month_end_dates, industry_map
from app.models.trainer import forward_return, HORIZON

setup_logger()
db.init_db()

# ---------- 数据 ----------
m = load_market(since="2016-01-01")
ac = m["close"] * m["adj"]
dates = pd.to_datetime(m["close"].index)
md_all = month_end_dates(dates)

conn = db.get_conn()
industry = industry_map(conn)
sc = pd.read_sql("SELECT ts_code, trade_date, score FROM model_score", conn)
conn.close()
sc["trade_date"] = pd.to_datetime(sc["trade_date"])
score_wide = sc.pivot_table(index="trade_date", columns="ts_code", values="score").sort_index()

md = md_all[md_all.isin(score_wide.index)]
fut = forward_return(ac, HORIZON)
labels = fut.loc[md]
inds = industry.reindex(labels.columns)
labels = labels - labels.T.groupby(inds, sort=False).transform("mean").T

print(f"评分月 {len(md)} 个: {md[0].date()} ~ {md[-1].date()} | 生产表 model_score")


def _stats(x: pd.Series) -> dict:
    x = x.dropna()
    if len(x) == 0:
        return {"n": 0, "mean": np.nan, "med": np.nan, "t": np.nan}
    return {"n": len(x), "mean": x.mean(), "med": x.median(),
            "t": x.mean() / (x.std(ddof=1) / np.sqrt(len(x))) if len(x) > 1 and x.std(ddof=1) > 0 else np.nan}


# ---------- 逐月分桶 ----------
rows = []
for d in md:
    s = score_wide.loc[d].dropna()
    lab = labels.loc[d].reindex(s.index)
    ok = lab.notna()
    s, lab = s[ok], lab[ok]
    if len(s) < 50:
        continue
    order = s.sort_values(ascending=False)
    n = len(order)
    rank_pct = np.arange(n) / n           # 0=最高分
    lab_o = lab.reindex(order.index)
    rec = {"date": d, "n": n}
    # 十分位(1=最高分)
    for k in range(10):
        sel = (rank_pct >= k / 10) & (rank_pct < (k + 1) / 10)
        rec[f"d{k+1}"] = lab_o[sel].mean()
        rec[f"d{k+1}_med"] = lab_o[sel].median()
    # 头部桶
    for k in (3, 6, 12, 20):
        top = lab_o.iloc[:k]
        rec[f"top{k}"] = top.mean()
        rec[f"top{k}_med"] = top.median()
        rec[f"top{k}_exbest"] = top.iloc[1:].mean() if k > 1 else np.nan  # 剔除最好一只
    rec["all"] = lab_o.mean()
    rows.append(rec)

df = pd.DataFrame(rows).set_index("date")
print(f"有效评分月 {len(df)} 个 (宇宙规模中位 {int(df['n'].median())} 只)\n")

# ---------- D1 十分位单调性 ----------
print("=" * 96)
print("D1 十分位(1=最高分)实现超额 —— 月均 / 中位 / t值")
print("=" * 96)
print(f"{'桶':>5} {'月均超额':>10} {'中位':>10} {'t':>7}   {'桶':>5} {'月均超额':>10} {'中位':>10} {'t':>7}")
for k in range(10):
    st = _stats(df[f"d{k+1}"])
    stm = _stats(df[f"d{k+1}_med"])
    print(f"{k+1:>5} {st['mean']:>+9.2%} {stm['mean']:>+9.2%} {st['t']:>+7.2f}", end="")
    if k % 2 == 1:
        print()
    else:
        print("   ", end="")
print()
sp = df["d1"] - df["d10"]
print(f"多空(1-10) 月均 {sp.mean():+.2%} | t {_stats(sp)['t']:+.2f}")

# ---------- D2 头部 ----------
print()
print("=" * 96)
print("D2 头部实现超额(等权月均) vs 全体")
print("=" * 96)
for k in (3, 6, 12, 20):
    st = _stats(df[f"top{k}"])
    stm = _stats(df[f"top{k}_med"])
    exb = _stats(df[f"top{k}_exbest"])
    print(f"  top{k:<3} 月均 {st['mean']:>+7.2%} (t {st['t']:>+5.2f}) | 中位 {stm['mean']:>+7.2%} | "
          f"剔除最好一只后 {exb['mean']:>+7.2%}")
st = _stats(df["all"])
print(f"  全体   月均 {st['mean']:>+7.2%} (t {st['t']:>+5.2f})")
print(f"  头部/全体比: top3 {_stats(df['top3'])['mean'] / st['mean']:.2f}x | "
      f"top6 {_stats(df['top6'])['mean'] / st['mean']:.2f}x | top12 {_stats(df['top12'])['mean'] / st['mean']:.2f}x")

# ---------- D2b 分年 ----------
print()
print("=" * 96)
print("D2b 分年: top3 / top6 / 全体 月均超额(等权)")
print("=" * 96)
for y, g in df.groupby(df.index.year):
    print(f"  {y} ({len(g):>2}月): top3 {g['top3'].mean():>+7.2%} | top6 {g['top6'].mean():>+7.2%} | "
          f"全体 {g['all'].mean():>+7.2%} | d1-d10 {(g['d1'] - g['d10']).mean():>+7.2%}")

# ---------- D3 头部命中率 ----------
print()
print("=" * 96)
print("D3 头部命中率与集中度")
print("=" * 96)
for k in (3, 6):
    win = (df[f"top{k}"] > 0).mean()
    best_share = (df[f"top{k}"] - df[f"top{k}_exbest"] * (k - 1) / k) / df[f"top{k}"].replace(0, np.nan)
    print(f"  top{k}: 跑赢行业均值月占比 {win:.1%} | 组合超额中最好一只贡献中位 {best_share.median():.1%}")

# ---------- D4 分数平滑(免重训) ----------
print()
print("=" * 96)
print("D4 廉价候选: N 期分数均值(信号平滑) —— 同一宇宙同一标签, 只换排序依据")
print("=" * 96)
sm = {}
for w in (2, 3):
    sm[w] = score_wide.rolling(w, min_periods=1).mean()

for w in (2, 3):
    res = []
    for d in md:
        s = sm[w].loc[d].dropna()
        lab = labels.loc[d].reindex(s.index)
        ok = lab.notna()
        s, lab = s[ok], lab[ok]
        if len(s) < 50:
            continue
        order = s.sort_values(ascending=False)
        lab_o = lab.reindex(order.index)
        res.append({f"top{k}": lab_o.iloc[:k].mean() for k in (3, 6, 12)} | {"all": lab_o.mean()})
    r = pd.DataFrame(res)
    line = f"  {w} 期均值: "
    for k in (3, 6, 12):
        st = _stats(r[f"top{k}"])
        line += f"top{k} {st['mean']:>+7.2%}(t{st['t']:>+5.2f}) "
    print(line + f"| 全体 {_stats(r['all'])['mean']:>+7.2%}")

print()
print("对照(原始 1 期):")
for k in (3, 6, 12):
    st = _stats(df[f"top{k}"])
    print(f"  top{k:<3} {st['mean']:>+7.2%} (t {st['t']:>+5.2f})", end="")
print()

# ---------- D5 头部累计净值(等权, 未计费) ----------
print()
print("=" * 96)
print("D5 头部等权累计(未计费, 仅看排序质量): 净值 / 年化 / 月 sd / 最大回撤")
print("=" * 96)
for k in (3, 6, 12):
    r = df[f"top{k}"]
    nav = (1 + r).cumprod()
    ann = nav.iloc[-1] ** (12 / len(r)) - 1
    dd = (nav / nav.cummax() - 1).min()
    print(f"  top{k:<3} 净值 {nav.iloc[-1]:.3f} | 年化 {ann:+.2%} | 月sd {r.std(ddof=1):.2%} | 回撤 {dd:.2%} | 胜率 {(r > 0).mean():.1%}")
