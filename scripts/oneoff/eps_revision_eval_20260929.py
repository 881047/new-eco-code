# -*- coding: utf-8 -*-
"""O4 EPS 修正动量族正式评估 —— plans/eps-revision-prereg-20260929.md 的执行体。

候选 3 个（分母 3）：rev_fy1_1m / rev_fy1_3m / rev_fy2_3m。
语义前提（预注册 §0）：fy1_eps=FY2026 预测、fy2_eps=FY2027 预测（采集时点财年重映射，
四条独立证据在预注册）；修正动量 = 同财年跨报告修正（天然同财年）。

判据（预注册 §3，跑前冻结）：
- 单因子档 gate.verdict_single 全套照跑 —— O4 数据窗 2024-01 起 ⇒ DEAD 段结构性无数据
  ⇒ S1/S2/S3 判"未测"（verdict_single 语义：未测不否决）；S5 同理传 None。
- ACTIVE 段降级读数：aS1=|ACTIVE head|≥0.3%/月、aS2=|ACTIVE t|≥2.0、
  S4=与现役池（factor 表 − factor_exclusions）|ρ|max≤0.7（Spearman，hetero 同款）。
  **触发条款：aS1 ∧ aS2 ∧ S4 ⇒ 剔除器 A/B 启动**；降级读数不得替代 S1-S3 出 PASS。
- 六判据门 gate.summarize（churn_cost 真实费率；DEAD NaN ⇒ p_adj NaN 判否，fail-closed）。
只读零写库。产物 experiments/eps_revision_eval_20260929.json。
"""
import json
import sqlite3
import sys
import warnings
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from app.alpha import critic, gate
from app import config as cfg_mod
from app.factors import factor_engine as fe
from app.factors.factor_engine import load_market, month_end_dates
from app.pit.engine import PITEngine

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "eps_revision_eval_20260929.json"
PREREG = "plans/eps-revision-prereg-20260929.md"
DB = ROOT / "data" / "neweco.db"
A_BAR, T_BAR = 0.003, 2.0


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def stock_matrix(m, df, col):
    """逐日存量矩阵 (ndays × ncodes)：报告日写入值，交易日轴 ffill（PIT：发布日已公开）。"""
    td = pd.DatetimeIndex(m["close"].index)
    codes = list(m["close"].columns)
    V = np.full((len(td), len(codes)), np.nan, dtype=np.float32)
    td_ix = {d: i for i, d in enumerate(td)}
    code_ix = {c: i for i, c in enumerate(codes)}
    sub = df.dropna(subset=[col])
    for t, c, v in zip(sub["t"], sub["ts_code"], sub[col]):
        i, j = td_ix.get(t), code_ix.get(c)
        if i is not None and j is not None:
            V[i, j] = v
    return pd.DataFrame(V, index=td, columns=codes).ffill()


def main() -> int:
    t0 = datetime.now()
    cfg = cfg_mod.load_config()
    excl = set(cfg.get("factor_exclusions") or [])
    m = load_market()
    md_all = month_end_dates(pd.DatetimeIndex(m["close"].index))
    md_eval = pd.DatetimeIndex([d for d in md_all if d >= pd.Timestamp("2019-01-01")])
    zones = gate.zone_of(md_eval)
    fwd = gate.forward_return(m, gate.HORIZON)
    pit = PITEngine(m=m)
    tradable = pit.tradable_matrix(md_eval)

    conn = sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)
    industry = fe.industry_map(conn)
    rep = pd.read_sql(
        "SELECT ts_code, report_date, fy1_eps, fy2_eps FROM research_report_em "
        "WHERE fy1_eps IS NOT NULL", conn)
    rep["t"] = pd.to_datetime(rep["report_date"], format="%Y%m%d")
    names = [r[0] for r in conn.execute("SELECT DISTINCT factor FROM factor ORDER BY factor")]
    live = [n for n in names if n not in excl]
    live_panels = {}
    for f in live:
        d = pd.read_sql("SELECT trade_date, ts_code, value FROM factor WHERE factor=?",
                        conn, params=(f,))
        w = d.pivot_table(index="trade_date", columns="ts_code",
                          values="value", aggfunc="last")
        w.index = pd.to_datetime(w.index, format="%Y%m%d")
        live_panels[f] = w.reindex(index=md_eval)
    conn.close()
    log(f"研报 {len(rep):,} 行 | 现役因子 {len(live)}（池 {len(names)} − 剔 {len(excl)}）")

    M1 = stock_matrix(m, rep, "fy1_eps")
    M2 = stock_matrix(m, rep, "fy2_eps")
    cands = {}
    for wname, W in (("1m", 21), ("3m", 63)):
        base = M1.shift(W)
        cands[f"rev_fy1_{wname}"] = (M1 - base) / base.abs().replace(0, np.nan)
    base2 = M2.shift(63)
    cands["rev_fy2_3m"] = (M2 - base2) / base2.abs().replace(0, np.nan)

    rows, triggered = [], []
    for name in sorted(cands):
        raw = cands[name].reindex(md_eval)
        if raw.dropna(how="all").empty:
            rows.append({"factor": name, "note": "全空"})
            continue
        sr = float(fe.sparse_ratio(raw))
        sub = fe.neutralize_sparse(raw, industry) if sr < fe.SPARSE_NONZERO_RATIO \
            else fe.neutralize(raw, industry)
        cost = critic.churn_cost(sub, md_eval)
        pm = sub.where(tradable.reindex(index=md_eval, columns=raw.columns).fillna(False))
        det = gate.evaluate(pm, fwd, md_eval, zones)
        if det.empty:
            rows.append({"factor": name, "note": "无有效截面"})
            continue
        dead, act = det[det.zone == "DEAD"], det[det.zone == "ACTIVE"]
        h_d = float(dead.head_excess.mean()) if len(dead) else float("nan")
        sd = float(dead.head_excess.std(ddof=1)) if len(dead) else float("nan")
        t_d = float(h_d / (sd / np.sqrt(len(dead)))) if len(dead) and sd > 0 else float("nan")
        h_a = float(act.head_excess.mean())
        sa = float(act.head_excess.std(ddof=1))
        t_a = float(h_a / (sa / np.sqrt(len(act)))) if sa > 0 else float("nan")

        # S4: 与现役池 |ρ|max（Spearman，hetero 同款）
        rho_max, rho_with = 0.0, ""
        for f, w in live_panels.items():
            a, b = sub.align(w, join="inner")
            both = a.notna() & b.notna()
            if both.sum().sum() < 500:
                continue
            av, bv = a.values[both.values], b.values[both.values]
            if len(av) < 500 or np.std(av) == 0 or np.std(bv) == 0:
                continue
            r = float(pd.Series(av).corr(pd.Series(bv), method="spearman"))
            if abs(r) > abs(rho_max):
                rho_max, rho_with = r, f

        rec = gate.summarize(name, det, n_tests=3,
                             cost_per_month=(cost or {}).get("cost_per_month"))
        vd = gate.verdict_single(h_d, t_d, h_a, shadow_abs_rho=rho_max,
                                 half_life_months=None)   # S5 未实判（DEAD 无数据），如实列报
        # ACTIVE 段降级读数（预注册 §3.2）
        a_s1 = abs(h_a) >= A_BAR
        a_s2 = abs(t_a) >= T_BAR
        s4_pass = bool(abs(rho_max) <= 0.7)
        fire = bool(a_s1 and a_s2 and s4_pass)
        if fire:
            triggered.append(name)
        rows.append({
            "factor": name, "sparse_ratio": round(sr, 4),
            "anchors_ACTIVE": int(len(act)), "anchors_DEAD": int(len(dead)),
            "ACTIVE_head": round(h_a, 4), "ACTIVE_head_t": round(t_a, 2),
            "DEAD_head": None if h_d != h_d else round(h_d, 4),
            "p_adj": round(rec["p_adj"], 4) if rec["p_adj"] == rec["p_adj"] else None,
            "cost_per_month": (cost or {}).get("cost_per_month"),
            "shadow_rho_max": round(rho_max, 4), "shadow_with": rho_with,
            "gate6_PASS": bool(rec["PASS"]),
            "s1_s5": {"s1": vd["s1_head"], "s2": vd["s2_t"], "s3": vd["s3_same_sign"],
                      "s4_decor": vd["s4_decor"], "s5": vd["s5_half_life"],
                      "PASS_single": vd["PASS_single"]},
            "downgraded": {"aS1_head": bool(a_s1), "aS2_t": bool(a_s2),
                           "s4_decor": s4_pass, "fire_AB": fire},
        })
        log(f"  {name:12s} ACTIVE {h_a:+.4f} t {t_a:+.2f} |ρ|max {rho_max:.3f}({rho_with}) "
            f"gate6 {rec['PASS']} aS1 {a_s1} aS2 {a_s2} S4 {s4_pass} ⇒ fire {fire}")

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"), "prereg": PREREG,
           "n_candidates": len(cands),
           "semantics_note": "fy1=FY2026/fy2=FY2027（采集时点重映射，预注册 §0 四证据）",
           "rows": rows,
           "verdict": {"n_fire": len(triggered), "triggered": triggered,
                       "note": "触发 ⇒ 剔除器 A/B（chase2 框架，单窗结构性条款："
                               "判决上限=候选登记，不进生产）"},
           "runtime_sec": round((datetime.now() - t0).total_seconds(), 1)}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"判决: 触发 A/B {len(triggered)}/{len(cands)} → {triggered or '无'}")
    print(f"产物: {OUT}  ({out['runtime_sec']}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
