# -*- coding: utf-8 -*-
"""字段因子早期价值判断 —— 用已抽取子集（ef 80% + operating 46%）跑预注册 §二 的
因子构造与既有门禁，回答"剩余 ~49 万条要不要花 ~1,000 元继续抽"。

与 pead_factor_eval_20260928.py 同构（gate.evaluate → summarize → verdict_single 逐字
同款），差异仅在事件值：PEAD 用 valence 计数，这里用 **字段值/总市值 标准化幅度**
（预注册 fulltext-field-factors-prereg-20260930.md §二 冻结口径）：
  - ef_surprise_mv  = (ef_np_min+ef_np_max)/2 ÷ 总市值(万元) —— 盈利预告幅度规模
  - ef_yoy          = ef_yoy_pct/100（原生幅度，winsorize[1%,99%]）
  - oc_amount_mv    = oc_amount_wan ÷ 总市值(万元) —— 合同规模相对公司体量
同(股,日)多公告取 |标准化值| 最大的一条（最显著事件）；窗内 Σ 累计（PEAD 同款）。
**性质：决策支持读数（回填 55%，激活条件 ≥90% 未满足）——非生产门禁。**
只读零写库。产物 experiments/field_factor_value_check.json。
"""
import json
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from app.alpha import critic, gate
from app import db as app_db
from app.factors import factor_engine as fe
from app.factors.factor_engine import load_market, month_end_dates
from app.pit.engine import PITEngine

ROOT = Path(__file__).resolve().parents[1]
LLM = ROOT / "data" / "text_corpus" / "fields_llm_all" / "llm_all.parquet"
OUT = ROOT / "experiments" / "field_factor_value_check.json"
WINDOWS = {"21": 21, "42": 42, "63": 63}
MIN_NAMES = 200


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def to_ts_code(code: str) -> str:
    c = str(code).strip()
    if c.endswith((".SZ", ".SH", ".BJ")):
        return c
    return c + (".SH" if c.startswith("6") else ".BJ" if c[0] in "489" else ".SZ")


def load_field_events() -> pd.DataFrame:
    """抽取长表 → 每文档一行字段值 → 事件表(ts_code, ann_date, field, value)。"""
    df = pd.read_parquet(LLM)
    df = df[df["missing_key"] == False]                       # noqa: E712
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df[df["value"].notna()]
    wide = df.pivot_table(index=["代码", "公告时间", "announcementId", "category"],
                          columns="field", values="value", aggfunc="first").reset_index()
    rows = []
    ef = wide[wide["category"] == "earnings_forecast"]
    ok = ef[ef["ef_np_min_wan"].notna() & ef["ef_np_max_wan"].notna()]
    for _, r in ok.iterrows():
        rows.append((r["代码"], r["公告时间"], "ef_surprise_mv",
                     (float(r["ef_np_min_wan"]) + float(r["ef_np_max_wan"])) / 2.0))
    yoy = ef[ef["ef_yoy_pct"].notna()]
    for _, r in yoy.iterrows():
        rows.append((r["代码"], r["公告时间"], "ef_yoy", float(r["ef_yoy_pct"]) / 100.0))
    oc = wide[wide["category"] == "operating_contract_data"]
    ok = oc[oc["oc_amount_wan"].notna()]
    for _, r in ok.iterrows():
        rows.append((r["代码"], r["公告时间"], "oc_amount_mv", float(r["oc_amount_wan"])))
    ev = pd.DataFrame(rows, columns=["code", "ann", "field", "value"])
    ev["ts_code"] = ev["code"].map(to_ts_code)
    ev["ann"] = pd.to_datetime(ev["ann"], format="mixed").dt.normalize()
    return ev


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
    with app_db.conn_ctx() as conn:
        industry = fe.industry_map(conn)
    code_ix = {c: i for i, c in enumerate(codes)}

    ev = load_field_events()
    ev = ev[ev["ts_code"].isin(code_ix)]
    log(f"字段事件: {len(ev):,} 条 | 字段 {ev['field'].nunique()} | 股票 {ev['ts_code'].nunique()}")

    # 交易日轴 + 顺延锚定(公告日 → 其后首个交易日); 总市值取公告日**前一交易日**
    # (PIT: 公告时点已知的是前一收盘市值)
    td = pd.DatetimeIndex(sorted(m["close"].index))
    td_ix = {d: i for i, d in enumerate(td)}
    mv = m.get("total_mv")
    if mv is None:
        with app_db.conn_ctx() as conn:
            import pandas as pd0
            db_mv = pd0.read_sql("SELECT ts_code, trade_date, total_mv FROM daily_basic "
                                 "WHERE trade_date >= '20180101'", conn)
        mv = (db_mv.assign(trade_date=pd0.to_datetime(db_mv["trade_date"], format="%Y%m%d"))
              .pivot(index="trade_date", columns="ts_code", values="total_mv")
              .reindex(index=td).ffill())
    mv = mv.reindex(columns=codes)
    mv_ix = {c: i for i, c in enumerate(mv.columns)}

    ev["ann_ts"] = ev["ann"]
    ev["ann_i"] = ev["ann"].map(lambda d: td_ix.get(d, np.nan))
    # 顺延: 公告日非交易日/盘后 → 首个 ≥ann 的交易日
    nxt = np.searchsorted(td.values, ev["ann"].values)
    ev["ti"] = np.where(ev["ann_i"].isna(), nxt, np.maximum(nxt, ev["ann_i"]))
    ev = ev[ev["ti"] < len(td)]
    ev["ti"] = ev["ti"].astype(int)
    ev = ev.reset_index(drop=True)           # 过滤后索引标签≠位置, groupby 回填需重置
    # 市值锚: ti 前一交易日的 total_mv(万元)
    mv_vals = np.full(len(ev), np.nan)
    for ts, grp in ev.groupby("ts_code"):
        ci = mv_ix.get(ts)
        if ci is None:
            continue
        col = mv.iloc[:, ci].to_numpy()
        mv_vals[grp.index.to_numpy()] = col[np.maximum(grp["ti"].to_numpy() - 1, 0)]
    ev["mv"] = mv_vals
    ev = ev[ev["mv"] > 0]
    ev["scaled"] = ev["value"] / ev["mv"]
    # winsorize[1%,99%] 按字段(极端规模比会扭曲 head_excess)
    for f in ev["field"].unique():
        s = ev["field"] == f
        lo, hi = ev.loc[s, "scaled"].quantile([0.01, 0.99])
        ev.loc[s, "scaled"] = ev.loc[s, "scaled"].clip(lo, hi)
    log(f"标准化后事件: {len(ev):,} 条 (winsorize[1%,99%] 按字段)")

    cands = {}
    for f in sorted(ev["field"].unique()):
        sub = ev[ev["field"] == f]
        V = np.zeros((len(td), len(codes)), dtype=np.float32)
        np.add.at(V, (sub["ti"].to_numpy(), sub["ts_code"].map(code_ix).to_numpy()),
                  sub["scaled"].to_numpy().astype(np.float32))
        cum = np.cumsum(V, axis=0)
        for wname, W in WINDOWS.items():
            E = np.full((len(md_eval), len(codes)), np.nan, dtype=np.float32)
            for i, d in enumerate(md_eval):
                if d not in td_ix:
                    continue
                k = td_ix[d]
                k0 = k - W
                lo = cum[k0] if k0 >= 0 else np.zeros(len(codes), dtype=np.float32)
                E[i] = cum[k] - lo
            cands[f"{f}_{wname}"] = pd.DataFrame(E, index=md_eval, columns=codes)
    log(f"候选面板 {len(cands)} 个（字段 × 窗）({time.time()-t0:.0f}s)")

    n_test = len(cands)
    rows = []
    for name, panel in cands.items():
        sr = float(fe.sparse_ratio(panel))
        sub = (fe.neutralize_sparse(panel, industry) if sr < fe.SPARSE_NONZERO_RATIO
               else fe.neutralize(panel, industry))
        cost = critic.churn_cost(sub, md_eval)
        pm = sub.where(tradable.reindex(index=md_eval, columns=codes).fillna(False))
        det = gate.evaluate(pm, fwd, md_eval, zones)
        if det.empty:
            rows.append({"factor": name, "note": "无有效截面", "sparse_ratio": round(sr, 4)})
            continue
        rec = gate.summarize(name, det, n_tests=n_test,
                             cost_per_month=(cost or {}).get("cost_per_month"))
        v5 = gate.verdict_single(rec["DEAD_head"], rec["DEAD_head_t"], rec["ACTIVE_head"],
                                 half_life_months=None)
        rows.append({"factor": name, "sparse_ratio": round(sr, 4),
                     "DEAD_head": round(rec["DEAD_head"], 4),
                     "DEAD_head_t": round(rec["DEAD_head_t"], 2),
                     "ACTIVE_head": round(rec["ACTIVE_head"], 4),
                     "p_adj": round(rec["p_adj"], 4) if rec["p_adj"] == rec["p_adj"] else None,
                     "PASS_gate6": bool(rec["PASS"]),
                     "s1_s4_PASS": bool(v5["PASS_single"])})
        log(f"  {name:22s} DEAD {rec['DEAD_head']:+.4f} t {rec['DEAD_head_t']:+.2f} "
            f"gate6 {rec['PASS']} S1-S4 {v5['PASS_single']}")

    n_gate6 = sum(1 for r in rows if r.get("PASS_gate6"))
    n_s14 = sum(1 for r in rows if r.get("s1_s4_PASS"))
    out = {"ran_at": datetime.now().isoformat(timespec="seconds"),
           "nature": "决策支持读数(回填 55% 子集)——非生产门禁(激活条件≥90%未满足)",
           "n_candidates": n_test, "n_events": int(len(ev)),
           "rows": rows,
           "verdict": {"n_pass_gate6": n_gate6, "n_pass_s1s4": n_s14},
           "runtime_sec": round(time.time() - t0, 1)}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"判决读数: 六判据门 {n_gate6}/{n_test} | 单因子档 S1-S4 {n_s14}/{n_test}")
    print(f"产物: {OUT}  ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
