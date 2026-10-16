# -*- coding: utf-8 -*-
"""周频袖套 A/B（31 vs 31+15）—— `plans/sleeve-ab-prereg-20260927.md` 的执行体。

两臂同种子 [42,7] 同锚点同进程（预注册 §一）:
  Arm A = build_panel 原样 31 因子; Arm B = 同 + 15 个筛选过门候选（引擎逐字求值,
  与 V0 同款 neutralize, 列集对齐袖套）。训练 = weekly/scores.train（月度重训,
  36 月窗, 禁运 3, horizon=5, 50 轮）。
组合层 = 配对简化模拟器（预注册 §二, 两臂逐规则相同）: top-100 / band=2 / t+1 开盘
  调仓 / 双边 30bp / 等权; 收益 = forward_excess 同源（相对 PIT 池等权 ⇒ 即超额序列）。
判据（§三 冻结）: P1 配对 Δ超额均值 > 0 且 t ≥ 2; P2 MDD 劣化 ≤ 2pp; P3 换手 ≤ +10pp。
判决: PASS(进拍板) / REJECT(线结清) / INDETERMINATE。
只读零写库。产物 experiments/sleeve_ab_20260927.json。
"""
import json
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
from app import db
from app.alpha import expr as AE
from app.alpha import zoos
from app.factors import factor_engine as fe
from app.factors.factor_engine import load_market
from app.weekly.scores import (HORIZON, build_panel, forward_excess,
                               train, week_end_dates)

ROOT = Path(__file__).resolve().parents[2]
SCREEN = json.loads((ROOT / "experiments" / "sleeve_bank_screen_20260927.json")
                    .read_text(encoding="utf-8"))
OUT = ROOT / "experiments" / "sleeve_ab_20260927.json"
PREREG = "plans/sleeve-ab-prereg-20260927.md"
CANDS = SCREEN["verdict"]["candidates"]          # 15 个, 机械来自二十三补
SEEDS_AB = [42, 7]
EVAL_FROM = pd.Timestamp("2021-01-01")
COST = 0.003
TOP_N, BAND = 100, 2


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def run_port(S: np.ndarray, fwd: np.ndarray, mk: np.ndarray):
    A, C = S.shape
    held = []                                    # 现有持仓（索引列表）
    exc_series, to_series, idx_series = [], [], []
    for i in range(A):
        s = np.where(mk[i] & np.isfinite(S[i]), S[i], -np.inf)
        n_ok = int((s > -np.inf).sum())
        if n_ok < TOP_N:
            continue
        order = np.argsort(-s)
        top = order[:TOP_N]
        top_set = set(top.tolist())
        keep_set = set(order[:TOP_N + BAND].tolist())
        # band 留存: 现有持仓中排名仍在前 TOP_N+BAND 的保留; 其余按分数补足到 TOP_N
        kept = [c for c in held if c in keep_set]
        enters = [c for c in top if c not in set(kept)]
        new_held = kept + enters[:TOP_N - len(kept)]
        to = 1 - len(set(new_held) & set(held)) / max(1, len(new_held)) if held else 1.0
        y = fwd[i]
        v = y[new_held]
        exc = float(np.nanmean(v)) if np.isfinite(v).any() else np.nan
        exc = exc - COST * to                      # 双边 30bp × 换手（预注册 §二）
        exc_series.append(exc); to_series.append(to); idx_series.append(i)
        held = new_held
    return np.array(exc_series), np.array(to_series), np.array(idx_series)


def metrics(exc: np.ndarray) -> dict:
    exc = exc[np.isfinite(exc)]
    if len(exc) < 12:
        return {"n": len(exc)}
    eq = float(np.prod(1 + exc) ** (52 / len(exc)) - 1)     # 周频 → 年化(超额口径)
    nav = np.cumprod(1 + exc)
    peak = np.maximum.accumulate(nav)
    mdd = float((nav / peak - 1).min())
    vol = float(exc.std(ddof=1) * np.sqrt(52))
    return {"n": int(len(exc)), "ann_excess": round(eq, 4), "mdd": round(mdd, 4),
            "vol_ann": round(vol, 4), "sharpe_excess": round(eq / vol, 3) if vol else None}


def main() -> int:
    t0 = time.time()
    m = load_market()
    cfg = cfg_mod.load_config()
    daily_dates = pd.DatetimeIndex(m["close"].index)
    all_weeks = week_end_dates(daily_dates)
    anchors = all_weeks[(all_weeks >= pd.Timestamp("2018-01-01"))
                        & (all_weeks <= pd.Timestamp("2026-08-31"))]
    log(f"锚点 {len(anchors)} ({anchors[0].date()}..{anchors[-1].date()}), 候选 {len(CANDS)}")

    # ---------- 两臂面板 ----------
    panel_a = build_panel(m, anchors, cfg)
    expr_map = {name: expr for name, expr, _ in zoos.qlib158()}
    with db.conn_ctx() as conn:
        industry = fe.industry_map(conn)
    cols = list(m["close"].columns)
    cand_week = {}
    eng = AE.Engine(AE.build_panel(m))
    for aid in CANDS:
        daily = eng.eval(expr_map[aid])
        cand_week[aid] = fe.neutralize(daily.reindex(index=anchors, columns=cols),
                                       industry)
    log(f"候选面板就绪 ({time.time()-t0:.0f}s)")

    Fa = panel_a["F"]
    Fb = np.concatenate([Fa, np.stack([cand_week[a].to_numpy(dtype=np.float32)
                                       for a in CANDS], axis=2)], axis=2)
    names_b = list(panel_a["names"]) + list(CANDS)
    panel_b = {**panel_a, "F": Fb, "names": names_b}
    log(f"Arm A 面板 {Fa.shape} | Arm B 面板 {Fb.shape}")

    # ---------- 训练（同种子同锚点） ----------
    S_a = train(panel_a, cfg, seeds=SEEDS_AB,
                progress=lambda n, t, el: log(f"  A {n}/{t} 期 ({el:.0f}s)") if n % 10 == 0 else None)
    log("Arm A 训练完成")
    S_b = train(panel_b, cfg, seeds=SEEDS_AB,
                progress=lambda n, t, el: log(f"  B {n}/{t} 期 ({el:.0f}s)") if n % 10 == 0 else None)
    log("Arm B 训练完成")

    # ---------- 组合层配对模拟 ----------
    fwd = forward_excess(m, panel_a["mask"], HORIZON)
    mk = panel_a["mask"].to_numpy(dtype=bool)
    exc_a, to_a, idx = run_port(S_a, fwd, mk)
    exc_b, to_b, _ = run_port(S_b, fwd, mk)
    dts = pd.DatetimeIndex([panel_a["anchors"][i] for i in idx])
    ev = dts >= EVAL_FROM
    ea, eb = exc_a[ev], exc_b[ev]
    d = eb - ea
    d = d[np.isfinite(d)]
    t_stat = float(d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))) if d.std(ddof=1) > 0 else np.nan
    m_a, m_b = metrics(ea), metrics(eb)
    to_a_m, to_b_m = float(np.nanmean(to_a[ev])), float(np.nanmean(to_b[ev]))
    p1 = bool(d.mean() > 0 and t_stat >= 2)
    p2 = bool(m_b["mdd"] >= m_a["mdd"] - 0.02)
    p3 = bool(abs(to_b_m - to_a_m) <= 0.10)
    if d.mean() <= 0:
        verdict = "REJECT"
    elif p1 and p2 and p3:
        verdict = "PASS"
    else:
        verdict = "INDETERMINATE"
    log(f"配对 Δ(每锚点) mean {d.mean()*1e4:+.2f}bp t={t_stat:.2f} n={len(d)} | "
        f"annA {m_a.get('ann_excess')} annB {m_b.get('ann_excess')} | "
        f"mddA {m_a.get('mdd')} mddB {m_b.get('mdd')} | 换手A {to_a_m:.3f} B {to_b_m:.3f}")
    log(f"判决: {verdict}")

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"), "prereg": PREREG,
           "candidates": CANDS, "seeds": SEEDS_AB, "n_anchors": int(len(anchors)),
           "eval_from": str(EVAL_FROM.date()), "n_eval": int(len(d)),
           "arm_a": m_a, "arm_b": m_b,
           "turnover": {"a": round(to_a_m, 4), "b": round(to_b_m, 4)},
           "paired": {"mean_delta_bp": round(float(d.mean()) * 1e4, 2),
                      "t": round(t_stat, 2), "n": int(len(d))},
           "criteria": {"p1": "Δmean>0 且 t≥2", "p2": "MDD 劣化≤2pp",
                        "p3": "|Δ换手|≤10pp"},
           "checks": {"p1": p1, "p2": p2, "p3": p3},
           "verdict": {"PASS": "PASS（进拍板）", "REJECT": "REJECT（线结清）",
                       "INDETERMINATE": "INDETERMINATE"}[verdict],
           "note": "配对简化模拟器(两臂同规则); 生产采纳须 replay 全保真验证+拍板",
           "runtime_sec": round(time.time() - t0, 1)}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"产物: {OUT}  ({time.time() - t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
