# -*- coding: utf-8 -*-
"""O4 EPS 修正动量剔除器 A/B —— plans/eps-revision-prereg-20260929.md §3.3 执行体。

触发来源：eps_revision_eval_20260929 判定 rev_fy1_1m aS1∧aS2∧S4 ⇒ fire。
构型（预注册冻结）：月末 rev_fy1_1m（中性化口径）在当期候选域（model_score ∩ PIT
可交易域）内 top 10% 分位剔除、不补位（chase6 同款，ChaseSeam 缝注入引擎零改动）。

臂：ctrl（生产原样，有效性门=逐位复现 pin 20260928_exe）/ B（剔除臂）/ 85 期窗
（20190731..20260831）；ctrl_fh / B_fh 全史窗（20170126..20260831）确认结构性 Δ≈0
（2019-07..2024-03 rev 无值不剔除）。

判据（预注册 §3.3，跑前冻结）：
- P1：ACTIVE 段（2024-01..2026-08 子窗）Δsys_ann ≥ +2.0pp；
- P2：ACTIVE 段 MDD 劣化 < 2pp；P3：换手 ≤ +5pp。
- 单窗结构性条款（预注册）：双窗同向判据结构性不可得 ⇒ 判决上限 = 候选登记，
  无论读数不进生产；前瞻复验点 ≈2027-01。
- 注记：预注册 P1 的"双种子集"表述沿组合层档模板——组合层回测为确定性（score 表
  固定、无重训），无种子随机源，按确定性读数执行；单窗不确定度由结构性条款覆盖。
run 留痕 20260929_abeps_* 写 backtest_* 表，保留不自清（20260926_abrisk 先例）。
"""
import json
import sqlite3
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from ab_governor_overlay import _metrics_from, _nav_from_db  # noqa: E402
from app import db  # noqa: E402
from app.backtest import engine as E  # noqa: E402
from app.factors import factor_engine as fe  # noqa: E402
from app.factors.factor_engine import load_market  # noqa: E402
from app.pit.engine import PITEngine  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "eps_revision_ab_20260929.json"
PREREG = "plans/eps-revision-prereg-20260929.md"
WIN = {"start": "20190731", "end": "20260831"}
WIN_FH = {"start": "20170126", "end": "20260831"}
N_PERIODS, N_PERIODS_FH = 85, 115
TOP_FRAC_EXCL = 0.10
ACTIVE_LO, ACTIVE_HI = "20240101", "20260831"
RUN_TAG = "20260929_abeps_"
# 有效性门：CLAUDE.md §0 现役 pin 20260928_exe（85 期）
PIN = {"ann": 0.3168, "mdd": -0.2363, "sys_ann": 0.2913, "sys_mdd": -0.1817}
PIN_TOL = 5e-4


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _anchors(lo, hi, expect):
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM model_score "
            "WHERE trade_date>=? AND trade_date<=? ORDER BY trade_date",
            (lo, hi)).fetchall()
    finally:
        conn.close()
    anchors = [r[0] for r in rows]
    assert len(anchors) == expect, f"锚点 {len(anchors)} != 期望 {expect}"
    return anchors


def build_excl_map(anchors, industry):
    """每锚点的剔除池：候选域内 rev_fy1_1m top 10% 分位（中性化口径，PIT）。"""
    m = load_market()
    td = pd.DatetimeIndex(m["close"].index)
    codes = list(m["close"].columns)
    conn = db.get_conn()
    try:
        rep = pd.read_sql(
            "SELECT ts_code, report_date, fy1_eps FROM research_report_em "
            "WHERE fy1_eps IS NOT NULL", conn)
    finally:
        conn.close()
    rep["t"] = pd.to_datetime(rep["report_date"], format="%Y%m%d")
    V = np.full((len(td), len(codes)), np.nan, dtype=np.float32)
    td_ix = {d: i for i, d in enumerate(td)}
    code_ix = {c: i for i, c in enumerate(codes)}
    for t, c, v in zip(rep["t"], rep["ts_code"], rep["fy1_eps"]):
        i, j = td_ix.get(t), code_ix.get(c)
        if i is not None and j is not None:
            V[i, j] = v
    M1 = pd.DataFrame(V, index=td, columns=codes).ffill()
    rev = (M1 - M1.shift(21)) / M1.shift(21).abs().replace(0, np.nan)
    rev = fe.neutralize_sparse(rev, industry)
    pit = PITEngine(m=m)
    dates = pit.dates

    conn = db.get_conn()
    try:
        scores = pd.read_sql(
            "SELECT trade_date, ts_code, score FROM model_score "
            "WHERE trade_date>=? AND trade_date<=?",
            conn, params=(anchors[0], anchors[-1]))
    finally:
        conn.close()
    excl_map, audit = {}, {"n_dom": [], "n_excl": []}
    for a in anchors:
        d = pd.Timestamp(a)
        uni = pit.tradable_mask(d)
        dom = scores[scores.trade_date == a]
        dom = [c for c in dom.ts_code if uni.get(c, False)]
        r = rev.loc[d].reindex(dom).dropna() if d in rev.index else pd.Series(dtype=float)
        k = max(1, int(round(len(dom) * TOP_FRAC_EXCL))) if len(dom) else 0
        pool = set(r.nlargest(k).index) if k else set()
        excl_map[a] = pool
        audit["n_dom"].append(len(dom))
        audit["n_excl"].append(len(pool))
    return excl_map, audit


class RevSeam:
    """allocate_lots 缝：剔除剔除池内候选行（不补位，chase 同款）。"""

    def __init__(self, anchors, excl_map, n_expected):
        self.anchors = anchors
        self.excl_map = excl_map
        self.n_expected = n_expected
        self._orig = E.allocate_lots
        self.n_calls = 0
        self.pool_hits = 0
        self.per_period = []

    def _alloc(self, lot_df, equity_cash, max_stocks, **kw):
        p = self.n_calls // 2
        self.n_calls += 1
        anchor = self.anchors[p] if p < len(self.anchors) else None
        pool = self.excl_map.get(anchor, set())
        if lot_df is None or len(lot_df) == 0 or not pool:
            if self.n_calls % 2 == 1:
                self.per_period.append(0)
            return self._orig(lot_df, equity_cash, max_stocks, **kw)
        drop = [c for c in lot_df.index if c in pool]
        lot2 = lot_df.drop(index=drop)
        if self.n_calls % 2 == 1:
            self.per_period.append(len(drop))
        self.pool_hits += len(drop)
        return self._orig(lot2, equity_cash, max_stocks, **kw)

    def __enter__(self):
        E.allocate_lots = self._alloc
        return self

    def __exit__(self, *exc):
        E.allocate_lots = self._orig
        expected = 2 * self.n_expected
        if self.n_calls != expected:
            raise RuntimeError(f"缝对账失败: {self.n_calls} != 2×{self.n_expected}")
        return False


def run_arm(arm, anchors, excl_map, win, tag_prefix, n_expected):
    tag = tag_prefix + arm
    t0 = time.time()
    if arm == "B":
        with RevSeam(anchors, excl_map, n_expected) as seam:
            metrics = E.run_backtest(run_tag=tag, start=win["start"], end=win["end"])
        extra = {"pool_hits": seam.pool_hits, "per_period": seam.per_period}
    else:
        metrics = E.run_backtest(run_tag=tag, start=win["start"], end=win["end"])
        extra = {}
    out = _metrics_from(metrics["run_date"], metrics)
    out.update({"arm": arm, "run_tag": tag, "run_date": metrics["run_date"],
                "elapsed_sec": round(time.time() - t0, 1), **extra})
    log(f"[{arm:>7}] ann {out['ann']:+.4f} mdd {out['mdd']:+.4f} sys {out['sys_ann']:+.4f}/"
        f"{out['sys_mdd']:+.4f} ({out['elapsed_sec']}s)")
    return out


def active_segment(run_date):
    """ACTIVE 段（20240101..20260831）年化/回撤（系统线，ppy 自适应）。"""
    rows = _nav_from_db(run_date)
    rows["trade_date"] = rows["trade_date"].astype(str)
    seg = rows[(rows.trade_date >= ACTIVE_LO) & (rows.trade_date <= ACTIVE_HI)]
    nav = seg["sys_nav"].astype(float).reset_index(drop=True)
    n = len(nav)
    if n < 2:
        return {"sys_ann_active": None, "sys_mdd_active": None,
                "n_nav_rows": n, "note": "段行数不足"}
    ppy = 12.0 * n / max(1, (len(rows) - 1))
    years = n / ppy
    ann = (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1
    mdd = float((nav / nav.cummax() - 1).min())
    return {"sys_ann_active": round(float(ann), 4), "sys_mdd_active": round(mdd, 4),
            "n_nav_rows": n}


def main() -> int:
    t0 = time.time()
    conn = sqlite3.connect(f"{(ROOT / 'data' / 'neweco.db').as_uri()}?mode=ro", uri=True)
    industry = fe.industry_map(conn)
    conn.close()

    anchors = _anchors(WIN["start"], WIN["end"], N_PERIODS + 1)
    anchors_fh = _anchors(WIN_FH["start"], WIN_FH["end"], N_PERIODS_FH + 1)
    excl_map, audit = build_excl_map(anchors, industry)
    excl_map_fh, audit_fh = build_excl_map(anchors_fh, industry)
    log(f"剔除池构建完成：85 期窗 域均值 {np.mean(audit['n_dom']):.0f} / "
        f"剔均值 {np.mean(audit['n_excl']):.1f} / 生效锚点 "
        f"{sum(1 for v in excl_map.values() if v)}；全史窗生效锚点 "
        f"{sum(1 for v in excl_map_fh.values() if v)}")

    results = {}
    for arm in ("ctrl", "B"):
        results[arm] = run_arm(arm, anchors, excl_map, WIN, RUN_TAG, N_PERIODS)
    for arm in ("ctrl_fh", "B_fh"):
        results[arm] = run_arm(arm, anchors_fh, excl_map_fh, WIN_FH, RUN_TAG + "fh_",
                               N_PERIODS_FH)

    # 有效性门：ctrl 逐位复现 pin
    pin_check = {k: abs(results["ctrl"][k] - v) <= PIN_TOL for k, v in PIN.items()}

    seg = {a: active_segment(results[a]["run_date"]) for a in ("ctrl", "B")}
    d_active = {
        "sys_ann_pp": round((seg["B"]["sys_ann_active"] - seg["ctrl"]["sys_ann_active"]) * 100, 2),
        "sys_mdd_pp": round((seg["B"]["sys_mdd_active"] - seg["ctrl"]["sys_mdd_active"]) * 100, 2),
    }
    d_fh = {"sys_ann_pp": round((results["B_fh"]["sys_ann"] - results["ctrl_fh"]["sys_ann"]) * 100, 2)}
    turn = {"ctrl": results["ctrl"].get("turnover"), "B": results["B"].get("turnover")}
    p1 = d_active["sys_ann_pp"] >= 2.0
    p2 = d_active["sys_mdd_pp"] > -2.0
    p3 = (turn["B"] - turn["ctrl"]) <= 0.05 if turn["ctrl"] is not None else None
    verdict = {
        "P1_active_sys_ann": {"delta_pp": d_active["sys_ann_pp"], "pass": bool(p1)},
        "P2_active_mdd": {"delta_pp": d_active["sys_mdd_pp"], "pass": bool(p2)},
        "P3_turnover": {"ctrl": turn["ctrl"], "B": turn["B"], "pass": p3},
        "fullhist_structural": {"delta_pp": d_fh["sys_ann_pp"],
                                "note": "全史窗剔除极少生效（rev 无值期两臂同构）"},
        "pin_check": pin_check,
        "verdict": ("PASS 上限=候选登记（单窗结构性条款，不进生产；前瞻复验 ~2027-01）"
                    if (p1 and p2 and (p3 is not False)) else
                    "REJECT 线结清（P1/P2/P3 有未过）"),
    }
    log(f"P1 Δsys_ann(ACTIVE) {d_active['sys_ann_pp']:+.2f}pp pass={p1} | "
        f"P2 Δmdd {d_active['sys_mdd_pp']:+.2f}pp pass={p2} | P3 pass={p3} | "
        f"全史 Δ {d_fh['sys_ann_pp']:+.2f}pp | pin 门 {all(pin_check.values())}")
    log(f"判决: {verdict['verdict']}")

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"), "prereg": PREREG,
           "config": {"top_frac_excl": TOP_FRAC_EXCL, "active_seg": [ACTIVE_LO, ACTIVE_HI],
                      "n_dom_mean": float(np.mean(audit["n_dom"])),
                      "n_excl_mean": float(np.mean(audit["n_excl"]))},
           "arms": results, "active_segments": seg, "verdict": verdict,
           "runtime_sec": round(time.time() - t0, 1)}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"产物: {OUT}  ({out['runtime_sec']}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
