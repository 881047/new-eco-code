# -*- coding: utf-8 -*-
"""要约收购事件族 Phase 1 事件研究（plans/tender-offer-event-study-prereg-20261002.md，
判据跑前冻结）。只读零写库。

锚点 = 每票首个标题含"要约收购"+"报告书"的公告日（60 自然日去重）；事件臂 = t0+1 开盘
买入（停牌顺延 ≤5 交易日）持有 21 交易日，超额 vs 中证800 同窗。判据 C1/C2/C3 见预注册。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
warnings_off = None  # noqa: 保持与仓库 oneoff 风格一致, 不额外压告警

import numpy as np
import pandas as pd

from app.factors.factor_engine import load_market

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "data" / "text_corpus" / "cninfo"
OUT = ROOT / "experiments" / "tender_offer_event_study_20261002.json"

HORIZONS = (1, 5, 10, 21, 42, 63)
MAIN_N = 21
DEDUP_DAYS = 60
MAX_DEFER = 5
COST = 0.006
C1_MIN_EVENTS = 100
C2_T = 2.0
C3_MIN_NET = 0.010
SUB_SPLIT = pd.Timestamp("2023-01-01")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def to_ts_code(code: str) -> str:
    """语料无后缀 6 位码 → 面板 ts_code(与 field_factor_value_check 同款映射)。"""
    c = str(code).strip()
    if c.endswith((".SZ", ".SH", ".BJ")):
        return c
    return c + (".SH" if c.startswith("6") else ".BJ" if c[0] in "489" else ".SZ")


def load_events() -> pd.DataFrame:
    """标题含"要约收购"+"报告书" → 每票 60 日窗首个 = 事件锚点。"""
    files = sorted(CORPUS.glob("*.parquet"))
    hits = []
    for f in files:
        df = pd.read_parquet(f, columns=["代码", "公告标题", "公告时间"])
        m = df[df["公告标题"].str.contains("要约收购", na=False)
               & df["公告标题"].str.contains("报告书", na=False)]
        if len(m):
            hits.append(m)
    allh = pd.concat(hits, ignore_index=True)
    allh["dt"] = pd.to_datetime(allh["公告时间"], format="mixed")
    allh = allh.sort_values("dt")
    ev, last = [], {}
    for _, r in allh.iterrows():
        k = r["代码"]
        if k not in last or (r["dt"] - last[k]).days > DEDUP_DAYS:
            ev.append({"code": k, "t0": r["dt"], "title": r["公告标题"]})
        last[k] = r["dt"]
    ev = pd.DataFrame(ev)
    # 北交所排除(生产 PIT 域同口径)
    ev = ev[~ev["code"].str.startswith(("4", "8", "9"))].reset_index(drop=True)
    ev["ts_code"] = ev["code"].map(to_ts_code)
    ev["partial"] = ev["title"].str.contains("部分要约", na=False)
    ev["full"] = ev["title"].str.contains("全面要约", na=False)
    return ev


def main() -> int:
    log("加载事件…")
    ev = load_events()
    log(f"事件 {len(ev)} 起(非北交所, 报告书锚点)")

    m = load_market()
    ao = (m["open"] * m["adj"]).astype(float)          # adj 开盘(买入价)
    ac = (m["close"] * m["adj"]).astype(float)
    bench = m["bench"].astype(float)
    td = pd.DatetimeIndex(m["close"].index)
    pos = {d: i for i, d in enumerate(td)}
    bench_pos_val = bench.reindex(td).to_numpy()

    rows = []
    n_defer_drop = 0
    for _, e in ev.iterrows():
        d = pd.Timestamp(e["t0"].normalize())
        i0 = pos.get(d)
        if i0 is None or i0 + 1 >= len(td):
            continue
        c = e["ts_code"]
        if c not in ao.columns:
            continue
        # t0+1 起顺延找可执行开盘(≤5 交易日)
        ib = None
        for k in range(1, MAX_DEFER + 1):
            if i0 + k < len(td):
                if pd.notna(ao.iloc[i0 + k][c]) and ao.iloc[i0 + k][c] > 0:
                    ib = i0 + k
                    break
        if ib is None:
            n_defer_drop += 1
            continue
        row = {"code": c, "t0": d.strftime("%Y%m%d"),
               "buy_date": td[ib].strftime("%Y%m%d"), "partial": bool(e["partial"]),
               "full": bool(e["full"])}
        for N in HORIZONS:
            j = ib + N
            if j >= len(td):
                continue
            r = ac.iloc[j][c] / ao.iloc[ib][c] - 1.0
            b = bench_pos_val[j] / bench_pos_val[ib] - 1.0
            row[f"ex_{N}"] = float(r - b) if pd.notna(r) else None
        rows.append(row)
    dfm = pd.DataFrame(rows)
    log(f"可执行事件 {len(dfm)} 起(顺延无价弃 {n_defer_drop})")

    def batch_stats(col):
        s = dfm[col].dropna()
        if len(s) < 5:
            return None
        mean, sd = float(s.mean()), float(s.std(ddof=1))
        t = mean / sd * (len(s) ** 0.5) if sd > 0 else float("nan")
        return {"n": int(len(s)), "mean": round(mean, 4), "median": round(float(s.median()), 4),
                "t": round(t, 2), "p5": round(float(s.quantile(0.05)), 4),
                "win_rate": round(float((s > 0).mean()), 4)}

    out = {"asof": datetime.now().isoformat(timespec="seconds"),
           "criteria_file": "plans/tender-offer-event-study-prereg-20261002.md",
           "n_events_total": int(len(ev)), "n_events_exec": int(len(dfm)),
           "n_defer_drop": int(n_defer_drop), "horizons": {}}
    for N in HORIZONS:
        out["horizons"][f"CAR_{N}"] = batch_stats(f"ex_{N}")

    main21 = out["horizons"][f"CAR_{MAIN_N}"]
    sub = {}
    for name, mask in (("w1_2019_2022", pd.to_datetime(dfm["t0"]) < SUB_SPLIT),
                       ("w2_2023_2026", pd.to_datetime(dfm["t0"]) >= SUB_SPLIT)):
        s = dfm.loc[mask, f"ex_{MAIN_N}"].dropna()
        if len(s) >= 5:
            sd = float(s.std(ddof=1))
            sub[name] = {"n": int(len(s)), "mean": round(float(s.mean()), 4),
                         "t": round(float(s.mean()) / sd * (len(s) ** 0.5), 2) if sd > 0 else None}
    grp = {}
    for gname, gmask in (("partial", dfm["partial"]), ("full", dfm["full"]),
                         ("unspecified", ~dfm["partial"] & ~dfm["full"])):
        s = dfm.loc[gmask, f"ex_{MAIN_N}"].dropna()
        if len(s) >= 5:
            sd = float(s.std(ddof=1))
            grp[gname] = {"n": int(len(s)), "mean": round(float(s.mean()), 4),
                          "t": round(float(s.mean()) / sd * (len(s) ** 0.5), 2) if sd > 0 else None}

    # 冻结判据
    c1 = len(dfm) >= C1_MIN_EVENTS
    c2 = bool(main21 and main21["mean"] > 0 and main21["t"] >= C2_T)
    c3 = bool(main21 and main21["mean"] - COST >= C3_MIN_NET)
    verdict = "GO" if (c1 and c2 and c3) else "NO-GO"
    out.update({"subwindows": sub, "groups": grp,
                "criteria": {"C1_events_ge_100": c1, "C2_CAR21_pos_t_ge_2": c2,
                             "C3_net_ge_1pct": c3},
                "verdict": verdict,
                "monthly": dfm.to_dict("records")})
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    log(f"判决: {verdict} | C1={c1} C2={c2} C3={c3}")
    for N in HORIZONS:
        s = out["horizons"][f"CAR_{N}"]
        if s:
            log(f"  CAR_{N}: n={s['n']} mean={s['mean']:+.4f} t={s['t']} "
                f"p5={s['p5']:+.4f} win={s['win_rate']:.1%}")
    log(f"子窗: {sub} | 分组: {grp}")
    log(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
