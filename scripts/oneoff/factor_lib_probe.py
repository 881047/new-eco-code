"""因子库探针（2026-09-14，一次性；**只读**，不写任何生产表）

## 为什么

`D:\\codex-home\\skills\\factor-*-alpha`（quantskills，2026-09-14 装）带来 800 条 OHLCV 候选，
按 06 号的新意度分析，本仓 31 因子里**没有**的四族是：震荡（rsi/stoch）、量价关系
（price_volume_corr / obv / volume_z）、形态通道（wick / range_ratio / drawdown / gap / intraday）、
趋势效率（efficiency / trend_strength）。本探针取其中**每族 1~2 条最简形式**，先回答两个
**描述性**问题（不做判决、不替代 KGD）：

  ① 它们在死区/活跃区的 **头部超额**（top10% 前向 5 日超额均值 − 同截面均值）与 IC 是多少；
  ② 它们与本仓现役价量因子（mom_20 / rev_5 / vol_20 / turnover_20 / skew_20）的
     **横截面秩相关**有多高 —— 太高的就是"换皮"，不值得进预注册。

## 口径

- 面板与周频研究同源：`fe.load_market(since='2014-01-01')` + **每个 ISO 周最后交易日**为锚点；
- 标签 = 与周频一致的 **open(t+1)→open(t+1+5) 超额**（同票池等权），只用 ≤ 锚点的信息算因子；
- 相关性只取**最近 60 个锚点**做抽样（够看量级，不必全history）。

## 纪律

**这不是实验判决**：真正的候选进仓必须走 06 号写明的预注册线（秩相关去重 → KGD → 组合两轴）。
本脚本只产出"值不值得开那条线"的证据；结果若被引用，必须连同"描述性、未过门禁"一起引用。

运行：`D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\factor_lib_probe.py`
产物：`logs/factor_lib_probe.json`（自己的日志文件，不碰生产表）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.factors import factor_engine as fe          # noqa: E402

BASE = Path(__file__).resolve().parent.parent.parent
H = 5
DEAD = ("2020-09-01", "2023-12-31")
ACTIVE = ("2024-01-01", "2026-08-31")


def week_anchors(dates: pd.DatetimeIndex) -> pd.DatetimeIndex:
    iso = dates.isocalendar()
    key = pd.MultiIndex.from_arrays([np.asarray(iso["year"]), np.asarray(iso["week"])])
    s = pd.Series(np.asarray(dates), index=key)
    return pd.DatetimeIndex(sorted(s.groupby(level=[0, 1]).max().values))


def new_factors(m: dict) -> dict:
    """四族各取 1~2 条（公式按 skill 索引里的最简形式实现，窗口取 10/14）。"""
    o, h, l, c, v = m["open"], m["high"], m["low"], m["close"], m["vol"]
    ret = c.pct_change()

    # ① 震荡族：RSI(14)（0~100，越高越强）、随机指标位置 STOCH(14)
    up = ret.clip(lower=0).rolling(14).mean()
    dn = (-ret.clip(upper=0)).rolling(14).mean()
    rsi = 100.0 * up / (up + dn).replace(0, np.nan)
    ll, hh = l.rolling(14).min(), h.rolling(14).max()
    stoch = (c - ll) / (hh - ll).replace(0, np.nan)

    # ② 量价关系族：20 日"收益率 vs 成交量变化"的滚动相关、OBV 斜率(10)
    vr = v.pct_change()
    pv_corr = ret.rolling(20).corr(vr)
    obv = (np.sign(ret).fillna(0.0) * v).cumsum()
    obv_slope = obv.diff(10) / obv.abs().rolling(10).mean().replace(0, np.nan)

    # ③ 形态通道族：上/下影线压力（10 日均值）、区间位置（10 日）
    rng = (h - l).replace(0, np.nan)
    upper = (h - np.maximum(o, c)) / rng
    lower = (np.minimum(o, c) - l) / rng
    wick_up = upper.rolling(10).mean()
    wick_dn = lower.rolling(10).mean()
    range_pos = (c - l.rolling(10).min()) / (h.rolling(10).max() - l.rolling(10).min()).replace(0, np.nan)

    # ④ 趋势效率族：net change / path length（10 日）
    path = ret.abs().rolling(10).sum()
    eff = (c / c.shift(10) - 1.0).abs() / path.replace(0, np.nan)

    return {"probe_rsi14": rsi, "probe_stoch14": stoch, "probe_pvcorr20": pv_corr,
            "probe_obvslope10": obv_slope, "probe_wickup10": wick_up,
            "probe_wickdn10": wick_dn, "probe_rangepos10": range_pos, "probe_eff10": eff}


def main() -> int:
    m = fe.load_market(since="2014-01-01")
    dates = pd.to_datetime(m["close"].index)
    anchors = week_anchors(dates)
    op = m["open"]
    fwd = op.shift(-(H + 1)) / op.shift(-1) - 1.0
    fwd = fwd.reindex(anchors)
    pool = fwd.notna()
    excess = fwd.sub(fwd.where(pool).mean(axis=1), axis=0)     # 同截面等权超额

    probes = {k: v.reindex(anchors) for k, v in new_factors(m).items()}
    ref = fe.compute_price_factors(m)
    refs = {k: ref[k].reindex(anchors) for k in
            ("mom_20", "rev_5", "vol_20", "turnover_20", "skew_20") if k in ref}

    out = {"anchors": len(anchors), "probes": {}, "refs": list(refs)}
    for name, df in probes.items():
        rec = {}
        for zone, (a, b) in (("DEAD", DEAD), ("ACTIVE", ACTIVE)):
            z = (df.index >= a) & (df.index <= b)
            sub_x, sub_y = df[z], excess[z]
            ic, head = [], []
            for i in range(len(sub_x)):
                s = pd.Series(sub_x.iloc[i]).dropna()
                t = pd.Series(sub_y.iloc[i]).reindex(s.index).dropna()
                s = s.reindex(t.index)
                if len(s) < 200:
                    continue
                ic.append(float(s.rank().corr(t.rank())))
                thr = t.quantile(0.9)
                head.append(float(t[s >= s.quantile(0.9)].mean() - t.mean()))
            rec[zone] = {"n": len(ic), "ic": round(float(np.nanmean(ic)), 4) if ic else None,
                         "head_excess": round(float(np.nanmean(head)) * 100, 4) if head else None}
        # 与现役因子的横截面秩相关（最近 60 个锚点，逐锚点平均）
        corr = {}
        for rname, rdf in refs.items():
            vals = []
            for i in range(max(0, len(df) - 60), len(df)):
                a, b = pd.Series(df.iloc[i]).dropna(), pd.Series(rdf.iloc[i]).dropna()
                b = b.reindex(a.index).dropna()
                a = a.reindex(b.index)
                if len(a) >= 200:
                    vals.append(abs(float(a.rank().corr(b.rank()))))
            corr[rname] = round(float(np.nanmean(vals)), 3) if vals else None
        rec["max_abs_corr_vs_refs"] = corr
        out["probes"][name] = rec

    p = BASE / "logs" / "factor_lib_probe.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"\n产物: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
