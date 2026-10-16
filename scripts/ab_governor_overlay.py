"""三盘决策函数(回撤治理)的历史 A/B —— 2026-09-25

## 问题

2026-09-25 上线的账户级回撤治理(滞回带 -20%/-10%, 谨慎态=停开新仓/退出照旧,
`app/portfolio/governor.py`)在历史上会怎么表现? 本脚本给两只盘各跑两臂回测:
**基线臂**(无治理) vs **治理臂**(同一引擎 + 治理器注入), Δ 即治理器的历史效应。
2万周盘(ECO-W2)改动为零 ⇒ 无 A/B 可做(其 C 层熔断在冻结预注册内, 见报告)。

## 方法(两臂同码同撮合 ⇒ Δ 归因干净)

- 引擎 = 生产回测引擎 `app/backtest/engine.py`, **撮合/成本口径零改动**(不触 §1 禁区);
- 治理臂用两个**模块级缝**注入(引擎文件不动):
  · `allocate_lots`: 谨慎态 ⇒ 只保留「上期虚拟持仓 ∩ 本期可交易候选」的原股数
    (= 停开新仓、退出照旧); 非谨慎态原样透传;
  · `_month_pnl`: 透传 + 观测 —— 每期第二次调用 = 系统线, 按循环同式复算 mret_sys,
    复利出观测用 sys_nav 并做滞回判定, 驱动**下一期**的 allocate_lots(与实盘一致:
    决策吃上一盯市日净值)。
- 缝的正确性三重自证: ① 每期调用序恒为 alloc×2 + pnl×2(full→sys 交替, 计数不等即中止);
  ② 我方复算的 sys_nav 必须与引擎落库 nav_rows 的系统线**逐位一致**(1e-9);
  ③ 月盘基线臂必须复现 pin `20260922_trbench` 读数(自证基线臂未扭曲口径)。
  任何一条不满足 raise 中止 —— 缝假设错了必须响, 不许带病出数。

## 两臂口径

- **月盘**: 现行 config(capital 2万 / top6 / 恒定 0.873 / hyst=0) + pin 窗口
  20190731..20260831(85 期)。治理判定吃 `paper.governor` 的阈值。
- **袖套(100万)**: 隔离表 `model_score_weekly_chk`(周锚点, `weekly_freq_baseline.py`
  同款, 不碰生产 model_score) + `top_n=100` + `hysteresis=700`(=band 8×) + w=1.0 flat
  + capital=100万 + `anchor="week"`; 窗口同月盘。治理判定吃 `weekly.governor` 的阈值。
  ⚠️ 基线臂绝对读数**不**等于 F52 —— 引擎对留存股按预算重新定份额, 袖套账本留存不动
  (有意差异); A/B 只回答治理器的 Δ, 不复刻 F52 的绝对数。
- ⚠️ 治理判定在**期频决策点**(实盘是日频盯市) ⇒ 触发时点略迟 ⇒ Δ 是**保守下界**。

## 留痕

run_tag = `20260925_abgov_{m,w}_{base,gov}` 四行入 backtest_summary/nav/nav_daily,
**保留不自清**(同 20260922_trbench 先例); 产物 experiments/governor_overlay_ab_<date>.json。
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from app import config as cfg_mod
from app import db
from app.logger import setup_logger
from app.backtest import engine as E
from app.portfolio import governor as gov_mod

setup_logger()
db.init_db()

ROOT = Path(__file__).resolve().parents[1]
WIN = {"start": "20190731", "end": "20260831"}
PIN = {"ann": 0.1782, "mdd": -0.2838, "sys_ann": 0.1497, "sys_mdd": -0.2280,
       "sys_mdd_daily": -0.3277, "excess": 0.118, "ic": 0.0962}      # 20260922_trbench
WEEKLY_ST = "model_score_weekly_chk"          # 隔离表(weekly_freq_baseline.py 同款)


class GovArm:
    """治理臂注入器(见模块 docstring)。**只在一个臂的生命周期内挂载**。"""

    def __init__(self, capital: float, trip: float, reset: float, timebox: int = 0):
        self.capital = float(capital)
        self.trip, self.reset = float(trip), float(reset)
        self.timebox = int(timebox)   # >0 = 时限版: 每次 scan-episode 只封前 K 期, 之后自动重新武装
        self._orig_alloc = E.allocate_lots
        self._orig_pnl = E._month_pnl
        self.cautious = False
        self.n_alloc = 0
        self.n_pnl = 0
        self.sys_nav = 1.0
        self.navs = []                # 每期期末系统线净值(与引擎 nav_rows 逐期对应)
        self.prev_full = {}
        self.prev_sys = {}
        self.trips = 0
        self.cautious_periods = 0
        self.mrets = []               # 每期系统线 mret(我方复算, 用于对账诊断)
        self._ep_start = None         # 当前 scan-episode 的触发期下标(时限版用)

    # ---- 缝 1: 行动(allocate_lots) ----
    def _alloc(self, lot_df, equity_cash, max_stocks, **kw):
        self.n_alloc += 1
        full = (self.n_alloc % 2 == 1)          # 每期: 第1次=满仓线, 第2次=系统线
        if not self.cautious:
            out = self._orig_alloc(lot_df, equity_cash, max_stocks, **kw)
        else:
            base = self.prev_full if full else self.prev_sys
            idx = lot_df.index if lot_df is not None and len(lot_df) else []
            sh = {c: s for c, s in base.items() if c in idx}
            if sh:
                px = lot_df["close"].reindex(list(sh)).fillna(0.0)
                cost = float((px * pd.Series(sh)).sum())
            else:
                cost = 0.0
            out = {"shares": sh, "weights": {c: 1.0 / len(sh) for c in sh} if sh else {},
                   "cost": cost, "n_eff": len(sh), "cash_left": equity_cash}
        if full:
            self.prev_full = dict(out["shares"])
        else:
            self.prev_sys = dict(out["shares"])
        return out

    # ---- 缝 2: 观测(_month_pnl, 系统线) ----
    def _pnl(self, alloc, execs, prev_picks, stuck, m, dates, pos1, st_codes,
             slip, cost, **kw):
        invested, pnl, credit = self._orig_pnl(
            alloc, execs, prev_picks, stuck, m, dates, pos1, st_codes, slip, cost, **kw)
        self.n_pnl += 1
        if self.n_pnl % 2 == 0:                 # 系统线(每期第二次调用)
            if self.n_alloc != self.n_pnl:      # 序错位 ⇒ 缝假设失效, 中止
                raise RuntimeError(
                    f"缝序错位: alloc={self.n_alloc} pnl={self.n_pnl} —— 注入假设失效, 中止")
            # ⚠️ 逐字复刻循环的 mret_sys 表达式, **保留操作数 dtype**(NEP 50 弱标量):
            # cash_r 在 repo 数据段是 np.float64 ⇒ 引擎该期 mret_sys 整体提升 float64;
            # 若在这里 float() 强转, 我方链会停在 float32, 每期 ~1e-7 舍入差 ⇒ 对账必挂。
            cap = self.capital
            cash_r = kw.get("cash_r", 0.0)
            if invested > cap * (1 + 1e-6):     # 与循环同式(含超资防御分支)
                mret = cash_r
            else:
                mret = pnl / cap + (cap - invested) / cap * cash_r + credit / cap
            self.sys_nav *= (1 + mret)
            self.navs.append(self.sys_nav)
            self.mrets.append(mret)
            g = gov_mod.hysteresis(self.navs, trip=self.trip, reset=self.reset)
            if g["cautious"]:
                if self._ep_start is None:
                    self._ep_start = len(self.navs) - 1
                    self.trips += 1
                cautious = (not self.timebox
                            or (len(self.navs) - 1 - self._ep_start) < self.timebox)
            else:
                self._ep_start = None
                cautious = False
            if cautious:
                self.cautious_periods += 1
            self.cautious = cautious
        return invested, pnl, credit

    def __enter__(self):
        E.allocate_lots = self._alloc
        E._month_pnl = self._pnl
        return self

    def __exit__(self, *exc):
        E.allocate_lots = self._orig_alloc
        E._month_pnl = self._orig_pnl
        return False


def _load_cfg_section(section: str) -> dict:
    return ((cfg_mod.load_config().get(section) or {}).get("governor") or {})


def _nav_from_db(run_date: str) -> pd.DataFrame:
    conn = db.get_conn()
    try:
        return pd.read_sql(
            "SELECT trade_date, nav, sys_nav, bench_nav FROM backtest_nav "
            "WHERE run_date=? ORDER BY trade_date", conn, params=(run_date,))
    finally:
        conn.close()


def _sys_validate(arm: GovArm, run_date: str) -> dict:
    """自证②: 我方复算的系统线净值 vs 引擎落库值, 逐位(1e-9)。"""
    rows = _nav_from_db(run_date)
    if len(rows) != len(arm.navs):
        raise RuntimeError(f"期数不齐: 引擎 {len(rows)} vs 观测 {len(arm.navs)}")
    eng = rows["sys_nav"].astype(float).tolist()
    max_abs = max(abs(a - b) for a, b in zip(eng, arm.navs))
    if max_abs > 1e-9:
        eng_m = [eng[0] / 1.0 - 1] + [eng[i] / eng[i - 1] - 1 for i in range(1, len(eng))]
        my_m = arm.mrets
        diffs = [(i, eng_m[i] - my_m[i]) for i in range(min(len(eng_m), len(my_m)))]
        nz = [d for d in diffs if d[1] != 0.0]
        head = "; ".join(f"p{i}: eng={eng_m[i]!r} my={my_m[i]!r} Δ={dd:.3e}"
                         for i, dd in nz[:3])
        raise RuntimeError(
            f"系统线净值对账失败: max|Δ|={max_abs:.3e} > 1e-9 —— 观测缝失真, 中止; "
            f"mret 不等的期数 {len(nz)}/{len(diffs)}; 首几个 {head}")
    return {"sys_nav_max_abs_diff": max_abs, "periods": len(rows)}


def _metrics_from(run_date: str, metrics: dict) -> dict:
    """从引擎 metrics + 落库净值整理单臂读数(sys_ann 按正确 ppy 重算 —— 引擎该键写死 12 期/年)。"""
    rows = _nav_from_db(run_date)
    daily = None
    conn = db.get_conn()
    try:
        daily = pd.read_sql(
            "SELECT trade_date, nav, sys_nav FROM backtest_nav_daily "
            "WHERE run_date=? ORDER BY trade_date", conn, params=(run_date,))
    finally:
        conn.close()
    ppy = int(metrics.get("periods_per_year") or 12)
    n = max(1, len(rows))
    sys_end = float(rows["sys_nav"].iloc[-1])
    out = {
        "periods": n, "ppy": ppy,
        "ann": float(metrics["ann_return"]), "mdd": float(metrics["max_drawdown"]),
        "excess_ann": float(metrics["excess_ann"]), "bench_ann": float(metrics["bench_ann"]),
        "sys_ann": round(sys_end ** (ppy / n) - 1, 4),
        "sys_mdd": round(float((rows["sys_nav"].astype(float) /
                                rows["sys_nav"].astype(float).cummax() - 1).min()), 4),
        "sys_mdd_daily": round(float((daily["sys_nav"].astype(float) /
                                      daily["sys_nav"].astype(float).cummax() - 1).min()), 4),
        "turnover": metrics.get("avg_turnover"), "ic": metrics.get("ic_mean"),
        "sharpe": metrics.get("sharpe"),
    }
    return out


def _episodes(navs: list, dates: list, trip: float, reset: float) -> list:
    """复扫滞回轨迹 → 触发 episode 表(触发净值点日期 ⇒ 解除日期)。"""
    eps, cur = [], None
    peak, tripped = None, False
    for d, v in zip(dates, navs):
        if peak is None or v > peak:
            peak = v
        dd = v / peak - 1.0
        if tripped:
            if dd >= reset - 1e-12:
                cur["reset_at"], cur["reset_dd"] = d, round(dd, 4)
                eps.append(cur)
                cur, tripped = None, False
        elif dd <= trip + 1e-12:
            tripped = True
            cur = {"trip_at": d, "trip_dd": round(dd, 4)}
    if cur:
        eps.append(cur)
    return eps


def run_arm(kind: str, arm: str) -> dict:
    """kind = monthly | weekly; arm = base | gov。返回单臂读数 dict。"""
    tag = f"20260925_abgov_{'m' if kind == 'monthly' else 'w'}_{arm}"
    gov_sec = _load_cfg_section("paper" if kind == "monthly" else "weekly")
    trip, reset = float(gov_sec.get("dd_trip", -0.20)), float(gov_sec.get("dd_reset", -0.10))

    if kind == "monthly":
        capital = float(cfg_mod.load_config().get("capital", 20000))
        kw = dict(start=WIN["start"], end=WIN["end"])
    else:
        _orig_cfg = cfg_mod.load_config

        def _cfg():
            c = dict(_orig_cfg())
            c["capital"] = 1_000_000.0
            c["regime_equity_weight"] = {"scan": {"attack": 1.0, "neutral": 1.0, "defense": 1.0}}
            c["risk_profile"] = "scan"
            return c
        cfg_mod.load_config = _cfg          # 袖套臂口径: 100万 / w=1.0 flat(weekly.equity_weight 同值)
        capital = 1_000_000.0
        kw = dict(score_table=WEEKLY_ST, top_n=100, hysteresis=700, anchor="week",
                  start=WIN["start"], end=WIN["end"])
    try:
        t0 = time.time()
        if arm.startswith("gov"):
            with GovArm(capital, trip, reset,
                        timebox=(3 if arm == "gov3" else 0)) as state:
                metrics = E.run_backtest(run_tag=tag, **kw)
                valid = _sys_validate(state, metrics["run_date"])
                episodes = _episodes(state.navs, _nav_from_db(metrics["run_date"])
                                     ["trade_date"].tolist(), trip, reset)
                extra = {"trips": state.trips, "cautious_periods": state.cautious_periods,
                         "cautious_share": round(state.cautious_periods /
                                                 max(1, len(state.navs)), 4),
                         "episodes": episodes, "seam_check": valid}
        else:
            metrics = E.run_backtest(run_tag=tag, **kw)
            extra = {}
        out = _metrics_from(metrics["run_date"], metrics)
        out.update({"arm": arm, "kind": kind, "run_tag": tag,
                    "run_date": metrics["run_date"], "elapsed_sec": round(time.time() - t0, 1),
                    "trip": trip, "reset": reset, **extra})
        return out
    finally:
        if kind == "weekly":
            cfg_mod.load_config = _orig_cfg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", choices=["monthly", "weekly", "both"], default="both")
    args = ap.parse_args()
    kinds = ["monthly", "weekly"] if args.account == "both" else [args.account]

    results = {}
    for kind in kinds:
        print(f"\n===== {kind} =====", flush=True)
        base = run_arm(kind, "base")
        print(f"[base] {json.dumps({k: base[k] for k in ('periods', 'ann', 'mdd', 'sys_ann', 'sys_mdd', 'sys_mdd_daily', 'excess_ann', 'turnover')}, ensure_ascii=False)}", flush=True)
        arms = ["gov"] + (["gov3"] if kind == "monthly" else [])
        gov_res = {}
        for arm in arms:
            gov_res[arm] = run_arm(kind, arm)
            print(f"[{arm:4}] {json.dumps({k: gov_res[arm][k] for k in ('periods', 'ann', 'mdd', 'sys_ann', 'sys_mdd', 'sys_mdd_daily', 'excess_ann', 'turnover', 'trips', 'cautious_periods') if k in gov_res[arm]}, ensure_ascii=False)}", flush=True)
        gov = gov_res["gov"]
        # 基线臂自证③(月盘): 必须复现 pin 读数
        if kind == "monthly":
            keymap = {"ann": "ann", "mdd": "mdd", "sys_ann": "sys_ann",
                      "sys_mdd": "sys_mdd", "sys_mdd_daily": "sys_mdd_daily",
                      "excess": "excess_ann", "ic": "ic"}
            diffs = {k: round(base[keymap[k]] - PIN[k], 6) for k in PIN}
            base["pin_check"] = diffs
            bad = {k: v for k, v in diffs.items() if abs(v) > 5e-4}
            if bad:
                print(f"⚠️ 基线臂与 pin 读数差超容差: {bad}(可能为数据漂移, 报告中如实登记)", flush=True)
            else:
                print("[pin 复现] 基线臂 = 20260922_trbench 全部读数(容差 5e-4) ✓", flush=True)
        results[kind] = {"base": base,
                         "gov": gov,
                         **({} if kind != "monthly" else {"gov3": gov_res["gov3"]}),
                         "delta": {k: round(gov[k] - base[k], 4)
                                   for k in ("ann", "mdd", "sys_ann", "sys_mdd",
                                             "sys_mdd_daily", "excess_ann")}}
        if "gov3" in gov_res:
            results[kind]["delta_gov3"] = {k: round(gov_res["gov3"][k] - base[k], 4)
                                           for k in ("ann", "mdd", "sys_ann", "sys_mdd",
                                                     "sys_mdd_daily", "excess_ann")}

    out_path = ROOT / "experiments" / f"governor_overlay_ab_{time.strftime('%Y%m%d')}.json"
    out_path.write_text(json.dumps(
        {"ran_at": time.strftime("%Y-%m-%d %H:%M:%S"), "window": WIN,
         "method": "基线臂 vs 治理臂, 两臂同码同撮合(engine 未改动, 缝注入+逐位对账自证)",
         "results": results}, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    print(f"\n产物: {out_path}")
    print("run 留痕(保留不自清):", [f"20260925_abgov_{'m' if k == 'monthly' else 'w'}_{a}"
                                   for k in kinds for a in ("base", "gov")])
    return 0


if __name__ == "__main__":
    sys.exit(main())
