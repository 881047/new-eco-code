# -*- coding: utf-8 -*-
"""拥挤度暴露约束 A/B —— `plans/crowding-cap-prereg-20260928.md` 的执行体。

机制（§一 冻结）: B1(规模/流动性块: size/amount_log/illiq)块 IC 序列（月末锚点对
naive 21 日收益的秩 IC，≥200 只）→ 冷_t = recent(6 月)×sign(long(24 月)) < 0 且
|long| ≥ 0.01 → 连续 2 期冷 ⇒ 下月候选池剔除流通市值最低五分位。
实现 = 评分表层: 生产 model_score 在激活月删最小市值五分位行 → 隔离表 crwd_cap →
生产引擎双窗回测。对照 = pin 20260927_excl5 在册读数（同表同引擎）。
判据（§二 冻结）: P1 MDD 改善 ≥2pp(prod)/P2 年化非劣 ≥−1.5pp/P3 激活占比 5-40%。
判决: PASS(进生产接线预注册) / REJECT(线结清) / INDETERMINATE。拍板由人。
产物 experiments/crowding_cap_20260928.json。
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
from app.pit.engine import PITEngine

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "crowding_cap_20260928.json"
PREREG = "plans/crowding-cap-prereg-20260928.md"
B1 = ("size", "amount_log", "illiq")
LONG_M, RECENT_M = 24, 6
LONG_BAR = 0.01
MV_Q = 0.20               # 剔除流通市值最低五分位
MIN_NAMES = 200
RUNS = {"prod": "20260927_excl5", "full": "20260927_excl5_full"}
PIN = {"prod": {"ann_return": 0.2832, "excess_ann": 0.2230, "max_drawdown": -0.2584,
                "avg_turnover": 0.8865},
       "full": {"ann_return": 0.1637, "excess_ann": 0.1107, "max_drawdown": -0.4336,
                "avg_turnover": 0.8778}}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    t0 = time.time()
    m = load_market()
    md_all = month_end_dates(pd.DatetimeIndex(m["close"].index))
    md_eval = pd.DatetimeIndex([d for d in md_all if d >= pd.Timestamp("2019-01-01")])
    fwd = gate.forward_return(m, gate.HORIZON)

    # ---------- B1 块 IC 序列（factor 表月末值 vs naive 21 日） ----------
    conn = sqlite3.connect(f"{(ROOT / 'data' / 'neweco.db').as_uri()}?mode=ro", uri=True)
    fac = pd.read_sql("SELECT trade_date, ts_code, factor, value FROM factor "
                      "WHERE factor IN ('size','amount_log','illiq')", conn)
    conn.close()
    circ_p = m["circ_mv"].sort_index()               # 日频流通市值面板(万元)
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
    ic_df = pd.DataFrame(ic).T.sort_index()          # 锚点 × 3 成员
    blk = ic_df.mean(axis=1)                          # 块 IC（成员等权）
    log(f"块 IC 序列 {len(blk)} 期")

    # ---------- 冷判定与激活月（real-time） ----------
    active = {}
    hist = []
    for d in blk.index:
        window = blk.loc[:d].iloc[:-1]                # 不含当月（当月 IC 用了未来收益）
        if len(window) < LONG_M:
            active[d] = False
            continue
        long_v = float(window.iloc[-LONG_M:].mean())
        recent_v = float(window.iloc[-RECENT_M:].mean())
        cold = (recent_v * (1.0 if long_v >= 0 else -1.0) < 0) and abs(long_v) >= LONG_BAR
        active[d] = bool(cold)
        hist.append({"anchor": str(d.date()), "long": round(long_v, 4),
                     "recent": round(recent_v, 4), "cold": bool(cold)})
    # 连续 2 期冷 ⇒ 下月激活
    activate = {}
    dates = sorted(active)
    for i, d in enumerate(dates):
        if i >= 1 and active.get(dates[i]) and active.get(dates[i - 1]):
            activate[dates[i]] = True
    act_months = [str(d.to_period("M")) for d, v in activate.items() if v]  # 与 ms["month"] 同格式
    cover = len(act_months) / max(1, len(dates))
    log(f"激活月 {len(act_months)} 个（覆盖 {cover:.0%}）: {act_months[:20]}")

    # ---------- 处理表：激活月删最小市值五分位 ----------
    conn = sqlite3.connect(f"{(ROOT / 'data' / 'neweco.db').as_uri()}?mode=ro", uri=True)
    ms = pd.read_sql("SELECT trade_date, ts_code, score FROM model_score", conn)
    conn.close()
    ms["d"] = pd.to_datetime(ms["trade_date"], format="%Y%m%d")
    ms["month"] = ms["d"].dt.to_period("M").astype(str)
    act_set = set(act_months)
    circ_long = circ_p.stack().rename("circ_mv").reset_index()
    circ_long["trade_date"] = circ_long["trade_date"].dt.strftime("%Y%m%d")
    circ_long["month"] = pd.to_datetime(circ_long["trade_date"], format="%Y%m%d") \
        .dt.to_period("M").astype(str)
    # 每月截面的市值分位（月末日）
    mv_pct = circ_long.groupby("month")["circ_mv"].rank(pct=True)
    circ_long["mv_pct"] = mv_pct
    drop_keys = set()
    for mth in act_months:
        sub = circ_long[(circ_long["month"] == mth) & (circ_long["mv_pct"] <= MV_Q)]
        drop_keys |= set(zip(sub["ts_code"], sub["month"]))
    is_drop = [(t, c) in drop_keys for t, c in
               zip(ms["ts_code"], ms["month"])]
    treat = ms[~pd.Series(is_drop, index=ms.index)]
    log(f"处理表: {len(ms):,} → {len(treat):,} 行（激活月删最小市值五分位）")

    tname = "crwd_cap"
    conn = sqlite3.connect(str(ROOT / "data" / "neweco.db"))
    conn.execute(f"CREATE TABLE IF NOT EXISTS {tname} ("
                 "ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL, "
                 "PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID")
    conn.execute(f"DELETE FROM {tname}")
    conn.commit()
    treat.drop(columns=["d", "month"]).assign(pred_ret=np.nan) \
        [["ts_code", "trade_date", "score", "pred_ret"]] \
        .to_sql(tname, conn, if_exists="append", index=False)
    conn.commit()
    conn.close()

    # ---------- 双窗回测 + 机械判决 ----------
    res = {}
    for wname, run_date in RUNS.items():
        start = "20190722" if wname == "prod" else None
        mm = run_backtest(score_table=tname, run_tag=f"crwd_{wname}", start=start)
        res[wname] = {"ann_return": round(float(mm["ann_return"]), 4),
                      "excess_ann": round(float(mm["excess_ann"]), 4),
                      "max_drawdown": round(float(mm["max_drawdown"]), 4),
                      "avg_turnover": round(float(mm["avg_turnover"]), 4)}
    checks = {}
    for w in ("prod", "full"):
        checks[f"p1_mdd_improve_{w}"] = bool(
            res[w]["max_drawdown"] >= PIN[w]["max_drawdown"] + 0.02)
        checks[f"p2_non_inferior_{w}"] = bool(
            res[w]["excess_ann"] >= PIN[w]["excess_ann"] - 0.015)
    p1 = checks["p1_mdd_improve_prod"]
    p2 = all(checks[f"p2_non_inferior_{w}"] for w in ("prod", "full"))
    p3 = bool(0.05 <= cover <= 0.40)
    if (not p2) or all(res[w]["max_drawdown"] - PIN[w]["max_drawdown"] < 0.01
                       for w in ("prod", "full")):
        verdict = "REJECT"
    elif p1 and p2 and p3:
        verdict = "PASS"
    else:
        verdict = "INDETERMINATE"
    log(f"判决: {verdict} (P1={p1} P2={p2} P3={p3})")

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"), "prereg": PREREG,
           "params": {"long_m": LONG_M, "recent_m": RECENT_M, "long_bar": LONG_BAR,
                      "mv_q": MV_Q, "block": "B1(size/amount_log/illiq)"},
           "activate_months": act_months,
           "activity_history_tail": hist[-24:],
           "runs": res, "checks": {**checks, "p3_coverage": p3},
           "verdict": {"PASS": "PASS（进生产接线预注册）", "REJECT": "REJECT（线结清）",
                       "INDETERMINATE": "INDETERMINATE"}[verdict],
           "note": "评分表层实现(零引擎改动); PASS 后生产接线另走预注册+回放复核",
           "runtime_sec": round(time.time() - t0, 1)}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"产物: {OUT}  ({time.time() - t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
