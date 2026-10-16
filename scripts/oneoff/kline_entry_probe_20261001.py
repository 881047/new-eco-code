# -*- coding: utf-8 -*-
"""K 线择机入场探针 —— `plans/kline-entry-timing-prereg-20261001.md` 的执行体。

问题: 生产入场 = 月锚点次日开盘(FILL_AT_OPEN); 对同一选股流, K 线条件化入场
(等回调/等弱势再买)能否优于 t+1 开盘。**只读零写库**。
设计核心 = oracle 包络判死门: 条件化入场价必然落在 {t+1 开盘} ∪ {第 1..10 日收盘}
之内 ⇒ 逐股票-月取 max_k 的组合均值是整个 K 线规则族的严格收益上界; 上界 < bar
(+0.50pp/年 部署地板) ⇒ DEAD, 条件臂不跑。
条件规则(冻结, 机制驱动): 锚点日收跌 ⇒ t+1 开盘买; 否则等窗口内第一个收跌日以该日
收盘买; 死线 = 第 10 日收盘。方向依据 = 反转池 + chase6(跑起来的票是更差买入)。
选股流: model_score → PIT 域 → chase 剔除 → top-6(行业帽≤2, 主流)/top-20(稳健档)。
产物 experiments/kline_entry_probe_20261001.json。
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

from app import config as cfg_mod
from app.factors.factor_engine import load_market
from app.pit.engine import PITEngine
from app.portfolio.chase import ChaseGate, theta_of

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "kline_entry_probe_20261001.json"
PREREG = "plans/kline-entry-timing-prereg-20261001.md"
D_WIN = 10                 # 入场窗口(交易日), 冻结
COST = 0.003               # single_side_cost, 两臂同率
BAR = 0.005                # +0.50pp/年 部署地板, 冻结
PROD_START = pd.Timestamp("2019-07-22")
UNBUYABLE_PCT = 5.0        # (high==low)∧(pct_chg>5) = 一字涨停不可买
ROLL_DAYS = 5              # 卖出停牌顺延上限


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def build_picks(score_panels, anchors, pit, gate, theta, top_n,
                industry=None, ind_cap=None):
    """选股流: PIT 域 + chase 剔除 + score 降序 (+ 行业帽 greedy)。"""
    picks = {}
    for a in anchors:
        s = score_panels.get(a)
        if s is None or s.dropna().empty:
            continue
        s = s.dropna()
        uni = pit.tradable_mask(a)
        s = s[[c for c in s.index if bool(uni.get(c, False))]]
        if s.empty:
            continue
        excl = gate.exclusions(a, s, theta=theta)
        s = s.drop(labels=[c for c in excl if c in s.index]).sort_values(ascending=False)
        out, used = [], {}
        for code, _v in s.items():
            if len(out) >= top_n:
                break
            if ind_cap is not None:
                ind = industry.get(code, "UNKNOWN")
                if used.get(ind, 0) >= ind_cap:
                    continue
                used[ind] = used.get(ind, 0) + 1
            out.append(code)
        picks[a] = out
    return picks


def main() -> int:
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = load_market()
    theta = theta_of(cfg) if cfg.get("chase", {}).get("enabled") else 2.0
    gate = ChaseGate(m=m, cfg=cfg)
    log(f"chase theta={theta}")

    adj = m["adj"]
    o_adj, c_adj = m["open"] * adj, m["close"] * adj
    unbuy = (m["high"] == m["low"]) & (m["pct"] > UNBUYABLE_PCT)
    dates = pd.DatetimeIndex(m["close"].index)

    conn = sqlite3.connect(f"{(ROOT / 'data' / 'neweco.db').as_uri()}?mode=ro", uri=True)
    ms = pd.read_sql("SELECT trade_date, ts_code, score FROM model_score", conn)
    ind_df = pd.read_sql("SELECT ts_code, industry FROM stock_basic", conn)
    conn.close()
    ms["d"] = pd.to_datetime(ms["trade_date"], format="%Y%m%d")
    score_panels = {d: g.set_index("ts_code")["score"] for d, g in ms.groupby("d")}
    anchors = pd.DatetimeIndex(sorted(score_panels))
    log(f"打分锚点 {len(anchors)}（{anchors[0].date()}..{anchors[-1].date()}）")
    industry = dict(zip(ind_df["ts_code"], ind_df["industry"]))
    pos_of = {d: i for i, d in enumerate(dates)}

    pit = PITEngine(m=m, cfg=cfg)
    picks6 = build_picks(score_panels, anchors[:-1], pit, gate, theta, 6,
                         industry=industry, ind_cap=2)
    picks20 = build_picks(score_panels, anchors[:-1], pit, gate, theta, 20)
    log(f"选股流: top-6 均值 {np.mean([len(v) for v in picks6.values()]):.1f} 只/期, "
        f"top-20 均值 {np.mean([len(v) for v in picks20.values()]):.1f} 只/期")

    def sell_open(code, nxt):
        """下一锚点次一交易日 open(顺延 ≤ROLL_DAYS; 该股停牌/缺价 ⇒ NaN)。"""
        p = pos_of[nxt] + 1
        for k in range(ROLL_DAYS + 1):
            if p + k < len(dates):
                v = o_adj.iat[p + k, o_adj.columns.get_loc(code)] \
                    if code in o_adj.columns else np.nan
                if not pd.isna(v) and v > 0:
                    return v
        return np.nan

    def sim_stream(picks, label):
        rows = []
        for a, stocks in picks.items():
            nxt = anchors[anchors.get_loc(a) + 1]
            pa = pos_of[a]
            d1 = pa + 1
            win = list(range(d1, min(d1 + D_WIN, len(dates))))
            if not win:
                continue
            for code in stocks:
                sell_px = sell_open(code, nxt)
                if pd.isna(sell_px) or sell_px <= 0:
                    continue
                ca = c_adj.iat[pa, c_adj.columns.get_loc(code)] \
                    if code in c_adj.columns else np.nan
                ca0 = c_adj.iat[pa - 1, c_adj.columns.get_loc(code)] \
                    if pa >= 1 and code in c_adj.columns else np.nan
                base_px = np.nan
                for d in win:
                    if code in unbuy.columns and not bool(unbuy.iat[d, unbuy.columns.get_loc(code)]):
                        v = o_adj.iat[d, o_adj.columns.get_loc(code)]
                        if not pd.isna(v) and v > 0:
                            base_px = v
                            break
                if pd.isna(base_px):
                    continue
                net = (1 - COST) ** 2
                r = {0: (sell_px / base_px) * net - 1}
                buyable_close = {}
                for d in win:
                    if code in unbuy.columns and not bool(unbuy.iat[d, unbuy.columns.get_loc(code)]):
                        v = c_adj.iat[d, c_adj.columns.get_loc(code)]
                        if not pd.isna(v) and v > 0:
                            buyable_close[d] = v
                for k in range(1, D_WIN + 1):
                    d = win[k - 1]
                    px = buyable_close.get(d)
                    if px is None:
                        prior = [dd for dd in buyable_close if dd < d]
                        px = buyable_close[max(prior)] if prior else None
                    if px is None:
                        continue
                    r[k] = (sell_px / px) * net - 1
                # 条件臂: 锚点日收跌 ⇒ k=0; 否则第一个收跌日收盘; 死线 = 窗口末最后可买收盘
                # cond_d 记窗口内偏移: 0=t+1开盘, 1..10=第 k 交易日收盘, 99=死线回退
                cond_px, cond_d = None, None
                if not pd.isna(ca) and not pd.isna(ca0) and ca < ca0:
                    cond_px, cond_d = base_px, 0
                else:
                    for k_i, d in enumerate(win, start=1):
                        if d not in buyable_close:
                            continue
                        cp = c_adj.iat[d - 1, c_adj.columns.get_loc(code)]
                        if not pd.isna(cp) and buyable_close[d] < cp:
                            cond_px, cond_d = buyable_close[d], k_i
                            break
                if cond_px is None and buyable_close:
                    lastd = max(buyable_close)
                    cond_px = buyable_close[lastd]
                    cond_d = 99
                if cond_px is None:
                    continue
                rows.append({"anchor": a, "code": code, "cond_d": cond_d,
                             **{f"k{k}": v for k, v in r.items()},
                             "cond": (sell_px / cond_px) * net - 1})
        df = pd.DataFrame(rows)
        if len(df):
            dist = df["cond_d"].value_counts().sort_index().to_dict()
            dist = {int(k): int(v) for k, v in dist.items()}
        else:
            dist = {}
        log(f"[{label}] 股票-月 {len(df):,}（条件臂入场日分布 k: {dist}）")
        return df

    def ann_stats(series, start_ts):
        s = series.sort_index()
        if len(s) < 12:
            return np.nan, np.nan, np.nan
        cum = float((1 + s).prod())
        years = max(0.5, (s.index[-1] - start_ts).days / 365.25)
        ann = cum ** (1 / years) - 1
        nav = (1 + s).cumprod()
        mdd = float((nav / nav.cummax() - 1).min())
        return ann, mdd, years

    streams = {}
    for label, picks in (("top6", picks6), ("top20", picks20)):
        df = sim_stream(picks, label)
        kcols = [c for c in df.columns if c.startswith("k")]
        base_m = df.groupby("anchor")["k0"].mean()
        k_delta = {}
        for k in range(1, D_WIN + 1):
            if f"k{k}" in df.columns:
                pair = df.dropna(subset=["k0", f"k{k}"])
                k_delta[k] = float((pair[f"k{k}"] - pair["k0"]).mean())
        oracle_m = df[kcols].max(axis=1).groupby(df["anchor"]).mean()
        cond_m = df.groupby("anchor")["cond"].mean()
        streams[label] = {"n": int(len(df)), "k_delta": k_delta,
                          "base_m": base_m, "oracle_m": oracle_m, "cond_m": cond_m}

    res = {}
    for label, st in streams.items():
        res[label] = {"n_stock_months": st["n"]}
        for wname in ("prod", "full"):
            start_ts = PROD_START if wname == "prod" else anchors[0]
            f = lambda s: s[s.index >= start_ts]
            b_ann, b_mdd, yrs = ann_stats(f(st["base_m"]), start_ts)
            o_ann, _, _ = ann_stats(f(st["oracle_m"]), start_ts)
            c_ann, c_mdd, _ = ann_stats(f(st["cond_m"]), start_ts)
            res[label][wname] = {
                "years": round(yrs, 2), "base_ann": round(b_ann, 4),
                "oracle_env_ann": round(o_ann, 4),
                "oracle_delta_ann": round(o_ann - b_ann, 4),
                "cond_ann": round(c_ann, 4),
                "cond_delta_ann": round(c_ann - b_ann, 4),
                "base_mdd": round(b_mdd, 4), "cond_mdd": round(c_mdd, 4),
                "cond_delta_mdd": round(c_mdd - b_mdd, 4)}
        log(f"[{label}] prod: base={res[label]['prod']['base_ann']:.4f} "
            f"oracle_env Δ={res[label]['prod']['oracle_delta_ann']:+.4f} "
            f"cond Δ={res[label]['prod']['cond_delta_ann']:+.4f}")

    main6 = res["top6"]
    e_gate = bool(main6["prod"]["oracle_delta_ann"] >= BAR)
    checks, verdict = {}, None
    if not e_gate:
        verdict = ("DEAD（oracle 包络判死：10 日窗口内不存在能过部署地板的入场日，"
                   "任何 K 线规则都一样）")
    else:
        d_prod = main6["prod"]["cond_delta_ann"]
        d_full = main6["full"]["cond_delta_ann"]
        p1 = bool(d_prod >= BAR and d_full > 0)
        p2 = bool(main6["prod"]["cond_delta_mdd"] >= -0.01
                  and main6["full"]["cond_delta_mdd"] >= -0.01)
        checks = {"p1": p1, "p2": p2, "d_prod": d_prod, "d_full": d_full, "bar": BAR}
        if d_prod <= 0 or not p2:
            verdict = "REJECT（线结清）"
        elif p1 and p2:
            verdict = "PASS（进生产接线预注册）"
        else:
            verdict = "INDETERMINATE（包络有肉但冻结规则抓不到/双窗异号）"
    log(f"判决: {verdict}")

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"), "prereg": PREREG,
           "params": {"d_win": D_WIN, "cost": COST, "bar": BAR, "theta": theta,
                      "unbuyable": "high==low & pct_chg>5", "roll_days": ROLL_DAYS},
           "k_delta_monthly_top6": {str(k): round(v, 5)
                                    for k, v in streams["top6"]["k_delta"].items()},
           "results": res, "e_gate": e_gate, "checks": checks, "verdict": verdict,
           "runtime_sec": round(time.time() - t0, 1)}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"产物: {OUT}  ({time.time() - t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
