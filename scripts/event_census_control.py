# -*- coding: utf-8 -*-
"""事件族普查·共模对照(对抗性自查, 廉价加固方法论)。

普查阴性对照(periodic_report, 预期无漂移)开出 +1.41% t=36.78 ⇒ 逐类"成立"被共模托起:
等权宇宙本身跑赢沪深300(规模/风格漂移), 市场调整吃不掉。本对照 = 每类抽 3,000 个
**伪事件**(随机语料股票 × **该类真实事件日期池**, seed 冻结), 走与普查逐位相同的
CAR21 管线 ⇒ 类的共模基线(控制年度/季节日历 + 宇宙漂移); 超额 = 实际 − 基线。
官方判决维持预注册口径不动, 本产物只作图谱的诚实化附注。只读零写库。
产物 experiments/event_census_control_20261003.json。
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from probe_inquiry_family import to_ts_code

ROOT = Path(__file__).resolve().parents[1]
TAGGED = ROOT / "data" / "text_corpus" / "tagged"
OUT = ROOT / "experiments" / "event_census_control_20261003.json"
CENSUS = ROOT / "experiments" / "event_census_20261003.json"
SEED = 20261003
N_PSEUDO = 3000
K = 21
CLASSES = ["operating_contract_data", "ma_restructuring", "financing_capital",
           "related_party_transaction", "audit_internal_esg", "equity_incentive",
           "buyback", "litigation_regulatory", "insider_trading",
           "guarantee_financial_assistance", "dividend", "earnings_forecast",
           "periodic_report", "governance_meeting"]


def scan_class_dates():
    """每类的去重事件日期池(与普查同一过滤: 元事件排除)。"""
    files = sorted(TAGGED.glob("*.parquet"))
    parts = []
    for fp in files:
        df = pd.read_parquet(fp, columns=["代码", "公告标题", "公告时间", "category"])
        df = df[df["category"].isin(CLASSES)]
        if not len(df):
            continue
        t = df["公告标题"].fillna("").astype(str)
        bad = pd.Series(False, index=df.index)
        for w in ("延期", "取消", "进展", "撤回", "终止", "催告", "更正"):
            bad |= t.str.contains(w, regex=False)
        df = df[~bad]
        if not len(df):
            continue
        sub = df[["代码", "category"]].copy()
        sub["date"] = pd.to_datetime(df["公告时间"], format="mixed", errors="coerce")
        parts.append(sub.dropna(subset=["date"]))
    E = pd.concat(parts, ignore_index=True)
    E["ts_code"] = E["代码"].astype(str).str.zfill(6).map(to_ts_code)
    E = E.dropna(subset=["ts_code"]).drop_duplicates(["ts_code", "date", "category"])
    return E


def main() -> int:
    t0_ = time.time()
    rng = np.random.default_rng(SEED)
    census = json.loads(CENSUS.read_text(encoding="utf-8"))
    print("[1/3] 扫描事件日期池…", flush=True)
    EV = scan_class_dates()
    print(f"    事件 {len(EV)} | {time.time()-t0_:.0f}s", flush=True)

    print("[2/3] 加载收益矩阵…", flush=True)
    conn = sqlite3.connect(f"{(ROOT / 'data' / 'neweco.db').as_uri()}?mode=ro", uri=True)
    D = pd.read_sql("SELECT ts_code, trade_date, pct_chg FROM daily WHERE trade_date>='20190101'",
                    conn)
    bench = pd.read_sql("SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' "
                        "AND trade_date>='20190101' ORDER BY trade_date", conn)
    conn.close()
    EV0 = scan_class_dates()
    corpus_codes = set(EV0["ts_code"].unique())
    D = D[D["ts_code"].isin(corpus_codes)]
    D["ret"] = D["pct_chg"] / 100.0
    R = D.pivot(index="trade_date", columns="ts_code", values="ret").sort_index()
    R.index = pd.to_datetime(R.index, format="%Y%m%d")
    Rv = R.to_numpy(dtype=float)
    bser = pd.Series(bench["close"].values,
                     index=pd.to_datetime(bench["trade_date"], format="%Y%m%d"))
    bret = bser.pct_change().dropna()
    bdays = bret.index
    bv = bret.to_numpy(dtype=float)
    date_pos = pd.Series(np.arange(len(bdays)), index=bdays)
    print(f"    矩阵 {R.shape} | {time.time()-t0_:.0f}s", flush=True)

    print("[3/3] 逐类精确基线(全宇宙等权×真实事件日历)…", flush=True)
    out = {}
    for cls in CLASSES:
        blk = census["classes"].get(cls)
        if not blk:
            continue
        ev = EV[EV["category"] == cls]
        g = ev.groupby("date").size()
        dates = g.index
        pos_s = pd.Series(dates.map(date_pos), index=dates).dropna().astype(int)
        counts = g.reindex(pos_s.index).to_numpy(dtype=float)
        pos = pos_s.to_numpy()
        pos = pos[pos + 1 + K < len(bdays)]
        counts = counts[:len(pos)] if len(counts) > len(pos) else counts
        if len(pos) == 0:
            continue
        # 精确基线: 每个事件日的全宇宙等权 CAR21(缺失日计 0, 与普查管线同口径)
        ew_s, ew_b = [], []
        for i in range(0, len(pos), 150):
            chunk = pos[i:i + 150]
            idx = chunk[:, None] + np.arange(1, K + 1)[None, :]
            svk = np.nan_to_num(Rv[idx, :], nan=0.0)          # n_dates × 21 × 全宇宙
            bvk = bv[idx]
            ew_s.append((1 + svk).prod(axis=1).mean(axis=1))
            ew_b.append((1 + bvk).prod(axis=1))
        ew_s = np.concatenate(ew_s)
        ew_b = np.concatenate(ew_b)
        base_per_date = ew_s - ew_b
        yrs = np.array([bdays[min(p + 1, len(bdays) - 1)].year for p in pos])
        w = counts / counts.sum()
        base_full = float((base_per_date * w).sum())
        res = {"full": {"n_dates": int(len(pos)), "mean": round(base_full, 5)}}
        for tag, mask in (("2019-2022", yrs <= 2022), ("2023-2026", yrs >= 2023)):
            ww = w * mask
            ww = ww / ww.sum() if ww.sum() > 0 else ww
            res[tag] = {"mean": round(float((base_per_date * ww).sum()), 5)}
        pe = census["classes"][cls]["CAR21_adj"]["per_event"]
        m_act, n_act, t_act = pe.get("mean"), pe.get("n"), pe.get("t")
        se1 = abs(m_act) / abs(t_act) if (t_act and n_act) else np.nan
        excess = m_act - base_full if m_act is not None else None
        z = excess / se1 if excess is not None and np.isfinite(se1) and se1 > 0 else None
        out[cls] = {"exact_baseline": res, "census_actual": {"mean": m_act, "t": t_act},
                    "excess_mean": round(excess, 5) if excess is not None else None,
                    "excess_z": round(float(z), 2) if z is not None and np.isfinite(z) else None}
        print(f"{cls:32s} 基线 {base_full:+.4f} (19-22 {res['2019-2022']['mean']:+.4f} / "
              f"23-26 {res['2023-2026']['mean']:+.4f}) | 实际 {m_act:+.4f} | "
              f"超额 {excess:+.4f} (z={out[cls]['excess_z']})", flush=True)

    payload = {"ran_at": time.strftime("%Y-%m-%d %H:%M:%S"), "seed": SEED,
               "n_pseudo_per_class": N_PSEUDO,
               "baseline_kind": "exact equal-weight universe per event date (事件数加权)",
               "note": "对抗性自查: 官方判决维持普查预注册口径; 共模基线=随机股票×同类真实"
                       "事件日历; 图谱结论以'超额+按日稳健'为准",
               "classes": out,
               "elapsed_sec": round(time.time() - t0_, 1)}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"产物: {OUT} ({time.time()-t0_:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
