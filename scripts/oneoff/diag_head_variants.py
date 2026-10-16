"""头部去噪: 排序变体筛选(2026-09-09 深夜, 免重训)

背景(diag_head_calibration.py): 十分位单调(d1 +0.52%/月 → d10 -1.67%/月, 多空 t=4.99),
但头部次序噪声大(top3 t=0.49 / 月胜率 47.8% / 最好一只贡献 38% 超额), 且等权持有
只数越多毛收益越好(top12 +9.54%/年 vs top3 +0.42%)。→ 头部选股质量是瓶颈。

本脚本在**同一宇宙、同一标签、同一 115 个月**上只换"排序依据", 筛选候选变体:
  V0 原始 score(对照)
  V1 score - λ·vol20 分位        (低波动倾斜; λ=0.25/0.5)
  V2 score - λ·价格分位          (低价倾斜; 1 万本金实测只有低价股买得起, 验证是否巧合)
  V3 score 过滤 20 日动量为负    (趋势过滤: 只买不在下跌趋势里的)
  V4 score - λ·换手率分位        (低换手倾斜)

**判据(先声明, 防事后挑数据)**: 变体必须同时满足
  ① top6 与 top12 的月均超额 > 对照, 且
  ② 前后两段(2017-2021 / 2022-2026)方向一致(两段都不低于对照 -0.10pp)
才进入引擎 A/B(scripts/run_backtest.py --tag) + 滚动样本外验证。否则弃用并记录。
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

# ---- 辅助面板(20 日波动率 / 价格 / 20 日动量 / 换手率) ----
ret = m["close"].pct_change()
vol20 = ret.rolling(20, min_periods=10).std()
px = m["close"]
mom20 = m["close"] / m["close"].shift(20) - 1
turn = m.get("turnover")   # load_market 的键名是 turnover(非 turnover_rate)
print(f"评分月 {len(md)} 个 | 宇宙中位 {int(score_wide.notna().sum(axis=1).median())} 只")


def _pct_rank(s: pd.Series) -> pd.Series:
    """截面分位 0~1(1=最大)"""
    return s.rank(pct=True, na_option="keep")


def evaluate(name, ranker, filt=None):
    """ranker(df)->排序用分数(降序取头部); filt(df)->bool 掩码(可选)"""
    per = []
    for d in md:
        s = score_wide.loc[d].dropna()
        lab = labels.loc[d].reindex(s.index)
        df = pd.DataFrame({"score": s, "label": lab})
        df["vol20"] = vol20.loc[d].reindex(s.index) if d in vol20.index else np.nan
        df["px"] = px.loc[d].reindex(s.index) if d in px.index else np.nan
        df["mom20"] = mom20.loc[d].reindex(s.index) if d in mom20.index else np.nan
        if turn is not None and d in turn.index:
            df["turn"] = turn.loc[d].reindex(s.index)
        df = df[df["label"].notna()]
        if filt is not None:
            df = df[filt(df)]
        if len(df) < 50:
            continue
        df["r"] = ranker(df)
        df = df.dropna(subset=["r"])
        o = df.sort_values("r", ascending=False)
        rec = {"date": d}
        for k in (3, 6, 12):
            rec[f"top{k}"] = o["label"].iloc[:k].mean()
        rec["all"] = o["label"].mean()
        per.append(rec)
    r = pd.DataFrame(per).set_index("date")
    half1 = r[r.index < "2022-01-01"]
    half2 = r[r.index >= "2022-01-01"]
    out = {"name": name, "n": len(r)}
    for k in (3, 6, 12):
        x = r[f"top{k}"]
        out[f"top{k}"] = x.mean()
        out[f"top{k}_t"] = x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))
        out[f"top{k}_h1"] = half1[f"top{k}"].mean()
        out[f"top{k}_h2"] = half2[f"top{k}"].mean()
    out["all"] = r["all"].mean()
    return out


VARIANTS = [
    ("V0 原始", lambda d: d["score"], None),
    ("V1a 低波动 .25", lambda d: d["score"] - 0.25 * _pct_rank(d["vol20"]) * 100, None),
    ("V1b 低波动 .50", lambda d: d["score"] - 0.50 * _pct_rank(d["vol20"]) * 100, None),
    ("V2a 低价 .25", lambda d: d["score"] - 0.25 * _pct_rank(d["px"]) * 100, None),
    ("V2b 低价 .50", lambda d: d["score"] - 0.50 * _pct_rank(d["px"]) * 100, None),
    ("V3 趋势过滤", lambda d: d["score"], lambda d: d["mom20"] > 0),
    ("V4a 低换手 .25", lambda d: d["score"] - 0.25 * _pct_rank(d["turn"]) * 100, None),
    ("V4b 低换手 .50", lambda d: d["score"] - 0.50 * _pct_rank(d["turn"]) * 100, None),
]

rows = []
for name, rk, fl in VARIANTS:
    try:
        rows.append(evaluate(name, rk, fl))
        print(f"  {name} 完成", flush=True)
    except Exception as e:  # 变体失败不阻断其余
        print(f"  {name} 失败: {e}", flush=True)

res = pd.DataFrame(rows)
base = res[res["name"] == "V0 原始"].iloc[0]

print()
print("=" * 122)
print("排序变体筛选(同一宇宙/标签/115 月; 数值=月均超额)")
print("=" * 122)
print(f"{'变体':>14} {'top3':>9} {'top6':>9} {'top12':>9} {'全体':>8} "
      f"{'top6 t':>7} {'top12 t':>7} {'h1_top6':>9} {'h2_top6':>9} {'判定':>7}")
for _, r in res.iterrows():
    ok = (r["top6"] > base["top6"]) and (r["top12"] > base["top12"]) \
        and (r["top6_h1"] >= base["top6_h1"] - 0.001) and (r["top6_h2"] >= base["top6_h2"] - 0.001)
    verdict = "候选" if ok else "—"
    print(f"{r['name']:>14} {r['top3']:>+8.2%} {r['top6']:>+8.2%} {r['top12']:>+8.2%} {r['all']:>+7.2%} "
          f"{r['top6_t']:>+7.2f} {r['top12_t']:>+7.2f} {r['top6_h1']:>+8.2%} {r['top6_h2']:>+8.2%} {verdict:>7}")
print()
print(f"判据: top6 且 top12 均 > 对照({base['top6']:+.2%} / {base['top12']:+.2%}), "
      f"且前后两段各自不低于对照 -0.10pp。达标才进引擎 A/B。")
