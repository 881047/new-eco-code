"""index_rebal_event_study_v2_20260928.py — 指数调样卫星层 v2 探针（只读）

预注册：plans/index-rebal-satellite-v2-prereg-20260928.md（判据跑数前冻结，含 §2.3 可交易性过滤）。
在 v1（20260928 PASS：纳入臂 +1.70%、批 t=3.59）之上量化四根"更优利用"杠杆：
  扩指数池(300/905/016/510 半/季) × 退出窗口(W_A/W_B/W_C) × 公告日敏感性带(−18/−14/−10)
  × 选股集中(流量压力代理 top-2) + 账户级 5,000 元预算模拟（真实费率含 5 元最低佣金）。

可交易性：科创板个股（688/689）50 万门槛不可实盘 ⇒ 000688 指数事件只作研究旁注，
R5/纸面轨宇宙 = 300/905/016/510 且逐股剔 688/689。

用法：
    ./.venv/Scripts/python.exe -X utf8 scripts/oneoff/index_rebal_event_study_v2_20260928.py
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

log = logging.getLogger("idx_rebal_v2")
OUT = BASE / "experiments" / "index_rebal_event_study_v2_20260928.json"
SAT_DIR = BASE / "data" / "satellite_rebal"


def _load_v1():
    spec = importlib.util.spec_from_file_location(
        "idx_rebal_v1", HERE / "index_rebal_event_study_20260928.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v1 = _load_v1()
log.info("v1 模块加载完成（复用拉取/统计逻辑）")

# (指数, 定期调整批月, 起拉年, 角色) —— 688 只作研究旁注（50 万门槛不可实盘）
INDICES = (
    ("000300.SH", (6, 12), 2019, "tradeable"),
    ("000905.SH", (6, 12), 2019, "tradeable"),
    ("000016.SH", (6, 12), 2019, "tradeable"),
    ("000510.SH", (3, 6, 9, 12), 2024, "tradeable"),
    ("000688.SH", (3, 6, 9, 12), 2020, "readonly_star"),
)
BAND = (-18, -14, -10)
BUDGET = 5000.0
G3_NET_YEAR = 150.0
SLIP = 0.001


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


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    SAT_DIR.mkdir(parents=True, exist_ok=True)
    pro = v1._pro()

    snaps, memb = {}, {}
    for code, _months, start_year, _role in INDICES:
        cache = SAT_DIR / f"weights_{code.replace('.', '_')}.parquet"
        if cache.exists():
            allsnaps = pd.read_parquet(cache)
            log.info("%s 快照走缓存 %s", code, cache.name)
        else:
            frames = []
            for y in range(start_year, 2027):
                for s, e in ((f"{y}0101", f"{y}0630"), (f"{y}0701", f"{y}1231")):
                    for attempt in range(3):
                        try:
                            df = pro.index_weight(index_code=code, start_date=s, end_date=e)
                            frames.append(df)
                            break
                        except Exception as exc:  # noqa: BLE001
                            log.warning("%s %s 尝试%d: %s", code, s, attempt + 1, str(exc)[:100])
                            time.sleep(2.0)
                    time.sleep(0.35)
            allsnaps = pd.concat(frames, ignore_index=True)
            allsnaps["ym"] = allsnaps["trade_date"].str[:6]
            allsnaps.to_parquet(cache)
        snaps[code] = allsnaps
        memb[code] = v1.membership(allsnaps, code)
        log.info("%s 快照月数 %d", code, len(memb[code]))

    # 交易日历 + 指数腿（库内 300/905；016/688/510 tushare 拉取后缓存到卫星数据目录）
    con = db.get_conn()
    try:
        cal = [r[0] for r in con.execute(
            "SELECT DISTINCT trade_date FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date")]
        idx_close = {}
        for code in ("000300.SH", "000905.SH"):
            idx_close[code] = v1.load_index(code)
    finally:
        con.close()
    for code in ("000016.SH", "000688.SH", "000510.SH"):
        df = pro.index_daily(ts_code=code, start_date="20130101", end_date="20260930")
        idx_close[code] = df.set_index("trade_date")["close"].astype(float).sort_index()
        log.info("%s 日线 %d 行", code, len(df))
        time.sleep(0.35)
    pd.DataFrame({k: v for k, v in idx_close.items()}).to_parquet(SAT_DIR / "index_daily_extra.parquet")
    log.info("指数日线缓存 data/satellite_rebal/index_daily_extra.parquet")

    # 事件构建（快照差分 + 生效日 + 快照日）
    events = []
    for code, eff_months, _sy, role in INDICES:
        m = memb[code]
        ssub = snaps[code]
        for ym in sorted(m):
            month = int(ym[4:])
            if month not in eff_months:
                continue
            ym_prev = prev_month(ym)
            if ym_prev not in m:
                continue
            m_next = m.get(next_month(ym), set())
            ins = {c for c in (m[ym] - m[ym_prev]) if c in m_next}
            outs = {c for c in (m[ym_prev] - m[ym]) if c not in m_next}
            if not ins and not outs:
                continue
            snap_date = ssub[ssub["ym"] == ym]["trade_date"].max()
            eff = second_friday(int(ym[:4]), month) + timedelta(days=1)
            eff_str = next((d for d in cal
                            if datetime.strptime(d, "%Y%m%d").date() >= eff), None)
            if eff_str is None:
                continue
            for direction, codes in (("in", ins), ("out", outs)):
                for c in sorted(codes):
                    wrow = ssub[(ssub["ym"] == ym) & (ssub["con_code"] == c)]["weight"]
                    events.append({"index": code, "role": role, "ym": ym,
                                   "batch": f"{code}_{ym}", "direction": direction,
                                   "ts_code": c, "eff_date": eff_str,
                                   "snap_date": snap_date,
                                   "weight": float(wrow.iloc[0]) if len(wrow) else np.nan})
    ev = pd.DataFrame(events)
    tradeable_ev = ev[(ev["role"] == "tradeable") & ev["ts_code"].map(star_ok)]
    star_cnt = int(len(ev) - len(tradeable_ev))
    log.info("事件总数 %d | 可交易宇宙 %d | STAR/旁注剔除 %d",
             len(ev), len(tradeable_ev), star_cnt)

    # 价格与流通市值
    codes = sorted(tradeable_ev["ts_code"].unique())
    px = v1.load_prices(codes)
    con = db.get_conn()
    try:
        ph = ",".join("?" * len(codes))
        mv = pd.read_sql(
            f"SELECT ts_code, trade_date, circ_mv FROM daily_basic WHERE ts_code IN ({ph})",
            con, params=codes)
    finally:
        con.close()
    mv["circ_mv"] = pd.to_numeric(mv["circ_mv"], errors="coerce")
    mv = mv.pivot_table(index="trade_date", columns="ts_code", values="circ_mv")

    def mcap_at(code: str, snap_date: str) -> float:
        if snap_date in mv.index and code in mv.columns:
            v = mv.at[snap_date, code]
            return float(v) if pd.notna(v) else np.nan
        return np.nan

    def window_car(r: pd.Series, ann_lag: int, end_mode: str):
        eff_dt = datetime.strptime(r["eff_date"], "%Y%m%d").date()
        ann_dt = eff_dt - timedelta(days=ann_lag)
        after = [d for d in cal if datetime.strptime(d, "%Y%m%d").date() > ann_dt]
        if not after:
            return None
        start = after[0]
        if end_mode == "A":
            ends = [d for d in cal if datetime.strptime(d, "%Y%m%d").date() < eff_dt]
        elif end_mode == "B":
            ends = [d for d in cal if datetime.strptime(d, "%Y%m%d").date() <= eff_dt]
        else:  # C：生效后第 5 个交易日
            after_eff = [d for d in cal if datetime.strptime(d, "%Y%m%d").date() >= eff_dt]
            ends = after_eff[5:6]
        if not ends:
            return None
        end = ends[-1]
        try:
            s0, s1 = px.at[start, r["ts_code"]], px.at[end, r["ts_code"]]
            i0, i1 = idx_close[r["index"]].get(start), idx_close[r["index"]].get(end)
            if pd.isna(s0) or pd.isna(s1) or pd.isna(i0) or pd.isna(i1) or s0 <= 0:
                return None
            return (s1 / s0 - 1.0) - (i1 / i0 - 1.0), start, end
        except KeyError:
            return None

    rows = []
    for _, r in tradeable_ev.iterrows():
        for lag in BAND:
            for mode in ("A", "B", "C"):
                if lag != -14 and mode != "A":
                    continue
                got = window_car(r, lag, mode)
                if got is None:
                    continue
                car, start, end = got
                rows.append({**r.to_dict(), "lag": lag, "mode": mode,
                             "car": car, "start": start, "end": end})
    kp = pd.DataFrame(rows)
    base14 = kp[kp["lag"] == -14]
    wa = base14[base14["mode"] == "A"]
    log.info("可测事件（W_A@−14）%d", len(wa))

    res = {"counts": {"events_all": int(len(ev)),
                      "events_tradeable": int(len(tradeable_ev)),
                      "star_or_readonly_dropped": star_cnt,
                      "wa_measured": int(len(wa))}}

    inc = wa[wa["direction"] == "in"].copy()
    inc["bkey"] = inc["batch"]
    inc = inc.sort_values("eff_date")
    r1 = v1.batch_stats(inc["car"], inc["bkey"])
    r1.update({"n": int(len(inc)), "event_mean": float(inc["car"].mean()),
               "event_hac_t": v1.hac_t(inc["car"])})
    res["R1_inclusion_tradeable"] = r1
    log.info("R1 纳入臂 n=%d 均值 %.4f%% 批t=%.2f", r1["n"], 100 * r1["event_mean"], r1["batch_t"])

    r2 = {}
    for mode in ("A", "B", "C"):
        sub = base14[(base14["mode"] == mode) & (base14["direction"] == "in")]
        bs = v1.batch_stats(sub["car"], sub["batch"])
        r2[mode] = {"n": int(len(sub)), "event_mean": float(sub["car"].mean()),
                    "batch_mean": bs["batch_mean"], "batch_t": bs["batch_t"]}
    res["R2_windows"] = r2

    r3 = {}
    for lag in BAND:
        sub = kp[(kp["lag"] == lag) & (kp["mode"] == "A") & (kp["direction"] == "in")]
        bs = v1.batch_stats(sub["car"], sub["batch"])
        r3[str(lag)] = {"n": int(len(sub)), "event_mean": float(sub["car"].mean()),
                        "batch_t": bs["batch_t"]}
    spread = max(x["event_mean"] for x in r3.values()) - min(x["event_mean"] for x in r3.values())
    res["R3_ann_band"] = {"readings": r3, "spread": spread}

    # R4 子组（探索性）：前 20 交易日动量三档 / 流通市值三档 / 分指数
    ann_map = {}
    for _, r in tradeable_ev[tradeable_ev["direction"] == "in"].iterrows():
        eff_dt = datetime.strptime(r["eff_date"], "%Y%m%d").date()
        ann_dt = eff_dt - timedelta(days=14)
        before = [d for d in cal if datetime.strptime(d, "%Y%m%d").date() <= ann_dt]
        if len(before) >= 21:
            w = before[-21:]
            try:
                ann_map[(r["batch"], r["ts_code"])] = float(
                    px.at[w[-1], r["ts_code"]] / px.at[w[0], r["ts_code"]] - 1.0)
            except KeyError:
                pass
    inc["mom20"] = [ann_map.get((b, c), np.nan) for b, c in zip(inc["batch"], inc["ts_code"])]
    inc["mcap"] = [mcap_at(r["ts_code"], r["snap_date"]) for _, r in inc.iterrows()]
    r4 = {"by_index": {}, "by_mom20_tercile": {}, "by_mcap_tercile": {}}
    for code, g in inc.groupby("index"):
        r4["by_index"][code] = {"n": int(len(g)), "event_mean": float(g["car"].mean())}
    try:
        inc["mom_bin"] = pd.qcut(inc["mom20"], 3, labels=["low", "mid", "high"], duplicates="drop")
        for b, g in inc.groupby("mom_bin", observed=True):
            r4["by_mom20_tercile"][str(b)] = {"n": int(len(g)), "event_mean": float(g["car"].mean())}
    except Exception as exc:  # noqa: BLE001
        r4["by_mom20_tercile"] = {"err": str(exc)[:120]}
    try:
        inc["mcap_bin"] = pd.qcut(inc["mcap"], 3, labels=["small", "mid", "big"], duplicates="drop")
        for b, g in inc.groupby("mcap_bin", observed=True):
            r4["by_mcap_tercile"][str(b)] = {"n": int(len(g)), "event_mean": float(g["car"].mean())}
    except Exception as exc:  # noqa: BLE001
        r4["by_mcap_tercile"] = {"err": str(exc)[:120]}
    res["R4_subgroups_exploratory"] = r4

    # R5：账户级模拟（5,000 元/批 × 每规则 top-2 × 冻结退出规则）
    # 主规则 = mcap_asc（流通市值最小 top-2，纸面轨采用）；对照 = weight_proxy（v2 预注册原代理）
    w_a_mean, w_b_mean = r2["A"]["batch_mean"], r2["B"]["batch_mean"]
    exit_mode = "B" if w_b_mean >= w_a_mean - 0.002 else "A"
    exit14 = base14[(base14["mode"] == exit_mode) & (base14["direction"] == "in")]

    def run_rule(rule: str) -> pd.DataFrame:
        rows = []
        for batch, g in exit14.groupby("batch"):
            g = g.copy()
            g["mcap"] = [mcap_at(c, sd) for c, sd in zip(g["ts_code"], g["snap_date"])]
            if rule == "weight_proxy":
                g["key"] = [w / mc if (w == w and mc == mc and mc and mc > 0) else np.nan
                            for w, mc in zip(g["weight"], g["mcap"])]
                g = g.sort_values("key", ascending=False)
            else:  # mcap_asc
                g = g.sort_values("mcap", ascending=False, na_position="last")
            g = g.dropna(subset=["car"]).head(2)
            if g.empty:
                continue
            amt = BUDGET / len(g)
            buy_fee, sell_fee = trade_cost(amt, "buy"), trade_cost(amt, "sell")
            for _, r in g.iterrows():
                net = ((amt - buy_fee) * (1 + r["car"]) * (1 - SLIP) - sell_fee) / amt - 1.0
                rows.append({"batch": batch, "ts_code": r["ts_code"],
                             "year": r["ym"][:4], "car": float(r["car"]), "net": float(net)})
        return pd.DataFrame(rows)

    def summarize(sel: pd.DataFrame) -> dict:
        if sel.empty:
            return {"n_legs": 0, "avg_year_yuan": float("nan")}
        sel = sel.copy()
        sel["yuan"] = sel["net"] * (BUDGET / sel.groupby("batch")["net"].transform("count"))
        by_year = sel.groupby("year")["yuan"].sum()
        return {"n_legs": int(len(sel)), "batches_traded": int(sel["batch"].nunique()),
                "mean_net_per_leg": float(sel["net"].mean()),
                "net_by_year": {k: float(v) for k, v in by_year.items()},
                "avg_year_yuan": float(np.mean(list(by_year.values)))}

    sel_paper = run_rule("mcap_asc")
    sel_proxy = run_rule("weight_proxy")
    res["R5_account_sim"] = {
        "budget_per_batch": BUDGET, "exit_mode": exit_mode,
        "paper_rule": summarize(sel_paper), "weight_proxy_rule": summarize(sel_proxy),
    }
    r5 = res["R5_account_sim"]["paper_rule"]
    avg_year_yuan = r5["avg_year_yuan"]

    g1 = bool(r1.get("batch_t", 0) == r1.get("batch_t", 0) and r1.get("batch_t", 0) >= 2.0
              and r1.get("batch_mean", -9) >= 0.008)
    g2 = bool(spread <= 0.008)
    g3 = bool(avg_year_yuan == avg_year_yuan and avg_year_yuan >= G3_NET_YEAR)
    g4 = bool((w_a_mean > 0) == (w_b_mean > 0))
    verdict = "GO" if (g1 and g2 and g3 and g4) else "NO-GO"
    res["gates"] = {"G1": g1, "G2": g2, "G3": g3, "G4": g4, "verdict": verdict}

    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    log.info("判决 %s | R1 n=%d 均值 %.4f%% 批t=%.2f | W_A %.4f%%/W_B %.4f%%/W_C %.4f%% | "
             "带差 %.4f | 卫星年均 %.0f 元 | 用时 %.0fs",
             verdict, r1["n"], 100 * r1["event_mean"], r1["batch_t"],
             100 * w_a_mean, 100 * w_b_mean, 100 * r2["C"]["batch_mean"],
             100 * spread, avg_year_yuan, time.time() - t0)
    print(json.dumps({"verdict": verdict, "gates": res["gates"], "R1": r1,
                      "R2": r2, "R3": res["R3_ann_band"],
                      "R5": {k: v for k, v in res["R5_account_sim"].items()}},
                     ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
