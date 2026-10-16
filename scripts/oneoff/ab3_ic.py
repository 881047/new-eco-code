"""ab_train_start_2019 的模型质量分解: 月度 rank IC(截面 Spearman), 按段对比

引擎口径的 ic_mean 是组合/宇宙层面的; 这里直接算「评分 vs 未来 21 日行业相对收益」
的截面秩相关, 把模型信号质量与建仓/整手/择时噪声分开。

关键事实: 两臂的滚动窗口在评分月 >= 2022-07-31 时**完全同源**(都是 2019-07..前月),
所以 2022-07 之后两模型逐位相同 —— 差异必然只落在 2020-08..2022-06。


# STATUS: BROKEN(2026-09-12 独立审计 #17)—— 读 model_score_star(不存在), 真实表名是
# `model_score_star19`。要复用得先改表名并重跑判据(那属于新一轮实验, 不是整理)。"""
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

SPLIT = pd.Timestamp("2022-07-31")

m = load_market(since="2016-01-01")
ac = m["close"] * m["adj"]
dates = pd.to_datetime(m["close"].index)
md_all = month_end_dates(dates)

conn = db.get_conn()
inds = industry_map(conn)
sc = {}
for t in ("model_score", "model_score_star"):
    d = pd.read_sql(f"SELECT ts_code, trade_date, score FROM {t}", conn)
    d["trade_date"] = pd.to_datetime(d["trade_date"])
    sc[t] = d.pivot_table(index="trade_date", columns="ts_code", values="score").sort_index()
conn.close()

fut = forward_return(ac, HORIZON)
labels = fut.loc[md_all]
labels = labels - labels.T.groupby(inds.reindex(labels.columns), sort=False).transform("mean").T

md = md_all[(md_all >= "2020-08-31") & (md_all <= "2026-08-31")]
md = md[md.isin(sc["model_score_star"].index)]
print(f"评分月 {len(md)} 个: {md[0].date()} → {md[-1].date()}")

rows = []
for d in md:
    lab = labels.loc[d]
    rec = {"date": d}
    for t, w in sc.items():
        s = w.loc[d].dropna() if d in w.index else pd.Series(dtype=float)
        common = s.index.intersection(lab.dropna().index)
        rec[t] = (s.reindex(common).corr(lab.reindex(common), method="spearman")
                  if len(common) >= 100 else np.nan)
        rec[t + "_n"] = len(common)
    rows.append(rec)

r = pd.DataFrame(rows).set_index("date")
print(f"两臂评分逐位相同的月数: {int((r['model_score'].round(9) == r['model_score_star'].round(9)).sum())} / {len(r)}")
print()
print("=" * 88)
print("月度 rank IC(截面 Spearman, 行业相对收益标签)")
print("=" * 88)
print(f"{'段':>24} {'月数':>5} {'生产模型':>12} {'仅科创起':>12} {'差':>10} {'t(配对差)':>10}")
for name, sub in (("前段 2020-08→2022-06", r[r.index < SPLIT]),
                  ("后段 2022-07→2026-08", r[r.index >= SPLIT]),
                  ("全段", r)):
    a, b = sub["model_score"], sub["model_score_star"]
    diff = (a - b).dropna()
    t = diff.mean() / (diff.std(ddof=1) / np.sqrt(len(diff))) if len(diff) > 2 else np.nan
    print(f"{name:>24} {len(sub):>5} {a.mean():>+12.4f} {b.mean():>+12.4f} "
          f"{diff.mean():>+10.4f} {t:>10.2f}")
print()
print("逐年:")
for y in sorted({d.year for d in r.index}):
    sub = r[r.index.year == y]
    print(f"  {y}: n={len(sub):>2}  生产 {sub['model_score'].mean():>+.4f}  "
          f"仅科创 {sub['model_score_star'].mean():>+.4f}")
