"""index_rebal_rule_v3_20260928.py — 选股规则 v3（ff_ratio）复活复验（只读）

预注册：plans/freefloat-backfill-rule-v3-prereg-20260928.md（判据跑数前冻结）。
数据源：tushare daily_basic.free_share 月末快照（backfill_freefloat_monthly_20260928.py 落地）。

机制推导（冻结）：自由流通市值加权指数 ⇒ weight/circ_mv = free_share/float_share
⇒ v2 获胜规则（年均 +345 元）的排序在公告时点可算 = 公告前最近月末的 ff_ratio。

判据：V1 覆盖 ≥90% / V2 G3 复验 ff_ratio 年均 ≥+150 元 / V3 与 weight_proxy top-2
Jaccard ≥0.5 / V4 两子窗年均同号。全过 ⇒ 追踪器升级（加记 ff_ratio top-2 全成本虚拟腿）；
任一失败 ⇒ 规则 v3 线结清。

用法：
    ./.venv/Scripts/python.exe -X utf8 scripts/oneoff/index_rebal_rule_v3_20260928.py
"""
import importlib.util
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfg_mod  # noqa: E402,F401
from app import db  # noqa: E402
from app.portfolio.fees import trade_cost  # noqa: E402

log = logging.getLogger("rule_v3")
OUT = BASE / "experiments" / "index_rebal_rule_v3_20260928.json"
SAT_DIR = BASE / "data" / "satellite_rebal"

INDICES = (
    ("000300.SH", (6, 12), 2019),
    ("000905.SH", (6, 12), 2019),
    ("000016.SH", (6, 12), 2019),
    ("000510.SH", (3, 6, 9, 12), 2024),
)
BUDGET = 5000.0
SLIP = 0.001
ANN_LAG = 14


def _load_v1():
    spec = importlib.util.spec_from_file_location(
        "idx_rebal_v1", HERE / "index_rebal_event_study_20260928.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v1 = _load_v1()


def prev_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[4:])
    return f"{y - 1}12" if m == 1 else f"{y}{m - 1:02d}"


def next_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[4:])
    return f"{y + 1}01" if m == 12 else f"{y}{m + 1:02d}"


def second_friday(year: int, month: int) -> date:
    import calendar
    mc = calendar.monthcalendar(year, month)
    fridays = [w[calendar.FRIDAY] for w in mc if w[calendar.FRIDAY] != 0]
    return date(year, month, fridays[1])


def star_ok(code: str) -> bool:
    return not code.startswith(("688", "689"))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()

    memb, snap_w, snap_date_map = {}, {}, {}
    for code, _months, _sy in INDICES:
        cache = SAT_DIR / f"weights_{code.replace('.', '_')}.parquet"
        snaps = pd.read_parquet(cache)
        snaps["ym"] = snaps["trade_date"].str[:6]
        memb[code] = v1.membership(snaps, code)
        snap_w[code] = snaps
        last = snaps.groupby("ym")["trade_date"].max()
        snap_date_map[code] = last.to_dict()
        log.info("%s 快照月数 %d", code, len(memb[code]))

    con = db.get_conn()
    try:
        cal = [r[0] for r in con.execute(
            "SELECT DISTINCT trade_date FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date")]
        idx_close = {}
        for code in ("000300.SH", "000905.SH"):
            idx_close[code] = v1.load_index(code)
    finally:
        con.close()
    extra = pd.read_parquet(SAT_DIR / "index_daily_extra.parquet")
    for code in ("000016.SH", "000510.SH"):
        idx_close[code] = extra[code].dropna()

    events = []
    for code, eff_months, _sy in INDICES:
        m = memb[code]
        for ym in sorted(m):
            month = int(ym[4:])
            if month not in eff_months:
                continue
            ym_prev = prev_month(ym)
            if ym_prev not in m:
                continue
            m_next = m.get(next_month(ym), set())
            ins = {c for c in (m[ym] - m[ym_prev]) if c in m_next and star_ok(c)}
            if not ins:
                continue
            sd = snap_date_map[code][ym]
            eff = second_friday(int(ym[:4]), month) + timedelta(days=1)
            eff_str = next((d for d in cal
                            if datetime.strptime(d, "%Y%m%d").date() >= eff), None)
            if eff_str is None:
                continue
            for c in sorted(ins):
                wrow = snap_w[code][(snap_w[code]["ym"] == ym)
                                    & (snap_w[code]["con_code"] == c)]["weight"]
                events.append({"index": code, "ym": ym, "batch": f"{code}_{ym}",
                               "ts_code": c, "eff_date": eff_str, "snap_date": sd,
                               "weight": float(wrow.iloc[0]) if len(wrow) else np.nan})
    ev = pd.DataFrame(events)
    log.info("纳入事件（可交易宇宙）%d", len(ev))

    codes = sorted(ev["ts_code"].unique())
    px = v1.load_prices(codes)
    con = db.get_conn()
    try:
        ph = ",".join("?" * len(codes))
        mvdf = pd.read_sql(
            f"SELECT ts_code, trade_date, circ_mv FROM daily_basic WHERE ts_code IN ({ph})",
            con, params=codes)
    finally:
        con.close()
    mvdf["circ_mv"] = pd.to_numeric(mvdf["circ_mv"], errors="coerce")
    mv = mvdf.pivot_table(index="trade_date", columns="ts_code", values="circ_mv")
    ff = pd.read_parquet(SAT_DIR / "freefloat_monthly.parquet")
    ff["ff_ratio"] = ff["free_share"] / ff["float_share"].where(ff["float_share"] > 0)
    ok = (ff["free_share"].notna() & (ff["float_share"] > 0)
          & (ff["free_share"] <= ff["float_share"]))
    ff_ok = ff[ok].pivot_table(index="trade_date", columns="ts_code", values="ff_ratio")

    # W_B 窗口 CAR（entry=公告后首日收盘，exit=生效日收盘；与 v2 逐字同口径）
    rows = []
    for _, r in ev.iterrows():
        eff_dt = datetime.strptime(r["eff_date"], "%Y%m%d").date()
        ann_dt = eff_dt - timedelta(days=ANN_LAG)
        after = [d for d in cal if datetime.strptime(d, "%Y%m%d").date() > ann_dt]
        ends = [d for d in cal if datetime.strptime(d, "%Y%m%d").date() <= eff_dt]
        if not after or not ends:
            continue
        start, end = after[0], ends[-1]
        try:
            s0, s1 = px.at[start, r["ts_code"]], px.at[end, r["ts_code"]]
            i0, i1 = idx_close[r["index"]].get(start), idx_close[r["index"]].get(end)
            if pd.isna(s0) or pd.isna(s1) or pd.isna(i0) or pd.isna(i1) or s0 <= 0:
                continue
            prev_ym = prev_month(r["ym"])
            ff_td = [d for d in ff_ok.index if d.startswith(prev_ym)]
            ffr = np.nan
            if ff_td:
                last_td = ff_td[-1]
                if r["ts_code"] in ff_ok.columns:
                    v = ff_ok.at[last_td, r["ts_code"]]
                    ffr = float(v) if pd.notna(v) else np.nan
            mvv = np.nan
            if r["snap_date"] in mv.index and r["ts_code"] in mv.columns:
                mvv = mv.at[r["snap_date"], r["ts_code"]]
            rows.append({**r.to_dict(), "car": (s1 / s0 - 1.0) - (i1 / i0 - 1.0),
                         "start": start, "end": end, "ff_ratio": ffr,
                         "prev_snap": ff_td[-1] if ff_td else "",
                         "mcap": float(mvv) if pd.notna(mvv) else np.nan})
        except KeyError:
            continue
    kp = pd.DataFrame(rows)
    log.info("可测事件 %d", len(kp))

    def run_rule(rule: str) -> pd.DataFrame:
        picks = []
        for batch, g in kp.groupby("batch"):
            g = g.copy()
            if rule == "ff_ratio":
                g["key"] = g["ff_ratio"]
                g = g.sort_values("key", ascending=False, na_position="last")
            elif rule == "weight_proxy":
                g["key"] = [w / mc if (w == w and mc == mc and mc and mc > 0) else np.nan
                            for w, mc in zip(g["weight"], g["mcap"])]
                g = g.sort_values("key", ascending=False, na_position="last")
            else:  # mcap_asc
                g["key"] = g["mcap"]
                g = g.sort_values("key", ascending=True, na_position="last")
            g = g.dropna(subset=["car"]).head(2)
            if g.empty:
                continue
            amt = BUDGET / len(g)
            buy_fee, sell_fee = trade_cost(amt, "buy"), trade_cost(amt, "sell")
            for _, r in g.iterrows():
                net = ((amt - buy_fee) * (1 + r["car"]) * (1 - SLIP) - sell_fee) / amt - 1.0
                picks.append({"batch": batch, "ym": r["ym"], "ts_code": r["ts_code"],
                              "year": r["ym"][:4], "car": float(r["car"]), "net": float(net)})
        return pd.DataFrame(picks)

    def summarize(sel: pd.DataFrame) -> dict:
        if sel.empty:
            return {"n_legs": 0, "avg_year_yuan": float("nan")}
        sel = sel.copy()
        sel["yuan"] = sel["net"] * (BUDGET / sel.groupby("batch")["net"].transform("count"))
        by_year = sel.groupby("year")["yuan"].sum()
        sw = {}
        for tag, cond in (("2019-2022", sel["year"].astype(int) <= 2022),
                          ("2023-2026", sel["year"].astype(int) >= 2023)):
            s = sel[cond]
            sw[tag] = round(float(s["yuan"].sum() / s["year"].nunique()), 1) if len(s) else None
        return {"n_legs": int(len(sel)), "batches": int(sel["batch"].nunique()),
                "mean_net_per_leg": round(float(sel["net"].mean()), 5),
                "net_by_year": {k: round(float(v), 1) for k, v in by_year.items()},
                "subwin_year_yuan": sw,
                "avg_year_yuan": round(float(np.mean(list(by_year.values))), 1)}

    sel_by_rule = {r: run_rule(r) for r in ("ff_ratio", "weight_proxy", "mcap_asc")}
    rules = {r: summarize(s) for r, s in sel_by_rule.items()}

    b_ff = set(map(tuple, sel_by_rule["ff_ratio"][["batch", "ts_code"]].values))
    b_wp = set(map(tuple, sel_by_rule["weight_proxy"][["batch", "ts_code"]].values))
    jac = len(b_ff & b_wp) / len(b_ff | b_wp) if (b_ff or b_wp) else 0.0

    cov = float(kp["ff_ratio"].notna().mean()) if len(kp) else 0.0
    r3 = rules["ff_ratio"]
    v1g = bool(cov >= 0.90)
    v2g = bool(r3["avg_year_yuan"] == r3["avg_year_yuan"] and r3["avg_year_yuan"] >= 150.0)
    v3g = bool(jac >= 0.5)
    swv = [v for v in r3["subwin_year_yuan"].values() if v is not None]
    v4g = bool(len(swv) == 2 and (swv[0] > 0) == (swv[1] > 0))
    verdict = "GO" if (v1g and v2g and v3g and v4g) else "NO-GO"

    res = {
        "prereg": "plans/freefloat-backfill-rule-v3-prereg-20260928.md",
        "n_events": int(len(kp)),
        "V1_coverage": {"ff_ratio_computable": round(cov, 4), "gate": v1g},
        "rules": rules,
        "V3_jaccard_ff_vs_weightproxy": round(jac, 4),
        "gates": {"V1": v1g, "V2": v2g, "V3": v3g, "V4": v4g, "verdict": verdict},
    }
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    log.info("判决 %s | 覆盖 %.3f | ff 年均 %.0f 元 | Jaccard %.3f | 用时 %.0fs",
             verdict, cov, r3["avg_year_yuan"], jac, time.time() - t0)
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
