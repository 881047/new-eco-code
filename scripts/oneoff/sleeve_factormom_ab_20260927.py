# -*- coding: utf-8 -*-
"""周频袖套 · 因子动量/分块倾斜 A/B —— `plans/sleeve-factormom-prereg-20260927.md` 的执行体。

两臂同种子 [42,7] 同锚点同进程, 训练语义逐字复刻 scores.train(月度重训/36 月周锚窗/
禁运 3 锚点/horizon=5/50 轮), 唯一差异 = Arm B 在每个重训点动态剔除"冷"分块:
  orientation = sign(块内因子等权 IC 均值 over 训练窗全段);
  trailing    = 块内因子等权 IC 均值 over tr 最后 12 锚点;
  冷          = oriented trailing IC < 0; 每重训点最多剔 2 块(取最负), 保持 ≥15 因子。
组合层 = 24 补同款配对简化模拟器(top-100/band=2/t+1 开盘/30bp, 两臂同规则)。
判据(§二 冻结): P1 配对 Δ超额均值>0 且 t≥2 / P2 MDD 劣化≤2pp / P3 换手 |Δ|≤10pp。
判决: PASS(进拍板) / REJECT(线结清) / INDETERMINATE。只读零写库。
产物 experiments/sleeve_factormom_ab_20260927.json。
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
from app.alpha import gate
from app.factors.factor_engine import load_market
from app.weekly import blocks as B
from app.weekly.scores import (HORIZON, EMBARGO, TRAIN_MONTHS, N_JOBS, PARAMS,
                               build_panel, forward_excess, week_end_dates, _rank_pct)

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "sleeve_factormom_ab_20260927.json"
PREREG = "plans/sleeve-factormom-prereg-20260927.md"
SEEDS_AB = [42, 7]
EVAL_FROM = pd.Timestamp("2021-01-01")
COST = 0.003
TOP_N, BAND = 100, 2
TRAIL_N = 12
MAX_EXCL_BLOCKS = 2


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def run_port(S: np.ndarray, fwd: np.ndarray, mk: np.ndarray):
    A, C = S.shape
    held = []
    exc_series, to_series, idx_series = [], [], []
    for i in range(A):
        s = np.where(mk[i] & np.isfinite(S[i]), S[i], -np.inf)
        if int((s > -np.inf).sum()) < TOP_N:
            continue
        order = np.argsort(-s)
        top = order[:TOP_N]
        keep_set = set(order[:TOP_N + BAND].tolist())
        kept = [c for c in held if c in keep_set]
        enters = [c for c in top if c not in set(kept)]
        new_held = kept + enters[:TOP_N - len(kept)]
        to = 1 - len(set(new_held) & set(held)) / max(1, len(new_held)) if held else 1.0
        v = fwd[i][new_held]
        exc = float(np.nanmean(v)) if np.isfinite(v).any() else np.nan
        exc = exc - COST * to
        exc_series.append(exc); to_series.append(to); idx_series.append(i)
        held = new_held
    return np.array(exc_series), np.array(to_series), np.array(idx_series)


def metrics(exc: np.ndarray) -> dict:
    exc = exc[np.isfinite(exc)]
    if len(exc) < 12:
        return {"n": len(exc)}
    eq = float(np.prod(1 + exc) ** (52 / len(exc)) - 1)
    nav = np.cumprod(1 + exc)
    peak = np.maximum.accumulate(nav)
    mdd = float((nav / peak - 1).min())
    vol = float(exc.std(ddof=1) * np.sqrt(52))
    return {"n": int(len(exc)), "ann_excess": round(eq, 4), "mdd": round(mdd, 4),
            "vol_ann": round(vol, 4),
            "sharpe_excess": round(eq / vol, 3) if vol else None}


def block_tilt(IC_all: np.ndarray, tr: np.ndarray, names: list) -> tuple:
    """冷分块判定(real-time: 只用 tr 内锚点的逐锚点 IC)。

    orientation = sign(块内因子等权 IC 均值 over tr 全段);
    trailing    = 块内因子等权 IC 均值 over tr 最后 TRAIL_N 锚点;
    冷          = trailing × sign(orientation) < 0; 每点最多剔 MAX_EXCL_BLOCKS 块。
    返回 (keep 因子名列表, 被剔块列表)。
    """
    sub = IC_all[tr]                                    # len(tr) × 31
    cold, ori_read = [], {}
    for b in B.BLOCK_KEYS:
        facs = [f for f in B.BLOCK_MEMBERS[b] if f in names]
        if not facs:
            continue
        cols = [names.index(f) for f in facs]
        blk = sub[:, cols].mean(axis=1)                 # 块内等权 IC 序列
        blk = blk[np.isfinite(blk)]
        if len(blk) < TRAIL_N + 10:
            continue
        ori = float(np.mean(blk))
        trail = float(np.mean(blk[-TRAIL_N:]))
        oriented = trail * (1.0 if ori >= 0 else -1.0)
        ori_read[b] = {"orientation": round(ori, 4), "trailing": round(trail, 4),
                       "oriented_trailing": round(oriented, 4)}
        if oriented < 0:
            cold.append((b, oriented))
    cold.sort(key=lambda x: x[1])
    excl = [b for b, _ in cold[:MAX_EXCL_BLOCKS]]
    keep = [f for f in names
            if f not in [m for b in excl for m in B.BLOCK_MEMBERS[b]]]
    return keep, excl, ori_read


def walk_forward(panel: dict, cfg: dict, IC_all: np.ndarray, momentum: bool) -> tuple:
    """逐字复刻 scores.train 语义; momentum=True 时每重训点动态剔除冷分块。"""
    import lightgbm as lgb

    F, names, anchors = panel["F"], panel["names"], panel["anchors"]
    A, C, K = F.shape
    Y = forward_excess(panel["m"], panel["mask"], HORIZON)
    seen, is_last = set(), np.zeros(A, dtype=bool)
    for i in range(A - 1, -1, -1):
        p = anchors[i].to_period("M")
        if p not in seen:
            seen.add(p)
            is_last[i] = True
    month_idx = np.where(is_last)[0]
    params = dict(PARAMS)
    params.update(n_jobs=N_JOBS, verbose=-1, deterministic=True)
    S = np.full((A, C), np.nan, dtype=np.float32)
    fi = {n: i for i, n in enumerate(names)}
    block_factors = {b: [fi[f] for f in B.BLOCK_MEMBERS[b] if f in fi]
                     for b in B.BLOCK_KEYS}
    activity, n_fit, t0 = [], 0, time.time()
    for mi, j in enumerate(month_idx):
        lo = anchors[j] - pd.DateOffset(months=TRAIN_MONTHS)
        tr = np.where((anchors >= lo) & (np.arange(A) <= j - 1 - EMBARGO))[0]
        if len(tr) < 40:
            continue
        keep = list(range(K))
        excl = []
        if momentum:
            keep_names, excl, _ = block_tilt(IC_all, tr, names)
            keep = [names.index(f) for f in keep_names]
            if excl:
                activity.append({"anchor": str(anchors[j].date()),
                                 "excluded_blocks": excl,
                                 "n_factors": len(keep)})
        nxt = month_idx[mi + 1] if mi + 1 < len(month_idx) else A
        tgt = np.arange(j, nxt)
        if len(tgt) == 0:
            continue
        Xtr = F[tr][:, :, keep].reshape(-1, len(keep))
        ytr = Y[tr].reshape(-1)
        ok = np.isfinite(ytr) & np.isfinite(Xtr).any(axis=1)
        Xtr, ytr = Xtr[ok], ytr[ok]
        if len(ytr) < 20000:
            continue
        Xte = F[tgt][:, :, keep].reshape(-1, len(keep))
        feat = [names[k] for k in keep]
        acc = np.zeros((len(tgt), C), dtype=np.float64)
        for s in SEEDS_AB:
            dtr = lgb.Dataset(Xtr, label=ytr, feature_name=feat)
            bo = lgb.train({**params, "seed": int(s)}, dtr, num_boost_round=50)
            acc += _rank_pct(bo.predict(Xte).reshape(len(tgt), C))
        S[tgt] = (acc / len(SEEDS_AB)).astype(np.float32)
        n_fit += 1
        if n_fit % 12 == 0:
            log(f"  {'B' if momentum else 'A'} {n_fit}/{len(month_idx)} 期 ({time.time()-t0:.0f}s)")
    log(f"{'B' if momentum else 'A'} 臂打分完成: {n_fit} 个模型, {time.time()-t0:.0f}s")
    return S, activity


def main() -> int:
    t0 = time.time()
    m = load_market()
    cfg = cfg_mod.load_config()
    daily_dates = pd.DatetimeIndex(m["close"].index)
    all_weeks = week_end_dates(daily_dates)
    anchors = all_weeks[(all_weeks >= pd.Timestamp("2018-01-01"))
                        & (all_weeks <= pd.Timestamp("2026-08-31"))]
    log(f"锚点 {len(anchors)}, 候选块 6, 分块定义 = weekly/blocks.py canonical")

    panel = build_panel(m, anchors, cfg)
    names = list(panel["names"])
    A, C, K = panel["F"].shape
    Y = forward_excess(m, panel["mask"], HORIZON)
    mk = panel["mask"].to_numpy(dtype=bool)
    log(f"面板 {panel['F'].shape} ({time.time()-t0:.0f}s)")

    # 逐锚点 × 逐因子秩 IC(real-time 原料; 秩一次算完供全程复用)
    RY = pd.DataFrame(Y).rank(axis=1).to_numpy()
    IC_all = np.full((A, K), np.nan)
    finF = np.isfinite(panel["F"])
    finY = np.isfinite(Y)
    for i in range(A):
        v = mk[i] & finY[i]
        if v.sum() < 200:
            continue
        ry = RY[i]
        for j in range(K):
            vj = v & finF[i, :, j]
            if vj.sum() < 200:
                continue
            IC_all[i, j] = float(np.corrcoef(
                pd.Series(panel["F"][i, :, j])[vj].rank().to_numpy(), ry[vj])[0, 1])
    log(f"逐锚点 IC 完成 ({time.time()-t0:.0f}s)")

    S_a, act_a = walk_forward(panel, cfg, IC_all, momentum=False)
    S_b, act_b = walk_forward(panel, cfg, IC_all, momentum=True)

    fwd = forward_excess(m, panel["mask"], HORIZON)
    exc_a, to_a, idx = run_port(S_a, fwd, mk)
    exc_b, to_b, _ = run_port(S_b, fwd, mk)
    dts = pd.DatetimeIndex([panel["anchors"][i] for i in idx])
    ev = dts >= EVAL_FROM
    ea, eb = exc_a[ev], exc_b[ev]
    d = (eb - ea)[np.isfinite(eb - ea)]
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
    log(f"配对 Δ mean {d.mean()*1e4:+.2f}bp t={t_stat:.2f} n={len(d)} | "
        f"annA {m_a.get('ann_excess')} annB {m_b.get('ann_excess')} | "
        f"mddA {m_a.get('mdd')} mddB {m_b.get('mdd')} | 换手 A {to_a_m:.3f} B {to_b_m:.3f}")
    log(f"判决: {verdict}")

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"), "prereg": PREREG,
           "seeds": SEEDS_AB, "n_anchors": int(len(anchors)),
           "eval_from": str(EVAL_FROM.date()), "n_eval": int(len(d)),
           "arm_a": m_a, "arm_b": m_b,
           "turnover": {"a": round(to_a_m, 4), "b": round(to_b_m, 4)},
           "paired": {"mean_delta_bp": round(float(d.mean()) * 1e4, 2),
                      "t": round(t_stat, 2), "n": int(len(d))},
           "criteria": {"p1": "Δmean>0 且 t≥2", "p2": "MDD 劣化≤2pp",
                        "p3": "|Δ换手|≤10pp"},
           "checks": {"p1": p1, "p2": p2, "p3": p3},
           "verdict": {"PASS": "PASS（进拍板）", "REJECT": "REJECT（线结清）",
                       "INDETERMINATE": "INDETERMINATE（延长观察）"}[verdict],
           "momentum_activity": act_b,
           "note": "分块倾斜试点(每重训点最多剔 2 冷块); 生产采纳须 replay 全保真验证+拍板",
           "runtime_sec": round(time.time() - t0, 1)}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"产物: {OUT}  ({time.time() - t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
