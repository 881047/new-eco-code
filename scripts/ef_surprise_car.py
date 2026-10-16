# -*- coding: utf-8 -*-
"""业绩预告 surprise 条件化事件研究 —— `plans/ef-surprise-car-prereg-20261003.md` 执行体。

事件 = 普查 ef 事件(单一来源 import event_census.scan_events) 左连 fields_llm_all 的
ef_yoy_pct(同日多条取均值); C1 方向分桶(yoy>0 / yoy<0), C2 桶内 |yoy| 秩三分位(描述)。
主判据 = spread = CAR21(好) − CAR21(坏) 的 Welch |t|≥2 且双子窗同号且各自|t|≥1.5,
且两桶超额(实际 − 精确等权宇宙基线)符号与 spread 一致。只读零写库。
产物 experiments/ef_surprise_car_20261003.json。
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

from event_census import scan_events
from probe_inquiry_family import to_ts_code

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ROOT / "data" / "text_corpus" / "fields_llm_all" / "llm_all.parquet"
OUT = ROOT / "experiments" / "ef_surprise_car_20261003.json"
PREREG = "plans/ef-surprise-car-prereg-20261003.md"
K = 21
MAX_MISSING = 3
BENCH = "000300.SH"


def welch_t(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 30 or len(b) < 30:
        return float("nan")
    m1, m2 = a.mean(), b.mean()
    v1, v2 = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
    return float((m1 - m2) / np.sqrt(v1 + v2))


def exact_baseline(pos_E: np.ndarray, counts: np.ndarray, Rv: np.ndarray,
                   bv: np.ndarray, len_bdays: int) -> float:
    """等权宇宙在给定事件日历上的 CAR21(事件数加权, 缺失日计 0, 与普查同口径)。"""
    keep = pos_E + 1 + K < len_bdays
    pos_E, counts = pos_E[keep], counts[keep]
    if len(pos_E) == 0:
        return float("nan")
    ew_s, ew_b = [], []
    for i in range(0, len(pos_E), 150):
        chunk = pos_E[i:i + 150]
        idx = chunk[:, None] + np.arange(1, K + 1)[None, :]
        svk = np.nan_to_num(Rv[idx, :], nan=0.0)
        bvk = bv[idx]
        ew_s.append((1 + svk).prod(axis=1).mean(axis=1))
        ew_b.append((1 + bvk).prod(axis=1))
    per_date = np.concatenate(ew_s) - np.concatenate(ew_b)
    w = counts / counts.sum()
    return float((per_date * w).sum())


def car21(i0: np.ndarray, cidx: np.ndarray, Rv: np.ndarray, bv: np.ndarray,
          len_bdays: int) -> tuple:
    ok = (i0 + 1 + K < len_bdays) & ~np.isnan(Rv[np.clip(i0, 0, len_bdays - 1), cidx])
    idx = np.clip(i0[:, None] + np.arange(1, K + 1)[None, :], 0, len_bdays - 1)
    svk = np.where(ok[:, None], np.nan_to_num(Rv[idx, cidx[:, None]], nan=0.0), np.nan)
    bvk = np.where(ok[:, None], bv[idx], np.nan)
    n_miss = np.isnan(svk).sum(axis=1)
    end_ok = ok & ~np.isnan(Rv[np.clip(i0 + K, 0, len_bdays - 1), cidx]) \
        & (n_miss <= MAX_MISSING)
    car = np.where(end_ok, (1 + np.nan_to_num(svk)).prod(axis=1)
                   - (1 + np.nan_to_num(bvk)).prod(axis=1), np.nan)
    return car, ok & end_ok      # 桶样本 = 完全有效事件(判决与基线同一集合)


def main() -> int:
    t0_ = time.time()
    print("[1/4] ef 事件(普查单一来源)…", flush=True)
    EV = scan_events()
    ev = EV[EV["category"] == "earnings_forecast"].copy()
    ev["code6"] = ev["ts_code"].str[:6]
    print(f"    ef 事件 {len(ev)}", flush=True)

    print("[2/4] 连接字段抽取(ef_yoy_pct, 同日多条取均值)…", flush=True)
    F = pd.read_parquet(FIELDS)
    F = F[(F["category"] == "earnings_forecast") & (F["field"] == "ef_yoy_pct")]
    F["ts_code"] = F["代码"].astype(str).str.zfill(6).map(to_ts_code)
    F["date"] = pd.to_datetime(F["公告时间"], format="mixed", errors="coerce").dt.normalize()
    F = F.dropna(subset=["ts_code", "date", "value"])
    yoy = F.groupby(["ts_code", "date"])["value"].mean()
    ev = ev.join(yoy.rename("yoy"), on=["ts_code", "date"])
    have = ev[ev["yoy"].notna()]
    print(f"    有 surprise 的事件 {len(have)} ({len(have)/len(ev):.1%}) | yoy 中位 "
          f"{have['yoy'].median():.1f} | p25 {have['yoy'].quantile(.25):.1f} / "
          f"p75 {have['yoy'].quantile(.75):.1f}", flush=True)

    print("[3/4] 收益矩阵…", flush=True)
    conn = sqlite3.connect(f"{(ROOT / 'data' / 'neweco.db').as_uri()}?mode=ro", uri=True)
    corpus_codes = sorted(ev["ts_code"].unique())
    frames = []
    for i in range(0, len(corpus_codes), 400):
        chunk = corpus_codes[i:i + 400]
        ph = ",".join("?" * len(chunk))
        frames.append(pd.read_sql(
            f"SELECT ts_code, trade_date, pct_chg FROM daily WHERE trade_date>='20190101' "
            f"AND ts_code IN ({ph})", conn, params=chunk))
    D = pd.concat(frames, ignore_index=True)
    D["ret"] = D["pct_chg"] / 100.0
    R = D.pivot(index="trade_date", columns="ts_code", values="ret").sort_index()
    R.index = pd.to_datetime(R.index, format="%Y%m%d")
    bench = pd.read_sql("SELECT trade_date, close FROM index_daily WHERE ts_code=? "
                        "AND trade_date>='20190101' ORDER BY trade_date", conn, params=(BENCH,))
    conn.close()
    Rv = R.to_numpy(dtype=float)
    code_ix = {c: i for i, c in enumerate(R.columns)}
    bser = pd.Series(bench["close"].values,
                     index=pd.to_datetime(bench["trade_date"], format="%Y%m%d"))
    bret = bser.pct_change().dropna()
    bdays = bret.index
    bv = bret.to_numpy(dtype=float)
    bpos = pd.Series(np.arange(len(bdays)), index=bdays)
    print(f"    矩阵 {R.shape} | {time.time()-t0_:.0f}s", flush=True)

    print("[4/4] 分桶 CAR21 + 精确基线…", flush=True)
    H = have.copy()
    # 普查同源: E = t0 后首个交易日(searchsorted), 非交易日公告的事件保留
    H["i0"] = np.searchsorted(bdays.values, H["date"].to_numpy().astype("datetime64[ns]"),
                              side="right")
    H = H[(H["i0"] > 0) & (H["i0"] < len(bdays) - 1)]
    H["cidx"] = H["ts_code"].map(code_ix)
    H = H.dropna(subset=["cidx"])
    H["cidx"] = H["cidx"].astype(int)
    H["year"] = H["date"].dt.year

    buckets = {}
    for tag, mask in (("good", H["yoy"] > 0), ("bad", H["yoy"] < 0)):
        B = H[mask]
        car, ok = car21(B["i0"].to_numpy(), B["cidx"].to_numpy(), Rv, bv, len(bdays))
        B = B[ok]
        car = car[ok]
        # 精确基线: 该桶自身事件日历(按 ts_code,date 计事件数)
        g = B.groupby("date").size()
        dpos = bpos.reindex(g.index).dropna().astype(int).to_numpy()
        cnts = g.reindex(bdays[dpos]).to_numpy(dtype=float)
        base = exact_baseline(dpos, cnts, Rv, bv, len(bdays))
        m = float(np.nanmean(car))
        se = float(np.nanstd(car, ddof=1) / np.sqrt(len(car)))
        buckets[tag] = {"n": int(len(car)), "mean_CAR21": round(m, 5),
                        "t_per_event": round(m / se, 2), "exact_baseline": round(base, 5),
                        "excess": round(m - base, 5), "excess_z": round((m - base) / se, 2),
                        "yoy_median": float(B["yoy"].median()),
                        "years": int(B["year"].min()), "-": int(B["year"].max())}
        B2 = B.copy()
        B2["car"] = car
        buckets[tag]["_df"] = B2
        print(f"    {tag}: n={len(car)} CAR21 {m:+.4f} (t={m/se:+.2f}) | 基线 {base:+.4f} | "
              f"超额 {m-base:+.4f}", flush=True)

    gdf, bdf = buckets["good"]["_df"], buckets["bad"]["_df"]
    spread_full = welch_t(gdf["car"].to_numpy(), bdf["car"].to_numpy())
    sw = {}
    for tag, lo, hi in (("2019-2022", 2019, 2022), ("2023-2026", 2023, 2026)):
        a = gdf[(gdf["year"] >= lo) & (gdf["year"] <= hi)]["car"].to_numpy()
        b = bdf[(bdf["year"] >= lo) & (bdf["year"] <= hi)]["car"].to_numpy()
        sw[tag] = {"spread_t": round(welch_t(a, b), 2), "n_good": int(len(a)),
                   "n_bad": int(len(b))}
    # C2 幅度三分位(桶内 |yoy| 秩, 描述读数)
    terr = {}
    for tag, dfx in (("good", gdf), ("bad", bdf)):
        dfx = dfx.copy()
        dfx["tercile"] = pd.qcut(dfx["yoy"].abs().rank(method="first"), 3, labels=[1, 2, 3])
        terr[tag] = {f"T{int(t)}": round(float(dfx[dfx['tercile'] == t]['car'].mean()), 5)
                     for t in (1, 2, 3)}

    s_full = abs(spread_full) >= 2.0
    t1, t2 = sw["2019-2022"]["spread_t"], sw["2023-2026"]["spread_t"]
    same = (np.isfinite(t1) and np.isfinite(t2) and abs(t1) >= 1.5 and abs(t2) >= 1.5
            and np.sign(t1) == np.sign(t2))
    spread_pos = spread_full > 0
    consist = ((buckets["good"]["excess"] > 0 and buckets["bad"]["excess"] < 0)
               if spread_pos else
               (buckets["good"]["excess"] < 0 and buckets["bad"]["excess"] > 0))
    verdict = "条件化效应成立" if (s_full and same and consist) else "本构造下未证实"
    print(f"    spread 全样本 t={spread_full:+.2f} | 子窗 {sw} | 一致性 {consist} | "
          f"判决: {verdict}", flush=True)

    for b in buckets.values():
        b.pop("_df")
    payload = {
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"), "prereg": PREREG,
        "n_ef_events": int(len(ev)), "n_with_yoy": int(len(have)),
        "coverage_by_year": have.groupby(have["date"].dt.year)["ts_code"].nunique().to_dict(),
        "buckets": buckets, "C1_spread": {"full_t": round(spread_full, 2),
                                          "subwindows": sw,
                                          "sign_consistency": bool(consist)},
        "C2_magnitude_terciles_descriptive": terr,
        "criteria": "spread |t|>=2 且双子窗同号且各自|t|>=1.5 且两桶超额符号与 spread 一致",
        "verdict": verdict,
        "elapsed_sec": round(time.time() - t0_, 1)}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"产物: {OUT} ({time.time()-t0_:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
