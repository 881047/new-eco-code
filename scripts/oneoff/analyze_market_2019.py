"""A 股 2019-07-22(科创板开市)起全景解析 —— 为模型改造提供事实基础

用户指令: 「对 A 股 2019 年科创板上市以来的数据进行解析, 全面解析市场, 对模型进行深度改造」

为什么必须先做这一步: ⑪D 已证现行模型**没有选股 alpha, 只有「小盘+高波动」风格暴露**。
在不知道「这个市场里到底什么在赚钱、什么稳定」的情况下改造模型, 就只是换一组特征再看
回测曲线 —— 那正是本项目反复吃亏的方式(8 个排序变体、九轮参数)。故先用数据回答:

  1. 市场全景   —— 这 7 年 A 股经历了什么(指数/池子/波动/成交/估值/转折点)
  2. 因子有效性 —— 31 个因子里**什么真正有效**, IC/多空/稳定性,**以及分年度是否稳定**
  3. 风格周期   —— size 与 vol 的多空曲线(模型的钱是从哪来的、什么时候来)
  4. 模型归因   —— 模型 IC 0.0869 放在单因子旁边是什么位置; 是不是 size×vol 的高维代理

口径: 与生产一致 —— 21 日前瞻收益、行业中性标签、月末截面、科创板按其开关(默认关)。
凡与生产不同处, 报告中显式标注。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app import db
from app.logger import setup_logger
from app.models import trainer as T

setup_logger()
db.init_db()

START = "20190722"
HORIZON = T.HORIZON
OUT = {}

t0 = time.time()
print("[{:>4.0f}s] 载入市场数据...".format(time.time() - t0), flush=True)
m = T.load_market()
ac = m["close"] * m["adj"]
dates = pd.to_datetime(m["close"].index)

print("[{:>4.0f}s] 载入因子宽表(21.9M 行, 慢)...".format(time.time() - t0), flush=True)
wide = T._load_factors_wide()
FACTORS = sorted(wide.columns.get_level_values("factor").unique())

md = T.month_end_dates(dates, today=pd.Timestamp("2026-09-10").date())
md = md[(md >= pd.Timestamp(START))]
fut = T.forward_return(ac, HORIZON)
labels_raw = fut.loc[md]
conn = db.get_conn()
inds = T.industry_map(conn)
print("[{:>4.0f}s] 因子 {} 个, 月末截面 {} 期: {} → {}".format(
    time.time() - t0, len(FACTORS), len(md), md[0].date(), md[-1].date()), flush=True)

# ============ 1. 市场全景 ============
print("\n" + "=" * 100)
print("1. 市场全景 —— 2019-07-22 起 (基准窗口; 数据到 {} 为止)".format(md[-1].date()))
print("=" * 100)

idx = pd.read_sql("SELECT ts_code, trade_date, close FROM index_daily "
                  "WHERE ts_code IN ('000906.SH','000300.SH','000905.SH','000001.SH')", conn)
pool = pd.read_sql("SELECT trade_date, close, n FROM pool_index", conn)
db_basic = pd.read_sql("SELECT trade_date, total_mv, pe_ttm FROM daily_basic", conn)
conn.close()

for df in (idx, pool, db_basic):
    df["trade_date"] = pd.to_datetime(df["trade_date"])
idx = idx.pivot(index="trade_date", columns="ts_code", values="close")
pool = pool.set_index("trade_date")["close"]
start_ts = pd.Timestamp(START)

series = {"中证800": idx["000906.SH"], "中证500": idx["000905.SH"],
          "沪深300": idx["000300.SH"], "上证指数": idx["000001.SH"],
          "池子(等权全A)": pool}
series = {k: v[v.index >= start_ts].dropna() for k, v in series.items()}

print("{:<16} {:>10} {:>10} {:>10} {:>9} {:>9}".format(
    "指数", "累计", "年化", "最大回撤", "年化波动", "夏普(0%)"))
for k, s in series.items():
    r = s.pct_change().dropna()
    yrs = (s.index[-1] - s.index[0]).days / 365.25
    cum = s.iloc[-1] / s.iloc[0] - 1
    ann = (1 + cum) ** (1 / yrs) - 1
    dd = (s / s.cummax() - 1).min()
    vol = r.std() * np.sqrt(252)
    print("{:<16} {:>+9.1%} {:>+9.2%} {:>+9.2%} {:>8.1%} {:>9.2f}".format(
        k, cum, ann, dd, vol, ann / vol if vol else float("nan")))

print("\n分年度收益:")
yrs = sorted({d.year for d in series["池子(等权全A)"].index})
print("{:<8}".format("年份") + "".join("{:>14}".format(k) for k in series))
for y in yrs:
    row = "{:<8}".format(y)
    for k, s in series.items():
        yr = s[s.index.year == y]
        row += "{:>13.1%} ".format(yr.iloc[-1] / yr.iloc[0] - 1) if len(yr) > 1 else "{:>14}".format("-")
    print(row)

# 成交额与估值
amt = m["amount"].loc[m["amount"].index >= start_ts].sum(axis=1) / 1e8
mv = db_basic[db_basic["trade_date"] >= start_ts].groupby("trade_date")["total_mv"].sum() / 1e4
print("\n流动性/估值:")
print("  全市场日成交额: 均值 {:.0f} 亿   最低 {:.0f} 亿({})   最高 {:.0f} 亿({})".format(
    amt.mean(), amt.min(), amt.idxmin().date(), amt.max(), amt.idxmax().date()))
print("  全 A 总市值:    均值 {:.1f} 万亿  起 {:.1f}  末 {:.1f}".format(
    mv.mean() / 1e4, mv.iloc[0] / 1e4, mv.iloc[-1] / 1e4))
OUT["market"] = {k: float(v.iloc[-1] / v.iloc[0] - 1) for k, v in series.items()}

# ============ 2. 因子有效性 ============
print("\n" + "=" * 100)
print("2. 因子有效性 —— 月度截面, 标签=行业中性 21 日前瞻收益(与生产模型同口径), {} 期".format(len(md)))
print("=" * 100)

lab = labels_raw.copy()
lab = lab - lab.T.groupby(inds.reindex(lab.columns), sort=False).transform("mean").T
lab_r = lab.rank(axis=1)

rows = []
for f in FACTORS:
    try:
        x = wide.xs(f, level="factor", axis=1).reindex(index=md, columns=lab.columns)
    except KeyError:
        continue
    xr = x.rank(axis=1)
    ic = xr.corrwith(lab_r, axis=1)
    ic = ic.dropna()
    if len(ic) < 12:
        continue
    # 分位多空(用原始前瞻收益, 标准因子测试口径)
    q = x.rank(axis=1, pct=True)
    top = labels_raw.where(q >= 0.8).mean(axis=1)
    bot = labels_raw.where(q <= 0.2).mean(axis=1)
    ls = (top - bot).dropna()
    ann_ls = (1 + ls).prod() ** (12 / len(ls)) - 1
    rows.append({
        "factor": f, "ic": ic.mean(), "icir": ic.mean() / ic.std(ddof=1) if ic.std(ddof=1) else np.nan,
        "t": ic.mean() / ic.std(ddof=1) * np.sqrt(len(ic)) if ic.std(ddof=1) else np.nan,
        "win": (ic > 0).mean(), "ls_ann": ann_ls, "n": len(ic),
        "ic_1h": ic[ic.index < pd.Timestamp("2023-01-01")].mean(),
        "ic_2h": ic[ic.index >= pd.Timestamp("2023-01-01")].mean(),
    })
fac = pd.DataFrame(rows).sort_values("t", key=lambda s: s.abs(), ascending=False).reset_index(drop=True)

print("{:<20} {:>8} {:>7} {:>7} {:>7} {:>9} {:>9} {:>9}".format(
    "因子", "IC均值", "ICIR", "t值", "胜率", "多空年化", "前半段IC", "后半段IC"))
print("-" * 100)
for _, r in fac.iterrows():
    print("{:<20} {:>+8.4f} {:>7.2f} {:>+7.2f} {:>6.1%} {:>+9.2%} {:>+9.4f} {:>+9.4f}".format(
        r["factor"], r["ic"], r["icir"], r["t"], r["win"], r["ls_ann"], r["ic_1h"], r["ic_2h"]))
print("-" * 100)
sig = fac[fac["t"].abs() >= 2.0]
print("|t|>=2 的因子: {} / {}".format(len(sig), len(fac)))
stable = fac[(fac["t"].abs() >= 2.0) & (np.sign(fac["ic_1h"]) == np.sign(fac["ic_2h"]))]
print("|t|>=2 且**两半段同号**(跨 regime 稳定): {} 个 —— {}".format(
    len(stable), ", ".join(stable["factor"].tolist()) or "无"))

# ============ 3. 风格周期 ============
print("\n" + "=" * 100)
print("3. 风格周期 —— size 与 vol_20 的多空收益(模型的钱从哪来、什么时候来)")
print("=" * 100)
print("{:<8}".format("年份") + "".join("{:>12}".format(f) for f in
                                     ["size", "vol_20", "mom_20", "rev_5", "turnover_20", "illiq"]))
print("-" * 100)
STYLE = ["size", "vol_20", "mom_20", "rev_5", "turnover_20", "illiq"]
ls_by = {}
for f in STYLE:
    x = wide.xs(f, level="factor", axis=1).reindex(index=md, columns=labels_raw.columns)
    q = x.rank(axis=1, pct=True)
    ls_by[f] = (labels_raw.where(q >= 0.8).mean(axis=1)
                - labels_raw.where(q <= 0.2).mean(axis=1)).dropna()
for y in yrs:
    row = "{:<8}".format(y)
    for f in STYLE:
        s = ls_by[f]
        s = s[s.index.year == y]
        row += "{:>11.1%} ".format((1 + s).prod() - 1) if len(s) else "{:>12}".format("-")
    print(row)
print("-" * 100)
for f in STYLE:
    s = ls_by[f]
    print("  {:<12} 全期多空年化 {:>+8.2%}   波动 {:>6.1%}   夏普 {:>5.2f}".format(
        f, (1 + s).prod() ** (12 / len(s)) - 1, s.std() * np.sqrt(12),
        ((1 + s).prod() ** (12 / len(s)) - 1) / (s.std() * np.sqrt(12))))

fac.to_csv(Path(__file__).resolve().parents[2] / "data" / "factor_ic_2019.csv",
           index=False, encoding="utf-8-sig")
print("\n[因子表已存 data/factor_ic_2019.csv]  总耗时 {:.0f}s".format(time.time() - t0))
