# -*- coding: utf-8 -*-
"""低谷加仓 T1 前瞻观察器 —— 台账五十补 PASS 判决的前置义务（用户 2026-10-01 拍板"同意"）。

职责（只读观察，零资金零交易，不写生产表）：
  1. 逐月重算 B1 块 IC 冷判定（冻结定义 = plans/trough-tilt-prereg-20260930.md §一，
     与 crowding_cap_20260928 / trough_tilt_20260930 同源；三处实现各自内联 =
     oneoff 证据自包含原则，修改须新预注册）；
  2. 记录**新触发事件**（2024-07 之后的激活锚点）及其已实现镜像价差
     （EW 最低市值五分位 − EW 全池，naive 21 日，前瞻窗完成即记）；
  3. 拍板条件进度：**≥2 个新触发事件 或 ≥12 个月日历（先到者）** ⇒ READY
     （提请生产接线预注册拍板），否则 WAITING。
新鲜状态语义：冷判定只用**前序** IC（无前视），故最新月末日锚点即可评估 =
"当前持仓月是否处于镜像激活窗"——与冻结公式一致，oneoff 只在 fwd 完整锚点上
迭代是其实现形状而非定义限制。
状态 = data/trough_tilt_observer/state.json（全量重算 + 幂等合并，可随时删除重建）。
调度 = 计划任务 NEWECO_trough_tilt_observer_monthly（每月 6 日 21:00，wscript 静默包装）。
"""
import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "data" / "trough_tilt_observer"
STATE = STATE_DIR / "state.json"

B1 = ("size", "amount_log", "illiq")
LONG_M, RECENT_M, LONG_BAR = 24, 6, 0.01
MV_Q_LO, MIN_NAMES = 0.20, 200
OBSERVER_START = date(2026, 10, 1)
BASELINE_EPISODE_END = "2024-07"      # L3 历史事件截止；之后为"新事件"
GATE_NEW_EPISODES, GATE_MONTHS = 2, 12
HISTORY_CAP = 60


# ---------------- 纯函数（单测覆盖） ----------------

def _is_cold(window: "pd.Series") -> bool:
    long_v = float(window.iloc[-LONG_M:].mean())
    recent_v = float(window.iloc[-RECENT_M:].mean())
    return bool(recent_v * (1.0 if long_v >= 0 else -1.0) < 0
                and abs(long_v) >= LONG_BAR)


def cold_flags(ic: "pd.Series", ext_index=None) -> "pd.Series":
    """冷判定（冻结）: recent(6)×sign(long(24))<0 且 |long|≥0.01；窗口=锚点前序
    完整 IC（不含当月）；前序不足 24 期 = 不可判 False（与 oneoff 语义一致）。
    ext_index 给定时在其全部锚点上评估（最新锚点自身 IC 可缺——冷判定只用前序）。"""
    idx = pd.DatetimeIndex(ext_index) if ext_index is not None else pd.DatetimeIndex(ic.index)
    out = {}
    for d in idx:
        window = ic[ic.index < d]
        out[d] = False if len(window) < LONG_M else _is_cold(window)
    return pd.Series(out, dtype=bool)


def activation_flags(cold: "pd.Series") -> "pd.Series":
    """连续 2 期冷 ⇒ 该锚点激活（与 oneoff 逐字同语义）。"""
    flags, vals = {}, list(cold.items())
    for i, (d, c) in enumerate(vals):
        flags[d] = bool(i >= 1 and c and vals[i - 1][1])
    return pd.Series(flags, dtype=bool)


def group_episodes(months: list) -> list:
    """连续月聚成事件。months = ['2024-06', ...] 升序。"""
    eps = []
    for m in sorted(months):
        p = pd.Period(m)
        if eps and (p - pd.Period(eps[-1][-1])).n <= 1:
            eps[-1].append(m)
        else:
            eps.append([m])
    return eps


def realized_spread(mv_pct_row: "pd.Series", fwd_row: "pd.Series",
                    q_lo: float = MV_Q_LO) -> dict:
    """单锚点已实现镜像价差: EW(最低五分位) − EW(全池)（naive 前瞻收益口径）。"""
    small = mv_pct_row[mv_pct_row <= q_lo].index
    r_small = float(fwd_row.reindex(small).mean()) if len(small) else None
    r_pool = float(fwd_row.mean())
    return {"r_small": None if r_small is None else round(r_small, 4),
            "r_pool": round(r_pool, 4),
            "spread": None if r_small is None else round(r_small - r_pool, 4),
            "n_small": int(len(small))}


def new_episode_count(episodes: list) -> int:
    """新触发事件数 = 最后一个月严格晚于基线事件截止的事件数。"""
    return sum(1 for ep in episodes if ep[-1] > BASELINE_EPISODE_END)


def gate_status(n_new: int, today: date) -> tuple:
    months_elapsed = (today.year - OBSERVER_START.year) * 12 \
        + (today.month - OBSERVER_START.month)
    ready = n_new >= GATE_NEW_EPISODES or months_elapsed >= GATE_MONTHS
    return ("READY", months_elapsed) if ready else ("WAITING", months_elapsed)


def merge_state(state: dict, entry: dict) -> dict:
    """幂等合并: history 追加并截断；其余字段整写（全量重算语义）。"""
    hist = list(state.get("history", [])) + [entry]
    return {**state, **{k: v for k, v in entry.items()
                        if k not in ("ran_at", "gate", "months_elapsed")},
            "history": hist[-HISTORY_CAP:]}


# ---------------- IO（--run） ----------------

def compute(m=None, fwd=None):
    from app.alpha import gate
    from app.factors.factor_engine import load_market, month_end_dates
    if m is None:
        m = load_market()
    if fwd is None:
        fwd = gate.forward_return(m, gate.HORIZON)
    conn = sqlite3.connect(f"{(ROOT / 'data' / 'neweco.db').as_uri()}?mode=ro", uri=True)
    fac = pd.read_sql("SELECT trade_date, ts_code, factor, value FROM factor "
                      "WHERE factor IN ('size','amount_log','illiq')", conn)
    conn.close()
    fac["trade_date"] = pd.to_datetime(fac["trade_date"].astype(str))
    md_eval = pd.DatetimeIndex([d for d in month_end_dates(
        pd.DatetimeIndex(m["close"].index)) if d >= pd.Timestamp("2019-01-01")])
    anchors = pd.DatetimeIndex(sorted(fac["trade_date"].unique()))
    anchors = anchors[anchors.isin(md_eval)]
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
        ranks = pd.DataFrame({f: jn[f].rank() for f in B1})
        ranks["r"] = jn["r"].rank()
        ic[d] = float(ranks.corr().loc["r", list(B1)].mean())
    ic = pd.Series(ic).sort_index()
    cold = cold_flags(ic, ext_index=anchors)      # 最新月末日锚点也可评估（新鲜状态）
    act = activation_flags(cold)
    return m, fwd, anchors, ic, cold, act


def realized_spreads(m, fwd, act_dates):
    """已完成前瞻窗的激活锚点 → 已实现镜像价差（口径 = 探针 §二.1 标定逐字同源：
    月内混池 pct rank + 月末日行取分位成员）。"""
    cl = m["circ_mv"].stack().rename("circ_mv").reset_index()
    cl["trade_date"] = cl["trade_date"].dt.strftime("%Y%m%d")
    cl["month"] = pd.to_datetime(cl["trade_date"], format="%Y%m%d").dt.to_period("M")
    cl["mv_pct"] = cl.groupby("month")["circ_mv"].rank(pct=True)
    out = {}
    for d in act_dates:
        if d not in fwd.index or fwd.loc[d].dropna().empty:
            continue
        mth = d.to_period("M")
        sub = cl[cl["month"] == mth]
        if sub.empty:
            continue
        sub = sub[sub["trade_date"] == sub["trade_date"].max()]
        mv_pct = pd.Series(sub["mv_pct"].to_numpy(), index=sub["ts_code"].to_numpy())
        out[str(mth)] = realized_spread(mv_pct, fwd.loc[d].dropna())
    return out


def run(today: date) -> dict:
    m, fwd, anchors, ic, cold, act = compute()
    act_dates = [d for d, v in act.items() if v]
    act_months = [str(d.to_period("M")) for d in act_dates]
    episodes = group_episodes(act_months)
    spreads = realized_spreads(m, fwd, act_dates)
    n_new = new_episode_count(episodes)
    status, months_elapsed = gate_status(n_new, today)
    entry = {
        "ran_at": str(today), "gate": status, "months_elapsed": months_elapsed,
        "new_episode_count": n_new,
        "latest_ic_anchor": str(ic.index[-1].date()) if len(ic) else None,
        "current_status": {"anchor": str(act.index[-1].date()) if len(act) else None,
                           "cold": bool(cold.iloc[-1]) if len(cold) else False,
                           "activation": bool(act.iloc[-1]) if len(act) else False},
        "activation_months": act_months, "episodes": episodes,
        "new_episodes": [ep for ep in episodes if ep[-1] > BASELINE_EPISODE_END],
        "realized_spreads": spreads,
    }
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    state = merge_state(state, entry)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # 2026-10-02 审查改原子写(tmp + os.replace, 与 block_ic_refresh 缓存同款):
    # 原直写终路径, 读半截状态文件的窗口虽小(单写者月频)但 --status 与 --run 可能并发。
    import os
    tmp = Path(str(STATE) + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    os.replace(tmp, STATE)
    return entry


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="执行观察（缺省即 --run）")
    ap.add_argument("--status", action="store_true", help="只读打印现有状态")
    args = ap.parse_args()
    if args.status:
        if not STATE.exists():
            print("无状态文件 —— 尚未跑过 --run")
            return 0
        s = json.loads(STATE.read_text(encoding="utf-8"))
        print(json.dumps(s.get("history", [])[-1], ensure_ascii=False, indent=1))
        return 0
    entry = run(date.today())
    print(f"[observer] gate={entry['gate']} new_episodes={entry['new_episode_count']} "
          f"months_elapsed={entry['months_elapsed']}")
    print(f"[observer] latest_ic_anchor={entry['latest_ic_anchor']} "
          f"current_status={entry['current_status']}")
    print(f"[observer] activation_months={entry['activation_months']}")
    print(f"[observer] episodes={entry['episodes']}")
    for mth, sp in entry["realized_spreads"].items():
        print(f"[observer] spread {mth}: {sp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
