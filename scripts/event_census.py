# -*- coding: utf-8 -*-
"""事件族普查 —— `plans/event-census-prereg-20261003.md` 的执行体。

14 个 rubric v3 事件类 × 同源事件研究管线(问询函研究判据逐字同源):
主读数 = 市场调整 CAR(21), 描述 = CAR(5/10/42/63)+IR, 聚类稳健 = 按 t0 日期聚合的
逐日均值 t, 子窗 = 2019-2022 / 2023-2026。普查 = 符号图谱(方向×强度×体制),
不立项收割机——阳性类进收割层须各自另立预注册。只读零写库。
产物 experiments/event_census_20261003.json。
"""
import json
import math
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from probe_inquiry_family import to_ts_code

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "text_corpus" / "tagged"
OUT = ROOT / "experiments" / "event_census_20261003.json"
PREREG = "plans/event-census-prereg-20261003.md"
WINDOWS = (5, 10, 21, 42, 63)
MAX_MISSING = 3
BENCH = "000300.SH"
CLASSES = ["operating_contract_data", "ma_restructuring", "financing_capital",
           "related_party_transaction", "audit_internal_esg", "equity_incentive",
           "buyback", "litigation_regulatory", "insider_trading",
           "guarantee_financial_assistance", "dividend", "earnings_forecast",
           "periodic_report", "governance_meeting"]
RE_EXCLUDE = json.loads('["延期","取消","进展","撤回","终止","催告","更正"]')


def scan_events():
    """14 类事件: 类内 (ts_code, date) 去重, 元事件排除(冻结)。"""
    files = sorted(CORPUS.glob("*.parquet"))
    parts = []
    for fp in files:
        df = pd.read_parquet(fp, columns=["代码", "公告标题", "公告时间", "category"])
        df = df[df["category"].isin(CLASSES)]
        if not len(df):
            continue
        t = df["公告标题"].fillna("").astype(str)
        bad = pd.Series(False, index=df.index)
        for w in RE_EXCLUDE:
            bad |= t.str.contains(w, regex=False)
        df = df[~bad]
        if not len(df):
            continue
        sub = df[["代码", "category"]].copy()
        sub["date"] = pd.to_datetime(df["公告时间"], format="mixed", errors="coerce")
        parts.append(sub.dropna(subset=["date"]))
    E = pd.concat(parts, ignore_index=True)
    E["ts_code"] = E["代码"].astype(str).str.zfill(6).map(to_ts_code)
    E = E.dropna(subset=["ts_code"])
    E = E.drop_duplicates(["ts_code", "date", "category"])
    return E


def class_stats(car: np.ndarray, dates: pd.Series, years: pd.Series) -> dict:
    out = {}
    x = car[~np.isnan(car)]
    if len(x) >= 30:
        m = float(x.mean())
        t = float(m / (x.std(ddof=1) / np.sqrt(len(x))))
        out["per_event"] = {"n": int(len(x)), "mean": round(m, 5), "t": round(t, 2)}
        # 聚类稳健: 按 t0 日期聚合
        g = pd.Series(car, index=dates).dropna().groupby(level=0).mean()
        if len(g) >= 30:
            gd = g.to_numpy()
            out["by_date"] = {"n_days": int(len(gd)),
                              "mean": round(float(gd.mean()), 5),
                              "t": round(float(gd.mean() / (gd.std(ddof=1) / np.sqrt(len(gd)))), 2)}
        else:
            out["by_date"] = {"n_days": int(len(g)), "mean": None, "t": None}
        sw = {}
        for lo, hi, tag in ((2019, 2022, "2019-2022"), (2023, 2026, "2023-2026")):
            xs = car[(years >= lo) & (years <= hi)]
            xs = xs[~np.isnan(xs)]
            if len(xs) >= 30:
                mm = float(xs.mean())
                sw[tag] = {"n": int(len(xs)), "mean": round(mm, 5),
                           "t": round(float(mm / (xs.std(ddof=1) / np.sqrt(len(xs)))), 2)}
            else:
                sw[tag] = {"n": int(len(xs)), "mean": None, "t": None}
        out["subwindows"] = sw
        out["verdict"] = bool(abs(out["per_event"]["t"]) >= 2.0 and same_sign(sw))
    else:
        out["per_event"] = {"n": int(len(x)), "mean": None, "t": None}
        out["verdict"] = False
    return out


def same_sign(sw: dict) -> bool:
    a, b = sw.get("2019-2022", {}), sw.get("2023-2026", {})
    ta, tb = a.get("t"), b.get("t")
    return (ta is not None and tb is not None and abs(ta) >= 1.5 and abs(tb) >= 1.5
            and np.sign(ta) == np.sign(tb))


def main() -> int:
    t0_ = time.time()
    print("[1/4] 扫描打标语料(14 类)…", flush=True)
    EV = scan_events()
    codes = sorted(EV["ts_code"].unique())
    print(f"    事件 {len(EV)} | 涉及 {len(codes)} 只 | {time.time()-t0_:.0f}s", flush=True)

    conn = sqlite3.connect(f"{(ROOT / 'data' / 'neweco.db').as_uri()}?mode=ro", uri=True)
    print("[2/4] 加载个股日收益矩阵…", flush=True)
    frames = []
    for i in range(0, len(codes), 400):
        chunk = codes[i:i + 400]
        ph = ",".join("?" * len(chunk))
        frames.append(pd.read_sql(
            f"SELECT ts_code, trade_date, pct_chg FROM daily WHERE trade_date>='20190101' "
            f"AND ts_code IN ({ph})", conn, params=chunk))
    D = pd.concat(frames, ignore_index=True)
    D["ret"] = D["pct_chg"] / 100.0
    R = (D.pivot(index="trade_date", columns="ts_code", values="ret").sort_index())
    R.index = pd.to_datetime(R.index, format="%Y%m%d")
    Rv = R.to_numpy(dtype=float)                      # dates × codes
    code_ix = {c: i for i, c in enumerate(R.columns)}
    bench = pd.read_sql("SELECT trade_date, close FROM index_daily WHERE ts_code=? "
                        "AND trade_date>='20190101' ORDER BY trade_date", conn, params=(BENCH,))
    conn.close()
    bser = pd.Series(bench["close"].values,
                     index=pd.to_datetime(bench["trade_date"], format="%Y%m%d"))
    bret = bser.pct_change().dropna()
    bdays = bret.index
    bpos = {d: i for i, d in enumerate(bdays)}
    bv = bret.to_numpy(dtype=float)
    bpos_of_date = np.searchsorted(bdays.values, R.index.values)
    print(f"    收益矩阵 {R.shape} | {time.time()-t0_:.0f}s", flush=True)

    results, summary = {}, []
    for cls in CLASSES:
        ev = EV[EV["category"] == cls]
        if not len(ev):
            continue
        ev = ev.sort_values("date")
        t0d = ev["date"].to_numpy()
        cidx = np.array([code_ix.get(c, -1) for c in ev["ts_code"]])
        keep = cidx >= 0
        ev, t0d, cidx = ev[keep], t0d[keep], cidx[keep]
        i0 = np.searchsorted(bdays.values, t0d, side="right")
        base_ok = (i0 < len(bdays) - max(WINDOWS))
        base_ok &= ~np.isnan(Rv[np.clip(i0, 0, len(bdays) - 1), cidx])
        ev, t0d, cidx, i0 = ev[base_ok], t0d[base_ok], cidx[base_ok], i0[base_ok]
        n_drop_e = int((~base_ok).sum())

        blk = {"n_events": int(len(ev)), "n_stocks": int(ev["ts_code"].nunique()),
               "dropped_no_E_close": n_drop_e,
               "yearly": ev.groupby(ev["date"].dt.year)["ts_code"].nunique().to_dict()}
        # IR: (t0, E] 累计收益差(缺失日个股腿计 0)
        p_t0 = np.searchsorted(bdays.values, t0d, side="right") - 1
        maxgap = int(np.max(i0 - p_t0)) if len(ev) else 1
        ir_idx = p_t0[:, None] + np.arange(1, maxgap + 1)[None, :]
        ir_mask = ir_idx <= i0[:, None]
        ir_idx_c = np.clip(ir_idx, 0, len(bdays) - 1)
        sv = np.where(ir_mask, np.nan_to_num(Rv[ir_idx_c, cidx[:, None]], nan=0.0), 1.0)
        bv_ = np.where(ir_mask, np.nan_to_num(bv[np.clip(ir_idx, 0, len(bv) - 1)], nan=0.0), 1.0)
        ir_adj = sv.prod(axis=1) - bv_.prod(axis=1)
        # CAR 窗口(向量化)
        for k in WINDOWS:
            ok = i0 + k < len(bdays)
            idx = np.clip(i0[:, None] + np.arange(1, k + 1)[None, :], 0, len(bdays) - 1)
            svk = np.where(ok[:, None], np.nan_to_num(Rv[idx, cidx[:, None]], nan=0.0), np.nan)
            bvk = np.where(ok[:, None], bv[idx], np.nan)
            n_miss = np.isnan(svk).sum(axis=1)
            end_ok = ok & ~np.isnan(Rv[np.clip(i0 + k, 0, len(bdays) - 1), cidx]) \
                & (n_miss <= MAX_MISSING)
            svk = np.nan_to_num(svk, nan=0.0)
            bvk = np.nan_to_num(bvk, nan=0.0)
            car = np.where(end_ok, (1 + svk).prod(axis=1) - (1 + bvk).prod(axis=1), np.nan)
            car_adj = car.copy()
            st = class_stats(car_adj, pd.Series(pd.to_datetime(t0d)), ev["date"].dt.year)
            blk[f"CAR{k}_adj"] = st
            if k == 21:
                blk["CAR21_raw"] = class_stats(
                    np.where(end_ok, (1 + svk).prod(axis=1) - 1, np.nan),
                    pd.Series(pd.to_datetime(t0d)), ev["date"].dt.year)
                blk["IR_adj"] = class_stats(ir_adj, pd.Series(pd.to_datetime(t0d)),
                                            ev["date"].dt.year)
            if k == max(WINDOWS):
                blk["dropped_window"] = {str(k): int((~end_ok).sum())}
        pe = blk["CAR21_adj"].get("per_event", {})
        if pe.get("t") is not None:
            p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(pe["t"]) / np.sqrt(2))))
            blk["p_adj_bonferroni_14"] = round(min(1.0, 14.0 * p), 5)
        results[cls] = blk

    print("[3/4] 汇总符号图谱…", flush=True)
    for cls, blk in results.items():
        pe = blk["CAR21_adj"].get("per_event", {})
        if pe.get("t") is None:
            continue
        v = "成立(+)" if (blk["CAR21_adj"]["verdict"] and pe["mean"] > 0) else \
            ("成立(-)" if blk["CAR21_adj"]["verdict"] else "未证实")
        summary.append({"class": cls, "n": pe["n"], "mean_CAR21": pe["mean"],
                        "t": pe["t"], "by_date_t": (blk["CAR21_adj"].get("by_date") or {}).get("t"),
                        "sw_2019_2022_t": (blk["CAR21_adj"].get("subwindows", {}).get("2019-2022") or {}).get("t"),
                        "sw_2023_2026_t": (blk["CAR21_adj"].get("subwindows", {}).get("2023-2026") or {}).get("t"),
                        "verdict": v})
    summary.sort(key=lambda r: -abs(r["t"]) if r["t"] is not None else 0)

    payload = {"ran_at": time.strftime("%Y-%m-%d %H:%M:%S"), "prereg": PREREG,
               "n_events_total": int(len(EV)),
               "sign_map": summary, "classes": results,
               "criteria": "逐事件|t|>=2 且双子窗同号且各自|t|>=1.5 (与问询函研究同源)",
               "note": "普查=符号图谱; 阳性类进收割层须各自另立预注册; 市场调整≠风格调整",
               "elapsed_sec": round(time.time() - t0_, 1)}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"产物: {OUT} ({time.time()-t0_:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
