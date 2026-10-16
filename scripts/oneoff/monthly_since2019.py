"""回测窗口缩到科创板上市(2019-07-22)之后, 导出逐月数据(2026-09-09, 用户要求)

跑一次生产口径回测(--start 20190722), 顺带插桩记录每月实际持仓, 输出:
  - data/backtest_monthly_2019plus.csv (utf-8-sig, Excel 可直接打开)
  - 控制台: 逐月表 + 窗口汇总 + 分年汇总

口径: 生产 model_score(4 种子集成)、6/5/4 状态自适应只数、ST/退 PIT 过滤、
      科创板排除(star_enabled=false)、成本 0.3%+滑点 0.1%、1 万本金整手建模。
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app import config as cfg_mod
from app import db
from app.logger import setup_logger
from app.backtest import engine as E

TAG = "since2019"
START = "20190722"
CSV = Path("data/backtest_monthly_2019plus.csv")

setup_logger()
db.init_db()

# ---- 插桩: 记录每月实际持仓(allocate_lots 每期调用两次: 满仓线 + 系统线) ----
rec: list = []
_orig_alloc = E.allocate_lots


def _alloc(*a, **k):
    out = _orig_alloc(*a, **k)
    rec.append(list(out["shares"].keys()))
    return out


E.allocate_lots = _alloc
metrics = E.run_backtest(score_table="model_score", run_tag=TAG, start=START)
E.allocate_lots = _orig_alloc

# ---- 复刻引擎的评分日序列(同口径), 用于把 nav 行对回评分日/状态 ----
cfg = cfg_mod.load_config()
conn = db.get_conn()
sc = pd.read_sql("SELECT ts_code, trade_date FROM model_score", conn)
if not cfg.get("star_enabled", False):
    sc = sc[~sc["ts_code"].str.startswith("688")]
me = set(pd.read_sql("SELECT MAX(cal_date) AS d FROM trade_cal WHERE is_open=1 "
                     "GROUP BY substr(cal_date,1,6)", conn)["d"])
sd = [d for d in sorted(sc["trade_date"].unique()) if d in me and d >= START]
regime = pd.read_sql("SELECT trade_date, state FROM regime", conn).set_index("trade_date")["state"]
nm = pd.read_sql("SELECT ts_code, name FROM stock_basic", conn).set_index("ts_code")["name"].to_dict()
nav = pd.read_sql("SELECT trade_date, nav, sys_nav, bench_nav FROM backtest_nav "
                  "WHERE run_date=? ORDER BY trade_date", conn, params=("20260909_" + TAG,))
conn.close()

nav = nav.set_index("trade_date")
n = len(nav)
assert n == len(sd) - 1, f"nav 行数 {n} != 评分期数 {len(sd)-1}"
assert len(rec) == 2 * n, f"插桩调用 {len(rec)} != 2×{n}"

rm = nav["nav"].pct_change()
rm.iloc[0] = nav["nav"].iloc[0] - 1.0
rs = nav["sys_nav"].pct_change()
rs.iloc[0] = nav["sys_nav"].iloc[0] - 1.0
rb = nav["bench_nav"].pct_change()
rb.iloc[0] = nav["bench_nav"].iloc[0] - 1.0

stocks = cfg.get("state_stocks", {"attack": 6, "neutral": 5, "defense": 4})
rows = []
for i, settle in enumerate(nav.index):
    picks = rec[2 * i]
    st = str(regime.get(sd[i], "neutral"))
    rows.append({
        "评分日": sd[i],
        "结算日": settle,
        "市场状态": st,
        "目标只数": int(stocks.get(st, 5)),
        "实际持仓": len(picks),
        "净值": round(float(nav["nav"].iloc[i]), 4),
        "月收益": float(rm.iloc[i]),
        "系统线净值": round(float(nav["sys_nav"].iloc[i]), 4),
        "系统线月收益": float(rs.iloc[i]),
        "基准净值": round(float(nav["bench_nav"].iloc[i]), 4),
        "基准月收益": float(rb.iloc[i]),
        "超额": float(rm.iloc[i] - rb.iloc[i]),
        "持仓": "/".join(f"{c}({nm.get(c, '')})" for c in picks),
    })
df = pd.DataFrame(rows)
CSV.parent.mkdir(parents=True, exist_ok=True)
df.to_csv(CSV, index=False, encoding="utf-8-sig")

print("=" * 118)
print(f"回测窗口 {sd[0]} ~ {sd[-1]} | 共 {n} 个月 | run_date=20260909_{TAG}")
print("=" * 118)
hdr = f"{'评分日':>8} {'结算日':>8} {'状态':>7} {'目标':>4} {'实际':>4} {'净值':>8} {'月收益':>9} {'系统线':>9} {'基准':>9} {'超额':>9}"
print(hdr)
for _, r in df.iterrows():
    print(f"{r['评分日']:>8} {r['结算日']:>8} {r['市场状态']:>7} {r['目标只数']:>4} {r['实际持仓']:>4} "
          f"{r['净值']:>8.4f} {r['月收益']:>+9.2%} {r['系统线月收益']:>+9.2%} {r['基准月收益']:>+9.2%} {r['超额']:>+9.2%}")

print()
print("窗口汇总(生产口径, 满仓线):")
tot = float(nav["nav"].iloc[-1]) - 1
ann = (1 + tot) ** (12 / n) - 1
mu, sdd = rm.mean(), rm.std(ddof=1)
print(f"  累计 {tot:+.2%} | 年化 {ann:+.2%} | 月均 {mu:+.3%} | 月 sd {sdd:.3%} | "
      f"t(对0) {mu/(sdd/math.sqrt(n)):+.2f}")
print(f"  月胜率 {(rm > 0).mean():.1%} | 最好 {rm.max():+.2%} | 最差 {rm.min():+.2%} | "
      f"基准年化 {((nav['bench_nav'].iloc[-1])**(12/n)-1):+.2%} | 超额年化 {ann - ((nav['bench_nav'].iloc[-1])**(12/n)-1):+.2%}")
print(f"  系统线: 年化 {((nav['sys_nav'].iloc[-1])**(12/n)-1):+.2%} | "
      f"月 sd {rs.std(ddof=1):.3%} | 胜率 {(rs > 0).mean():.1%}")
dd = (nav["nav"] / nav["nav"].cummax() - 1).min()
print(f"  最大回撤(月频) {dd:.2%} | 日频 {metrics['mdd_daily']:.2%} | 夏普 {metrics['sharpe']:.3f} | "
      f"IC {metrics['ic_mean']:.4f} / ICIR {metrics['icir']:.3f} | 平均持仓 {metrics['avg_stocks']:.2f} 只 | "
      f"闲置 {metrics['avg_cash_idle']:.1%} | 换手 {metrics['avg_turnover']:.1%}")
print()
print("分年:")
idx = pd.to_datetime(df["结算日"]).dt.year
for y in sorted(set(idx)):
    m = (idx == y).values
    g, gs, gb = rm[m], rs[m], rb[m]
    print(f"  {y}: 策略 {(1+g).prod()-1:+7.2%} | 系统线 {(1+gs).prod()-1:+7.2%} | "
          f"基准 {(1+gb).prod()-1:+7.2%} | 超额 {(1+g).prod()-(1+gb).prod():+7.2%} | "
          f"胜月 {int((g>0).sum())}/{len(g)} | 最差 {g.min():+6.2%}")
print()
print(f"CSV 已写入: {CSV.resolve()}")
