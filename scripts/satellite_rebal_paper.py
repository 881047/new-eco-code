"""satellite_rebal_paper.py — 指数调样事件流·全纳入群体追踪器（纸面，零资金）

预注册：plans/index-rebal-satellite-v2-prereg-20260928.md（v2 判决：edge GO 级
[纳入臂批 t=2.22、公告日带差 0.18pp]，但 5,000 元卫星轨在冻结选股规则下 NO-GO
[mcap_asc 年均 −623 元 vs weight_proxy +345 元 ⇒ 2 名额/批的收割层被选股噪声主导]）。

本追踪器是 v2 判决的合规实践形态：**不做选股、不动资金**——把每批"全部纳入候选"
做成等权群体纸面序列（group CAR），攒 OOS 样本供未来拍板（S14"攒纸面样本"逻辑）。
与 paper_* 表、weekly 账本、月频回测完全隔离（自己的 ledger）。

数据面（全部只读）：
- 候选名单：`data/satellite_rebal/batch_<YYYYMM>_candidates.json`——由人在公告落地后
  从中证指数官网调样公告抄录（程序不猜名单；v2 探针证明月末快照差分对入场时点太晚）。
  文件格式：{"index": "000905.SH", "batch": "202612", "ann_date": "20261128"（真公告日）,
            "eff_date": "20261214"（生效日，可空=按第二周五次一交易日推算）,
            "candidates": [{"ts_code": "..."}, ...]}
- 行情：库内 daily×adj_factor（收盘价，复权）+ index_daily（指数腿）。
- 规则（预注册 §2.4 冻结）：入场=公告后首个交易日收盘；退出=生效日前最后一交易日收盘；
  群体读数=等权 CAR（个股收益−目标指数收益），**不计费用、不模拟整手**（零资金纸面）。

模式：
    --scan            现状：下一批日历 / 等待中的名单 / 在途批次
    --run             处理到期动作（开仓登记 / 到期结算），幂等
    --mark            在途批次逐日盯市（写当日群体净值）
    --verify          台账完整性自检（恒等式 + 孤儿批次）

用法：
    ./.venv/Scripts/python.exe -X utf8 scripts/satellite_rebal_paper.py --scan
"""
import argparse
import calendar
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfg_mod  # noqa: E402,F401
from app import db  # noqa: E402

log = logging.getLogger("sat_rebal")
SAT_DIR = BASE / "data" / "satellite_rebal"
LEDGER = SAT_DIR / "ledger.json"

# 定期调整批月与生效日规则（v2 预注册 §1；真公告日以 candidates.json 为准）
BATCH_MONTHS = {"000300.SH": (6, 12), "000905.SH": (6, 12),
                "000016.SH": (6, 12), "000510.SH": (3, 6, 9, 12)}


def second_friday_next_td(year: int, month: int, cal_days: list) -> str:
    """该月第二个周五的次一交易日（中证规则）。

    未来日期不在历史日历里（index_daily 只到今天）⇒ 找不到日历命中时按工作日
    外推（批月 3/6/9/12 的中旬无长假，误差风险在真公告日核验时消除）。"""
    mc = calendar.monthcalendar(year, month)
    fridays = [w[calendar.FRIDAY] for w in mc if w[calendar.FRIDAY] != 0]
    target = date(year, month, fridays[1]) + timedelta(days=1)
    for d in cal_days:
        if datetime.strptime(d, "%Y%m%d").date() >= target:
            got = datetime.strptime(d, "%Y%m%d").date()
            if (got.year, got.month) != (year, month):
                break  # 日历稀疏/缺段：跨月命中视为未命中，走外推
            return d
    d = target
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%Y%m%d")


def next_batches(today: date, cal_days: list, n: int = 4) -> list:
    """未来 n 个定期调整批（按 (年,月) 逐月前扫，直到凑满；上限 15 个月）。"""
    out, seen = [], set()
    for offset in range(0, 16):
        y, m = today.year, today.month
        mm = m + offset
        y += (mm - 1) // 12
        mm = (mm - 1) % 12 + 1
        for idx, months in BATCH_MONTHS.items():
            if mm in months:
                key = (y, mm, idx)
                if key in seen:
                    continue
                eff = second_friday_next_td(y, mm, cal_days)
                if eff and eff >= today.strftime("%Y%m%d"):
                    seen.add(key)
                    out.append({"y": y, "m": mm, "eff_date": eff, "index": idx})
                    if len(out) >= n:
                        return out
    return out


def load_cal() -> list:
    con = db.get_conn()
    try:
        return [r[0] for r in con.execute(
            "SELECT DISTINCT trade_date FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date")]
    finally:
        con.close()


def load_close_pivot(codes: list) -> pd.DataFrame:
    con = db.get_conn()
    try:
        ph = ",".join("?" * len(codes))
        px = pd.read_sql(f"SELECT ts_code, trade_date, close FROM daily WHERE ts_code IN ({ph})",
                         con, params=codes)
        ad = pd.read_sql(f"SELECT ts_code, trade_date, adj_factor FROM adj_factor "
                         f"WHERE ts_code IN ({ph})", con, params=codes)
        idx = pd.read_sql("SELECT ts_code, trade_date, close FROM index_daily "
                          "WHERE ts_code IN ('000300.SH','000905.SH','000016.SH','000510.SH')", con)
    finally:
        con.close()
    px = px.merge(ad, on=["ts_code", "trade_date"], how="left")
    px["aclose"] = px["close"].astype(float) * pd.to_numeric(px["adj_factor"], errors="coerce").fillna(1.0)
    stock_pv = px.pivot_table(index="trade_date", columns="ts_code", values="aclose")
    idx_pv = idx.pivot_table(index="trade_date", columns="ts_code", values="close").astype(float)
    return stock_pv, idx_pv


def first_td_after(cal_days: list, d: date) -> str:
    for td in cal_days:
        if datetime.strptime(td, "%Y%m%d").date() > d:
            return td
    return ""


def last_td_before(cal_days: list, d: date) -> str:
    out = ""
    for td in cal_days:
        if datetime.strptime(td, "%Y%m%d").date() < d:
            out = td
        else:
            break
    return out


def load_ledger() -> dict:
    if LEDGER.exists():
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    return {"batches": {}}


def save_ledger(lg: dict) -> None:
    SAT_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(lg, ensure_ascii=False, indent=2), encoding="utf-8")


def load_candidate_file(ym: str) -> dict | None:
    p = SAT_DIR / f"batch_{ym}_candidates.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def open_batch(lg: dict, ym: str, meta: dict, cal_days: list, stock_pv: pd.DataFrame,
               idx_pv: pd.DataFrame, today: str | None = None) -> bool:
    """公告后首个交易日收盘登记全部候选（幂等：已开批次跳过）。"""
    if ym in lg["batches"]:
        return False
    ann = datetime.strptime(meta["ann_date"], "%Y%m%d").date()
    entry_td = first_td_after(cal_days, ann)
    today_str = today or date.today().strftime("%Y%m%d")
    if not entry_td or entry_td > today_str:
        return False  # 入场日未到
    codes = [c["ts_code"] for c in meta["candidates"]
             if not c["ts_code"].startswith(("688", "689"))]
    skipped_star = len(meta["candidates"]) - len(codes)
    legs = {}
    for c in codes:
        try:
            px0 = stock_pv.at[entry_td, c]
        except KeyError:
            px0 = float("nan")
        if pd.isna(px0):
            legs[c] = {"entry": None, "status": "no_price"}
            continue
        legs[c] = {"entry": float(px0), "status": "open"}
    eff_dt = (datetime.strptime(meta["eff_date"], "%Y%m%d").date()
              if meta.get("eff_date") else
              datetime.strptime(second_friday_next_td(int(ym[:4]), int(ym[4:]), cal_days),
                                "%Y%m%d").date())
    lg["batches"][ym] = {
        "index": meta["index"], "ann_date": meta["ann_date"],
        "ann_is_real": bool(meta.get("ann_is_real", True)),
        "eff_date": eff_dt.strftime("%Y%m%d"), "entry_td": entry_td,
        "exit_td": last_td_before(cal_days, eff_dt),
        "n_candidates": len(codes), "skipped_star": skipped_star, "legs": legs,
        "marks": {},
    }
    log.info("批次 %s 开仓登记：候选 %d（STAR 剔 %d），入场日 %s", ym, len(codes),
             skipped_star, entry_td)
    return True


def settle_batch(lg: dict, ym: str, cal_days: list, stock_pv: pd.DataFrame,
                 idx_pv: pd.DataFrame, today: str | None = None) -> bool:
    b = lg["batches"][ym]
    if b.get("status") == "settled":
        return False
    today_str = today or date.today().strftime("%Y%m%d")
    if b["exit_td"] > today_str:
        return False
    idx_code = b["index"]
    cars, n_ok = [], 0
    for c, leg in b["legs"].items():
        if leg["status"] != "open":
            continue
        try:
            px1 = stock_pv.at[b["exit_td"], c]
        except KeyError:
            px1 = float("nan")
        if pd.isna(px1):
            leg["status"] = "no_price_exit"
            continue
        i0 = idx_pv.at[b["entry_td"], idx_code] if idx_code in idx_pv.columns else float("nan")
        i1 = idx_pv.at[b["exit_td"], idx_code] if idx_code in idx_pv.columns else float("nan")
        if pd.isna(i0) or pd.isna(i1) or leg["entry"] in (None, 0):
            continue
        leg["exit"] = float(px1)
        leg["car"] = px1 / leg["entry"] - 1.0 - (i1 / i0 - 1.0)
        cars.append(leg["car"])
        n_ok += 1
    b["status"] = "settled"
    b["settled_at"] = today_str
    b["cohort_n"] = n_ok
    b["cohort_car"] = float(sum(cars) / len(cars)) if cars else None
    log.info("批次 %s 结算：群体 n=%d，等权 CAR=%.4f%%", ym, n_ok,
             100 * b["cohort_car"] if cars else float("nan"))
    return True


def mark_batch(lg: dict, ym: str, stock_pv: pd.DataFrame, idx_pv: pd.DataFrame,
               today_str: str) -> None:
    b = lg["batches"][ym]
    if b.get("status") == "settled" or b["entry_td"] > today_str:
        return
    idx_code = b["index"]
    try:
        i0 = idx_pv.at[b["entry_td"], idx_code]
        i1 = idx_pv.at[today_str, idx_code]
    except KeyError:
        return
    if pd.isna(i0) or pd.isna(i1):
        return
    cars = []
    for c, leg in b["legs"].items():
        if leg["status"] != "open":
            continue
        try:
            px = stock_pv.at[today_str, c]
        except KeyError:
            continue
        if pd.isna(px):
            continue
        cars.append(px / leg["entry"] - 1.0 - (i1 / i0 - 1.0))
    if cars:
        b["marks"][today_str] = {"n": len(cars), "cohort_car": float(sum(cars) / len(cars))}


def verify(lg: dict) -> list:
    errs = []
    for ym, b in lg["batches"].items():
        if b.get("status") == "settled":
            n_open = sum(1 for l in b["legs"].values() if l["status"] == "open")
            if n_open:
                errs.append(f"{ym}: 已结算但 {n_open} 腿仍 open")
            if b.get("cohort_car") is None:
                errs.append(f"{ym}: 已结算但无群体 CAR")
            if b["entry_td"] >= b["exit_td"]:
                errs.append(f"{ym}: 入场日不早于退出日")
        marks = b.get("marks", {})
        if list(marks) != sorted(marks):
            errs.append(f"{ym}: 盯市键未按日期排序")
    return errs


def pick_ff_ratio(ym: str, top_n: int = 2, asof: str | None = None) -> list:
    """ff_ratio（free_share/float_share，公告前最近月末）top-N 报告器——只读。

    ⚠️ 规则 v3（ff_ratio 作 weight_proxy 孪生）已按预注册判 NO-GO 线结清
    （Jaccard 0.40 < 0.5，plans/freefloat-backfill-rule-v3-prereg-20260928.md）。
    本模式是**未验证候选规则的前瞻 OOS 观测**：从 2026-12 批起逐批记录其选择，
    攒样本供未来新预注册；历史回测读数（+545 元/年）只作登记不作采纳依据。"""
    cf = load_candidate_file(ym)
    if not cf:
        return []
    ffp = SAT_DIR / "freefloat_monthly.parquet"
    if not ffp.exists():
        raise SystemExit("缺 freefloat_monthly.parquet —— 先跑 backfill_freefloat_monthly_20260928.py")
    ff = pd.read_parquet(ffp)
    asof_str = asof or date.today().strftime("%Y%m%d")
    ff = ff[ff["trade_date"] <= asof_str]
    rows = []
    for c in cf["candidates"]:
        code = c["ts_code"]
        if code.startswith(("688", "689")):
            continue
        sub = ff[ff["ts_code"] == code].dropna(subset=["free_share", "float_share"])
        sub = sub[sub["float_share"] > 0]
        if sub.empty:
            rows.append({"ts_code": code, "ff_ratio": None, "asof": None})
            continue
        last = sub.sort_values("trade_date").iloc[-1]
        rows.append({"ts_code": code, "ff_ratio": float(last["free_share"] / last["float_share"]),
                     "asof": str(last["trade_date"])})
    have = [r for r in rows if r["ff_ratio"] is not None]
    have.sort(key=lambda r: -r["ff_ratio"])
    return have[:top_n]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--mark", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--pick", metavar="YM",
                    help="ff_ratio top-2 前瞻观测（未验证候选规则，见规则 v3 预注册）")
    args = ap.parse_args()
    if args.pick:
        picks = pick_ff_ratio(args.pick)
        print(json.dumps({"batch": args.pick, "note": "ff_ratio 前瞻观测（未验证候选规则）",
                          "picks": picks}, ensure_ascii=False, indent=2))
        return
    if not (args.scan or args.run or args.mark or args.verify):
        ap.print_help()
        return

    lg = load_ledger()
    if args.verify:
        errs = verify(lg)
        print("台账自检：", "通过" if not errs else errs)
        return
    if args.scan:
        cal = load_cal()
        today = date.today()
        print("下一批日历（真公告日以 candidates JSON 为准）：")
        for x in next_batches(today, cal):
            ym = f"{x['y']}{x['m']:02d}"
            cf = load_candidate_file(ym)
            state = "名单已就位" if cf else "等待公告（候选名单未登记）"
            if ym in lg["batches"]:
                state = f"已登记（{lg['batches'][ym].get('status', 'open')}）"
            print(f"  {x['index']} {ym} 生效日 {x['eff_date']} —— {state}")
        for ym, b in lg["batches"].items():
            if b.get("status") != "settled":
                last = sorted(b.get("marks", {}))[-1:] or []
                print(f"  在途：{ym} 入场 {b['entry_td']} 退出 {b['exit_td']} "
                      f"候选 {b['n_candidates']}" +
                      (f" 最新盯市 {last[0]}: {b['marks'][last[0]]['cohort_car']:.4f}" if last else ""))
        return

    cal = load_cal()
    today_str = date.today().strftime("%Y%m%d")
    acted = False
    for ym in sorted({f"{x['y']}{x['m']:02d}" for x in next_batches(date.today(), cal, 8)}):
        cf = load_candidate_file(ym)
        if not cf or ym in lg["batches"]:
            continue
        codes = [c["ts_code"] for c in cf["candidates"]]
        stock_pv, idx_pv = load_close_pivot(codes)
        acted |= open_batch(lg, ym, cf, cal, stock_pv, idx_pv, today=today_str)
    for ym, b in list(lg["batches"].items()):
        if b.get("status") == "settled":
            continue
        codes = [c for c, l in b["legs"].items() if l["entry"]]
        stock_pv, idx_pv = load_close_pivot(codes)
        if args.mark and b["entry_td"] <= today_str:
            mark_batch(lg, ym, stock_pv, idx_pv, today_str)
        acted |= settle_batch(lg, ym, cal, stock_pv, idx_pv, today=today_str)
    if acted or args.mark:
        save_ledger(lg)
        log.info("台账已写 %s", LEDGER.name)
    errs = verify(lg)
    if errs:
        log.warning("自检异常：%s", errs)


if __name__ == "__main__":
    main()
