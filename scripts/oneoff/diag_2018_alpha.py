"""2018 负 alpha 归因(2026-09-10 深夜): 是"选股失效"还是"风格失效"?

**为什么问这个**: 因子画像显示 2018 年入选票的标准化暴露**几乎全中性**(size -0.11,
vol_20 -0.31, lhb_cnt20 -0.27), 即模型那年选出的票与全市场没有区分度, **却仍亏了
24% alpha**(分窗口回归: 2018 内 alpha -24.46%/年 vs 全样本 +7.10%)。这直接决定了
"改造模型"有没有靶子:

  - 若入选票**跑输同市值档位的随机股** → 选股本身在那年失效, 是模型/特征的问题,
    改造模型有靶子(但要知道失效的机制)。
  - 若入选票**与同档位随机股打平** → 那年的负 alpha 是**风格**造成的, 中性 z 只是
    假象(市值档位内仍有系统性差异, 或 z 是全市场标准化而候选域是窄域)。此时"改造
    模型去掉风格"也没有靶子, 因为亏损根本不是选股带来的。

**方法**: 对每个评分日的每只入选票, 在**同一天的候选域内**取同市值档位的股票作对照
组(相对市值 ±30%), 比较下期收益。三段对照:
  pick        入选票本身
  size_ctrl   同市值档位对照组均值(核心对照)
  all_ctrl    候选域全体均值(参照)

收益口径: 评分日收盘(后复权) → 下一评分日收盘, 与回测的月度持有期同构。
只读分析, 不跑回测, 不写库。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app import db
from app.models.names import NameLookup

MV_BAND = 0.30          # 同市值档: 相对总市值 ±30%
YEARS = ["2017", "2018", "2019", "2020", "2021", "2022", "2023", "2024", "2025", "2026"]

nl = NameLookup()
conn = db.get_conn()

sc = pd.read_sql("SELECT ts_code, trade_date, score FROM model_score "
                 "WHERE trade_date >= '20170101' ORDER BY trade_date", conn)
sc["trade_date"] = sc["trade_date"].astype(str)
dates = sorted(sc["trade_date"].unique())
nxt = {d: dates[i + 1] for i, d in enumerate(dates[:-1])}
need = sorted(set(dates) | set(nxt.values()))

qmarks = ",".join("?" * len(need))
daily = pd.read_sql(
    "SELECT ts_code, trade_date, close FROM daily WHERE trade_date IN ({})".format(qmarks),
    conn, params=need)
adj = pd.read_sql(
    "SELECT ts_code, trade_date, adj_factor FROM adj_factor WHERE trade_date IN ({})".format(qmarks),
    conn, params=need)
basic = pd.read_sql(
    "SELECT ts_code, trade_date, total_mv, industry FROM ("
    "  SELECT b.ts_code, b.trade_date, b.total_mv, s.industry "
    "  FROM daily_basic b LEFT JOIN stock_basic s ON s.ts_code = b.ts_code"
    "  WHERE b.trade_date IN ({}) )".format(qmarks), conn, params=need)
conn.close()

for d in (daily, adj, basic):
    d["trade_date"] = d["trade_date"].astype(str)

px = daily.merge(adj, on=["ts_code", "trade_date"], how="left")
px["adj_factor"] = px["adj_factor"].fillna(1.0)
px["padj"] = px["close"] * px["adj_factor"]
# (date, code) -> padj
pv = px.set_index(["trade_date", "ts_code"])["padj"]
mv = basic.set_index(["trade_date", "ts_code"])["total_mv"]
ind = basic.set_index(["trade_date", "ts_code"])["industry"]

print("评分日 {} 个  {:s} ~ {:s}".format(len(dates), dates[0], dates[-1]))

# 候选域: 每个评分日 daily_basic 里存在的代码
uni = {d: basic[basic["trade_date"] == d]["ts_code"].tolist() for d in dates}

rows = []
for d in dates:
    if d not in nxt:
        continue
    d2 = nxt[d]
    codes = uni.get(d, [])
    if not codes:
        continue
    g = sc[sc["trade_date"] == d]
    g = g[g["ts_code"].isin(codes)]
    g = g[~g["ts_code"].map(lambda c: nl.name_at(c, d)).str.contains("ST|退", na=False)]
    if g.empty:
        continue
    # 候选域下期收益 / 当期市值(都按 code 对齐成 Series, 后面全向量化)
    p0 = pv.loc[d].reindex(codes)
    p1 = pv.loc[d2].reindex(codes)
    ok = p0.notna() & p1.notna() & (p0 > 0)
    fwd = (p1[ok] / p0[ok] - 1)
    if len(fwd) < 50:
        continue
    mvs = mv.loc[d].reindex(fwd.index)
    fwd = fwd[mvs.notna() & (mvs > 0)]
    mvs = mvs[mvs.notna() & (mvs > 0)]
    if len(fwd) < 50:
        continue
    all_ctrl = float(fwd.mean())

    for r in g.nlargest(6, "score").itertuples():
        c = r.ts_code
        if c not in fwd.index:
            continue
        m0 = mvs[c]
        band = mvs[(mvs / m0 - 1).abs() <= MV_BAND].index
        if len(band) < 20:
            continue
        size_ctrl = float(fwd[band].mean())
        rows.append({"y": d[:4], "date": d, "code": c, "score": float(r.score),
                     "pick": float(fwd[c]), "size_ctrl": size_ctrl, "all_ctrl": all_ctrl,
                     "n_band": len(band),
                     "industry": ind.loc[(d, c)] if (d, c) in ind.index else None})

R = pd.DataFrame(rows)
R["vs_size"] = R["pick"] - R["size_ctrl"]
R["vs_all"] = R["pick"] - R["all_ctrl"]
print("样本 {} 个 (票,月) 组合".format(len(R)))

print()
print("=== 按年: 入选票 vs 同市值档位对照组 (下期收益, 等权均值) ===")
print("{:>6s} {:>6s} {:>9s} {:>9s} {:>9s} {:>10s} {:>10s} {:>8s}".format(
    "年", "样本", "入选票", "同档位", "候选域", "vs同档位", "vs候选域", "档位N"))
for y in YEARS:
    g = R[R["y"] == y]
    if g.empty:
        continue
    print("{:>6s} {:>6d} {:>9.2%} {:>9.2%} {:>9.2%} {:>10.2%} {:>10.2%} {:>8.0f}".format(
        y, len(g), g["pick"].mean(), g["size_ctrl"].mean(), g["all_ctrl"].mean(),
        g["vs_size"].mean(), g["vs_all"].mean(), g["n_band"].mean()))

print()
print("=== 汇总(按 (票,月) 等权) ===")
for lab, g in [("全样本", R), ("2018", R[R["y"] == "2018"]),
               ("2018 外", R[R["y"] != "2018"]),
               ("2024-2025", R[R["y"].isin(["2024", "2025"])])]:
    if g.empty:
        continue
    se = g["vs_size"].std() / np.sqrt(len(g))
    print("  {:10s} n={:4d}  vs同档位 月均 {:+.2%}/月  (t={:5.2f})   vs候选域 {:+.2%}/月"
          .format(lab, len(g), g["vs_size"].mean(), g["vs_size"].mean() / se if se else np.nan,
                  g["vs_all"].mean()))

print()
print("=== 2018 逐月 ===")
g = R[R["y"] == "2018"].sort_values("date")
for r in g.groupby("date").agg(pick=("pick", "mean"), size=("size_ctrl", "mean"),
                               d=("vs_size", "mean"), n=("code", "size")).itertuples():
    print("  {}  入选 {:>7.2%}   同档位 {:>7.2%}   差 {:>7.2%}   ({} 只)".format(
        r.Index, r.pick, r.size, r.d, r.n))

# 按月聚合再统计 —— 同月入选票高度相关(同风格同行情), 股票级 t 会高估显著性。
# 先把同月各票求均值降成一个观测, 再做单样本 t。这是本诊断唯一可信的显著性口径。
print()
print("=== 按月聚合(降相关后) ===")
M = R.groupby(["date", "y"], as_index=False)[["vs_size", "vs_all"]].mean()
for lab, g in [("全样本", M), ("2018", M[M["y"] == "2018"]),
               ("2018 外", M[M["y"] != "2018"]),
               ("2024-2025", M[M["y"].isin(["2024", "2025"])]),
               ("2021", M[M["y"] == "2021"])]:
    if len(g) < 3:
        continue
    for col in ("vs_size", "vs_all"):
        v = g[col]
        se = v.std(ddof=1) / np.sqrt(len(v))
        pos = int((v > 0).sum())
        print("  {:10s} {:9s}  n={:3d} 月均 {:>+7.2%}  t={:>5.2f}  正收益月 {:2d}/{:2d}"
              .format(lab, col, len(v), v.mean(), v.mean() / se if se else np.nan,
                      pos, len(v)))

R.to_csv("data/diag_2018_alpha.csv", index=False, encoding="utf-8-sig")
print("\n明细已存 data/diag_2018_alpha.csv ({} 行)".format(len(R)))
