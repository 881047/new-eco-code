# -*- coding: utf-8 -*-
"""PEAD 因子族 S5 时效轴补测 —— 三十三补的收尾（预注册 §三 承诺 S1-S5，评估时 S5 未测）。

42 候选面板（14 类 × 漂移窗 21/42/63 交易日，事件强度 E=Σ valence_signed）重建后，
用 `gate.ic_by_lag`（lag = 月锚步 0..5，与月频 S5 同机器）算衰减曲线 →
`gate.half_life_from_curve`（bar = 2.0 月锚步，min_anchors 24）。
只读零写库。产物 experiments/pead_s5_readings_20260928.json。
"""
import json
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from app.alpha import gate
from app.factors import factor_engine as fe
from app.factors.factor_engine import load_market, month_end_dates
from app.pit.engine import PITEngine

ROOT = Path(__file__).resolve().parents[2]
TAGGED = ROOT / "data" / "text_corpus" / "tagged"
OUT = ROOT / "experiments" / "pead_s5_readings_20260928.json"
PREREG = "plans/pead-factor-prereg-20260927.md"
WINDOWS = {"21": 21, "42": 42, "63": 63}
MIN_NAMES = 200


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def to_ts_code(code: str) -> str:
    c = str(code).strip()
    if c.endswith((".SZ", ".SH", ".BJ")):
        return c
    return c + (".SH" if c.startswith("6") else ".BJ" if c[0] in "489" else ".SZ")


def main() -> int:
    t0 = time.time()
    m = load_market()
    md_all = month_end_dates(pd.DatetimeIndex(m["close"].index))
    md_eval = pd.DatetimeIndex([d for d in md_all if d >= pd.Timestamp("2019-01-01")])
    zones = gate.zone_of(md_eval)
    fwd = gate.forward_return(m, gate.HORIZON)
    pit = PITEngine(m=m)
    tradable = pit.tradable_matrix(md_eval)
    codes = list(m["close"].columns)
    with __import__("app.db", fromlist=["conn_ctx"]).conn_ctx() as conn:
        industry = fe.industry_map(conn)

    parts = []
    for p in sorted(TAGGED.glob("*.parquet")):
        try:
            parts.append(pd.read_parquet(p, columns=["代码", "公告时间", "category",
                                                     "valence"]))
        except Exception as e:                                 # noqa: BLE001
            log(f"  跳过损坏文件 {p.name}: {e}")
    ev = pd.concat(parts, ignore_index=True)
    ev = ev.rename(columns={"代码": "ts_code", "公告时间": "t", "category": "cat"})
    ev["ts_code"] = ev["ts_code"].map(to_ts_code)
    ev["t"] = pd.to_datetime(ev["t"], format="mixed").dt.normalize()
    vs = ev["valence"]
    ev["signed"] = np.select([vs == "pos", vs == "neg"], [1.0, -1.0], default=0.0)
    ev = ev[ev["cat"] != "other"]
    ev = ev[ev["ts_code"].isin(set(codes))]
    log(f"事件表 {len(ev):,} 条 | 类 {ev['cat'].nunique()}")

    rows = []
    for cat in sorted(ev["cat"].unique()):
        sub = ev[ev["cat"] == cat]
        V = pd.DataFrame(0.0, index=md_eval, columns=codes)
        grp = sub.groupby(["t", "ts_code"])["signed"].sum()
        for (d, c), v in grp.items():
            if d in V.index and c in V.columns:
                V.loc[d, c] = v
        sr = float(fe.sparse_ratio(V))
        sub_n = (fe.neutralize_sparse(V, industry) if sr < fe.SPARSE_NONZERO_RATIO
                 else fe.neutralize(V, industry))
        for wname in WINDOWS:
            name = f"{cat}_{wname}"
            pm = sub_n.where(tradable.reindex(index=md_eval, columns=codes).fillna(False))
            cur = gate.ic_by_lag(pm, tradable, fwd, md_eval, zones,
                                 lags=(0, 1, 2, 3, 4, 5), zone="DEAD")
            ic_dead = {L: (float(s.mean()) if len(s) else np.nan, len(s))
                       for L, s in cur.items()}
            hl = gate.half_life_from_curve(ic_dead)
            rows.append({"factor": name, "hl_months": hl["half_life_months"],
                         "censored": hl["censored"], "s5": hl["s5_half_life"],
                         "n_min": hl["n_anchors_min"],
                         "ic0": None if ic_dead[0][0] != ic_dead[0][0]
                         else round(ic_dead[0][0], 4)})
        log(f"  {cat} × 3 窗完成 ({time.time()-t0:.0f}s)")

    n_pass = sum(1 for r in rows if r["s5"] is True)
    n_fail = sum(1 for r in rows if r["s5"] is False)
    n_none = sum(1 for r in rows if r["s5"] is None)
    out = {"ran_at": datetime.now().isoformat(timespec="seconds"),
           "prereg": PREREG + "（三十三补·续 S5 补测）",
           "rows": rows,
           "summary": {"n_pass": n_pass, "n_fail": n_fail, "n_none": n_none},
           "runtime_sec": round(time.time() - t0, 1)}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"S5 读数: 通过 {n_pass} / 判否 {n_fail} / 未测 {n_none}（42 候选）", flush=True)
    print(f"产物: {OUT}  ({time.time() - t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
