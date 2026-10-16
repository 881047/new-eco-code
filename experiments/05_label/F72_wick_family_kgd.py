"""F72 —— **影线族**（upper/lower wick pressure）预注册因子门禁（2026-09-14）

## 来源与动机

2026-09-14 装上四套 `quantskills` 因子库（见 KB 06），并用一次性探针
（`scripts/oneoff/factor_lib_probe.py`）对"本仓 31 因子里没有的四族"做了描述性读数：
震荡（rsi/stoch）、量价（pv_corr/obv）、形态（rangepos）、趋势效率（eff）里，
**六条要么与现役因子高相关（0.72/0.77 = 换皮），要么量级很小**；唯一同时满足
"低冗余（|ρ| 0.12~0.31）+ 方向可分"的是**影线族**，其中 `wickdn10` 两区都是正 IC。
⇒ 本实验只测影线族（探针结论不构成判决，判决以本文件 §判据 为准）。

## §1 因子定义（按 skill 库 `factor_index.json` 的最简形式；只用 ≤ 锚点的信息）

    rng      = high − low                      （=0 记 NaN）
    upper    = (high − max(open, close)) / rng
    lower    = (min(open, close) − low) / rng
    F_uw{W}  = upper 的 W 日滚动均值            W ∈ {5, 10}
    F_lw{W}  = lower 的 W 日滚动均值            W ∈ {5, 10}

共 **4 条**（N=4）。全部在**周锚点**（每个 ISO 周最后交易日）上取值。

## §2 判据（**跑数前冻结**；四条全过才叫"过门禁"，否则按否证登记）

1. **死区**（2020-09-01~2023-12-31）头部十分位超额均值 **≥ +0.30%/期**；
2. 同上 **t ≥ 2.0**（逐锚点头部超额序列的均值 t = mean / (sd/√n)）；
3. **活跃区**（2024-01-01~2026-08-31）头部超额 **≥ 0 且与死区同号**；
4. 与现役参考因子（`mom_20 / rev_5 / vol_20 / turnover_20 / skew_20`）的横截面秩相关
   **|ρ| ≤ 0.90**（最近 60 锚点）—— 防"换皮"。

**Bonferroni**：N=4 ⇒ 单条 p 阈值 0.05/4 = 0.0125（t 双侧 ≈ 2.50）。判据 2 报两种口径：
**原始 t ≥ 2.0** 与 **Bonferroni 后 t ≥ 2.50**，两者都写在读数里（避免"过了原始、没过校正"被含糊带过）。

## §3 口径

- 面板与周频生产同源：`fe.load_market(since='2014-01-01')`；锚点 = 周最后交易日；
- 标签 = **open(t+1)→open(t+1+5) 超额**（同截面等权均值扣除）；有效截面 ≥200 只；
- 头部 = 因子值前 10%（逐锚点分位）；仅报读数，**不产生交易信号、不改生产、不换 pin**。

运行：`D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F72_wick_family_kgd.py`
产物：`experiments/05_label/F72_wick_family_kgd_result.json`
"""
from __future__ import annotations

import json
import importlib.util
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.factors import factor_engine as fe          # noqa: E402

BASE = Path(__file__).resolve().parent.parent.parent
HERE = Path(__file__).resolve().parent
H = 5
DEAD = ("2020-09-01", "2023-12-31")
ACTIVE = ("2024-01-01", "2026-08-31")
MIN_HEAD, MIN_T, MIN_T_BONF, MAX_RHO = 0.003, 2.0, 2.50, 0.90
HAC_LAG = 2          # = ceil(H/5)+1，与 F1b 的预注册口径一致


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 2026-09-14 **修补（用户问"门禁科学吗"引出的自查）**：初版用 iid t，而 5 日标签在周锚点上
# **重叠**（相邻锚点共享 4 天）+ 因子暴露持续 ⇒ iid t 被高估。项目早有标准实现与口径
# （`F1b.hac_t`，Bartlett 核，lag=ceil(H/5)+1=2），这里**直接复用**，不另写一份。
f1b = _load("f1b", HERE / "F1b_weekly_open_overlap.py")


def week_anchors(dates: pd.DatetimeIndex) -> pd.DatetimeIndex:
    iso = dates.isocalendar()
    key = pd.MultiIndex.from_arrays([np.asarray(iso["year"]), np.asarray(iso["week"])])
    s = pd.Series(np.asarray(dates), index=key)
    return pd.DatetimeIndex(sorted(s.groupby(level=[0, 1]).max().values))


def build_factors(m: dict) -> dict:
    o, h, l, c = m["open"], m["high"], m["low"], m["close"]
    rng = (h - l).replace(0, np.nan)
    upper = (h - np.maximum(o, c)) / rng
    lower = (np.minimum(o, c) - l) / rng
    out = {}
    for w in (5, 10):
        out[f"F_uw{w}"] = upper.rolling(w).mean()
        out[f"F_lw{w}"] = lower.rolling(w).mean()
    return out


def zone_stats(fac: pd.DataFrame, excess: pd.DataFrame, a: str, b: str) -> dict:
    z = (fac.index >= a) & (fac.index <= b)
    fx, ex = fac[z], excess[z]
    heads, ics = [], []
    for i in range(len(fx)):
        s = pd.Series(fx.iloc[i]).dropna()
        t = pd.Series(ex.iloc[i]).reindex(s.index).dropna()
        s = s.reindex(t.index)
        if len(s) < 200:
            continue
        ics.append(float(s.rank().corr(t.rank())))
        heads.append(float(t[s >= s.quantile(0.9)].mean() - t.mean()))
    if not heads:
        return {"n": 0}
    arr = np.asarray(heads, dtype=float)
    t_naive = float(arr.mean() / (arr.std(ddof=1) / np.sqrt(len(arr)))) if len(arr) > 1 else None
    t_hac = f1b.hac_t(arr, HAC_LAG) if len(arr) >= 10 else float("nan")
    return {"n": len(arr), "head_pct": round(float(arr.mean()) * 100, 4),
            "t": round(t_naive, 2) if t_naive is not None else None,          # iid（仅供参考）
            "t_hac": round(float(t_hac), 2) if np.isfinite(t_hac) else None,  # 判据用这个
            "ic": round(float(np.nanmean(ics)), 4) if ics else None}


def main() -> int:
    t0 = time.time()
    m = fe.load_market(since="2014-01-01")
    dates = pd.to_datetime(m["close"].index)
    anchors = week_anchors(dates)
    op = m["open"]
    fwd = (op.shift(-(H + 1)) / op.shift(-1) - 1.0).reindex(anchors)
    excess = fwd.sub(fwd.mean(axis=1), axis=0)

    facs = {k: v.reindex(anchors) for k, v in build_factors(m).items()}
    ref = fe.compute_price_factors(m)
    # 2026-09-14 修补：冗余检查从"5 个参考因子"扩到**全部价量因子**（OHLCV 因子的天然对照族），
    # 只留 5 个会漏掉"与 ret_max20/high_52w 等撞车"的情形。
    refs = {k: v.reindex(anchors) for k, v in ref.items()}

    res = {"script": "F72_wick_family_kgd", "n_factors": len(facs),
           "criteria": {"min_head_pct": MIN_HEAD * 100, "min_t": MIN_T,
                        "min_t_bonferroni": MIN_T_BONF, "max_abs_rho": MAX_RHO,
                        "dead": DEAD, "active": ACTIVE, "horizon": H},
           "factors": {}, "secs": None}
    for name, fac in facs.items():
        d = zone_stats(fac, excess, *DEAD)
        a = zone_stats(fac, excess, *ACTIVE)
        rho = {}
        for rn, rdf in refs.items():
            vals = []
            for i in range(max(0, len(fac) - 60), len(fac)):
                s, r = pd.Series(fac.iloc[i]).dropna(), pd.Series(rdf.iloc[i]).dropna()
                r = r.reindex(s.index).dropna()
                s = s.reindex(r.index)
                if len(s) >= 200:
                    vals.append(abs(float(s.rank().corr(r.rank()))))
            rho[rn] = round(float(np.nanmean(vals)), 3) if vals else None
        max_rho = max((v for v in rho.values() if v is not None), default=None)
        passed = bool(d.get("n") and a.get("n")
                      and d["head_pct"] / 100 >= MIN_HEAD and (d["t_hac"] or 0) >= MIN_T
                      and a["head_pct"] >= 0 and np.sign(a["head_pct"]) == np.sign(d["head_pct"])
                      and (max_rho is None or max_rho <= MAX_RHO))
        res["factors"][name] = {"dead": d, "active": a, "max_abs_rho": max_rho,
                                "rho_detail": rho, "pass": passed,
                                "pass_bonferroni": bool(passed and (d["t_hac"] or 0) >= MIN_T_BONF)}
    res["secs"] = round(time.time() - t0, 1)
    p = BASE / "experiments" / "05_label" / "F72_wick_family_kgd_result.json"
    p.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res["factors"], ensure_ascii=False, indent=1))
    n_pass = sum(1 for v in res["factors"].values() if v["pass"])
    print(f"\n过门禁: {n_pass}/{len(res['factors'])}（Bonferroni 后 "
          f"{sum(1 for v in res['factors'].values() if v['pass_bonferroni'])}/{len(res['factors'])}）"
          f" | 耗时 {res['secs']}s | 产物 {p}")
    return 0 if n_pass == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
