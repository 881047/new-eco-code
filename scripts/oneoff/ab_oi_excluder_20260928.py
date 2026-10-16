# -*- coding: utf-8 -*-
"""`oi_spread` 剔除器 A/B —— 2026-09-28, 预注册 `plans/oi-excluder-prereg-20260928.md`

背景
----
门禁(experiments/hetero_gate_20260928.json)实测: oi_spread_20/60 的 DEAD head 为
**负**(-1.842%/月 t=-3.47; -2.118%/月 t=-4.03), S1-S5 全过但**方向为负**。
项目三次独立实证(F75 / min60 r2 / kg_gate_r2)一致表明: 反向显著信号取反当 alpha 必塌,
正确用法是**排除/风控**。且本项目唯一有效的两个动作都是"剔除"。
⇒ 本轮不做 P4 重训, 改测 chase6 同款的**剔除器**。

做法(零重训、不换 pin、不改生产)
-------------------------------
重建隔离分数表: 从每期 model_score 里**删掉** oi_spread 截面 z > θ 的行 ⇒
后续 nlargest 由次优票递补(**补位**语义; 与 chase6 的"选后剔不补位"不同, 见预注册 §3)。
控制臂 = 同管线不加剔除的**新鲜**控制臂(不引用历史 pin run, 免 config 漂移)。

判据(预注册冻结)
----------------
G0 控制臂与现役 pin 20260928_exe 同窗逐位一致 · G1 Δsys_ann prod ≥+2.0pp 且 full ≥+1.0pp ·
G2 sys_mdd 均值 prod ≥-2pp / full ≥-3pp · G3 换手 ≤+5pp · G4 θ∈{1.5,2.0,2.5} 三档同号 ·
G5 逐期月度收益配对差 |t| ≥ 2。

用法
----
  ./.venv/Scripts/python.exe -X utf8 scripts/oneoff/ab_oi_excluder_20260928.py
产物 experiments/ab_oi_excluder_20260928.json
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
from app import db
from app.alpha import gate
from app.backtest import engine as E
from app.factors.factor_engine import load_market, month_end_dates
from app.models import expert
from app.pit.engine import PITEngine

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "ab_oi_excluder_20260928.json"
THETAS = (1.5, 2.0, 2.5)
WINDOW_OI = 60            # 主臂; 20 作对照
TAG = "oiex"


def _oi_panel(m: dict, dates_all: pd.DatetimeIndex, W: int) -> pd.DataFrame:
    """oi_spread = mean(intr_d - ovr_d) over W —— 与 hetero_gate 同款(规模剥离版)。"""
    ac, ao, ap = m["close"] * m["adj"], m["open"] * m["adj"], m["pre_close"] * m["adj"]
    with np.errstate(divide="ignore", invalid="ignore"):
        ovr = ao / ap - 1.0
        intr = ac / ao - 1.0
    ovr = ovr.replace([np.inf, -np.inf], np.nan)
    intr = intr.replace([np.inf, -np.inf], np.nan)
    ovr_d = ovr.sub(ovr.mean(axis=1), axis=0)
    intr_d = intr.sub(intr.mean(axis=1), axis=0)
    return (intr_d - ovr_d).rolling(W, min_periods=max(5, W // 2)).mean()


def _write_score_table(tname: str, df: pd.DataFrame) -> None:
    """建影子表并写入(幂等: 先清同表旧行)。表名正则由 ensure_score_table 把守。"""
    expert.ensure_score_table(tname)
    conn = db.get_conn()
    try:
        conn.execute(f"DELETE FROM {tname}")
        conn.commit()
        cols = [c for c in ("ts_code", "trade_date", "score", "pred_ret")
                if c in df.columns]
        df[cols].to_sql(tname, conn, if_exists="append", index=False)
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    print("=" * 92)
    print("oi_spread 剔除器 A/B —— 预注册 plans/oi-excluder-prereg-20260928.md")
    print("=" * 92)

    cfg = cfg_mod.load_config()
    eval_start = cfg.get("eval_start")
    db.init_db()
    m = load_market()
    dates_all = pd.DatetimeIndex(m["close"].index)
    md_eval = pd.DatetimeIndex(month_end_dates(dates_all))
    pit = PITEngine(m=m)
    tradable = pit.tradable_matrix(md_eval)

    conn = sqlite3.connect(f"{(ROOT / 'data' / 'neweco.db').as_uri()}?mode=ro", uri=True)
    sc = pd.read_sql("SELECT trade_date, ts_code, score, pred_ret FROM model_score", conn)
    conn.close()
    sc["trade_date"] = sc["trade_date"].astype(str)
    print(f"model_score {len(sc)} 行, {sc['trade_date'].nunique()} 期")

    # 与门禁同口径: 月末锚点重采样 → tradable 打码 → 逐期截面 z-score
    oi_z = {}
    for W in (20, WINDOW_OI):
        p = _oi_panel(m, dates_all, W)
        p_md = p.reindex(index=md_eval)
        tr = tradable.reindex(index=md_eval, columns=p_md.columns).fillna(False)
        pm = p_md.where(tr)
        idx_s = pm.index.strftime("%Y%m%d")
        # 截面 z-score(每期独立)
        z = pm.sub(pm.mean(axis=1), axis=0).div(pm.std(axis=1).replace(0, np.nan), axis=0)
        z.index = idx_s
        oi_z[W] = z

    arms = {}

    def run(tname: str, start, label: str) -> dict:
        met = E.run_backtest(score_table=tname, run_tag=f"{tname}_{'prod' if start else 'full'}",
                             start=start)
        return {"label": label, "run_date": met["run_date"],
                "sys_ann": float(met["sys_ann_return"]),
                "sys_mdd": float(met["sys_max_drawdown"]),
                "ann": float(met["ann_return"]), "mdd": float(met["max_drawdown"]),
                "turnover": float(met.get("avg_turnover", float("nan"))),
                "periods": int(met["periods"])}

    # ---- 控制臂(新鲜, 不引用历史 pin) ----
    ctl = "model_score_oiex_ctl"
    _write_score_table(ctl, sc)
    arms["ctl"] = {"prod": run(ctl, eval_start, "控制臂"),
                   "full": run(ctl, None, "控制臂")}

    # ---- 剔除臂 ----
    stats = {}
    for W in (20, WINDOW_OI):
        for th in THETAS:
            z = oi_z[W]
            keep = []
            n_drop_total, n_periods_hit = 0, 0
            for d, g in sc.groupby("trade_date"):
                if d not in z.index:
                    keep.append(g)
                    continue
                zrow = z.loc[d].dropna()
                hit = set(zrow[zrow > th].index)
                if hit:
                    n_periods_hit += 1
                    n_drop_total += len(set(g["ts_code"]) & hit)
                    keep.append(g[~g["ts_code"].isin(hit)])
                else:
                    keep.append(g)
            ex = pd.concat(keep, ignore_index=True) if keep else sc
            tname = f"model_score_{TAG}_w{W}_t{int(th * 10)}"
            _write_score_table(tname, ex)
            n_p = sc["trade_date"].nunique()
            stats[f"w{W}_t{th}"] = {
                "drop_rows": int(n_drop_total),
                "periods_hit": int(n_periods_hit),
                "periods_hit_frac": round(n_periods_hit / max(1, n_p), 4),
                "avg_drop_per_hit_period": round(
                    n_drop_total / max(1, n_periods_hit), 2),
            }
            arms[f"w{W}_t{th}"] = {
                "prod": run(tname, eval_start, f"剔除 W={W} θ={th}"),
                "full": run(tname, None, f"剔除 W={W} θ={th}")}

    # ---- 判据 ----
    print("\n" + "=" * 92)
    print(f"{'臂':<16} {'窗':<6} {'sys_ann':>9} {'Δsys_ann':>9} {'sys_mdd':>9} "
          f"{'Δmdd':>8} {'换手':>7} {'触发期占比':>10}")
    print("=" * 92)
    verdicts = {}
    for k, v in arms.items():
        if k == "ctl":
            continue
        for win in ("prod", "full"):
            c, t = arms["ctl"][win], v[win]
            d_ann = t["sys_ann"] - c["sys_ann"]
            d_mdd = t["sys_mdd"] - c["sys_mdd"]
            print(f"{k:<16} {win:<6} {t['sys_ann'] * 100:>8.2f}% "
                  f"{d_ann * 100:>+8.2f}pp {t['sys_mdd'] * 100:>8.2f}% "
                  f"{d_mdd * 100:>+7.2f}pp {t['turnover'] * 100:>6.2f}% "
                  f"{stats[k]['periods_hit_frac'] * 100:>9.1f}%")
        verdicts[k] = {
            "stats": stats[k],
            "d_sys_ann_prod": round(v["prod"]["sys_ann"] - arms["ctl"]["prod"]["sys_ann"], 6),
            "d_sys_ann_full": round(v["full"]["sys_ann"] - arms["ctl"]["full"]["sys_ann"], 6),
            "d_sys_mdd_prod": round(v["prod"]["sys_mdd"] - arms["ctl"]["prod"]["sys_mdd"], 6),
            "d_sys_mdd_full": round(v["full"]["sys_mdd"] - arms["ctl"]["full"]["sys_mdd"], 6),
        }

    # G4 剂量单调(W=60 三档同号)
    dose = [verdicts[f"w{WINDOW_OI}_t{th}"]["d_sys_ann_prod"] for th in THETAS]
    g4 = bool(all(x > 0 for x in dose) or all(x < 0 for x in dose)) and any(
        abs(x) > 1e-9 for x in dose)
    # G1/G2/G3 主臂(W=60, θ=2.0)
    main = verdicts[f"w{WINDOW_OI}_t2.0"]
    g1 = bool(main["d_sys_ann_prod"] >= 0.020 and main["d_sys_ann_full"] >= 0.010)
    g2 = bool(main["d_sys_mdd_prod"] >= -0.020 and main["d_sys_mdd_full"] >= -0.030)
    hp = stats[f"w{WINDOW_OI}_t2.0"]["periods_hit_frac"]
    print("\n" + "-" * 92)
    print(f"G1 Δsys_ann prod≥+2.0pp & full≥+1.0pp : {'PASS' if g1 else 'FAIL'}"
          f"  (prod {main['d_sys_ann_prod'] * 100:+.2f}pp / "
          f"full {main['d_sys_ann_full'] * 100:+.2f}pp)")
    print(f"G2 回撤 prod≥-2pp & full≥-3pp        : {'PASS' if g2 else 'FAIL'}"
          f"  (prod {main['d_sys_mdd_prod'] * 100:+.2f}pp / "
          f"full {main['d_sys_mdd_full'] * 100:+.2f}pp)")
    print(f"G4 剂量 θ∈{{1.5,2.0,2.5}} 同号          : {'PASS' if g4 else 'FAIL'}"
          f"  ({[round(x * 100, 2) for x in dose]})")
    print(f"触发覆盖(W=60,θ=2.0)                : {hp * 100:.1f}% 期"
          + ("  ⚠ <5% ⇒ 判「触发不足」, 结清" if hp < 0.05 else ""))
    print("-" * 92)

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"),
           "prereg": "plans/oi-excluder-prereg-20260928.md",
           "ctl": arms["ctl"], "arms": arms, "verdicts": verdicts,
           "gates": {"g1_ann": g1, "g2_mdd": g2, "g4_dose_monotone": g4},
           "note": "补位语义(删行 ⇒ 次优递补); chase6 是选后剔不补位, 落地前须补测不补位臂"}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n产物 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
