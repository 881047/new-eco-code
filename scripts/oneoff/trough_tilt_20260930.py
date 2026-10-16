# -*- coding: utf-8 -*-
"""低谷加仓探针 A/B —— `plans/trough-tilt-prereg-20260930.md` 的执行体。

L3 拥挤度约束(crowding_cap_20260928, REJECT)的镜像假设: 剔小盘=卖在低谷亏 5.68pp,
反向操作(触发期向小盘倾斜)能否捕获冷块均值回归。触发器逐字复用 L3 B1(块 IC 冷判定
recent 6 月 × sign(long 24 月)<0 且 |long|≥0.01, 连续 2 期)⇒ 激活月两臂:
  T1 强镜像: 激活月候选池只留流通市值最低五分位(严格镜像 L3 的"剔最低五分位")
  T2 弱镜像: 激活月剔除流通市值最高五分位(温和向小盘倾斜)
实现 = 评分表层(零引擎改动, L3/txt2 先例)。对照 = 生产 model_score 同 session 同引擎
双窗重跑(L3 时点的 pin 20260927_excl5 已被 20260928_exe 取代, 不能用硬编码读数)。
bar 先标定后冻结(acceptance-bar 纪律): --calibrate-only 只跑触发器+天花板标定,
不建处理表不跑臂; bar 冻结进预注册后才允许跑处理臂。
产物 experiments/trough_tilt_20260930.json。
"""
import json
import sqlite3
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
from app.backtest.engine import run_backtest
from app.factors.factor_engine import load_market, month_end_dates

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "trough_tilt_20260930.json"
PREREG = "plans/trough-tilt-prereg-20260930.md"
B1 = ("size", "amount_log", "illiq")
LONG_M, RECENT_M = 24, 6
LONG_BAR = 0.01
MV_Q_LO, MV_Q_HI = 0.20, 0.80   # 最低/最高五分位边界
MIN_NAMES = 200
# 对照臂 pin 复现守卫(20260928_exe, CLAUDE.md §0): ann_return 满仓尺
CTRL_PIN = {"prod": 0.3168, "full": 0.1865}
CTRL_TOL = 0.002
# 判据冻结规则(预注册 §二): bar = max(0.30 × C_ann, +0.50pp), C=触发期
# EW(最低五分位)−EW(全池) 累计价差按 prod 窗年数年化(粗口径天花板, 不含打分选股)。
CAPTURE, BAR_FLOOR = 0.30, 0.005
PROD_START = "20190722"
RUNS = {"prod": PROD_START, "full": None}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def compute_activation():
    """触发器: 逐字复用 crowding_cap_20260928.py 的 B1 块 IC 冷判定。"""
    m = load_market()
    md_all = month_end_dates(pd.DatetimeIndex(m["close"].index))
    md_eval = pd.DatetimeIndex([d for d in md_all if d >= pd.Timestamp("2019-01-01")])
    fwd = gate.forward_return(m, gate.HORIZON)

    conn = sqlite3.connect(f"{(ROOT / 'data' / 'neweco.db').as_uri()}?mode=ro", uri=True)
    fac = pd.read_sql("SELECT trade_date, ts_code, factor, value FROM factor "
                      "WHERE factor IN ('size','amount_log','illiq')", conn)
    conn.close()
    fac["trade_date"] = pd.to_datetime(fac["trade_date"].astype(str))
    anchors = pd.DatetimeIndex(sorted(fac["trade_date"].unique()))
    anchors = anchors[anchors.isin(md_eval)]
    log(f"月锚点 {len(anchors)}（{anchors[0].date()}..{anchors[-1].date()}）")

    ic = {}
    for d in anchors:
        w = fac[fac["trade_date"] == d.strftime("%Y%m%d")].pivot(
            index="ts_code", columns="factor", values="value").reindex(columns=list(B1))
        if d not in fwd.index:
            continue
        r = fwd.loc[d].dropna()
        jn = w.join(r.rename("r"), how="inner").dropna()
        if len(jn) < MIN_NAMES:
            continue
        ic[d] = {f: float(jn[f].rank().corr(jn["r"].rank())) for f in B1}
    ic_df = pd.DataFrame(ic).T.sort_index()
    blk = ic_df.mean(axis=1)
    log(f"块 IC 序列 {len(blk)} 期")

    active = {}
    for d in blk.index:
        window = blk.loc[:d].iloc[:-1]
        if len(window) < LONG_M:
            active[d] = False
            continue
        long_v = float(window.iloc[-LONG_M:].mean())
        recent_v = float(window.iloc[-RECENT_M:].mean())
        cold = (recent_v * (1.0 if long_v >= 0 else -1.0) < 0) and abs(long_v) >= LONG_BAR
        active[d] = bool(cold)
    activate = {}
    dates = sorted(active)
    for i, d in enumerate(dates):
        if i >= 1 and active.get(dates[i]) and active.get(dates[i - 1]):
            activate[dates[i]] = True
    act_dates = [d for d, v in activate.items() if v]
    act_months = [str(d.to_period("M")) for d in act_dates]
    cover = len(act_months) / max(1, len(dates))
    log(f"激活月 {len(act_months)} 个（覆盖 {cover:.1%}）: {act_months}")
    last_eval = pd.DatetimeIndex(blk.index)[-1]   # 评估窗末端(有完整前瞻收益的最后锚点)
    return m, fwd, act_dates, act_months, cover, len(dates), last_eval


def circ_panel(m):
    """月内截面前市值分位(L3 verbatim: 全月交易日混池 rank)。"""
    circ_p = m["circ_mv"].sort_index()
    circ_long = circ_p.stack().rename("circ_mv").reset_index()
    circ_long["trade_date"] = circ_long["trade_date"].dt.strftime("%Y%m%d")
    circ_long["month"] = pd.to_datetime(circ_long["trade_date"], format="%Y%m%d") \
        .dt.to_period("M").astype(str)
    circ_long["mv_pct"] = circ_long.groupby("month")["circ_mv"].rank(pct=True)
    return circ_long


def calibrate(m, fwd, act_dates):
    """§B 天花板标定: 激活锚点的五分位 EW 价差(粗口径, 不含打分选股)。
    C1 = Σ[EW(最低五分位) − EW(全池)]  (T1 严格镜像的 stake)
    C2 = Σ[EW(全池\最高五分位) − EW(全池)]  (T2 温和镜像参考)"""
    cl = circ_panel(m)
    rows, c1 = [], 0.0
    c2 = 0.0
    for d in act_dates:
        mth = str(d.to_period("M"))
        if d not in fwd.index:
            continue
        r = fwd.loc[d].dropna()
        sub = cl[cl["month"] == mth]
        last_dt = sub["trade_date"].max()          # 月末日截面取分位归属
        sub = sub[sub["trade_date"] == last_dt]
        small = set(sub.loc[sub["mv_pct"] <= MV_Q_LO, "ts_code"])
        big = set(sub.loc[sub["mv_pct"] >= MV_Q_HI, "ts_code"])
        r_small = float(r[r.index.isin(small)].mean()) if small else np.nan
        r_big = float(r[r.index.isin(big)].mean()) if big else np.nan
        r_pool = float(r.mean())
        r_mid = float(r[~r.index.isin(big)].mean())
        rows.append({"anchor": str(d.date()), "n_small": len(small),
                     "r_small": round(r_small, 4), "r_pool": round(r_pool, 4),
                     "r_big": round(r_big, 4), "spread_small_pool": round(r_small - r_pool, 4)})
        c1 += (r_small - r_pool)
        c2 += (r_mid - r_pool)
    return rows, c1, c2


def run_diag():
    """--diag: 只读分歧诊断——从 backtest_nav 留痕(tt_* runs)取 ctrl/t1/t2 prod 窗
    逐月净值, 找分歧期与累计贡献。不重跑引擎不改判决, 结果并回 JSON。"""
    conn = sqlite3.connect(f"{(ROOT / 'data' / 'neweco.db').as_uri()}?mode=ro", uri=True)

    def navs(tag):
        rd = conn.execute(
            "SELECT run_date FROM backtest_summary WHERE run_date LIKE ? "
            "ORDER BY run_date DESC LIMIT 1", (f"%_{tag}",)).fetchone()
        if rd is None:
            return None
        df = pd.read_sql("SELECT trade_date, nav FROM backtest_nav WHERE run_date=? "
                         "ORDER BY trade_date", conn, params=(rd[0],))
        df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
        return df

    out_path = OUT
    data = json.loads(out_path.read_text(encoding="utf-8"))
    diag = {"source": "backtest_nav 留痕(只读), run_date 前缀取最新"}
    pairs = [("t1", "tt_ctrl_prod", "tt_t1_prod"), ("t2", "tt_ctrl_prod", "tt_t2_prod")]
    for arm, ctag, ttag in pairs:
        c, t = navs(ctag), navs(ttag)
        if c is None or t is None:
            diag[arm] = "留痕缺失"
            continue
        m = pd.merge(c, t, on="trade_date", suffixes=("_c", "_t"))
        rc = m["nav_c"].pct_change()      # 先在全序列上算月收益, 再筛分歧期
        rt = m["nav_t"].pct_change()
        mm = m.assign(rc=rc, rt=rt, dm=rc - rt).dropna(subset=["dm"])
        big = mm[mm["dm"].abs() > 1e-9]
        contrib = float((1 + big["rt"]).prod() / (1 + big["rc"]).prod() - 1)
        diag[arm] = {
            "n_periods": int(len(mm)), "n_divergent": int(len(big)),
            "months": [{"month": str(d.to_period("M")),
                        "ctrl": round(float(r), 4), "arm": round(float(a), 4),
                        "delta": round(float(dd), 4)}
                       for d, r, a, dd in zip(big["trade_date"], big["rc"],
                                              big["rt"], big["dm"])],
            "cum_contribution": round(contrib, 4)}
        log(f"diag {arm}: 分歧 {len(big)}/{len(mm)} 期, 累计 {contrib:+.2%} "
            f"{[x['month'] for x in diag[arm]['months']]}")
    conn.close()
    data["divergence_diagnostic"] = diag
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")
    print(f"[diag] 已并入 {out_path}", flush=True)
    return 0


def main() -> int:
    t0 = time.time()
    if "--diag" in sys.argv:
        return run_diag()
    calib_only = "--calibrate-only" in sys.argv
    m, fwd, act_dates, act_months, cover, n_anchors, last_eval = compute_activation()

    rows, c1, c2 = calibrate(m, fwd, act_dates)
    years_prod = max(0.5, (last_eval - pd.Timestamp(PROD_START)).days / 365.25)
    c1_ann, c2_ann = c1 / years_prod, c2 / years_prod
    bar = max(CAPTURE * c1_ann, BAR_FLOOR)
    log(f"标定: C1={c1:+.4f}({c1_ann:+.4%}/年) C2={c2:+.4f}({c2_ann:+.4%}/年) "
        f"⇒ bar={bar:.4%}/年")
    for r_ in rows:
        log(f"  {r_['anchor']}: small {r_['r_small']:+.4f} vs pool {r_['r_pool']:+.4f} "
            f"(Δ {r_['spread_small_pool']:+.4f}, n_small={r_['n_small']})")

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"), "prereg": PREREG,
           "calibrate_only": calib_only,
           "params": {"block": "B1(size/amount_log/illiq)", "long_m": LONG_M,
                      "recent_m": RECENT_M, "long_bar": LONG_BAR,
                      "mv_q_lo": MV_Q_LO, "mv_q_hi": MV_Q_HI,
                      "capture": CAPTURE, "bar_floor": BAR_FLOOR},
           "activate_months": act_months, "n_anchors": n_anchors, "coverage": cover,
           "calibration": {"per_anchor": rows, "c1_cum": round(c1, 4),
                           "c1_ann": round(c1_ann, 4), "c2_cum": round(c2, 4),
                           "c2_ann": round(c2_ann, 4),
                           "years_prod": round(years_prod, 2), "bar": round(bar, 4)},
           "runtime_sec": round(time.time() - t0, 1)}
    if calib_only:
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                       encoding="utf-8")
        print(f"[calibrate-only] 产物: {OUT}  ({time.time() - t0:.0f}s)", flush=True)
        return 0
    if not act_months:
        out["verdict"] = "INDETRMINATE（零激活，机制未触发）"
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                       encoding="utf-8")
        log(f"判决: {out['verdict']}")
        return 0

    # ---------- 处理表 ----------
    conn = sqlite3.connect(f"{(ROOT / 'data' / 'neweco.db').as_uri()}?mode=ro", uri=True)
    ms = pd.read_sql("SELECT trade_date, ts_code, score FROM model_score", conn)
    conn.close()
    ms["d"] = pd.to_datetime(ms["trade_date"], format="%Y%m%d")
    ms["month"] = ms["d"].dt.to_period("M").astype(str)
    act_set = set(act_months)
    cl = circ_panel(m)
    small_keys, big_keys = set(), set()
    for mth in act_months:
        sub = cl[cl["month"] == mth]
        last_dt = sub["trade_date"].max()
        sub = sub[sub["trade_date"] == last_dt]
        small_keys |= {(t, mth) for t in sub.loc[sub["mv_pct"] <= MV_Q_LO, "ts_code"]}
        big_keys |= {(t, mth) for t in sub.loc[sub["mv_pct"] >= MV_Q_HI, "ts_code"]}
    in_act = ms["month"].isin(act_set)
    key = list(zip(ms["ts_code"], ms["month"]))
    is_small = pd.Series([k in small_keys for k in key], index=ms.index)
    is_big = pd.Series([k in big_keys for k in key], index=ms.index)
    t1 = ms[(~in_act) | is_small]              # 强镜像: 激活月只留最低五分位
    t2 = ms[(~in_act) | (~is_big)]             # 弱镜像: 激活月剔最高五分位
    log(f"处理表 T1: {len(ms):,} → {len(t1):,} 行; T2: {len(ms):,} → {len(t2):,} 行")

    def write_table(name, df):
        conn = sqlite3.connect(str(ROOT / "data" / "neweco.db"))
        conn.execute(f"CREATE TABLE IF NOT EXISTS {name} ("
                     "ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL, "
                     "PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID")
        conn.execute(f"DELETE FROM {name}")
        conn.commit()
        df.drop(columns=["d", "month"]).assign(pred_ret=np.nan) \
            [["ts_code", "trade_date", "score", "pred_ret"]] \
            .to_sql(name, conn, if_exists="append", index=False)
        conn.commit()
        conn.close()
    write_table("trough_t1", t1)
    write_table("trough_t2", t2)

    # ---------- 对照(同 session 重跑, pin 复现守卫) + 两臂双窗 ----------
    res = {}
    res["ctrl"] = {}
    for w, start in RUNS.items():
        mm = run_backtest(run_tag=f"tt_ctrl_{w}", start=start)
        res["ctrl"][w] = {k: round(float(mm[k]), 4) for k in
                          ("ann_return", "excess_ann", "max_drawdown", "avg_turnover")}
    pin_ok = all(abs(res["ctrl"][w]["ann_return"] - CTRL_PIN[w]) <= CTRL_TOL
                 for w in RUNS)
    log(f"对照臂: {json.dumps(res['ctrl'])}  pin复现={'OK' if pin_ok else 'FAIL'}")
    if not pin_ok:
        out["verdict"] = "ABORT（对照臂未复现现行 pin，环境不匹配，不判）"
        out["ctrl"] = res["ctrl"]
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                       encoding="utf-8")
        print(f"产物: {OUT}  ({time.time() - t0:.0f}s)", flush=True)
        return 1

    tables = {"t1": "trough_t1", "t2": "trough_t2"}
    for arm, tname in tables.items():
        res[arm] = {}
        for w, start in RUNS.items():
            mm = run_backtest(score_table=tname, run_tag=f"tt_{arm}_{w}", start=start)
            res[arm][w] = {k: round(float(mm[k]), 4) for k in
                           ("ann_return", "excess_ann", "max_drawdown", "avg_turnover")}
        log(f"{arm}: {json.dumps(res[arm])}")

    # ---------- 机械判决(预注册 §二) ----------
    checks, verdicts = {}, {}
    for arm in tables:
        d_exc_prod = round(res[arm]["prod"]["excess_ann"]
                           - res["ctrl"]["prod"]["excess_ann"], 4)
        d_exc_full = round(res[arm]["full"]["excess_ann"]
                           - res["ctrl"]["full"]["excess_ann"], 4)
        d_mdd_prod = round(res[arm]["prod"]["max_drawdown"]
                           - res["ctrl"]["prod"]["max_drawdown"], 4)
        d_mdd_full = round(res[arm]["full"]["max_drawdown"]
                           - res["ctrl"]["full"]["max_drawdown"], 4)
        p1 = bool(d_exc_prod >= bar and d_exc_full > 0)
        p2 = bool(d_mdd_prod >= -0.01 and d_mdd_full >= -0.01)
        p3 = bool(cover >= 0.02 and len(act_months) >= 2)
        if d_exc_prod <= 0 or not p2:
            verdict = "REJECT（线结清）"
        elif p1 and p2 and p3:
            verdict = "PASS（进生产接线预注册）"
        else:
            verdict = "INDETERMINATE"
        one_episode = (len(act_months) >= 2
                       and (pd.Period(act_months[-1]) - pd.Period(act_months[0])).n <= 2)
        if verdict.startswith("PASS") and one_episode:
            verdict += "〔单事件标注: 前瞻观察 ≥4 期为接线前置〕"
        checks[arm] = {"d_excess_prod": d_exc_prod, "d_excess_full": d_exc_full,
                       "d_mdd_prod": d_mdd_prod, "d_mdd_full": d_mdd_full,
                       "p1": p1, "p2": p2, "p3": p3, "bar": round(bar, 4)}
        verdicts[arm] = verdict
        log(f"{arm} 判决: {verdict} (P1={p1} P2={p2} P3={p3} Δexc={d_exc_prod:+.4f})")

    out["ctrl"] = res["ctrl"]
    out["arms"] = {a: res[a] for a in tables}
    out["checks"] = checks
    out["verdicts"] = verdicts
    out["note"] = ("评分表层实现(零引擎改动); 触发器逐字复用 L3 B1; 对照同 session 重跑"
                   "并守卫 pin 复现; PASS 后生产接线另走预注册+回放复核")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"产物: {OUT}  ({time.time() - t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
