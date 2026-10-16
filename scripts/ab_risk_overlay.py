# -*- coding: utf-8 -*-
"""月盘量价风控层 A/B —— 建仓时追高剔除 / 市场风险闸（2026-09-26）

判据与臂设计**先冻结**在 `plans/risk-overlay-prereg-20260926.md`，本文件只是执行体：

  · base            生产 config 原样 —— 有效性门：逐位复现 pin `20260922_trbench` 7 项(5e-4)
  · base20          candidate_top=20(引擎 nlargest(20) + allocate_lots 向下补位) —— 诊断臂,
                    把"补位效应"从"剔除效应"里剥出来(P5-1 先例: 补位在 1 万本金下恶化回撤)
  · chase6 / chase20  建仓名单剔除 chase_z>2.0 的候选(6=不补位 / 20=向下补位), θ∈{1.5,2.5} 只报告
  · gate            中证500 近 20 交易日回撤 ≤ −8% ⇒ 当月 equity_cash ×0.5

信号(冻结): chase_z = max(z(mom5), z(devvwap)), 截面 = 当期锚点 model_score ∩ PIT 可交易域;
mom5 = adjClose(d)/adjClose(d−5)−1; devvwap = close(d)/vwap5−1, vwap5 = 10×Σamount/Σvol(近5日含d)。
无前视: 只用锚点日 d 及以前的数据。

方法: 缝注入(引擎零改动, 同 `scripts/ab_governor_overlay.py` 先例) —— `E.allocate_lots`
包一层(每期恰 2 次调用, 退出断言 2×期数), 指标复用 governor 框架的 `_metrics_from`/
`_nav_from_db`/`PIN`(不写第二套指标实现)。只读生产表; run 留痕
`20260926_abrisk_m_*` 写 backtest_* 表, 保留不自清(同 20260922_trbench 先例)。
"""
import argparse

import numpy as np
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pandas as pd

from ab_governor_overlay import PIN, _metrics_from, _nav_from_db  # noqa: E402
from app import db  # noqa: E402  (ab_governor_overlay 导入时已 setup_logger/init_db)
from app.backtest import engine as E  # noqa: E402
from app.factors.factor_engine import load_market  # noqa: E402
from app.pit.engine import PITEngine  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WIN = {"start": "20190731", "end": "20260831"}          # 与 pin/governor A/B 同窗
ANCHOR_LO, ANCHOR_HI = "20190731", "20260831"
N_PERIODS_EXPECT = 85
THETA_MAIN = 2.0
THETA_GRID = [1.5, 2.5, 3.0]                             # 灵敏度/剂量网格(第三轮)
GATE_DD = -0.08                                          # 中证500 近20日回撤阈值
GATE_SCALE = 0.5
BM_GATE = "000905.SH"
RUN_TAG = "20260926_abrisk_m_"
CRIT = {"dd_improve": 0.030, "ann_cost": 0.020}          # 冻结判据 X(§五)


# ---------------------------------------------------------------- 信号预计算
def _anchors(lo: str, hi: str, expect: int) -> list:
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


def _chase_and_gate(anchors: list, signal: str = "daily"):
    """返回 (chase: {θ: {anchor: {code: z}}}, gate: {anchor: bool}, audit: dict)"""
    panel = load_market(since=None)
    pit = PITEngine(m=panel)
    dates = pit.dates
    close, adj = panel["close"], panel["adj"]
    amount, vol = panel["amount"], panel["vol"]
    conn = db.get_conn()
    try:
        bm = pd.read_sql(
            "SELECT trade_date, close FROM index_daily WHERE ts_code=?",
            conn, params=(BM_GATE,)).set_index("trade_date")["close"]
    finally:
        conn.close()

    chase = {t: {} for t in [THETA_MAIN] + THETA_GRID}
    gate, audit = {}, {"n_universe": [], "n_z": [], "gate_periods": 0}
    lr5 = cv5 = None
    if signal == "min60":
        # F75/r4 冻结配方(第三轮预注册 §一.2): x2/x3 净化 → 5 日滚动 → 锚点交易日取值
        # (不用 resample("ME")——日历月末与交易日月末错位会引入一个月陈旧)
        d60 = pd.read_parquet(ROOT / "data" / "min60_derived" / "daily.parquet")
        d60 = d60[(d60["x2"].abs() <= 0.21) & (d60["x3"].abs() <= 0.15)]
        lr = d60.pivot(index="date", columns="ts_code", values="x2")
        cv = d60.pivot(index="date", columns="ts_code", values="x3")
        lr.index = pd.to_datetime(lr.index)
        cv.index = pd.to_datetime(cv.index)
        lr5, cv5 = lr.rolling(5).sum(), cv.rolling(5).mean()
        print(f"[min60] 覆盖 {lr.index.min().date()} ~ {lr.index.max().date()} "
              f"({lr.shape[1]} 只)", flush=True)
    for a in anchors:
        d = pd.Timestamp(a)
        pos = dates.get_loc(d)
        uni = pit.tradable_mask(d)
        # 当期锚点打分 ∩ 可交易域
        conn = db.get_conn()
        try:
            sc = pd.read_sql(
                "SELECT ts_code, score FROM model_score WHERE trade_date=?",
                conn, params=(a,))
        finally:
            conn.close()
        codes = [c for c in sc["ts_code"] if uni.get(c, False)]
        audit["n_universe"].append(len(codes))
        # 近 5 日(含 d)信号; 向量化取整列再切行
        c_raw = close.iloc[pos].astype(float)
        c_adj = (close.iloc[pos] * adj.iloc[pos]).astype(float)
        c_adj0 = (close.iloc[pos - 5] * adj.iloc[pos - 5]).astype(float)
        amt5 = amount.iloc[pos - 4: pos + 1].sum().astype(float)
        vol5 = vol.iloc[pos - 4: pos + 1].sum().astype(float)
        if signal == "min60":
            r_lr = lr5.loc[d] if d in lr5.index else pd.Series(dtype=float)
            r_cv = cv5.loc[d] if d in cv5.index else pd.Series(dtype=float)
            z1 = r_lr.reindex(codes).dropna()
            z2 = r_cv.reindex(codes).dropna()
            z1 = (z1 - z1.mean()) / z1.std(ddof=1) if len(z1) > 2 else z1 * np.nan
            z2 = (z2 - z2.mean()) / z2.std(ddof=1) if len(z2) > 2 else z2 * np.nan
            cz = pd.concat([z1, z2], axis=1).max(axis=1).dropna()
        else:
            mom5 = c_adj / c_adj0 - 1.0
            vwap5 = 10.0 * amt5 / vol5.replace(0.0, np.nan)
            devvwap = c_raw / vwap5.astype(float) - 1.0
            z1 = mom5.reindex(codes)
            z2 = devvwap.reindex(codes)
            z1 = (z1 - z1.mean()) / z1.std(ddof=1)
            z2 = (z2 - z2.mean()) / z2.std(ddof=1)
            cz = pd.concat([z1, z2], axis=1).max(axis=1).dropna()   # max(z1, z2)
        audit["n_z"].append(int(len(cz)))
        for t in chase:
            chase[t][a] = {c: float(v) for c, v in cz[cz > t].items()}
        # 市场闸: 中证500 近 20 交易日(含 d)回撤
        bm_d = bm.reindex([x.strftime("%Y%m%d") for x in
                           dates[max(0, pos - 19): pos + 1]]).astype(float)
        dd20 = float(bm_d.iloc[-1] / bm_d.max() - 1.0)
        gate[a] = bool(dd20 <= GATE_DD)
        audit["gate_periods"] += int(gate[a])
    return chase, gate, audit


# ---------------------------------------------------------------- 缝注入
class ChaseSeam:
    """allocate_lots 缝: 剔除 chase_z>θ 的候选行(补位交给引擎现有向下贪心机制)。"""

    def __init__(self, anchors: list, theta_map: dict, n_expected: int):
        self.anchors = anchors
        self.zmap = theta_map          # {anchor: {code: z}}
        self.n_expected = n_expected
        self._orig = E.allocate_lots
        self.n_calls = 0
        self.pool_hits = 0             # 池内实际剔除数(两线合计)
        self.per_period = []           # 每期剔除数(首线口径)

    def _alloc(self, lot_df, equity_cash, max_stocks, **kw):
        p = self.n_calls // 2
        self.n_calls += 1
        anchor = self.anchors[p] if p < len(self.anchors) else None
        zmap = self.zmap.get(anchor, {})
        if lot_df is None or len(lot_df) == 0 or not zmap:
            if self.n_calls % 2 == 1:
                self.per_period.append(0)
            return self._orig(lot_df, equity_cash, max_stocks, **kw)
        drop = [c for c in lot_df.index if c in zmap]
        n0 = len(lot_df)
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
            raise RuntimeError(
                f"缝对账失败: allocate_lots 调用 {self.n_calls} != 2×{self.n_expected}"
                " —— 期号对齐假设失效, 中止")
        return False


class GateSeam:
    """allocate_lots 缝: 闸开当月 equity_cash ×0.5(满仓线/系统线同乘, 无退出动作)。"""

    def __init__(self, anchors: list, gate: dict, n_expected: int):
        self.anchors = anchors
        self.gate = gate
        self.n_expected = n_expected
        self._orig = E.allocate_lots
        self.n_calls = 0
        self.scaled_periods = 0

    def _alloc(self, lot_df, equity_cash, max_stocks, **kw):
        p = self.n_calls // 2
        self.n_calls += 1
        anchor = self.anchors[p] if p < len(self.anchors) else None
        if self.gate.get(anchor):
            equity_cash = equity_cash * GATE_SCALE
            if self.n_calls % 2 == 1:
                self.scaled_periods += 1
        return self._orig(lot_df, equity_cash, max_stocks, **kw)

    def __enter__(self):
        E.allocate_lots = self._alloc
        return self

    def __exit__(self, *exc):
        E.allocate_lots = self._orig
        expected = 2 * self.n_expected
        if self.n_calls != expected:
            raise RuntimeError(
                f"缝对账失败: allocate_lots 调用 {self.n_calls} != 2×{self.n_expected}")
        return False


# ---------------------------------------------------------------- 跑臂
def run_arm(arm: str, anchors: list, chase: dict, gate: dict,
            win: dict, tag_prefix: str, n_expected: int) -> dict:
    tag = tag_prefix + arm
    kw = dict(start=win["start"], end=win["end"])
    seam = None
    t0 = time.time()
    if arm == "base20":
        kw["candidate_top"] = 20
    elif arm.startswith("chase20@"):
        kw["candidate_top"] = 20
        theta = float(arm.split("@")[1])
        seam = ChaseSeam(anchors, chase[theta], n_expected)
    elif arm.startswith("chase6@"):
        seam = ChaseSeam(anchors, chase[float(arm.split("@")[1])], n_expected)
    elif arm == "chase6":
        seam = ChaseSeam(anchors, chase[THETA_MAIN], n_expected)
    elif arm == "chase20":
        kw["candidate_top"] = 20
        seam = ChaseSeam(anchors, chase[THETA_MAIN], n_expected)
    elif arm == "gate":
        seam = GateSeam(anchors, gate, n_expected)
    try:
        if seam is not None:
            with seam:
                metrics = E.run_backtest(run_tag=tag, **kw)
            extra = ({"pool_exclusions": seam.pool_hits,
                      "per_period_exclusions": seam.per_period}
                     if isinstance(seam, ChaseSeam) else
                     {"scaled_periods": seam.scaled_periods})
        else:
            metrics = E.run_backtest(run_tag=tag, **kw)
            extra = {}
        out = _metrics_from(metrics["run_date"], metrics)
        out.update({"arm": arm, "run_tag": tag, "elapsed_sec": round(time.time() - t0, 1),
                    **extra})
        print(f"[{arm:>9}] {json.dumps({k: out[k] for k in
              ('periods', 'ann', 'mdd', 'sys_ann', 'sys_mdd', 'sys_mdd_daily',
               'excess_ann', 'turnover')}, ensure_ascii=False)} "
              f"({out['elapsed_sec']}s)", flush=True)
        return out
    finally:
        pass


def judge(delta: dict) -> dict:
    """§五冻结判据: 回撤轴改善 ≥3.0pp 且 收益轴损耗 ≤2.0pp, 双轴同时。
    sys_mdd_daily 为负值, 改善 = Δ ≥ +3.0pp(回撤变浅)。"""
    dd_ok = delta["sys_mdd_daily"] >= CRIT["dd_improve"]
    ret_ok = delta["sys_ann"] >= -CRIT["ann_cost"]
    return {"dd_improve_pp": round(delta["sys_mdd_daily"] * 100, 2),
            "dd_ok": bool(dd_ok), "ann_cost_pp": round(delta["sys_ann"] * 100, 2),
            "ret_ok": bool(ret_ok),
            "verdict": "过判据(采纳建议)" if (dd_ok and ret_ok) else "按冻结判据不采纳"}


def judge_confirm(delta: dict) -> dict:
    """chase6 全史窗确认判据(冻结): Δsys_ann ≥ +1.0pp 且 Δsys_mdd ≥ 0。"""
    ret_ok = delta["sys_ann"] >= 0.010
    dd_ok = delta["sys_mdd"] >= 0.0
    return {"ann_improve_pp": round(delta["sys_ann"] * 100, 2), "ret_ok": bool(ret_ok),
            "mdd_delta_pp": round(delta["sys_mdd"] * 100, 2), "dd_ok": bool(dd_ok),
            "verdict": ("确认: 值得进生产评估的候选(仍需第三轮敏感性+重钉+拍板)"
                        if (ret_ok and dd_ok)
                        else "85期读数判「窗口运气」, 观察作废, chase6 线结清")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default=None)
    ap.add_argument("--fullhist", action="store_true",
                    help="chase6 全史窗确认模式(plans/chase6-fullhist-prereg-20260926.md)")
    ap.add_argument("--signal", choices=["daily", "min60"], default="daily",
                    help="min60 = F75 原信号(chase6-round3-prereg §一.2)")
    ap.add_argument("--out", default=None, help="产物文件名(默认按模式)")
    ap.add_argument("--tag", default=None, help="run_tag 前缀(默认按模式)")
    args = ap.parse_args()

    if args.fullhist:
        win = {"start": "20170126", "end": "20260831"}
        n_periods = 115
        tag_prefix = args.tag or "20260926_abrisk_fh_"
        arms = (args.arms or "base,chase6").split(",")
        out_name = args.out or "chase6_fullhist_20260926.json"
        prereg = "plans/chase6-round3-prereg-20260926.md" if (args.tag or args.out)             else "plans/chase6-fullhist-prereg-20260926.md"
    else:
        win = WIN
        n_periods, tag_prefix = N_PERIODS_EXPECT, (args.tag or RUN_TAG)
        arms = (args.arms or "base,base20,chase6,chase20,gate,"
                              "chase20@1.5,chase20@2.5").split(",")
        out_name = args.out or "risk_overlay_ab_20260926.json"
        prereg = "plans/chase6-round3-prereg-20260926.md" if (args.tag or args.out)             else "plans/risk-overlay-prereg-20260926.md"

    anchors = _anchors(win["start"], win["end"], n_periods + 1)
    print(f"锚点 {len(anchors)} 个: {anchors[0]}..{anchors[-1]} ({n_periods} 期)")
    chase, gate, audit = _chase_and_gate(anchors, signal=args.signal)
    print(f"信号预计算完成: 截面均值 {sum(audit['n_z']) / len(audit['n_z']):.0f} / "
          f"域均值 {sum(audit['n_universe']) / len(audit['n_universe']):.0f}; "
          f"θ=2.0 截面剔除数均值 "
          f"{sum(len(v) for v in chase[THETA_MAIN].values()) / len(anchors):.0f}; "
          f"闸开 {audit['gate_periods']} 期", flush=True)

    results, base = {}, None
    for arm in arms:
        r = run_arm(arm, anchors, chase, gate, win, tag_prefix, n_periods)
        results[arm] = r
        if arm == "base":
            base = r
            if args.fullhist:
                print("[有效性] 全史窗 base 臂为新测量: 在册全史读数(11.59%/−51.26%)系 "
                      "20260912 时代 pin、经换基准+重训已过期, 不作逐位对账; "
                      "结构自证 = 期数/缝对账/两臂同窗同数据", flush=True)
                continue
            keymap = {"ann": "ann", "mdd": "mdd", "sys_ann": "sys_ann",
                      "sys_mdd": "sys_mdd", "sys_mdd_daily": "sys_mdd_daily",
                      "excess": "excess_ann", "ic": "ic"}
            diffs = {k: round(base[keymap[k]] - PIN[k], 6) for k in PIN}
            base["pin_check"] = diffs
            bad = {k: v for k, v in diffs.items() if abs(v) > 5e-4}
            if bad:
                raise RuntimeError(
                    f"有效性门失败: 基线臂与 pin 读数差超容差 {bad} —— 按 §五 本轮作废")
            print("[pin 复现] 基线臂 = 20260922_trbench 全部读数(容差 5e-4) ✓", flush=True)

    judged = () if args.fullhist else ("chase6", "chase20", "gate")
    for arm in arms:
        if arm == "base":
            continue
        delta = {k: round(results[arm][k] - base[k], 4)
                 for k in ("ann", "mdd", "sys_ann", "sys_mdd", "sys_mdd_daily",
                           "excess_ann")}
        results[arm]["delta_vs_base"] = delta
        if arm in judged:
            results[arm]["judgement"] = judge(delta)
            print(f"[{arm:>9}] 判决: {json.dumps(results[arm]['judgement'],
                                                ensure_ascii=False)}", flush=True)
        if args.fullhist and arm.startswith("chase6"):
            results[arm]["judgement"] = judge_confirm(delta)
            print(f"[{arm:>9}] 确认判决: "
                  f"{json.dumps(results[arm]['judgement'], ensure_ascii=False)}", flush=True)

    if args.fullhist:
        criteria = {"confirm_ann_improve": 0.010, "confirm_mdd_nonWorse": 0.0}
        limits = [
            "全史窗 base 臂为新测量(在册全史读数系 20260912 时代 pin, 换基准+重训后过期) "
            "⇒ 无逐位 pin_check, 有效性靠结构自证(115 期/缝对账/两臂同窗同数据)",
            "θ=2.0 与信号定义在 85 期窗冻结、本轮一字不改 ⇒ 全史窗满足 F28 式确认语义",
            "确认也只是'值得进生产评估的候选': 第三轮(敏感性/重钉流程/用户拍板)仍未做",
        ]
    else:
        criteria = CRIT
        limits = [
            "信号/闸决策在期频锚点(实盘可日频刷新) ⇒ Δ 是保守下界(同 governor 登记)",
            "追高信号是日频代理(min60 版才是 t≈−5.9 原信号); 不可辨 ≠ 原信号无效",
            "85 期单窗(2017-2021 追高年代不在窗内); 补全史窗须另立预注册",
            "chase20 引入 candidate_top=20 补位行为变更, Δ_vs_base 含补位效应(真实采纳差), "
            "base20 臂做归因分解",
            "过判据也只是采纳建议: 进生产须重钉流程 + 用户拍板(CLAUDE.md §1)",
        ]

    out_path = ROOT / "experiments" / out_name
    out_path.write_text(json.dumps({
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "prereg": prereg,
        "window": win, "criteria": criteria,
        "signal": {"theta_main": THETA_MAIN, "theta_grid": THETA_GRID,
                   "gate_dd": GATE_DD, "gate_scale": GATE_SCALE, "bm_gate": BM_GATE,
                   "signal": args.signal},
        "signal_audit": {"avg_universe": round(sum(audit["n_universe"]) / len(anchors), 1),
                         "avg_z_cross_section": round(sum(audit["n_z"]) / len(anchors), 1),
                         "avg_universe_excluded_theta2": round(
                             sum(len(v) for v in chase[THETA_MAIN].values()) / len(anchors), 1),
                         "gate_periods": audit["gate_periods"]},
        "results": results,
        "limitations": limits,
    }, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n产物: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
