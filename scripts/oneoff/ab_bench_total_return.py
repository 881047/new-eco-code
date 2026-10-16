"""A/B：回测基准腿 价格指数 vs 合成全收益（2026-09-19；**不改任何生产代码/口径/pin**）

判据**跑之前**冻结在 `plans/b1-total-return-benchmark-ab-20260919.md`，本文照抄要点。

## 为什么

`backtest/engine.py:612-614` 的基准腿取 `m["bench"]` = `index_daily.close`（000906.SH 中证800
**价格指数**），而持仓腿取 `close × adj_factor`（**全收益**，分红按除权日计入）。两条腿不同尺
⇒ `excess_ann` 被系统性抬高一个"基准股息率"（三补二十五 B1 实测：算术 2.24pp/年，年度带
1.74~2.72，覆盖 2019-01~2026-08）。本轮回答"抬高到底多少、两轴各变不变"，给换不换 pin 提供数。

## 四臂（两窗 × 两尺）

- 生产窗（`start=cfg.eval_start=20190722` ⇒ 85 期，现行 pin 的口径）：`A` = 现状，`B` = 全收益尺
- 全史窗（`start=None` ⇒ 115 期）：`A_full` / `B_full` —— 2017~2018-01 无成分权重，该段用**窗口
  均值**填充，**只作敏感度**（§4），不作判决依据

两臂共用同一份 panel、同一套参数、同一进程；唯一变量 = `m["bench"]`。

## 三道门（冻结）+ 两道实现完整性前置

- **A0 复现**：A 臂对照 pin 行 `20260912_limitquant`；**对不上不算失败**（09-16~18 因子表刷新已过时），
  只用来量出那段数据漂移有多大。
- **A1 不变性（真正的门）**：除 `bench_ann/excess_ann/win_rate` 外，两臂全部指标必须 **Δ=0**。
  任一条不等 ⇒ 前提证伪（bench 进了决策），本轮作废并登记，**不许改判据继续跑**。
- **A2 量级门**：Δexcess_ann ∈ [1.5, 3.0]pp/年（带来源是跑之前已实测的 1.74~2.72 ± 复利余量）。
  落在带外 = 我的实现算错了，去查实现，**不许把区间改到能过**。

## 全收益尺怎么造（合成，非中证发布值）

`bench_TR_t = bench_PR_t × Π_{s≤t}(1 + y_{m(s)}/1200/D_{m(s)})`
`y_m` = 该月末成分股市值加权 `dv_ttm`(%)，直接复用 B1 审计脚本的 `dividend_yield_by_month`
（按文件路径 import，不复制公式）；`D_m` = 该月交易日数；年化率按 `/12` 摊到月再摊到日（平滑）。
dtype 跟着回到 float32，与 `load_market` 原样一致（否则两臂比较会引入假差异）。

## 产物

`logs/ab_bench_total_return.json`（四臂全指标 + 逐字段 Δ + 三道门结果）+ 本脚本 stdout。
回测行带 `20260919_b1tr_*` tag 落 `backtest_summary/backtest_nav/backtest_nav_daily`
（与 `pin_baseline.py`、`abg_*` 同一套隔离写法，只动自己 tag 那摊，不碰生产 `run_date`）。

运行：`D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\ab_bench_total_return.py`
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

OUT = BASE / "logs" / "ab_bench_total_return.json"
AUDIT = BASE / "scripts" / "oneoff" / "audit_bench_total_return_gap.py"
PIN_ROW = "20260912_limitquant"                    # 现行 pin 的 backtest_summary 行
BENCH_FIELDS = {"bench_ann", "excess_ann", "win_rate"}
A2_BAND_PP = (1.5, 3.0)                            # Δexcess 的冻结区间(百分点/年)


def _load_yield_module():
    """按路径加载 B1 审计脚本，复用它的股息率口径（不复制公式）。"""
    spec = importlib.util.spec_from_file_location("_b1_gap", AUDIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def tr_multiplier(dates: pd.DatetimeIndex, y_month: pd.Series) -> tuple:
    """逐日复利因子：年化 y_m(%) 先 /12 摊到月，再按该月交易日数平摊。

    ⚠️ `y_month` 的索引是 `index_weight` 的**月末交易日**（`20190131` 这种 8 位串），不是 `YYYYMM`。
    第一版直接用 `YYYYMM` 去查 ⇒ **一个都没命中**、全序列退化成常数均值 —— 而它仍然落在 A2 的
    预期带内（均值≈时变值），量级门抓不到，属"结果看着对、实现与冻结定义不符"。预注册 §1 冻结的
    是"逐月实测 y_m"，所以这里按 `[:6]` 建月键映射，并把"窗内是否仍有填充"如实报出来。
    """
    keys = pd.Index(dates.strftime("%Y%m"))
    mean_y = float(y_month.mean())
    ymap = {str(k)[:6]: float(v) for k, v in y_month.items()}
    filled = [k in ymap for k in keys]
    y = np.array([ymap.get(k, mean_y) for k in keys], dtype=float)
    per_month_days = pd.Series(1.0, index=keys).groupby(keys).transform("size").to_numpy()
    daily = 1.0 + (y / 100.0) / 12.0 / per_month_days
    covered = [str(k) for k, f in zip(keys, filled) if f]
    stats = {"imputed_days": int(sum(1 for f in filled if not f)),
             "covered_months": len(ymap),
             "covered_from": min(covered) if covered else None,
             "covered_to": max(covered) if covered else None,
             "yield_min": round(float(min(ymap.values())), 3) if ymap else None,
             "yield_max": round(float(max(ymap.values())), 3) if ymap else None}
    return pd.Series(daily, index=dates).cumprod(), stats, round(mean_y, 3)


def run_arm(engine, panel_factory, tag: str, start: str | None):
    import unittest.mock as mk
    with mk.patch.object(engine, "load_market", panel_factory):
        m = engine.run_backtest(start=start, run_tag=tag)
    m.pop("run_date", None)
    return m


def diff(a: dict, b: dict) -> dict:
    """逐字段对比；返回 {field: {a, b, delta}}，并挑出"承诺 Δ=0 却不等"的字段。"""
    changed, viol = {}, []
    for k in sorted(set(a) | set(b)):
        va, vb = a.get(k), b.get(k)
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)) \
                and va is not None and vb is not None \
                and not isinstance(va, bool) and not isinstance(vb, bool):
            d = float(va) - float(vb)
            if k in BENCH_FIELDS:
                changed[k] = {"a": va, "b": vb, "delta": round(d, 4)}
            elif abs(d) > 0:
                viol.append({"field": k, "a": va, "b": vb, "delta": round(d, 6)})
        elif va != vb:
            if k in BENCH_FIELDS:
                changed[k] = {"a": va, "b": vb}
            else:
                viol.append({"field": k, "a": va, "b": vb})
    return {"expected_to_change": changed, "invariance_violations": viol}


def main() -> int:
    from app import config as cfg_mod
    from app import db
    from app.backtest import engine

    gap = _load_yield_module()
    code = cfg_mod.benchmark_code()
    con = gap._ro()
    try:
        y_month = gap.dividend_yield_by_month(con, code)
        pin = con.execute("SELECT metrics FROM backtest_summary WHERE run_date=?",
                          (PIN_ROW,)).fetchone()
    finally:
        con.close()
    if y_month.empty:
        print("index_weight ∩ dv_ttm 全空 —— 无法构造全收益尺")
        return 1
    print(f"股息率序列: {len(y_month)} 个月（{y_month.index.min()}~{y_month.index.max()}），"
          f"窗口均值 {float(y_month.mean()):.3f}%/年")

    import functools
    real_load = engine.load_market
    stats = {}

    def panel_factory(tr: bool = False):
        m = real_load()
        if not tr:
            return m
        mult, st, mean_y = tr_multiplier(m["bench"].index, y_month)
        stats.update(st)
        stats["mean_yield_pct"] = mean_y
        out = dict(m)
        out["bench"] = (m["bench"] * mult).astype(np.float32)     # dtype 跟原样一致
        return out

    eval_start = cfg_mod.load_config().get("eval_start") or None
    # 判决窗（生产窗）内一个月都不许靠填充 —— §4 承诺的硬条件，跑之前就核
    ymap_months = {str(k)[:6] for k in y_month.index}
    win_months = sorted({d.strftime("%Y%m") for d in
                         pd.date_range(pd.Timestamp(eval_start), pd.Timestamp("20260831"))})
    missing_in_window = [m for m in win_months if m not in ymap_months]

    arms = {}
    for label, tag, start in (("A_prod", "b1tr_prod_a", eval_start),
                              ("B_prod", "b1tr_prod_b", eval_start),
                              ("A_full", "b1tr_full_a", None),
                              ("B_full", "b1tr_full_b", None)):
        tr = label.startswith("B")
        pf = functools.partial(panel_factory, tr)
        print(f"[{label}] run_backtest(start={start}, run_tag={tag}, bench={'TR' if tr else 'PR'}) …")
        arms[label] = run_arm(engine, pf, tag, start)
        print(f"    → 年化 {arms[label]['ann_return']*100:.2f}% / 基准 "
              f"{arms[label]['bench_ann']*100:.2f}% / 超额 {arms[label]['excess_ann']*100:.2f}% / "
              f"回撤 {arms[label]['max_drawdown']*100:.2f}% / IC {arms[label].get('ic_mean')}")
    print(f"  尺的覆盖: {stats.get('covered_months')} 个月实测逐月股息率"
          f"（{stats.get('covered_from')}~{stats.get('covered_to')}，"
          f"逐月 y ∈ [{stats.get('yield_min')}, {stats.get('yield_max')}%]，"
          f"面板内 {stats.get('imputed_days')} 个交易日靠窗口均值填（全在权重起点之前））；"
          f"**判决窗 {win_months[0]}~{win_months[-1]} 内缺失月 = {missing_in_window or '无'}**")

    d_prod, d_full = diff(arms["A_prod"], arms["B_prod"]), diff(arms["A_full"], arms["B_full"])
    de_prod = (arms["A_prod"]["excess_ann"] - arms["B_prod"]["excess_ann"]) * 100.0
    de_full = (arms["A_full"]["excess_ann"] - arms["B_full"]["excess_ann"]) * 100.0
    a1_ok = not d_prod["invariance_violations"] and not d_full["invariance_violations"]
    a2_ok = A2_BAND_PP[0] <= de_prod <= A2_BAND_PP[1]
    # A3/A4 是**实现完整性前置**，不是结果判据（A2 的区间一个字都没改）。
    # ⚠️ 它们是第一跑之后加的（过程登记在预注册 §7：第一版 B 臂因月键形态对不上而静默退化成
    # 常数，Δ 却仍落在 A2 带内 ⇒ A2 抓不到这类"结果看着对、构造是错的"错）。
    #   A3 = 判决窗内不许有任何一个月靠均值填（§4 的承诺）；
    #   A4 = B 臂必须真的用上逐月 y_m（防"退化成常数"）。
    a3_ok = not missing_in_window
    a4_ok = bool(stats.get("yield_min") is not None and stats.get("yield_max") is not None
                 and float(stats["yield_max"]) - float(stats["yield_min"]) > 1e-6)
    pin_m = json.loads(pin[0]) if pin and pin[0] else {}
    a0 = {k: {"pin_20260912": pin_m.get(k), "arm_A_prod": arms["A_prod"].get(k)}
          for k in ("ann_return", "bench_ann", "excess_ann", "max_drawdown", "win_rate",
                    "avg_turnover", "ic_mean") if k in pin_m}
    # A0 的唯一容许差: `avg_turnover` 的分母在 2026-09-14 由 n_periods 改成"真正计入的期数"
    # (05 号 P2-1，当时 goldens 已**故意重钉**，只改这一个字段)。所以 pin 行(09-12 跑)与本臂
    # 之比应当正好是 n/(n−1) = 85/84 —— 把它算出来，而不是含糊成"数据漂移"。
    n_p = arms["A_prod"].get("periods")
    to_ratio = (float(arms["A_prod"]["avg_turnover"]) / float(pin_m["avg_turnover"])
                if pin_m.get("avg_turnover") and n_p else None)
    a0_turnover_expected = (round((n_p / (n_p - 1.0)) * float(pin_m["avg_turnover"]), 4)
                            if pin_m.get("avg_turnover") and n_p else None)
    a0_ok = all(
        abs(float(v["arm_A_prod"]) - float(v["pin_20260912"])) < 1e-9
        or k == "avg_turnover" and to_ratio is not None
        and abs(to_ratio - n_p / (n_p - 1.0)) < 5e-4
        for k, v in a0.items()
        if isinstance(v.get("arm_A_prod"), (int, float)) and v.get("pin_20260912") is not None)

    res = {
        "prereg": "plans/b1-total-return-benchmark-ab-20260919.md",
        "benchmark_code": code,
        "yield_source": "index_weight ∩ daily_basic.dv_ttm 市值加权（复用 B1 审计函数）",
        "yield_months": int(len(y_month)),
        "yield_mean_pct_per_year": round(float(y_month.mean()), 3),
        "bench_construction": {
            "monthly_yield_coverage_months": stats.get("covered_months"),
            "covered_from": stats.get("covered_from"), "covered_to": stats.get("covered_to"),
            "monthly_yield_range_pct": [stats.get("yield_min"), stats.get("yield_max")],
            "mean_pct_per_year": stats.get("mean_yield_pct"),
            "panel_days_filled_by_mean": stats.get("imputed_days"),
            "verdict_window_months_missing_weights": missing_in_window,
            "verdict_window": f"{win_months[0]}~{win_months[-1]}",
        },
        "arms": arms,
        "diff_prod": d_prod, "diff_full": d_full,
        "gates": {
            "A0_reproduction": {
                "pass": a0_ok, "values": a0,
                "turnover_note": "唯一容许差 = avg_turnover 的分母口径(05 号 P2-1, 09-14 改"
                                 "『真正计入的期数』、goldens 当时故意重钉且只动这一字段)："
                                 f"pin 0.8912 × n/(n−1)={a0_turnover_expected} vs 本臂 "
                                 f"{arms['A_prod'].get('avg_turnover')}",
                "meaning": "A 臂必须复现现行 pin 行(除那一个已知字段) —— 复现不上说明 harness 不是生产 harness"},
            "A1_invariance": {"pass": a1_ok,
                              "violations": d_prod["invariance_violations"]
                                            + d_full["invariance_violations"],
                              "meaning": "除 bench_ann/excess_ann/win_rate 外两臂必须逐位相等"
                                         "（不等 = bench 进了决策 = 前提证伪）"},
            "A2_magnitude": {"pass": a2_ok, "delta_excess_ann_pp_per_year": round(de_prod, 3),
                             "frozen_band_pp": list(A2_BAND_PP),
                             "full_window_delta_pp": round(de_full, 3)},
            "A3_window_fully_covered": {
                "pass": a3_ok, "missing_months": missing_in_window,
                "meaning": "判决窗内一个月都不许靠均值填（预注册 §4 的承诺）"},
            "A4_monthly_yield_actually_used": {
                "pass": a4_ok, "monthly_range_pct": [stats.get("yield_min"), stats.get("yield_max")],
                "meaning": "B 臂必须用上逐月 y_m；第一版因月键形态错配静默退化成常数，"
                           "而 Δ 仍落在 A2 带内 ⇒ 数值门抓不到（预注册 §7）"},
        },
        "not_a_pin_change": "本脚本不改任何生产代码/口径/pin；结论交用户拍板",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print("\n== 五道门 ==")
    print(f"  A1 不变性（除基准三字段外两臂逐位相等）: {'PASS' if a1_ok else 'FAIL'}"
          + ("" if a1_ok else f"  违反项: {d_prod['invariance_violations']}"))
    print(f"  A2 量级门 Δexcess ∈ {A2_BAND_PP}pp/年: {'PASS' if a2_ok else 'FAIL'}"
          f"  实测生产窗 {de_prod:+.2f}pp/年（全史 {de_full:+.2f}）")
    print(f"  A0 复现现行 pin 行（唯一容许差=换手分母 P2-1）: {'PASS' if a0_ok else 'FAIL'}  " + "; ".join(
        f"{k} {v['pin_20260912']}→{v['arm_A_prod']}" for k, v in a0.items()))
    print(f"  A3 判决窗零填充: {'PASS' if a3_ok else 'FAIL'}  缺失月 {missing_in_window or '无'}")
    print(f"  A4 B 臂用上逐月股息率: {'PASS' if a4_ok else 'FAIL'}  "
          f"逐月 y ∈ [{stats.get('yield_min')}, {stats.get('yield_max')}%]/年")
    print("\n== 判决口径（两轴）==")
    print(f"  超额轴: {arms['A_prod']['excess_ann']*100:+.2f} → {arms['B_prod']['excess_ann']*100:+.2f} pp/年"
          f"（Δ {de_prod:+.2f}）")
    print(f"  回撤轴: {arms['A_prod']['max_drawdown']*100:.2f} → "
          f"{arms['B_prod']['max_drawdown']*100:.2f} %（Δ "
          f"{(arms['A_prod']['max_drawdown']-arms['B_prod']['max_drawdown'])*100:+.2f}pp）")
    print(f"  胜率: {arms['A_prod']['win_rate']:.4f} → {arms['B_prod']['win_rate']:.4f}"
          f"  ← 逐月比较，受摊销方式影响，只作参考（预注册 §5-2）")
    print(f"\n产物: {OUT.relative_to(BASE)}   （回测行 tag: 20260919_b1tr_*）")
    if not (a1_ok and a3_ok and a4_ok):
        print("⚠️ 有前置门未过 ⇒ 按预注册本轮读数不采信：A1 未过 = bench 进了决策（等级升高）；"
              "A3/A4 未过 = 尺的构造不符，修实现后重跑（不许改 A2 区间）")
        return 2
    if not a2_ok:
        print("⚠️ A2 未过 ⇒ 实现有错，去查口径实现，**不许把区间改到能过**")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
