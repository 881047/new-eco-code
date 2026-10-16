"""F44 · `holder_chg_4q` 当**剔除器**（不是加一路多头分）—— 组合层两轴判决（预注册）

## 为什么换机制再测一次

上一轮 F43 把该因子**并按进融合分**（`2×W5+1×pit+1×holder`，权重各 1），结果：
全史 **−2.9pp**（死区 24.8→19.6）、留出集 +2pp、MDD −36.9%→−32.2%、换手 −27%，增量留出集 **t≈0**
⇒ 按预注册判据不采纳。复盘出两点：

1. **加法式融入会稀释**：这个因子的信息是"**谁该被躲开**"，把它当第四路多头分，等于拿它去和
   `2×W5+pit` 抢排名，好的持仓被稀释（F22 早就记录过"融合不单调"）；
2. F43 那版还**丢了 12 期**（因子早期无覆盖 ⇒ 该臂缺锚点 ⇒ 交集 491→479），样本都不齐。

所以这一版换机制、也修样本：**在选股之前，把"筹码结构最差的 20%"直接剔除**（不参与排名），
其余一切照旧。**没值的票一律不剔**（早期未披露 ⇒ 该期行为与 BASE 完全一致）⇒ **一期都不会丢**。

## 单臂（预注册就一个，不做权重扫描）

    BASE : 现役线上构型（2×W5+1×pit，100 万/top100/带 8×/满仓 w=1.0）
    FILT : 同上，但每个锚点**先剔掉该锚点内 `holder_chg_4q` 最低 20% 的票**（只剔有值的），
           再按原分取前 100 —— 持仓数量不变（仍是 100 只），只是名单换人。

## 预注册判据（两轴；与 F43 同一套，便于并排读）

    ① 全史（491 期同窗口）两套种子超额 ≥ BASE + **1.0pp**；
    ② ACTIVE = 留出集(2024-01~2026-08) 两套种子超额 ≥ BASE；
    ③ 全史 MDD 两套种子不劣于 BASE × 1.10；
    ④ **对照**：BASE 必须复现 F41 记录的满仓读数（全史 +18.00/+17.30、ACTIVE +10.38/+11.80，
       且同窗口 491 期）。复现不了 ⇒ 口径漂了，FILT 一律不得解读。

三条全过 ⇒ 采纳（另开"换 pin + 重算方案"流程）；**若只有 ②③ 好、① 不过，照样不采纳**
（那是"近期更好"的老陷阱，R1 已登记）。任一不过 ⇒ 写进证伪登记。

## 诚实边界

- **又一次**看那段已开封的留出集（F28/F41/F43 之后）⇒ ACTIVE 栏只作方向，不产生新证据；
- 因子覆盖**逐年变化**（2016≈0%、2018≈50%、2024≈85%、2026≈91%）⇒ 这个"剔除"约束是**前松后紧**的，
  全史口径下的收益差异里混着"约束强度随时间上升"，不能当成常数效应读；逐段读数（DEAD 比 ACTIVE）
  恰好能反映这一点，所以两段必须并排看；
- 剔除比例 20% 是**事前定的一次**，不是扫出来的；要做剂量（10%/30%）必须另开 F45。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F44_holder_filter_months.py
"""
import hashlib
import importlib.util
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent
sys.path.insert(0, str(BASE))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


f1 = _load("f1", HERE / "F1_weekly_ic_decay.py")
f1b = _load("f1b", HERE / "F1b_weekly_open_overlap.py")
f17 = _load("f17", HERE / "F17_die_registry_kgd.py")
f22 = _load("f22", HERE / "F22_pair_final_selection.py")
f29 = _load("f29", HERE / "F29_tail_weighted_training.py")

from app import config as cfg_mod                 # noqa: E402
from app import db                                # noqa: E402
from app.factors import factor_engine as fe       # noqa: E402
from app.factors import holder_factors            # noqa: E402
from app.pit.engine import PITEngine              # noqa: E402

log = logging.getLogger("f44")
OUT = HERE / "F44_holder_filter_months_result.json"
FULL = ("2010-01-01", "2030-12-31")
DEAD = ("2020-09-01", "2023-12-31")
ACTIVE = ("2024-01-01", "2026-08-31")
SEED_FILES = {"seedA": "F4_scores_seedA.npz", "seedB": "F4_scores_seedB.npz"}
W = 1.0                       # 线上现役敞口(满仓)
EXCL_PCT = 20.0               # 剔除最差 20%（事前定, 不扫描）
MIN_MEASURED = 200            # 单锚点至少这么多只有值才做剔除
BASE_REF = {"full": [18.00, 17.30], "active": [10.38, 11.80], "n_common": 491}


def apply_filter(fused: np.ndarray, H: np.ndarray, ok_pool: np.ndarray) -> tuple:
    """逐锚点剔除 `holder_chg_4q` 最低 EXCL_PCT% 的票（只剔有值的; 全无值 ⇒ 不剔）。

    返回 (改过的分数矩阵副本, 统计: 应用了剔除的锚点数 / 平均剔除占比)。
    """
    out = fused.copy()
    applied, ratios = 0, []
    for k in range(out.shape[0]):
        v = H[k]
        ok = ok_pool[k] & np.isfinite(v) & np.isfinite(out[k])
        if ok.sum() < MIN_MEASURED:
            continue                      # 该锚点没有可用因子 ⇒ 不剔(与 BASE 完全一致)
        thr = float(np.percentile(v[ok], EXCL_PCT))
        bad = ok & (v <= thr)
        out[k, bad] = np.nan
        applied += 1
        ratios.append(float(bad.sum()) / float(ok_pool[k].sum() or 1))
    return out, {"applied_anchors": applied,
                 "mean_excluded_share_of_pool": round(float(np.mean(ratios)), 4) if ratios else 0.0}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    cols = list(m["close"].columns)
    pit = PITEngine(m=m, cfg=cfg)
    mask = f1.tradable_frame(pit, wk, cols)
    mask_np = mask.fillna(False).to_numpy()
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    conn = sqlite3.connect(f"file:{BASE / 'data' / 'neweco.db'}?mode=ro", uri=True)
    PITSC = f17.table_scores(conn, "model_score_die_pit", wk, cols)
    conn.close()
    conn = db.get_conn()
    try:
        Hz = pd.DataFrame(dict(holder_factors.compute_holder_factors_safe(dates))
                          ["holder_chg_4q"]).reindex(index=wk, columns=cols)
    finally:
        conn.close()
    H = Hz.to_numpy(dtype=np.float64)
    log.info("holder_chg_4q 就绪 | 整格覆盖 %.1f%% | 剔除比例 %.0f%%",
             100 * np.isfinite(H).mean(), EXCL_PCT)

    c = {**cfg, "capital": f29.CAPITAL,
         "regime_equity_weight": {"aggressive": {"attack": W, "neutral": W, "defense": W}}}
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "equity_weight": W, "exclude_pct": EXCL_PCT, "factor": "holder_chg_4q",
           "windows": {"full": FULL, "dead": DEAD, "active": ACTIVE},
           "base_reference": BASE_REF, "arms": {}, "filter_stats": {},
           "increment": {}, "verdict": {}, "secs": None}
    series, common = {}, None
    for seed, fn in SEED_FILES.items():
        z = np.load(HERE / fn, allow_pickle=True)
        W5 = pd.DataFrame(z["scores"],
                          index=pd.to_datetime([str(d) for d in z["anchors"]]),
                          columns=[str(c_) for c_ in z["cols"]]).reindex(index=wk, columns=cols)
        fused = pd.DataFrame(f22.wr((W5, 2), (PITSC, 1)), index=wk, columns=cols)
        f_np = fused.to_numpy(dtype=np.float64)
        filt_np, fstat = apply_filter(f_np, H, mask_np)
        res["filter_stats"][seed] = fstat
        log.info("[%s] 剔除生效锚点 %d | 平均剔除占票池 %.2f%%", seed, fstat["applied_anchors"],
                 100 * fstat["mean_excluded_share_of_pool"])
        arms = {"BASE": fused, "FILT": pd.DataFrame(filt_np, index=wk, columns=cols)}
        for tag, Sdf in arms.items():
            s = f29._run_series(m, wk, mask, pit, c, pos_of, mask_np, op_adj, Sdf)
            series[(seed, tag)] = s
            ds = set(pd.to_datetime(s.loc[s["ret"].notna() & s["bench_ret"].notna(), "date"]))
            common = ds if common is None else (common & ds)
    common = sorted(common or [])
    log.info("同窗口对齐: %d 期", len(common))
    for (seed, tag), s in series.items():
        full = f29._aligned_stats(s, common)
        dead = f29._zone(s, *DEAD)
        active = f29._zone(s, *ACTIVE)
        res["arms"].setdefault(seed, {})[tag] = {"full": full, "dead": dead, "active": active}
        log.info("[%s|%s] 全史 %spp (MDD %.1f%%, 换手 %s) | DEAD %spp | ACTIVE %spp (MDD %.1f%%)",
                 seed, tag, full.get("excess_pp"), 100 * full.get("mdd", float("nan")),
                 full.get("turnover"), dead.get("excess_pp"), active.get("excess_pp"),
                 100 * active.get("mdd", float("nan")))

    ctrl = {}
    for i, seed in enumerate(SEED_FILES):
        b = res["arms"][seed]["BASE"]
        ctrl[seed] = {"full_was": BASE_REF["full"][i], "full_now": b["full"].get("excess_pp"),
                      "active_was": BASE_REF["active"][i], "active_now": b["active"].get("excess_pp")}
        ctrl[seed]["ok"] = (abs(float(b["full"].get("excess_pp")) - BASE_REF["full"][i]) < 0.05
                            and abs(float(b["active"].get("excess_pp")) - BASE_REF["active"][i]) < 0.05)
    ctrl_ok = all(v["ok"] for v in ctrl.values()) and len(common) == BASE_REF["n_common"]
    res["control"] = {"ok": ctrl_ok, "detail": ctrl, "n_common": len(common),
                      "note": "BASE 必须复现 F41 满仓读数且同窗口 491 期(本臂不丢期)"}
    log.info("对照(BASE 复现 F41 / 窗口 %d): %s", len(common), "通过" if ctrl_ok else "**失败**")

    for seed in SEED_FILES:
        b, a = series[(seed, "BASE")], series[(seed, "FILT")]
        d = (a.assign(date=pd.to_datetime(a["date"])).set_index("date")["ret"]
             - b.assign(date=pd.to_datetime(b["date"])).set_index("date")["ret"]).dropna()
        act = d[(d.index >= ACTIVE[0]) & (d.index <= ACTIVE[1])]
        res["increment"][seed] = {
            "t_diff_full": round(float(f1b.hac_t(d.to_numpy(), 2)), 2) if len(d) > 10 else None,
            "active_mean_pp_per_period": round(100 * float(act.mean()), 3) if len(act) else None,
            "t_diff_active": (round(float(f1b.hac_t(act.to_numpy(), 2)), 2)
                              if len(act) > 10 else None)}
        log.info("[%s] FILT−BASE: 全史 t=%s | 留出集 每期 %+.3fpp (t=%s)", seed,
                 res["increment"][seed]["t_diff_full"],
                 res["increment"][seed]["active_mean_pp_per_period"] or 0.0,
                 res["increment"][seed]["t_diff_active"])

    passed = True
    for seed in SEED_FILES:
        b, a = res["arms"][seed]["BASE"], res["arms"][seed]["FILT"]
        if not (a["full"].get("excess_pp") is not None
                and a["full"]["excess_pp"] >= b["full"]["excess_pp"] + 1.0
                and a["active"].get("excess_pp") is not None
                and a["active"]["excess_pp"] >= b["active"]["excess_pp"]
                and a["full"].get("mdd") is not None
                and a["full"]["mdd"] >= b["full"]["mdd"] * 1.10):
            passed = False
    res["verdict"] = {
        "control_ok": ctrl_ok,
        "adopt": bool(passed and ctrl_ok),
        "criteria": "两套种子同时: ①全史 ≥ BASE+1.0pp ②ACTIVE ≥ BASE ③全史 MDD ≥ BASE×1.10; ④对照须复现",
        "note": "ACTIVE 是已开封留出集, 只作方向; 只有 ②③ 好而 ① 不过 ⇒ 照样不采纳(R1)"}
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== F44 判决: 采纳=%s (对照=%s) | %.1fs → %s ==",
             res["verdict"]["adopt"], ctrl_ok, res["secs"], OUT)


if __name__ == "__main__":
    main()
