"""ab_train_start_2019 结果分解: 按年 + 按训练窗口是否满 36 月分段

背景: 对照臂(仅科创板起训练)的 md_all[0] = 2019-07-31, 窗口 = md_all[i-36:i-1],
满 35 月窗口要到 i>=36(评分月 2022-07-31 起); 此前窗口只有 12~34 个月。
生产臂同期窗口始终满 35 月(含 2017-2019 样本)。→ 差异应集中在前段。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from app import db

RUNS = [("R  生产·全窗口", "20260909_ab3_ref2w_full"),
        ("Rm 生产·同窗口", "20260909_ab3_ref2w_match"),
        ("S  仅科创起", "20260909_ab3_star2w"),
        ("S18 起点2018-01", "20260909_ab3_ts20182w")]

conn = db.get_conn()
nav = {}
for label, run in RUNS:
    df = pd.read_sql("SELECT trade_date, nav, bench_nav FROM backtest_nav WHERE run_date=? "
                     "ORDER BY trade_date", conn, params=(run,))
    if df.empty:
        print(f"!! 缺 run_date={run}")
        continue
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    nav[label] = df.set_index("trade_date")
conn.close()


def seg_ret(s: pd.Series, a: str = None, b: str = None) -> float:
    if a:
        s = s[s.index >= a]
    if b:
        s = s[s.index <= b]
    return float(s.iloc[-1] / s.iloc[0] - 1) if len(s) > 1 else float("nan")


def yearly(s: pd.Series, y: int) -> float:
    seg = s[s.index.year == y]
    if not len(seg):
        return float("nan")
    prev = s[s.index < f"{y}-01-01"]
    base = prev.iloc[-1] if len(prev) else seg.iloc[0]
    return float(seg.iloc[-1] / base - 1)


years = sorted({d.year for df in nav.values() for d in df.index})

print("=" * 92)
print("分段累计收益(净值口径, 起点=段首月)")
print("=" * 92)
print(f"{'臂':>16} {'前段 2020-08→2022-06':>22} {'后段 2022-07→2026-08':>22} {'全段':>12}")
for label, df in nav.items():
    s = df["nav"]
    print(f"{label:>16} {seg_ret(s, '2020-08-01', '2022-06-30'):>21.2%} "
          f"{seg_ret(s, '2022-07-01', '2026-08-31'):>21.2%} "
          f"{seg_ret(s):>11.2%}")
bench = nav[RUNS[1][0]]["bench_nav"]
print(f"{'基准(全A)':>16} {seg_ret(bench, '2020-08-01', '2022-06-30'):>21.2%} "
      f"{seg_ret(bench, '2022-07-01', '2026-08-31'):>21.2%} {seg_ret(bench):>11.2%}")

print()
print("=" * 92)
print("逐年收益(净值口径; 2020 为 8-12 月部分年度)")
print("=" * 92)
print(f"{'臂':>16} " + " ".join(f"{y:>9}" for y in years))
for label, df in nav.items():
    print(f"{label:>16} " + " ".join(f"{yearly(df['nav'], y):>+9.2%}" for y in years))
print(f"{'基准(全A)':>16} " + " ".join(f"{yearly(bench, y):>+9.2%}" for y in years))
