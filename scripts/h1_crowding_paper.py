# -*- coding: utf-8 -*-
"""H1 拥挤翻转·前瞻纸面 6 个月初判 runner（判据冻结件：plans/h1-crowding-6m-paper-prereg-20261003.md）

独立小账本：只读生产库（mode=ro），写 data/h1_crowding_paper/（运行时账本，gitignore）
与 experiments/h1_crowding_paper_status.json（滚动状态快照，入库）。不写任何生产表。

口径（与预注册逐字对应，实现澄清见预注册追记）：
- 代理：PIT held_ratio = fund_hold.held_shares ÷ 流通股本(circ_mv×1e4÷同日收盘价)，
        锚间前向结转；PIT 可用日 {0331:20, 0930:20, 0630:45, 1231:85} 交易日（②档案逐字沿用）。
- 体制：原始代理（中性化前）截面均值的 250 交易日滚动分位 ≥ 中位 = 拥挤期；
        滚动窗不足或无数据 = unknown ⇒ 当月不剔除（fail-closed）。
- 月频锚 = 生产 model_score 月末锚 top300（只读）；处理臂在拥挤期剔除中性化拥挤度
  最高十分位（截面秩回归残差 ~ log(circ_mv)+pb+turnover）；出清期两臂逐位相同。
- 前瞻窗：first_run 之后第一个月末锚起 +6 个日历月；first_run 前已存在的锚只作
  演练（in_window=false），其观测不入判决。
- 周观测：两臂日对数收益差的周求和（ISO 周，周内末个交易日的下一交易日已过 ⇒ 收口）；
  零成本纸面；停牌/缺价按 0 收益结转（两臂一致）；成员生效日 = 锚后首个交易日（两臂同日切换）。
- 判决（机械字段）：P1 mean(周差)>0 且 t≥2.5（K=5 Bonferroni 单侧 α=0.01）；
  P2 窗内臂 MDD ≤ 基线 MDD + 2pp；判决词由人填。
幂等：任意一步失败只记日志，下轮自愈；fund_hold 新季度按可用日到期拉取一次并冻结
（拉取成功不回刷，防披露进度爬坡引入非 PIT 修订）。
"""
import bisect
import json
import logging
import math
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[1]  # scripts/ 根 → 仓库根（勿改成 parents[2]）
DB = f"file:{(BASE / 'data' / 'neweco.db').as_posix()}?mode=ro"
LEDGER = BASE / "data" / "h1_crowding_paper"
STATE = LEDGER / "state.json"
LOG_FILE = LEDGER / "h1_paper.log"
STATUS = BASE / "experiments" / "h1_crowding_paper_status.json"
HIST_CSV = LEDGER / "fund_hold_history.csv"  # ①铲全史冻结副本（初始化时拷贝一次）
INCR_CSV = LEDGER / "fund_hold_new.csv"  # 前瞻增量季度（一次拉取一次冻结）
SOURCE_CSV = Path(r"D:\NEW ECO审查报告历史\fund_hold_all.csv")

TOP_N = 300
DROP_N = 30  # 十分位 × 300（可计算候选不足 30 ⇒ 当月不剔除，fail-closed 退化为基线臂）
ROLL_TD = 250
AVAIL_TD = {"0331": 20, "0930": 20, "0630": 45, "1231": 85}
T_BAR = 2.5
MDD_TOL = 0.02
WINDOW_MONTHS = 6

LEDGER.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("h1paper")


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


_ASOF = None  # 仅测试：--as-of YYYYMMDD 时由 __main__ 注入；生产恒 None


def today8():
    return _ASOF or now()[:10].replace("-", "")


def atomic_write_json(path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"first_run_date": None, "anchors": {}, "weeks": {}, "last_nav_date": None,
            "cumlog_base": {}, "cumlog_arm": {}, "window_start": None, "window_end": None}


def ro_conn():
    return sqlite3.connect(DB, uri=True)


def trading_days(con):
    cal = pd.read_sql("SELECT cal_date, is_open FROM trade_cal", con)
    return sorted(cal.loc[cal["is_open"] == 1, "cal_date"].astype(str))


def load_fund_hold():
    """历史冻结副本 + 增量季度合并为 period 面板原始数据。"""
    frames = []
    if HIST_CSV.exists():
        frames.append(pd.read_csv(HIST_CSV, dtype={"ts_code": str}))
    if INCR_CSV.exists():
        frames.append(pd.read_csv(INCR_CSV, dtype={"ts_code": str}))
    if not frames:
        return pd.DataFrame(columns=["ts_code", "period", "fund_cnt", "held_shares"])
    fh = pd.concat(frames, ignore_index=True)
    fh["period"] = fh["period"].astype(str)
    fh["held_shares"] = pd.to_numeric(fh["held_shares"], errors="coerce")
    return fh.drop_duplicates(subset=["period", "ts_code"], keep="first")


def avail_map(td, periods):
    """period → PIT 可用日（披露期结束后 20/45/85 交易日，②档案逐字）。"""
    out = {}
    for p in periods:
        j = bisect.bisect_right(td, p) - 1
        if j < 0:
            continue
        need = AVAIL_TD.get(p[4:])
        if need is not None and j + need < len(td):
            out[p] = td[j + need]
    return out


def pull_due_quarters(td, loaded, today):
    """到期未拉的新季度：季末日 ≤ today 且 可用日 ≤ today；一次拉取一次冻结。"""
    import akshare as ak

    due = []
    for y in range(2019, int(today[:4]) + 1):
        for p in ("0331", "0630", "0930", "1231"):
            period = f"{y}{p}"
            if period > today or period in loaded:
                continue
            j = bisect.bisect_right(td, period) - 1
            if j < 0:
                continue
            need = AVAIL_TD.get(p)
            if need is not None and j + need < len(td) and td[j + need] <= today:
                due.append(period)
    if not due:
        return 0
    new_rows = []
    for period in due:
        try:
            df = ak.stock_report_fund_hold(date=period)
            if df is None or len(df) == 0:
                raise RuntimeError("empty response")
            # 按位置解析（接口 header 错位，①铲教训）：col2=代码 col4=家数 col5=持股总数
            sub = df.iloc[:, [2, 4, 5]].copy()
            sub.columns = ["ts_code", "fund_cnt", "held_shares"]
            sub["period"] = period
            sub["pull_date"] = now()[:10]
            new_rows.append(sub)
            log.info(f"fund_hold pulled {period}: rows={len(sub)}")
        except Exception as e:
            log.warning(f"fund_hold pull FAIL {period}: {type(e).__name__}: {str(e)[:100]}")
        time.sleep(1.2)
    if new_rows:
        old = pd.read_csv(INCR_CSV, dtype={"ts_code": str}) if INCR_CSV.exists() else None
        df = pd.concat(([old] if old is not None else []) + new_rows, ignore_index=True)
        tmp = INCR_CSV.with_suffix(".csv.tmp")
        df.to_csv(tmp, index=False)
        tmp.replace(INCR_CSV)
    return len(new_rows)


def period_mean_series(con, fh, td, am):
    """各披露期在其 PIT 可用日的原始 held_ratio 截面均值（体制代理的阶梯序列）。"""
    if len(fh) == 0:
        return {}
    out = {}
    for period, g in fh.groupby("period"):
        if period not in am:
            continue
        avail = am[period]
        ph = "'" + avail + "'"
        d1 = pd.read_sql(f"SELECT ts_code, close FROM daily WHERE trade_date={ph}", con)
        d2 = pd.read_sql(f"SELECT ts_code, circ_mv FROM daily_basic WHERE trade_date={ph}", con)
        m = d1.merge(d2, on="ts_code", how="inner")
        m["float_shares"] = m["circ_mv"].astype(float) * 1e4 / m["close"].astype(float)
        g2 = g.merge(m[["ts_code", "float_shares"]], on="ts_code", how="inner")
        ratio = g2["held_shares"] / g2["float_shares"]
        ratio = ratio.replace([np.inf, -np.inf], np.nan).dropna()
        if len(ratio) >= 100:
            out[period] = {"avail": avail, "mean": float(ratio.mean()), "n": int(len(ratio))}
    return out


def regime_at(means, td, date):
    """date 当日体制状态：最近可用期均值在含当日止 250 交易日窗内的分位。PIT 安全。"""
    avails = sorted(m["avail"] for m in means.values())
    avails_le = [a for a in avails if a <= date]
    if not avails_le:
        return "unknown", None
    j = bisect.bisect_right(td, date) - 1
    if j < 0:
        return "unknown", None
    win_start = max(0, j - ROLL_TD + 1)
    step = {}  # avail → mean
    for p, m in means.items():
        step[m["avail"]] = m["mean"]
    step_dates = sorted(step)
    hist = []
    for i in range(win_start, j + 1):
        k = bisect.bisect_right(step_dates, td[i]) - 1
        if k >= 0:
            hist.append(step[step_dates[k]])
    if len(hist) < ROLL_TD:
        return "unknown", None  # 滚动窗不足：体制未知 ⇒ 不剔除（fail-closed）
    cur = step[step_dates[bisect.bisect_right(step_dates, date) - 1]]
    q = float(np.mean(np.asarray(hist) <= cur))
    return ("crowded" if q >= 0.5 else "clear"), q


def top300(con, anchor):
    rows = pd.read_sql(
        "SELECT ts_code FROM model_score WHERE trade_date=? ORDER BY score DESC LIMIT ?",
        con, params=(str(anchor), TOP_N),
    )
    return sorted(rows["ts_code"].astype(str))


def neutralize_and_drop(con, anchor, universe, fh, am):
    """截面秩回归残差中性化；返回（剔除名单, 覆盖诊断）。
    PIT：只用可用日 ≤ 锚点的最近披露期；缺任一回归量的股票不进候选（留在两臂）。"""
    avails_ok = [p for p, a in am.items() if a <= str(anchor)]
    if not avails_ok:
        return [], {"reason": "no period available at anchor"}
    latest = max(avails_ok)
    ph = "'" + str(anchor) + "'"
    d = pd.read_sql(f"SELECT ts_code, close FROM daily WHERE trade_date={ph}", con)
    b = pd.read_sql(
        f"SELECT ts_code, circ_mv, pb, turnover_rate FROM daily_basic WHERE trade_date={ph}", con
    )
    m = d.merge(b, on="ts_code", how="left")
    m = m[m["ts_code"].isin(universe)].copy()
    held = fh[fh["period"] == latest].set_index("ts_code")["held_shares"]
    m["float_shares"] = m["circ_mv"].astype(float) * 1e4 / m["close"].astype(float)
    m["held_ratio"] = m["ts_code"].map(held) / m["float_shares"]
    m["log_circ_mv"] = np.log(m["circ_mv"].astype(float))
    m = m.replace([np.inf, -np.inf], np.nan)
    panel = m.set_index("ts_code")[["held_ratio", "log_circ_mv", "pb", "turnover_rate"]]
    panel = panel.rename(columns={"turnover_rate": "turnover"}).apply(pd.to_numeric, errors="coerce")
    diag = {"period": latest, "avail": am[latest],
            "panel_n": int(panel["held_ratio"].notna().sum()), "universe_n": len(universe)}
    d2 = panel.dropna()
    if len(d2) < DROP_N:
        diag["reason"] = f"candidates {len(d2)} < {DROP_N}"
        return [], diag
    R = d2.rank(pct=True)
    X = np.column_stack([np.ones(len(R)), R["log_circ_mv"], R["pb"], R["turnover"]])
    y = R["held_ratio"].to_numpy(dtype=float)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = pd.Series(y - X @ beta, index=d2.index)
    dropped = sorted(resid.sort_values(ascending=False).head(DROP_N).index)
    diag["dropped_n"] = len(dropped)
    return dropped, diag


def member_returns(con, members, dates):
    """等权日再平衡组合日简单收益；缺价（停牌/退市收尾）按 0 收益结转，两臂同口径。"""
    if not members or not dates:
        return {}
    ph = "'" + "','".join(str(x) for x in dates) + "'"
    df = pd.read_sql(f"SELECT ts_code, trade_date, pct_chg FROM daily WHERE trade_date IN ({ph})", con)
    df["ret"] = pd.to_numeric(df["pct_chg"], errors="coerce") / 100.0
    piv = df.pivot_table(index="trade_date", columns="ts_code", values="ret", aggfunc="last")
    out = {}
    for d in dates:
        if d in piv.index:
            r = piv.loc[d, piv.columns.intersection(members)]
            out[d] = float(r.reindex(members).fillna(0.0).mean())
        else:
            out[d] = 0.0
    return out


def mdd_of(cumlog):
    arr = np.asarray(cumlog, dtype=float)
    if len(arr) == 0:
        return 0.0
    arr = arr - arr[0]  # 窗内重定基，演练期累积不污染窗内 MDD
    nav = np.exp(arr - np.maximum.accumulate(arr))
    return float(1.0 - nav.min())


def week_of(d):
    iso = datetime.strptime(d, "%Y%m%d").isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def process(con, td, state):
    today = today8()
    if state["first_run_date"] is None:
        state["first_run_date"] = today
        if SOURCE_CSV.exists() and not HIST_CSV.exists():
            HIST_CSV.write_bytes(SOURCE_CSV.read_bytes())
            log.info(f"history CSV copied: {len(pd.read_csv(SOURCE_CSV))} rows")
        elif not SOURCE_CSV.exists() and not HIST_CSV.exists():
            log.error("fund_hold_all.csv 源缺失且无本地副本 —— 终止")
            return state
        log.info(f"first run {today}")

    # 1) 到期新季度拉取（一次拉取一次冻结）
    fh = load_fund_hold()
    loaded = set(fh["period"].unique()) if len(fh) else set()
    if pull_due_quarters(td, loaded, today):
        fh = load_fund_hold()
    am = avail_map(td, fh["period"].unique() if len(fh) else [])

    # 2) 体制阶梯序列（确定性 + PIT；历史段只依赖冻结 CSV，重算不变 ⇒ "只外推不回改"自然成立）
    means = period_mean_series(con, fh, td, am)
    if not means:
        log.warning("regime series empty —— 终止本轮")
        return state

    # 3) 新锚点处理：只处理「first_run 时点的当前锚（演练）+ 其后新锚」；
    #    更早的历史锚与前瞻窗无关，不进账本（in_window 判据 + 账本体积双重理由）
    anchors = [r[0] for r in con.execute(
        "SELECT DISTINCT trade_date FROM model_score ORDER BY trade_date").fetchall()]
    latest_le = max([a for a in anchors if a <= today and a <= state["first_run_date"]], default=None)
    new_anchors = [a for a in anchors if a not in state["anchors"] and a <= today
                   and (a > state["first_run_date"] or a == latest_le)]
    for a in new_anchors:
        in_window = bool(state["first_run_date"] and a > state["first_run_date"])
        reg, q = regime_at(means, td, a)
        uni = top300(con, a)
        if reg == "crowded":
            dropped, diag = neutralize_and_drop(con, a, uni, fh, am)
        else:
            dropped, diag = [], {"reason": f"regime={reg}"}
        state["anchors"][a] = {
            "in_window": in_window, "regime": reg, "regime_q": q,
            "universe": uni, "dropped": dropped, "diag": diag,
        }
        if in_window and not state.get("window_start"):
            state["window_start"] = a
            y, mo = int(a[:4]) + (WINDOW_MONTHS + int(a[4:6]) - 1) // 12, (int(a[4:6]) - 1 + WINDOW_MONTHS) % 12 + 1
            state["window_end"] = f"{y}{mo:02d}" + a[6:]
        log.info(f"anchor {a}: regime={reg} q={q} in_window={in_window} dropped={len(dropped)} diag={diag}")

    # 4) 日收益补算（生效日 = 锚后首个交易日；两臂同日切换 ⇒ 配对差不受共同滞后影响）
    if not state["anchors"]:
        return state
    first_needed = min(bisect.bisect_right(td, a) for a in state["anchors"])
    if first_needed >= len(td):
        return state
    if state["last_nav_date"] and state["last_nav_date"] in td:
        start_j = td.index(state["last_nav_date"]) + 1
    else:
        start_j = first_needed
    dates = [d for d in td[start_j:] if d <= today]
    if not dates:
        return state
    eff = {}
    for a in sorted(state["anchors"]):
        j = bisect.bisect_right(td, a)
        if j < len(td):
            eff.setdefault(td[j], a)  # 锚 → 生效日（月频锚不撞日）
    cum_base = dict(state["cumlog_base"])
    cum_arm = dict(state["cumlog_arm"])
    prev_d = td[td.index(dates[0]) - 1] if td.index(dates[0]) > 0 else None
    cur_base = cum_base.get(prev_d, 0.0)
    cur_arm = cum_arm.get(prev_d, 0.0)
    cur_anchor_base = cur_anchor_arm = None
    # 各生效日对应锚（含历史）：重建生效日→锚映射并向前滚动成员集
    anchors_sorted = sorted(state["anchors"])
    eff_dates = [d for d in td if d in eff and d <= dates[-1] and d >= td[first_needed]]
    for d in dates:
        if d in eff:
            a = eff[d]
            rec = state["anchors"][a]
            cur_anchor_base = list(rec["universe"])
            cur_anchor_arm = [c for c in rec["universe"] if c not in set(rec["dropped"])] or list(rec["universe"])
        rb = member_returns(con, cur_anchor_base, [d]).get(d, 0.0) if cur_anchor_base else 0.0
        ra = member_returns(con, cur_anchor_arm, [d]).get(d, 0.0) if cur_anchor_arm else 0.0
        cur_base += math.log(1.0 + rb)
        cur_arm += math.log(1.0 + ra)
        cum_base[d], cum_arm[d] = cur_base, cur_arm

    # 5) 周观测：先登记本轮日期，再收口（周内末个交易日的下一交易日已过 ⇒ 该周完整）
    for d in dates:
        state["weeks"].setdefault(week_of(d), {"days": []})["days"].append(d)
    for wk in list(state["weeks"]):
        rec = state["weeks"][wk]
        if rec.get("done"):
            continue
        idx = sorted(td.index(x) for x in rec["days"] if x in td)
        if not idx:
            continue
        i0, i1 = idx[0], idx[-1]
        if i1 + 1 < len(td) and td[i1 + 1] <= today:
            prev_d2 = td[i0 - 1] if i0 > 0 else None
            b0 = cum_base.get(prev_d2, 0.0)
            a0 = cum_arm.get(prev_d2, 0.0)
            lr_base = cum_base[td[i1]] - b0
            lr_arm = cum_arm[td[i1]] - a0
            anchor_d = max(a for a in state["anchors"] if a <= td[i1])
            rec_a = state["anchors"][anchor_d]
            rec.update({
                "done": True, "week_end": td[i1], "n_days": len(idx),
                "in_window": bool(rec_a["in_window"]), "regime": rec_a["regime"],
                "log_ret_base": round(float(lr_base), 6), "log_ret_arm": round(float(lr_arm), 6),
                "diff": round(float(lr_arm - lr_base), 6),
            })
            log.info(f"week {wk} done: diff={lr_arm - lr_base:+.5f} regime={rec_a['regime']} "
                     f"in_window={rec_a['in_window']}")
            # 本轮新处理日的登记（含未收口周）已移至收口前——登记先行防漏周

    state["last_nav_date"] = dates[-1]
    state["cumlog_base"] = {k: round(v, 8) for k, v in cum_base.items()}
    state["cumlog_arm"] = {k: round(v, 8) for k, v in cum_arm.items()}
    return state


def judge(state):
    """机械判决字段（判决词由人填）。只统计 in_window 周观测；MDD 用窗内重定基日频。"""
    obs = sorted(
        [(w, r) for w, r in state["weeks"].items() if r.get("done") and r.get("in_window")],
        key=lambda x: x[1]["week_end"],
    )
    diffs = np.array([r["diff"] for _, r in obs], dtype=float)
    out = {"n_obs": int(len(diffs)), "window_start": state.get("window_start"),
           "window_end": state.get("window_end")}
    if len(diffs) == 0:
        out.update({"verdict_mechanical": "PENDING", "verdict_human": "PENDING"})
        return out
    mean = float(diffs.mean())
    t = float(mean / (diffs.std(ddof=1) / math.sqrt(len(diffs)))) if len(diffs) > 1 else 0.0
    ws = sorted(k for k in state["cumlog_base"] if not state.get("window_start") or k >= state["window_start"])
    mdd_b = mdd_of([state["cumlog_base"][k] for k in ws])
    mdd_a = mdd_of([state["cumlog_arm"][k] for k in ws])
    p1 = bool(mean > 0 and t >= T_BAR)
    p2 = bool(mdd_a <= mdd_b + MDD_TOL)
    out.update({"mean_diff_bp": round(mean * 1e4, 2), "t_one_sided": round(t, 3),
                "mdd_base": round(mdd_b, 4), "mdd_arm": round(mdd_a, 4), "P1": p1, "P2": p2})
    if state.get("window_end") and today8() >= state["window_end"]:
        out["verdict_mechanical"] = ("PASS" if (p1 and p2) else
                                     "REJECT" if (mean <= 0 or t <= 0) else "INDETERMINATE")
    else:
        out["verdict_mechanical"] = "PENDING"
    out["verdict_human"] = "PENDING"
    return out


def main():
    state = load_state()
    con = ro_conn()
    try:
        td = trading_days(con)
        state = process(con, td, state)
        atomic_write_json(STATE, state)
        latest_anchor = max(state["anchors"]) if state["anchors"] else None
        status = {"as_of": now(), "first_run_date": state["first_run_date"],
                  "n_anchors": len(state["anchors"]),
                  "n_anchors_in_window": sum(1 for r in state["anchors"].values() if r["in_window"]),
                  "regime_now": state["anchors"][latest_anchor]["regime"] if latest_anchor else None,
                  "judge": judge(state)}
        atomic_write_json(STATUS, status)
        log.info(f"status: {json.dumps(status['judge'], ensure_ascii=False)}")
    finally:
        con.close()


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    if "--as-of" in args:  # 测试专用：伪时钟演练日收益/周收口路径；须先备份 state.json
        _ASOF = args[args.index("--as-of") + 1].replace("-", "")
    if "--smoke" in args:
        con = ro_conn()
        try:
            td = trading_days(con)
            src = HIST_CSV if HIST_CSV.exists() else SOURCE_CSV
            fh = pd.read_csv(src, dtype={"ts_code": str})
            fh["period"] = fh["period"].astype(str)
            am = avail_map(td, fh["period"].unique())
            means = period_mean_series(con, fh, td, am)
            print(f"[smoke] periods={len(means)} last={max(means) if means else None}")
            for probe in ("20220630", "20240930", "20260630", "20260930"):
                reg, q = regime_at(means, td, probe)
                print(f"[smoke] regime@{probe}: {reg} q={q}")
            uni = top300(con, "20260930")
            dropped, diag = neutralize_and_drop(con, "20260930", uni, fh, am)
            print(f"[smoke] top300 n={len(uni)} dropped_n={len(dropped)} diag={diag}")
        finally:
            con.close()
        # 周观测数学自检（合成数据，不触账本）
        r_arm = [0.01, -0.005, 0.002]
        r_base = [0.004, -0.001, 0.003]
        diff = sum(math.log(1 + a) - math.log(1 + b) for a, b in zip(r_arm, r_base))
        expect = math.log(1.01 / 1.004) + math.log(0.995 / 0.999) + math.log(1.002 / 1.003)
        assert abs(diff - expect) < 1e-12, "weekly diff math broken"
        print(f"[smoke] weekly diff math OK ({diff:+.6f})")
        sys.exit(0)
    main()
