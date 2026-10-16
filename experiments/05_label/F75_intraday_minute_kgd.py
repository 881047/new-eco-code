"""F75 · 日内（baostock 60min）因子族 KGD 门禁 —— **预注册见 `F75_intraday_prereg.md`**

数据：`data/baostock_min60/*.parquet`（5,444 票 / 2020-01-02~，每交易日 4 根：10:30·11:30·14:00·15:00 收）
→ 中间面板 `data/min60_derived/daily.parquet`（由 `work/build_min60_daily.py` 生成，32 进程 7 秒）。
数据质量（2026-09-17 实读）：3,084 万行、每日恰 4 根、时间戳只有那 4 个、零成交额 0.04%、无 NaN；
与 Tushare 日线对齐 **Σvolume/(daily.vol×100) 与 Σamount/(daily.amount×1000) 中位数都是 1.000**（n=5,767）。

## 三条因子（定义在预注册里冻结，此处逐字实现）

在锚点日 T（周频锚点 = 每周最后交易日），取 T−4..T 五个交易日：
  `id60_late_vol_share` = mean(尾盘 bar 成交额 / 当日成交额)
  `id60_late_ret`       = sum(尾盘 bar 收益 close/open − 1)
  `id60_close_vs_dayvwap` = mean(收盘价 / 当日 VWAP − 1)，VWAP = Σamount/Σvolume

**先声明**的数据质量过滤（不看收益、只看价格合理性）：|x2| ≤ 0.21（单小时涨跌超不过最宽日限幅）、
|x3| ≤ 0.15（收盘价与当日成交均价偏离超 15% 视为近乎停牌的退化解）。实测剔除 **2,009 行 / 0.026%**。

## 判据（预注册冻结；N_used = max(本批 3, 累计 171 + 3) = 174）

  ① 死区（2020-09-01~2023-12-31）头部超额（top decile − 池等权）≥ **0.5%/月 ≈ 0.119pp/5日**
  ② HAC t（lag=2，复用 F1b 实现）**≥ 2.0**；Bonferroni（N=174）后 p < 0.05（≈ t ≥ 3.10）
  ③ 死区与活跃区（2024-01-01~2026-08-31）**同号**
  ④ 与在役基线（`model_score`）秩相关 **< 0.95**
  ⑤ 经济门槛：净头部（扣 自换手 × 往返真实费率）仍 ≥ 0.119pp/5日
  ⇒ 五条全过才算"进组合层"；组合层两轴判决另跑（预注册第 5 条）。
"""
import importlib.util
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent
HERE = Path(__file__).resolve().parent
H = 5
DEAD = ("2020-09-01", "2023-12-31")
ACTIVE = ("2024-01-01", "2026-08-31")
MIN_T = 2.0
MAX_RHO = 0.95
N_BATCH, N_CUMULATIVE = 3, 171
MIN_HEAD_PP = round(0.5 * 5 / 21, 4)          # 0.5%/月 折算到 5 日锚点
SANITY_X2, SANITY_X3 = 0.21, 0.15


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


f1b = _load("f1b", HERE / "F1b_weekly_open_overlap.py")      # hac_t（项目唯一实现）

import sys                                                    # noqa: E402
sys.path.insert(0, str(BASE))
from app import config as cfg_mod                             # noqa: E402
from app import db                                            # noqa: E402
from app.alpha import factory                                 # noqa: E402
from app.factors import factor_engine as fe                    # noqa: E402
from app.portfolio import fees                                # noqa: E402


def week_anchors(dates: pd.DatetimeIndex) -> pd.DatetimeIndex:
    iso = dates.isocalendar()
    key = pd.MultiIndex.from_arrays([np.asarray(iso["year"]), np.asarray(iso["week"])])
    s = pd.Series(np.asarray(dates), index=key)
    return pd.DatetimeIndex(sorted(s.groupby(level=[0, 1]).max().values))


def build_factors(anchors: pd.DatetimeIndex) -> dict:
    d = pd.read_parquet(BASE / "data" / "min60_derived" / "daily.parquet")
    n0 = len(d)
    d = d[(d["x2"].abs() <= SANITY_X2) & (d["x3"].abs() <= SANITY_X3)]
    print(f"质量过滤：{n0:,} → {len(d):,}（剔除 {n0 - len(d):,} 行）", flush=True)
    out = {}
    for col, how, name in (("x1", "mean", "id60_late_vol_share"),
                           ("x2", "sum", "id60_late_ret"),
                           ("x3", "mean", "id60_close_vs_dayvwap")):
        w = d.pivot(index="date", columns="ts_code", values=col)
        w.index = pd.to_datetime(w.index)
        r = w.rolling(5)
        f = (r.mean() if how == "mean" else r.sum()).reindex(anchors)
        out[name] = f
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
    t_hac = f1b.hac_t(arr, 2) if len(arr) >= 10 else float("nan")
    return {"n": len(arr), "head_pp": round(float(arr.mean()) * 100, 4),
            "t_hac": round(float(t_hac), 2) if np.isfinite(t_hac) else None,
            "ic": round(float(np.nanmean(ics)), 4) if ics else None}


def turnover_cost(top: list) -> float:
    """相邻锚点 top decile 的换手 × 往返真实费率（用项目 fees.trade_cost，10 万名义）。"""
    cfg = cfg_mod.load_config()
    rt = (fees.trade_cost(100_000, "buy", params=fees.fee_params(cfg))
          + fees.trade_cost(100_000, "sell", params=fees.fee_params(cfg))) / 100_000
    turns = []
    for i in range(1, len(top)):
        a, b = set(top[i - 1]), set(top[i])
        if a and b:
            turns.append(len(b - a) / len(b))
    return float(np.mean(turns)) if turns else float("nan"), rt


def main() -> int:
    t0 = time.time()
    m = fe.load_market(since="2019-12-01")
    dates = pd.to_datetime(m["close"].index)
    anchors = week_anchors(dates)
    anchors = anchors[anchors >= pd.Timestamp("2020-02-01")]        # 5 日滚动窗 + 前向标签
    op = m["open"]
    fwd = (op.shift(-(H + 1)) / op.shift(-1) - 1.0).reindex(anchors)
    mask = factory.tradable_mask(m, dates).reindex(anchors)          # PIT 可交易域（唯一实现）
    pool = fwd.where(mask).mean(axis=1)
    excess = fwd.sub(pool, axis=0)
    print(f"锚点 {len(anchors)} 个（{anchors[0]:%Y-%m-%d} ~ {anchors[-1]:%Y-%m-%d}）", flush=True)

    facs = build_factors(anchors)
    # 在役基线：生产 model_score（月频）→ 前向填充到周锚点
    con = db.get_conn()
    base = pd.read_sql("SELECT ts_code, trade_date, score FROM model_score", con)
    con.close()
    base["trade_date"] = pd.to_datetime(base["trade_date"])
    base = base.pivot(index="trade_date", columns="ts_code", values="score").sort_index()
    base = base.reindex(base.index.union(anchors)).ffill().reindex(anchors)
    refs = {k: v.reindex(anchors) for k, v in fe.compute_price_factors(m).items()}
    refs["model_score"] = base

    res = {"script": "F75_intraday_minute_kgd", "n_factors": len(facs),
           "criteria": {"min_head_pp": MIN_HEAD_PP, "min_t": MIN_T, "max_abs_rho": MAX_RHO,
                        "bonferroni_denom": max(N_BATCH, N_CUMULATIVE + N_BATCH),
                        "dead": DEAD, "active": ACTIVE, "horizon": H,
                        "sanity": {"x2": SANITY_X2, "x3": SANITY_X3},
                        "data": "baostock 60min 2020-01-02 起（北交所不支持，缺口已登记）"},
           "factors": {}, "secs": None}
    for name, fac in facs.items():
        d = zone_stats(fac, excess, *DEAD)
        a = zone_stats(fac, excess, *ACTIVE)
        rho = {}
        for rn, rdf in refs.items():
            vals = []
            for i in range(max(0, len(fac) - 60), len(fac)):
                s = pd.Series(fac.iloc[i]).dropna()
                r = pd.Series(rdf.iloc[i]).dropna().reindex(s.index).dropna()
                s = s.reindex(r.index)
                if len(s) >= 200:
                    vals.append(abs(float(s.rank().corr(r.rank()))))
            rho[rn] = round(float(np.nanmean(vals)), 3) if vals else None
        rho_base = rho.pop("model_score", None)
        tops = []
        for i in range(len(fac)):
            s = pd.Series(fac.iloc[i]).dropna()
            if len(s) >= 200:
                tops.append(list(s[s >= s.quantile(0.9)].index))
        turn, rt = turnover_cost(tops)
        n_used = max(N_BATCH, N_CUMULATIVE + N_BATCH)
        t_hac = (d.get("t_hac") or 0.0)
        from app.models.expert import _p_two_sided
        p_adj = min(1.0, _p_two_sided(t_hac) * n_used) if t_hac else 1.0
        cost_pp = turn * rt * 100 if np.isfinite(turn) else float("nan")
        net_pp = (d.get("head_pp") or 0.0) - cost_pp
        same_sign = bool(d.get("n") and a.get("n")
                         and np.sign(a.get("head_pp") or 0) == np.sign(d.get("head_pp") or 0))
        passed = bool(d.get("n") and a.get("n")
                      and (d.get("head_pp") or -9) >= MIN_HEAD_PP and t_hac >= MIN_T
                      and same_sign and p_adj < 0.05
                      and (rho_base is None or rho_base < MAX_RHO)
                      and net_pp >= MIN_HEAD_PP)
        res["factors"][name] = {
            "dead": d, "active": a, "rho_model_score": rho_base,
            "max_abs_rho_pv": max((v for v in rho.values() if v is not None), default=None),
            "rho_detail_top5": dict(sorted(((k, v) for k, v in rho.items() if v is not None),
                                           key=lambda kv: -kv[1])[:5]),
            "turnover": round(turn, 4) if np.isfinite(turn) else None,
            "round_trip_fee_pct": round(rt * 100, 3),
            "cost_pp": round(cost_pp, 4) if np.isfinite(cost_pp) else None,
            "net_head_pp": round(net_pp, 4),
            "p_adj_174": round(p_adj, 5), "same_sign": same_sign, "pass": passed,
        }
    res["secs"] = round(time.time() - t0, 1)
    out = HERE / "F75_intraday_minute_kgd_result.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res["factors"], ensure_ascii=False, indent=1))
    n_pass = sum(1 for v in res["factors"].values() if v["pass"])
    print(f"\n过门禁: {n_pass}/{len(res['factors'])} | 耗时 {res['secs']}s | 产物 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
