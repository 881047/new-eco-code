"""池子基准诊断(2026-09-10 修正版): 策略吃的是谁的 beta?

第一版有三个错, 已全部修掉:
  ① 过滤只排了 8*/4*, 漏掉 9* —— 北交所(920xxx.BJ)的 pre_close 是 0.01 占位值,
     pct_chg 会飙到 15 万%(实测 2015-2026 共 8k 行), 把等权均值抬到年化 +39.9% 的假象。
     生产侧 board_of() 是 4/8/9 → bse, bse_enabled=false 时被排除, 未受污染。
  ② 日频再平衡的等权均值自带「再平衡红利」(≈0.5σ²/日, 小盘横截面 σ~5% → +20pp/年),
     不是被动持有能拿到的收益。改按**月度复利再平衡**(与策略同频)。
  ③ 全A序列起点 2013 vs 回测窗口 2017-02 不一致, 年化算错。
本脚本: 窗口对齐 2017-02-03→2026-08-31, 逐股按月复利(exp(sum(ln(1+r))), SQL 内精确),
月度等权再平衡合成「池子指数」, 与策略/中证800 同口径对比。只读, 不写库。
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

# 与生产选股口径一致: 剔科创(688)/北交(4,8,9); |pct_chg|<=21 挡脏数据(创业板 20cm 上限内)
FLT = ("ts_code NOT LIKE '688%' AND ts_code NOT LIKE '8%' AND ts_code NOT LIKE '4%' "
       "AND ts_code NOT LIKE '9%' AND amount > 0 AND pct_chg IS NOT NULL "
       "AND ABS(pct_chg) <= 21 AND trade_date >= '20170101' AND trade_date <= '20260831'")

mon = pd.read_sql("""
    SELECT ts_code, substr(trade_date,1,6) AS ym,
           exp(sum(ln(1 + pct_chg/100.0))) - 1 AS mret
    FROM daily WHERE {} GROUP BY ts_code, ym
""".format(FLT), conn)

tot = pd.read_sql("""
    SELECT ts_code, exp(sum(ln(1 + pct_chg/100.0))) - 1 AS cum
    FROM daily WHERE {} GROUP BY ts_code
""".format(FLT), conn)

idx = pd.read_sql("SELECT trade_date, close FROM index_daily "
                  "WHERE ts_code='000906.SH' ORDER BY trade_date", conn)
bt = pd.read_sql("SELECT trade_date, nav, sys_nav FROM backtest_nav "
                 "WHERE run_date=? ORDER BY trade_date", conn, params=(RUN,))
conn.close()

pool_m = mon.groupby("ym")["mret"].mean()          # 月度等权再平衡
pool = (1 + pool_m).cumprod()
n_mon = mon.groupby("ym")["mret"].size()

idx["ym"] = idx["trade_date"].str[:6]
b800 = idx.groupby("ym")["close"].last()
b800 = b800 / b800.iloc[0]
bt["ym"] = bt["trade_date"].str[:6]
bt = bt.set_index("ym")
nav = bt["nav"].astype(float) / float(bt["nav"].iloc[0])
sysn = bt["sys_nav"].astype(float) / float(bt["sys_nav"].iloc[0])

print("=" * 92)
print("窗口 {} → {}   池子 {} 只股/月均 {} 只   中证800 {} 期   回测 {} 期".format(
    pool.index[0], pool.index[-1], len(mon), int(n_mon.mean()),
    len(b800), len(bt)))
print("=" * 92)


def ann(s):
    yrs = (len(s) - 1) / 12.0
    return s.iloc[-1] ** (1 / yrs) - 1


def mdd(s):
    return float((s / s.cummax() - 1).min())


print("\n【1】同一个池子, 被动持有 vs 策略(月度, 2017-02→2026-08)")
print("    {:>16} {:>10} {:>12}".format("", "年化", "最大回撤(月频)"))
for nm, s in (("池子(等权全A)", pool), ("中证800", b800),
              ("满仓线", nav), ("系统线", sysn)):
    s = s.dropna()
    print("    {:>16} {:>+9.2%} {:>13.2%}".format(nm, ann(s), mdd(s)))

print("\n【2】个股层面(全窗口累计, 2017-02→2026-08): 池子里随便拿一只的命运")
tot["cum"] = tot["cum"].astype(float)
qs = tot["cum"].quantile([0.1, 0.25, 0.5, 0.75, 0.9])
print("    {:>22} {:>10} {:>10} {:>10} {:>10} {:>10}".format(
    "分位", "P10", "P25", "中位", "P75", "P90"))
print("    {:>22} {:>+9.1%} {:>+9.1%} {:>+9.1%} {:>+9.1%} {:>+9.1%}".format(
    "9.5年累计收益", *qs.values))
print("    亏损股占比 {:.1%}   翻倍股占比 {:.1%}   跌超一半 {:.1%}   (n={})".format(
    (tot["cum"] < 0).mean(), (tot["cum"] > 1).mean(), (tot["cum"] < -0.5).mean(), len(tot)))

print("\n【3】策略 vs 池子: 月度回归(超额=策略-beta×池子)")
for nm, s in (("满仓线", nav), ("系统线", sysn)):
    x = pd.concat([pool, s], axis=1).dropna()
    x.columns = ["p", "s"]
    x = x.iloc[1:]
    rp = x["p"].pct_change().dropna()
    rs = x["s"].pct_change().dropna()
    i = rp.index.intersection(rs.index)
    beta, alpha = np.polyfit(rp.loc[i], rs.loc[i], 1)
    resid = rs.loc[i] - (alpha + beta * rp.loc[i])
    r2 = 1 - resid.var() / rs.loc[i].var()
    print("    {:>6}: beta={:.2f}  R²={:.2f}  年化 alpha={:+.2%}  (n={} 月)".format(
        nm, beta, r2, (1 + alpha) ** 12 - 1, len(i)))

print("\n【4】2024-02-07 谷底: 择时看的中证800 当时在跌吗(日频, 2023-11-20→2024-02-07)")
# ⚠️ 2026-09-22 登记（结项审计）：下面紧跟的两条 `pd.read_sql(...) if False else None`
# 是**恒不执行的死查询**（`d` / `dd` 两个变量只被赋 None）。本目录是**证据、不得删除**
# （见 README 两条硬规矩）⇒ 就地登记，**不清理**：这两行记录了"当时想查什么"
# （先按 FLT 过滤、后改为逐条 NOT LIKE 排除），而真正生效的是再下面那条
# `dly = pd.read_sql(...)`。读到这里的结论时请以 `dly` 为准。
d = pd.read_sql("""
    SELECT trade_date, AVG(pct_chg) AS m FROM daily WHERE {} GROUP BY trade_date
""".format(FLT), conn) if False else None
dd = pd.read_sql("""SELECT trade_date, AVG(pct_chg)/100.0 AS m FROM daily
    WHERE ts_code NOT LIKE '688%' AND ts_code NOT LIKE '8%' AND ts_code NOT LIKE '4%'
      AND ts_code NOT LIKE '9%' AND amount>0 AND pct_chg IS NOT NULL AND ABS(pct_chg)<=21
      AND trade_date BETWEEN '20231120' AND '20240207' GROUP BY trade_date
    ORDER BY trade_date""", conn) if False else None
conn = sqlite3.connect(str(BASE / "data" / "neweco.db"))
dly = pd.read_sql("""SELECT trade_date, AVG(pct_chg)/100.0 AS m FROM daily
    WHERE ts_code NOT LIKE '688%' AND ts_code NOT LIKE '8%' AND ts_code NOT LIKE '4%'
      AND ts_code NOT LIKE '9%' AND amount>0 AND pct_chg IS NOT NULL AND ABS(pct_chg)<=21
      AND trade_date BETWEEN '20231120' AND '20240207' GROUP BY trade_date
    ORDER BY trade_date""", conn)
i2 = pd.read_sql("""SELECT trade_date, close FROM index_daily WHERE ts_code='000906.SH'
    AND trade_date BETWEEN '20231120' AND '20240207' ORDER BY trade_date""", conn)
b2 = pd.read_sql("""SELECT trade_date, sys_nav FROM backtest_nav_daily
    WHERE run_date=? AND trade_date BETWEEN '20231120' AND '20240207' ORDER BY trade_date""",
    conn, params=(RUN,))
conn.close()
pool_d = (1 + dly.set_index("trade_date")["m"].fillna(0)).cumprod()
print("    池子(日频累计, 含日频再平衡红利→高估其涨幅) {:+.2%}".format(pool_d.iloc[-1] - 1))
print("    中证800                                        {:+.2%}".format(
    i2["close"].iloc[-1] / i2["close"].iloc[0] - 1))
print("    系统线                                          {:+.2%}".format(
    b2["sys_nav"].iloc[-1] / b2["sys_nav"].iloc[0] - 1))
