# -*- coding: utf-8 -*-
"""问询函事件研究 —— `plans/inquiry-family-prereg-20261003.md` §三(判据冻结)的执行体。

事件 = 清洁问询函(冻结正则, 直接 import 勘察模块保证单一来源), 池化事件研究:
主读数 = 市场调整 CAR(21) = Σ_{d=E+1..E+21}(r_stock − r_bench), E = t0 后首个交易日,
严格 PIT(只读 E 收盘之后)。判据冻结: |t|≥2 且双子窗(2019-2022/2023-2026)同号且
各自 |t|≥1.5 ⇒ "效应成立", 否则"本构造下未证实"。只读零写库, 不计 alpha_bank 分母。
产物 experiments/inquiry_event_study_20261003.json。
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

from probe_inquiry_family import (RE_EXCLUDE, RE_INTER, RE_LETTER, RE_REPLY,
                                  to_ts_code)

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "text_corpus" / "cninfo"
OUT = ROOT / "experiments" / "inquiry_event_study_20261003.json"
PREREG = "plans/inquiry-family-prereg-20261003.md"
WINDOWS = (5, 10, 21, 42, 63)
MAX_MISSING = 3
BENCH = "000300.SH"


def scan_letters():
    """冻结正则的清洁问询函事件集(与勘察脚本同一实现)。"""
    files = sorted(CORPUS.glob("*.parquet"))
    parts = []
    for fp in files:
        df = pd.read_parquet(fp, columns=["代码", "公告标题", "公告时间"])
        t = df["公告标题"].fillna("").astype(str)
        m = (t.str.contains(RE_LETTER) & ~t.str.contains(RE_REPLY)
             & ~t.str.contains(RE_INTER) & ~t.str.contains(RE_EXCLUDE))
        if m.any():
            sub = df[m][["代码", "公告时间"]].copy()
            sub["dt"] = pd.to_datetime(sub["公告时间"], format="mixed", errors="coerce")
            parts.append(sub[sub["dt"].notna()])
    L = pd.concat(parts, ignore_index=True)
    L["ts_code"] = L["代码"].astype(str).str.zfill(6).map(to_ts_code)
    L["date"] = L["dt"].dt.normalize()
    ev = (L.dropna(subset=["ts_code"]).groupby(["ts_code", "date"]).size()
           .reset_index(name="n_titles"))
    return ev


def main() -> int:
    t0_ = time.time()
    print("[1/4] 扫描语料清洁问询函…", flush=True)
    ev = scan_letters()
    codes = sorted(ev["ts_code"].unique())
    print(f"    事件 {len(ev)} (去重后) | 涉及 {len(codes)} 只", flush=True)

    conn = sqlite3.connect(f"{(ROOT / 'data' / 'neweco.db').as_uri()}?mode=ro", uri=True)
    print("[2/4] 加载个股日收益(daily.pct_chg 除权昨收口径)…", flush=True)
    frames = []
    for i in range(0, len(codes), 400):
        chunk = codes[i:i + 400]
        ph = ",".join("?" * len(chunk))
        frames.append(pd.read_sql(
            f"SELECT ts_code, trade_date, pct_chg FROM daily WHERE trade_date>='20190101' "
            f"AND ts_code IN ({ph})", conn, params=chunk))
    D = pd.concat(frames, ignore_index=True)
    D["ret"] = D["pct_chg"] / 100.0
    stock_wide = (D.pivot(index="trade_date", columns="ts_code", values="ret")
                    .sort_index())
    stock_wide.index = pd.to_datetime(stock_wide.index, format="%Y%m%d")
    bench = pd.read_sql(
        "SELECT trade_date, close FROM index_daily WHERE ts_code=? AND trade_date>='20190101' "
        "ORDER BY trade_date", conn, params=(BENCH,))
    conn.close()
    bser = pd.Series(bench["close"].values,
                     index=pd.to_datetime(bench["trade_date"], format="%Y%m%d"))
    bret = bser.pct_change().dropna()
    bdays = bret.index                      # 基准交易日历(冻结的 E/窗口定位)

    print("[3/4] 逐事件计算 IR 与 CAR(5/10/21/42/63)…", flush=True)
    bpos = {d: i for i, d in enumerate(bdays)}
    rows, n_drop_e, n_drop_win = [], 0, {k: 0 for k in WINDOWS}
    for _, r in ev.iterrows():
        t0 = r["date"]; code = r["ts_code"]
        i0 = bdays.searchsorted(t0, side="right")     # E = t0 后首个交易日
        if i0 >= len(bdays) - 1:
            n_drop_e += 1
            continue
        E = bdays[i0]
        if code not in stock_wide.columns or pd.isna(stock_wide.at[E, code]):
            n_drop_e += 1                              # E 无个股收盘(停牌/未上市)
            continue
        s = stock_wide[code]
        # 即时反应 IR: (t0, E] 累计收益差; 缺失日个股腿按 0 计(冻结)
        seg = bdays[bdays.searchsorted(t0, side="right"):i0 + 1]
        cum_s = float(np.prod([1.0 + (s.at[d] if pd.notna(s.at[d]) else 0.0)
                               for d in seg]))
        cum_b = float(np.prod([1.0 + bret.at[d] for d in seg]))
        ir_adj = cum_s - cum_b
        # CAR 窗口: d = E+1..E+k(基准日历), 缺失≤3 且窗末日个股有收盘(冻结)
        for k in WINDOWS:
            if i0 + k >= len(bdays):
                n_drop_win[k] += 1
                continue
            win = bdays[i0 + 1:i0 + 1 + k]
            sv = s.reindex(win)
            n_miss = int(sv.isna().sum())
            if n_miss > MAX_MISSING or pd.isna(sv.iloc[-1]):
                n_drop_win[k] += 1
                continue
            sv0 = sv.fillna(0.0)
            bv = bret.reindex(win).fillna(0.0)
            rows.append({"ts_code": code, "t0": t0, "year": t0.year, "k": k,
                         "car_adj": float((1 + sv0).prod() - (1 + bv).prod()),
                         "car_raw": float((1 + sv0).prod() - 1),
                         "n_missing": n_miss, "ir_adj": ir_adj})

    R = pd.DataFrame(rows)
    print(f"    有效事件-窗对 {len(R)} | E 段丢弃 {n_drop_e} | 窗丢弃 {n_drop_win}",
          flush=True)

    def stat(sub, col):
        x = sub[col].to_numpy(dtype=float)
        if len(x) < 30:
            return {"n": int(len(x)), "mean": None, "t": None}
        m = float(x.mean())
        t = m / (float(x.std(ddof=1)) / np.sqrt(len(x)))
        return {"n": int(len(x)), "mean": round(m, 5), "t": round(float(t), 2)}

    print("[4/4] 判决统计…", flush=True)
    readings = {}
    for k in WINDOWS:
        sub = R[R["k"] == k]
        readings[f"CAR{k}_adj"] = stat(sub, "car_adj")
        readings[f"CAR{k}_raw"] = stat(sub, "car_raw")
    readings["IR_adj"] = stat(R[R["k"] == 21], "ir_adj")

    subw = {}
    for lo, hi, tag in ((2019, 2022, "2019-2022"), (2023, 2026, "2023-2026")):
        s21 = R[(R["k"] == 21) & (R["year"] >= lo) & (R["year"] <= hi)]
        subw[tag] = stat(s21, "car_adj")

    prim = readings["CAR21_adj"]
    a, b = subw["2019-2022"], subw["2023-2026"]
    same_sign = (prim["t"] is not None and a["t"] is not None and b["t"] is not None
                 and np.sign(prim["t"]) == np.sign(a["t"]) == np.sign(b["t"]))
    ok = (prim["t"] is not None and abs(prim["t"]) >= 2.0 and same_sign
          and a["t"] is not None and b["t"] is not None
          and abs(a["t"]) >= 1.5 and abs(b["t"]) >= 1.5)
    verdict = "效应成立" if ok else "本构造下未证实"

    payload = {
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "prereg": PREREG,
        "events_deduped": int(len(ev)),
        "event_stock_year": ev.groupby(ev["date"].dt.year)["ts_code"].nunique().to_dict(),
        "dropped": {"no_E_or_no_close_at_E": n_drop_e, "per_window": n_drop_win},
        "readings": readings,
        "primary": "CAR21_adj",
        "subwindows_CAR21_adj": subw,
        "criteria": "|t|>=2 且双子窗同号且各自|t|>=1.5 (预注册 §三冻结)",
        "verdict": verdict,
        "prior_note": "先验=负向(监管注意力/信息披露质量族, 反向家族候选); "
                      "显著为负⇒反向家族第7例登记, 不自动立项剔除器 A/B",
        "elapsed_sec": round(time.time() - t0_, 1),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(json.dumps({"readings": readings, "subwindows": subw, "verdict": verdict},
                     ensure_ascii=False, indent=1))
    print(f"产物: {OUT} ({time.time() - t0_:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
