"""index_rebal_event_study_20260928.py — 指数调样事件流探针（只读）

预注册：plans/index-rebal-event-study-prereg-20260928.md（判据跑数前冻结）。
动机：全部 13 免费信息族 + 文本/异构族组合层结清后，唯一机制正交（被动资金流、非信息）
的候选增量；月频盘 avg_cash_idle≈0.313（20260928_exe）的闲置现金是天然卫星层座位。

数据：
- 事件重建：tushare `index_weight` 月末快照（000300.SH / 000905.SH，2019-01~2026-09），
  5月→6月、11月→12月差分 = 半年度定期调样；6月末→7月末（12月末→次年1月末）回检
  剔除临时调整（进后即出 / 出后即回）。
- 生效日 = 该月第二个周五的次一交易日（中证规则）；公告日近似 = 生效日 − 14 日历日。
- 价格：库内 `daily.close × adj_factor`（全收益，跨分红季无污染）；指数腿 = 目标指数
  `index_daily.close`。窗口 = [公告日后首个交易日, 生效日前最后一交易日] 收盘→收盘。

判据（冻结，详见预注册 §3）：
- V1 纳入臂：批级均值 CAR ≥ +0.8% 且批级 t ≥ 2，N ≥ 100（批 = 指数×年×批，事件级 HAC t 仅参考）。
- V2 调出臂：批级均值 ≤ −0.4% 且 t ≤ −2。
- V3 稳健臂：2019-2022 / 2023-2026 两子窗批级均值同号。
- V4 可成交臂：事件级均值 − 0.3%（往返成本）≥ +0.5%。
- PASS = V1∧V3∧V4 且 V2 同号；REJECT = V1 批级 t<2 或 CAR<0；其余 INDETERMINATE。

用法：
    ./.venv/Scripts/python.exe -X utf8 scripts/oneoff/index_rebal_event_study_20260928.py
"""
import calendar
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

from app import config as cfg_mod  # noqa: E402,F401  (触发 load_dotenv)
from app import db  # noqa: E402

log = logging.getLogger("idx_rebal")
OUT = BASE / "experiments" / "index_rebal_event_study_20260928.json"

INDICES = ("000300.SH", "000905.SH")
START_YM = (2019, 1)
ANN_LAG_DAYS = 14          # 公告日近似 = 生效日 − 14 日历日（预注册 §2）
COST_RT = 0.003            # V4 往返成本（佣金 0.026% + 印花 0.05% + 滑点 0.1%×2）
V1_CAR, V1_T, V1_N = 0.008, 2.0, 100
V2_CAR, V2_T = -0.004, -2.0
V4_NET = 0.005


def _pro():
    import os
    import tushare as ts
    token = os.environ.get("TUSHARE_TOKEN") or os.environ.get("TUSHARE_KEY")
    if not token:
        raise SystemExit("缺 TUSHARE_TOKEN（.env）")
    return ts.pro_api(token)


def fetch_snapshots(pro, index_code: str) -> pd.DataFrame:
    """按半年分片拉月度权重快照（每月一行/票，月末 trade_date）。"""
    frames = []
    spans = []
    for y in range(2019, 2027):
        spans.append((f"{y}0101", f"{y}0630"))
        spans.append((f"{y}0701", f"{y}1231"))
    for s, e in spans:
        for attempt in range(3):
            try:
                df = pro.index_weight(index_code=index_code, start_date=s, end_date=e)
                frames.append(df)
                log.info("%s %s~%s rows=%d", index_code, s, e, len(df))
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("%s %s~%s 尝试%d 失败: %s", index_code, s, e, attempt + 1, str(exc)[:120])
                time.sleep(2.0)
        else:
            raise SystemExit(f"index_weight 拉取失败 {index_code} {s}~{e}")
        time.sleep(0.4)
    out = pd.concat(frames, ignore_index=True)
    out["ym"] = out["trade_date"].str[:6]
    return out


def membership(snaps: pd.DataFrame, index_code: str) -> dict:
    sub = snaps[snaps["index_code"] == index_code]
    # 每月只取最后一份快照（若上游同月有多份，取并集会污染成员差分）
    last = sub.groupby("ym")["trade_date"].transform("max")
    sub = sub[sub["trade_date"] == last]
    return {ym: set(g["con_code"]) for ym, g in sub.groupby("ym")}


def second_friday_next_td(year: int, month: int, cal_days: list) -> str:
    """该月第二个周五的次一交易日（中证半年度生效日规则）。"""
    mc = calendar.monthcalendar(year, month)
    fridays = [w[calendar.FRIDAY] for w in mc if w[calendar.FRIDAY] != 0]
    second = date(year, month, fridays[1])
    ann = second + timedelta(days=1)
    for d in cal_days:
        dd = datetime.strptime(d, "%Y%m%d").date()
        if dd >= ann:
            return d
    raise SystemExit(f"日历里找不到 {year}-{month:02d} 生效日")


def month_end_membership_or_none(memb: dict, ym: str):
    return memb.get(ym)


def load_prices(codes: list) -> pd.DataFrame:
    """event 股票池的 adj close（close×adj_factor），宽表。"""
    con = db.get_conn()
    try:
        ph = ",".join("?" * len(codes))
        px = pd.read_sql(
            f"SELECT ts_code, trade_date, close FROM daily WHERE ts_code IN ({ph})", con,
            params=codes)
        ad = pd.read_sql(
            f"SELECT ts_code, trade_date, adj_factor FROM adj_factor WHERE ts_code IN ({ph})",
            con, params=codes)
    finally:
        con.close()
    if px.empty:
        raise SystemExit("daily 无行情")
    px = px.merge(ad, on=["ts_code", "trade_date"], how="left")
    px["adj_factor"] = pd.to_numeric(px["adj_factor"], errors="coerce").fillna(1.0)
    px["close"] = pd.to_numeric(px["close"], errors="coerce")
    px = px.dropna(subset=["close"])
    px["aclose"] = px["close"] * px["adj_factor"]
    return px.pivot_table(index="trade_date", columns="ts_code", values="aclose")


def load_index(index_code: str) -> pd.Series:
    con = db.get_conn()
    try:
        df = pd.read_sql("SELECT trade_date, close FROM index_daily WHERE ts_code=?",
                         con, params=(index_code,))
    finally:
        con.close()
    s = df.set_index("trade_date")["close"].astype(float).sort_index()
    return s


def hac_t(series: pd.Series, lag: int = 5) -> float:
    """事件级 t（Newey-West，按生效日排序）。"""
    x = series.dropna().to_numpy()
    n = len(x)
    if n < 3:
        return float("nan")
    e = x - x.mean()
    s0 = float(np.sum(e * e)) / n
    for l in range(1, lag + 1):
        if n <= l:
            break
        gamma = float(np.sum(e[l:] * e[:-l])) / n
        s0 += 2.0 * (1.0 - l / (lag + 1.0)) * gamma
    se = (s0 / n) ** 0.5
    return float(x.mean() / se) if se > 0 else float("nan")


def batch_stats(car: pd.Series, keys: pd.Series) -> dict:
    """批级均值与 t（批 = keys 的每个取值）。"""
    df = pd.DataFrame({"car": car, "k": keys}).dropna()
    bm = df.groupby("k")["car"].mean()
    n_b = len(bm)
    if n_b < 2:
        return {"n_batch": n_b, "batch_mean": float(bm.mean()) if n_b else float("nan"),
                "batch_t": float("nan")}
    t = float(bm.mean() / (bm.std(ddof=1) / (n_b ** 0.5)))
    return {"n_batch": n_b, "batch_mean": float(bm.mean()), "batch_t": t,
            "batch_means": {str(k): round(float(v), 5) for k, v in bm.items()}}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    pro = _pro()

    snaps = {}
    for idx in INDICES:
        snaps[idx] = fetch_snapshots(pro, idx)
    memb = {idx: membership(snaps[idx], idx) for idx in INDICES}

    con = db.get_conn()
    try:
        cal = [r[0] for r in con.execute(
            "SELECT DISTINCT trade_date FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date")]
    finally:
        con.close()
    idx_px = {idx: load_index(idx) for idx in INDICES}

    events = []
    counts = {"temporary_dropped": 0, "no_price_dropped": 0, "batches": 0}
    for idx in INDICES:
        m = memb[idx]
        for year in range(2019, 2027):
            for base_m, eff_m, next_m in ((5, 6, 7), (11, 12, 1)):
                ym_base, ym_eff = f"{year}{base_m:02d}", f"{year}{eff_m:02d}"
                ny = year + 1 if eff_m == 12 else year
                ym_next = f"{ny}{next_m:02d}"
                if ym_base not in m or ym_eff not in m:
                    continue
                if ym_next not in m and ym_eff == f"{year}12":
                    counts["no_next_snapshot"] = counts.get("no_next_snapshot", 0) + 1
                    continue
                m_next = m.get(ym_next, set())
                ins = m[ym_eff] - m[ym_base]
                outs = m[ym_base] - m[ym_eff]
                ins_ok = {c for c in ins if c in m_next}       # 进后仍在 = 定期调样
                outs_ok = {c for c in outs if c not in m_next}  # 出后不回 = 定期调样
                counts["temporary_dropped"] += (len(ins) - len(ins_ok)) + (len(outs) - len(outs_ok))
                if not ins_ok and not outs_ok:
                    continue
                eff = second_friday_next_td(year, eff_m, cal)
                eff_dt = datetime.strptime(eff, "%Y%m%d").date()
                ann_dt = eff_dt - timedelta(days=ANN_LAG_DAYS)
                after_ann = [d for d in cal if datetime.strptime(d, "%Y%m%d").date() > ann_dt]
                before_eff = [d for d in cal if datetime.strptime(d, "%Y%m%d").date() < eff_dt]
                if not after_ann or not before_eff:
                    continue
                start, end = after_ann[0], before_eff[-1]
                counts["batches"] += 1
                for direction, codes in (("in", ins_ok), ("out", outs_ok)):
                    for c in sorted(codes):
                        events.append({"index": idx, "year": year, "batch": f"{year}{eff_m:02d}",
                                       "direction": direction, "ts_code": c,
                                       "ann_date": ann_dt.strftime("%Y%m%d"),
                                       "eff_date": eff, "start": start, "end": end})

    ev = pd.DataFrame(events)
    log.info("事件总数 %d（临时调整剔除 %d）", len(ev), counts["temporary_dropped"])
    codes = sorted(ev["ts_code"].unique())
    px = load_prices(codes)

    cars, kept = [], []
    for _, r in ev.iterrows():
        try:
            s0, s1 = px.at[r["start"], r["ts_code"]], px.at[r["end"], r["ts_code"]]
            i0 = idx_px[r["index"]].get(r["start"])
            i1 = idx_px[r["index"]].get(r["end"])
            if pd.isna(s0) or pd.isna(s1) or pd.isna(i0) or pd.isna(i1) or s0 <= 0:
                counts["no_price_dropped"] += 1
                continue
            car = (s1 / s0 - 1.0) - (i1 / i0 - 1.0)
            cars.append(car)
            kept.append(r)
        except KeyError:
            counts["no_price_dropped"] += 1
    kp = pd.DataFrame(kept)
    kp["car"] = cars
    kp["bkey"] = kp["index"] + "_" + kp["batch"]
    kp["subwin"] = np.where(kp["year"] <= 2022, "2019-2022", "2023-2026")
    log.info("可测事件 %d（缺价剔除 %d）", len(kp), counts["no_price_dropped"])

    res = {"prereg": "plans/index-rebal-event-study-prereg-20260928.md",
           "counts": counts, "n_events_kept": int(len(kp))}
    verdicts = {}

    for direction, tag in (("in", "inclusion"), ("out", "deletion")):
        sub = kp[kp["direction"] == direction]
        if sub.empty:
            verdicts[tag] = {"n": 0}
            continue
        bs = batch_stats(sub["car"], sub["bkey"])
        read = {"n": int(len(sub)),
                "event_mean": float(sub["car"].mean()),
                "event_hac_t": hac_t(sub.sort_values("eff_date")["car"]),
                **bs}
        for sw in ("2019-2022", "2023-2026"):
            s2 = sub[sub["subwin"] == sw]
            read[sw] = batch_stats(s2["car"], s2["bkey"])
        verdicts[tag] = read

    inc, dele = verdicts.get("inclusion", {}), verdicts.get("deletion", {})
    v1 = bool(inc.get("n", 0) >= V1_N and inc.get("batch_mean", -9) >= V1_CAR
              and inc.get("batch_t") is not None and inc.get("batch_t", 0) >= V1_T)
    v2 = bool(dele.get("batch_mean", 9) <= V2_CAR and (dele.get("batch_t") or 0) <= V2_T)
    sw1 = inc.get("2019-2022", {}).get("batch_mean")
    sw2 = inc.get("2023-2026", {}).get("batch_mean")
    v3 = bool(sw1 is not None and sw2 is not None and sw1 > 0 and sw2 > 0)
    v4 = bool(inc.get("event_mean", -9) - COST_RT >= V4_NET)
    same_sign_v2 = bool(dele.get("batch_mean", 9) < 0)
    if v1 and v3 and v4 and same_sign_v2:
        verdict = "PASS"
    elif not v1 or inc.get("batch_mean", -9) < 0:
        verdict = "REJECT"
    else:
        verdict = "INDETERMINATE"
    verdicts["gates"] = {"V1": v1, "V2": v2, "V3": v3, "V4": v4, "V2_same_sign": same_sign_v2}
    verdicts["verdict"] = verdict
    res["readings"] = verdicts

    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    log.info("判决 %s | 纳入均值 %.4f%% 批t=%.2f | 调出均值 %.4f%% | 用时 %.0fs",
             verdict, 100 * inc.get("event_mean", float("nan")),
             inc.get("batch_t", float("nan")), 100 * dele.get("event_mean", float("nan")),
             time.time() - t0)
    print(json.dumps({"verdict": verdict, "gates": verdicts["gates"],
                      "inclusion": {k: v for k, v in inc.items() if k != "batch_means"},
                      "deletion": {k: v for k, v in dele.items() if k != "batch_means"}},
                     ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
