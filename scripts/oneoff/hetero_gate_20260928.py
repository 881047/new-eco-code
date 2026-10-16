# -*- coding: utf-8 -*-
"""异构信息门禁(P2 股东户数 + P3 隔夜-日内) —— 2026-09-28, 预注册
`plans/hetero-gate-prereg-20260928.md`(判据跑数前冻结, 跑后不许增删候选)。

为什么是这两族
--------------
项目供给端头号失败模式: 「量价同源面上挖到的必然是已知因子的影子」(gp_000 与 size
ρ=0.951、LLM r3 点名因子 ρ=1.0)。⇒ 破局只能靠异构信息, 且必须机械过同一道门禁。

  · P2 股东户数: holder_num 190,622 行已就绪, app/factors/holder_factors.py 已封装,
    复用现成实现, **零补数成本**。
  · P3 隔夜-日内: 只需日频 open/pre_close/close(2013 起全史)。**不必受 min60 的
    2020-01 起点限制** —— 这是本脚本最重要的一个简化。

判据(照搬现役, 不调门槛): S1 |DEAD head|≥0.3%/月 · S2 |t|≥2 · S3 死活同号 ·
S4 与现役 26 因子 |ρ|max≤0.7 · S5 IC 半衰期 ≥2 月。

硬约束: **不改生产**(不调 compute_all / 不写 factor 表 / 不动 model_score),
**不换 pin**(门禁层走 gate.forward_return 朴素标签, 不经过回测撮合)。

用法
----
  ./.venv/Scripts/python.exe -X utf8 scripts/oneoff/hetero_gate_20260928.py
产物 experiments/hetero_gate_20260928.json
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app import config as cfg_mod
from app.alpha import gate
from app.factors.factor_engine import load_market, month_end_dates
from app.factors.holder_factors import compute_holder_factors
from app.pit.engine import PITEngine

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "hetero_gate_20260928.json"
WINDOWS = (20, 60)
COVER_BAR = 0.50          # 覆盖体检: 每期截面非 NaN 占比 < 50% ⇒ 记"覆盖不足", 不否决


def _zscore(df: pd.DataFrame) -> pd.DataFrame:
    """截面 winsorize(2%/98%) + z-score —— 与 factor_engine.neutralize 的同一手法
    (此处不去行业: 门禁测的是排序力, 去行业会引入行业映射的前视口径分歧)。"""
    lo = df.quantile(0.02, axis=1)
    hi = df.quantile(0.98, axis=1)
    w = df.clip(lower=lo, upper=hi, axis=0)
    sd = w.std(axis=1).replace(0, np.nan)
    return w.sub(w.mean(axis=1), axis=0).div(sd, axis=0)


def build_overnight_intraday(m: dict, dates_all: pd.DatetimeIndex) -> dict:
    """族 B: 隔夜-日内 8 个候选。

    ovr_d  = open/pre_close - 1        (隔夜跳空)
    intr_d = close/open     - 1        (日内)
    规模剥离版为**第一版**(预注册写死): 先减当日截面均值, 再取 W 日均值。
    理由: 隔夜跳空的截面差异主要由流动性/规模驱动, 减当日截面均值即剥掉共同规模因子。
    close_pos 不剥离(它本来就是日内区间位置, 与规模无直接关系)。
    """
    ac = m["close"] * m["adj"]
    ao = m["open"] * m["adj"]
    ap = m["pre_close"] * m["adj"]
    hi = m["high"] * m["adj"]
    lo = m["low"] * m["adj"]

    with np.errstate(divide="ignore", invalid="ignore"):
        ovr = ao / ap - 1.0
        intr = ac / ao - 1.0
        cpos = (ac - lo) / (hi - lo)

    ovr = ovr.replace([np.inf, -np.inf], np.nan)
    intr = intr.replace([np.inf, -np.inf], np.nan)
    cpos = cpos.replace([np.inf, -np.inf], np.nan)

    # 规模剥离: 减**当日**截面均值(不是全样本均值 —— 隔夜效应有强烈的日历共同分量)
    ovr_d = ovr.sub(ovr.mean(axis=1), axis=0)
    intr_d = intr.sub(intr.mean(axis=1), axis=0)

    out = {}
    for W in WINDOWS:
        out[f"ovr_mean_{W}"] = ovr_d.rolling(W, min_periods=max(5, W // 2)).mean()
        out[f"intr_rev_{W}"] = -intr_d.rolling(W, min_periods=max(5, W // 2)).mean()
        out[f"oi_spread_{W}"] = (intr_d - ovr_d).rolling(
            W, min_periods=max(5, W // 2)).mean()
        out[f"close_pos_{W}"] = cpos.rolling(W, min_periods=max(5, W // 2)).mean()
    return out


def main() -> int:
    print("=" * 92)
    print("异构信息门禁(P2 股东户数 + P3 隔夜-日内) —— 预注册 hetero-gate-prereg-20260928")
    print("=" * 92)

    cfg = cfg_mod.load_config()
    excl = set(cfg.get("factor_exclusions") or [])
    m = load_market()
    dates_all = pd.DatetimeIndex(m["close"].index)
    md_all = month_end_dates(dates_all)
    md_eval = pd.DatetimeIndex(md_all[md_all >= pd.Timestamp("2019-01-01")])
    zones = gate.zone_of(md_eval)
    fwd = gate.forward_return(m, gate.HORIZON)
    pit = PITEngine(m=m)
    tradable = pit.tradable_matrix(md_eval)
    print(f"锚点 {len(md_eval)} 个({md_eval[0].date()} ~ {md_eval[-1].date()}); "
          f"DEAD {int((zones == 'DEAD').sum())} / ACTIVE {int((zones == 'ACTIVE').sum())}")

    conn = sqlite3.connect(f"{(ROOT / 'data' / 'neweco.db').as_uri()}?mode=ro", uri=True)

    # ---- 现役因子(用于 S4 影子判定) ----
    names = [r[0] for r in conn.execute(
        "SELECT DISTINCT factor FROM factor ORDER BY factor")]
    live = [n for n in names if n not in excl]
    live_panels = {}
    for f in live:
        d = pd.read_sql("SELECT trade_date, ts_code, value FROM factor WHERE factor=?",
                        conn, params=(f,))
        w = d.pivot_table(index="trade_date", columns="ts_code",
                          values="value", aggfunc="last")
        w.index = pd.to_datetime(w.index, format="%Y%m%d")
        live_panels[f] = w.reindex(index=md_eval)
    print(f"现役因子 {len(live)} 个(池 {len(names)} − 剔 {len(excl)})")

    # ---- 构造候选 ----
    cands = {}

    # 族 A: 股东户数(复用现成实现, 传**全日频** dates 以保住 ann_date 对齐精度)
    try:
        hf = compute_holder_factors(dates_all)
        for k, v in hf.items():
            cands[k] = v.reindex(index=md_eval)
        print(f"族 A 股东户数: {sorted(hf)}")
    except Exception as e:
        print(f"族 A 构造失败: {type(e).__name__}: {e}")

    # 族 B: 隔夜-日内
    oi = build_overnight_intraday(m, dates_all)
    for k, v in oi.items():
        cands[k] = v.reindex(index=md_eval)
    print(f"族 B 隔夜-日内: {len(oi)} 个")

    print("\n" + "=" * 92)
    print(f"{'候选':<18} {'DEAD head':>10} {'t':>7} {'ACT head':>10} "
          f"{'|rho|max':>9} {'HL':>5} {'覆盖':>6}  S1 S2 S3 S4 S5  PASS")
    print("=" * 92)

    results = []
    for name in sorted(cands):
        raw = cands[name].reindex(index=md_eval)
        if raw.dropna(how="all").empty:
            print(f"{name:<18} —— 全空, 跳过")
            continue
        # 覆盖体检(打码前统计, 看的是"因子自己有多少票")
        cov = float(raw.notna().sum(axis=1).mean() /
                    max(1, int(tradable.notna().sum(axis=1).mean())))
        cover_ok = cov >= COVER_BAR

        pz = _zscore(raw)
        pm = pz.where(tradable.reindex(index=md_eval,
                                       columns=pz.columns).fillna(False))
        det = gate.evaluate(pm, fwd, md_eval, zones)
        if det.empty:
            print(f"{name:<18} —— 无有效截面")
            results.append({"name": name, "note": "无有效截面"})
            continue
        dead, act = det[det.zone == "DEAD"], det[det.zone == "ACTIVE"]
        if dead.empty or act.empty:
            print(f"{name:<18} —— DEAD/ACTIVE 缺区")
            results.append({"name": name, "note": "DEAD/ACTIVE 缺区"})
            continue
        h_d = float(dead.head_excess.mean())
        sd = float(dead.head_excess.std(ddof=1))
        t_d = float(h_d / (sd / np.sqrt(len(dead)))) if sd > 0 else 0.0
        h_a = float(act.head_excess.mean())

        # S4: 与现役 26 因子的 |rho|max(Spearman)
        rho_max, rho_with = 0.0, ""
        for f, w in live_panels.items():
            a, b = pz.align(w.reindex(index=md_eval), join="inner")
            both = a.notna() & b.notna()
            if both.sum().sum() < 500:
                continue
            av = a.values[both.values]
            bv = b.values[both.values]
            if len(av) < 500 or np.std(av) == 0 or np.std(bv) == 0:
                continue
            r = float(pd.Series(av).corr(pd.Series(bv), method="spearman"))
            if abs(r) > abs(rho_max):
                rho_max, rho_with = r, f

        # S5: 执行延迟 IC 半衰期
        ic_lag = gate.ic_by_lag(raw, tradable, fwd, md_eval, zones)
        # ic_by_lag 返回 {L: Series(锚点日 → IC)}, 而 half_life_from_curve 要
        # {L: (均值, 锚点数)} —— 在此转一手(空 Series 记 NaN, 沿 fail-closed 惯例)
        ic_dead = {}
        for L, v in ic_lag.items():
            s = v if isinstance(v, pd.Series) else pd.Series(v)
            ic_dead[L] = (float(s.mean()) if len(s) else np.nan, int(len(s)))
        hl = gate.half_life_from_curve(ic_dead)
        hl_v = hl.get("half_life_months")

        vd = gate.verdict_single(h_d, t_d, h_a, shadow_abs_rho=rho_max,
                                 half_life_months=hl_v)
        mark = lambda x: "✓" if x is True else ("✗" if x is False else "·")
        print(f"{name:<18} {h_d * 100:>9.3f}% {t_d:>7.2f} {h_a * 100:>9.3f}% "
              f"{rho_max:>9.3f} {('%.1f' % hl_v) if hl_v is not None else 'n/a':>5} "
              f"{cov * 100:>5.0f}%  "
              f"{mark(vd['s1_head'])}  {mark(vd['s2_t'])}  {mark(vd['s3_same_sign'])}  "
              f"{mark(vd['s4_decor'])}  {mark(vd['s5_half_life'])}  "
              f"{'PASS' if vd['PASS_single'] else '—'}"
              + ("" if cover_ok else "  ⚠覆盖不足"))

        results.append({
            "name": name, "dead_head": round(h_d, 6), "dead_t": round(t_d, 3),
            "active_head": round(h_a, 6),
            "dead_top6": round(float(dead.top6_excess.mean()), 6),
            "n_dead": int(len(dead)), "n_active": int(len(act)),
            "coverage": round(cov, 4), "coverage_ok": bool(cover_ok),
            "shadow_rho_max": round(rho_max, 4), "shadow_with": rho_with,
            "half_life": hl, "ic_by_lag": {str(k): (round(v[0], 6) if v[0] == v[0] else None,
                                                    v[1]) for k, v in ic_dead.items()},
            "verdict": vd,
        })

    out = {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "prereg": "plans/hetero-gate-prereg-20260928.md",
        "anchors": len(md_eval),
        "window": [str(md_eval[0].date()), str(md_eval[-1].date())],
        "live_factors_n": len(live), "windows": list(WINDOWS),
        "cover_bar": COVER_BAR,
        "results": results,
        "n_pass": int(sum(1 for r in results if r.get("verdict", {}).get("PASS_single"))),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 92)
    print(f"过闸 {out['n_pass']} / {len([r for r in results if 'verdict' in r])} 个"
          f" —— 产物 {OUT}")
    print("过闸者 ⇒ 进 P4(影子表 + 双种子集重训 + portfolio_admission_gate)")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
