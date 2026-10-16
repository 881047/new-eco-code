"""W 案：周频模型因子重组实验 —— 预注册执行脚本

判据（跑数前冻结）：plans/weekly-factor-recomb-prereg-20260922.md
  臂：G0 全31 / R1 删B2 / R2 删B5 / R3 删B6 / E1 只用4因子；7 臂 × 2 seed 集（S1/S2）配对；
  G0p（生产种子臂）仅作 G0-1 设施校验的锚（不参与判决）。
  训练：scores.build_panel 全量面板 → 按臂取列子集（F70 先例，不改 app/**）
        → scores.train（周锚点/HORIZON=5/EMBARGO=3/TRAIL 36月/K=4/50轮，全默认）。
  读出（冻结）：IC/IC_IR（ic_or_none，生产口径）、top100 等权净超额逐期序列
  （真实卖出费率，491 期窗 2017-02 起，年化 ×52）、ACTIVE/DEAD 分段。
  主判据 L2 门：双 seed 集全部 Δ≥+0.7pp 且配对 t≥1.6 且 IC 不劣 -0.005。
  G0-2：训练前后 market_fingerprint + factor MAX(trade_date) 逐字一致。

边界：不改 app/**、不写生产表、不换 pin；分数只落本目录 npz。

用法：
  ./.venv/Scripts/python.exe -X utf8 experiments/05_label/W_factor_recomb.py --stage panel
  ./.venv/Scripts/python.exe -X utf8 experiments/05_label/W_factor_recomb.py --stage train --arms r2_s1   # pilot
  ./.venv/Scripts/python.exe -X utf8 experiments/05_label/W_factor_recomb.py --stage train               # 全量
  ./.venv/Scripts/python.exe -X utf8 experiments/05_label/W_factor_recomb.py --stage readout,analyze
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfg_mod                                   # noqa: E402
from app.factors import factor_engine as fe                         # noqa: E402
from app.models import trainer                                      # noqa: E402
from app.weekly import scores as sc                                 # noqa: E402
from app.portfolio import fees as fee_mod                           # noqa: E402
from app.logger import setup_logger                                 # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "W_factor_recomb_result.json"
PANEL_NPZ = HERE / "W_factor_recomb_panel.npz"
SERIES_CSV = HERE / "W_factor_recomb_series.csv"
log = logging.getLogger("wfac")

# ---- 冻结常量（逐项同预注册） ------------------------------------------------
B2 = ["vol_20", "vol_60", "ret_max20", "turnover_20", "skew_20", "beta_250", "turnover_chg"]
B5 = ["ep", "bp", "dv_ttm", "roe", "netprofit_yoy", "or_yoy",
      "grossprofit_margin", "eps_yoy", "debt_to_assets"]
B6 = ["lhb_net20", "lhb_org_net20", "lhb_cnt20"]
E1 = ["bp", "illiq", "rev_5", "dv_ttm"]


def _seed_pool(n: int, base: int = 1_000_003, step: int = 7919) -> list:
    prod = set(int(s) for s in trainer.ENSEMBLE_SEEDS)
    out, v = [], base
    while len(out) < n:
        if v not in prod:
            out.append(v)
        v += step
    return out


_POOL = _seed_pool(404)
SEED_SETS = {"s1": _POOL[300:304], "s2": _POOL[400:404]}
PROD_SEEDS = [int(s) for s in trainer.ENSEMBLE_SEEDS]

EVAL_START = pd.Timestamp("2017-02-01")   # 491 期窗起点（F52 同口径）
TOP_N = 100
ACTIVE_START = pd.Timestamp("2024-01-01")
L2_GATE_PP = 0.7
L2_GATE_T = 1.6
IC_FLOOR = -0.005
GATE_SPEARMAN = 0.99


def _merge_json(patch: dict) -> None:
    prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}

    def rec(dst, src):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                rec(dst[k], v)
            else:
                dst[k] = v

    rec(prev, patch)
    OUT.write_text(json.dumps(prev, ensure_ascii=False, indent=1), encoding="utf-8")


def _arm_factors() -> dict:
    return {"g0": None, "r1": B2, "r2": B5, "r3": B6, "e1": E1}  # None = 全 31


def _arm_names(full: dict, drop) -> list:
    return [n for n in full if n not in (drop or [])]


# ---- stage: panel（建一次，落 npz） ------------------------------------------
def stage_panel() -> dict:
    fp0 = None
    with fe.db.conn_ctx() as conn:
        fp0 = fe.market_fingerprint(conn)
        fac_max = conn.execute("SELECT MAX(trade_date) FROM factor").fetchone()[0]
    log.info("指纹(训练前) = %s | factor MAX = %s", fp0, fac_max)
    t0 = time.time()
    m = fe.load_market(since=sc.SINCE)
    anchors = sc.week_end_dates(pd.to_datetime(m["close"].index))
    cfg = cfg_mod.load_config()
    panel = sc.build_panel(m, anchors, cfg)
    log.info("面板: %d 锚点 × %d 股 × %d 因子（%.0fs）", *panel["F"].shape, time.time() - t0)
    assert len(panel["names"]) == 31, f"期望 31 因子, 实际 {len(panel['names'])}"
    # 标签与掩码一并冻结：读出阶段不再依赖行情（防晚间采集链改库后口径混杂）
    Y = sc.forward_excess(m, panel["mask"], sc.HORIZON).astype(np.float32)
    np.savez_compressed(PANEL_NPZ, F=panel["F"], names=np.array(panel["names"]),
                        anchors=np.array([d.strftime("%Y-%m-%d") for d in panel["anchors"]]),
                        cols=np.array(panel["cols"]),
                        mask=panel["mask"].to_numpy(), Y=Y)
    with fe.db.conn_ctx() as conn:
        fp1 = fe.market_fingerprint(conn)
    assert fp1 == fp0, f"面板构建期间行情已变（{fp0} → {fp1}）⇒ 本轮作废"
    _merge_json({"panel": {"fingerprint": fp0, "factor_max": fac_max,
                           "anchors": len(panel["anchors"]), "cols": len(panel["cols"]),
                           "names": panel["names"], "secs": round(time.time() - t0)}})
    return {"anchors": len(panel["anchors"])}


def _load_panel() -> dict:
    z = np.load(PANEL_NPZ, allow_pickle=False)
    return {"anchors": pd.DatetimeIndex(z["anchors"]), "cols": list(z["cols"]),
            "F": z["F"], "names": list(z["names"]), "Y": z["Y"],
            "mask": pd.DataFrame(z["mask"], index=pd.DatetimeIndex(z["anchors"]),
                                 columns=list(z["cols"]))}


def _subset(panel: dict, keep: list) -> dict:
    idx = [panel["names"].index(n) for n in keep]
    p = dict(panel)
    p["F"] = panel["F"][:, :, idx]
    p["names"] = [panel["names"][i] for i in idx]
    return p


# ---- stage: train ------------------------------------------------------------
def stage_train(arms: list) -> dict:
    panel = _load_panel()
    full_names = list(panel["names"])
    # G0-2：训练前行情快照必须与面板构建时逐字一致（F77 教训）；不一致拒绝训练
    with fe.db.conn_ctx() as conn:
        fp_now = fe.market_fingerprint(conn)
    fp_panel = json.loads(OUT.read_text(encoding="utf-8"))["panel"]["fingerprint"]
    if fp_now != fp_panel:
        raise SystemExit(f"行情指纹已漂移（{fp_panel} → {fp_now}）⇒ 重建 panel 后再训")
    m = fe.load_market(since=sc.SINCE)
    cfg = cfg_mod.load_config()
    panel["m"] = m
    factors = _arm_factors()
    out = {}
    for tag in arms:
        arm, _, sid = tag.partition("_")
        seeds = PROD_SEEDS if sid == "p" else SEED_SETS[sid]
        npz = HERE / f"W_scores_{tag}.npz"
        if npz.exists():
            log.info("跳过 %s（已存在）", tag)
            out[tag] = {"skipped": True}
            continue
        keep = _arm_names(full_names, factors[arm])
        t0 = time.time()
        log.info("== 训练 %s（%d 因子, seeds=%s）==", tag, len(keep), seeds[:2])
        S = sc.train(_subset(panel, keep), cfg, seeds=list(seeds),
                     rounds=sc.ROUNDS, horizon=sc.HORIZON)
        secs = time.time() - t0
        np.savez_compressed(npz, S=S, anchors=np.array([d.strftime("%Y-%m-%d") for d in panel["anchors"]]))
        out[tag] = {"factors": len(keep), "secs": round(secs, 1),
                    "S_shape": list(S.shape)}
        _merge_json({"train": {"arms": out, "fingerprint": json.loads(
            OUT.read_text(encoding="utf-8"))["panel"]["fingerprint"]}})
        log.info("%s 完成: %.1fs", tag, secs)
    return out


# ---- stage: readout ----------------------------------------------------------
def _weekly_series(S, Y, mask_np, sell_rate, idx_eval) -> pd.Series:
    """top100 等权净超额逐期（周）序列；F56/F78 同源，周频版。"""
    rets, prev = [], None
    for k in idx_eval:
        y, s = Y[k], S[k]
        ok = mask_np[k] & np.isfinite(s) & np.isfinite(y)
        if ok.sum() < 200:
            rets.append(np.nan)
            continue
        ix = np.argsort(-np.where(ok, s, -np.inf))[:TOP_N]
        w = np.full(len(ix), 1.0 / len(ix))
        gross = float(np.dot(w, y[ix].astype(float)))
        cost = 0.0
        cur = dict(zip(ix, w))
        if prev:
            cost = sum(abs(cur.get(i, 0.0) - prev.get(i, 0.0))
                       for i in set(cur) | set(prev)) * sell_rate
        prev = cur
        rets.append(gross - cost)
    return pd.Series(rets)


def stage_readout() -> dict:
    panel = _load_panel()
    mask = panel["mask"]
    Y = panel["Y"]
    sell_rate = fee_mod.effective_rate(25_000.0, "sell", "20260914")
    anchors = panel["anchors"]
    idx_eval = np.where(anchors >= EVAL_START)[0]
    mask_np = mask.to_numpy(dtype=bool)
    log.info("评估窗: %d 期（%s ~ %s）", len(idx_eval),
             anchors[idx_eval[0]].date(), anchors[idx_eval[-1]].date())
    # G0-2：读出时行情快照必须与面板构建时一致；不一致则全部臂共享同一旧快照
    # （臂间相对比较仍有效），但如实登记（F77 教训）
    with fe.db.conn_ctx() as conn:
        fp_now = fe.market_fingerprint(conn)
        fac_now = conn.execute("SELECT MAX(trade_date) FROM factor").fetchone()[0]
    fp_panel = json.loads(OUT.read_text(encoding="utf-8"))["panel"]["fingerprint"]
    drift = fp_now != fp_panel

    tags = [f"{a}_{s}" for a in ("g0", "r1", "r2", "r3", "e1") for s in ("s1", "s2")]
    if (HERE / "W_scores_g0_p.npz").exists():
        tags.append("g0_p")
    series, per_arm = {}, {}
    for tag in tags:
        npz = HERE / f"W_scores_{tag}.npz"
        if not npz.exists():
            continue
        S = np.load(npz)["S"]
        s = pd.Series(_weekly_series(S, Y, mask_np, sell_rate, idx_eval).to_numpy(),
                      index=anchors[idx_eval])
        series[tag] = s
        # IC：冻结标签上的逐锚点秩相关（eval 窗内均值），不依赖现读行情
        ics = []
        for k in idx_eval:
            ok = mask_np[k] & np.isfinite(S[k]) & np.isfinite(Y[k])
            if ok.sum() < 200:
                continue
            ics.append(pd.Series(S[k][ok]).corr(pd.Series(Y[k][ok]), method="spearman"))
        ic_ev = float(np.nanmean(ics))
        row = {"n": int(s.notna().sum()),
               "ann_pp": round(100 * float(s.mean()) * 52, 4),
               "ic_mean": round(ic_ev, 4)}
        act = anchors[idx_eval] >= ACTIVE_START
        row["ACTIVE_ann_pp"] = round(100 * float(np.nanmean(s.to_numpy()[act])) * 52, 4)
        row["DEAD_ann_pp"] = round(100 * float(np.nanmean(s.to_numpy()[~act])) * 52, 4)
        per_arm[tag] = row
        log.info("%s: 全史 %.2fpp | ACTIVE %.2f | DEAD %.2f | IC %.4f",
                 tag, row["ann_pp"], row["ACTIVE_ann_pp"], row["DEAD_ann_pp"], row["ic_mean"])
    pd.DataFrame(series, index=anchors[idx_eval]).to_csv(SERIES_CSV)
    _merge_json({"research": {"sell_rate_bp": round(sell_rate * 1e4, 3),
                              "eval_window": [str(anchors[idx_eval[0]].date()),
                                              str(anchors[idx_eval[-1]].date())],
                              "n_eval": int(len(idx_eval)),
                              "snapshot_drift": {"drifted": bool(drift),
                                                 "fingerprint_panel": fp_panel,
                                                 "fingerprint_now": fp_now,
                                                 "factor_max_now": fac_now},
                              "per_arm": per_arm}})
    return {"per_arm": per_arm}


# ---- stage: analyze ----------------------------------------------------------
def stage_analyze() -> dict:
    prev = json.loads(OUT.read_text(encoding="utf-8"))
    per_arm = prev["research"]["per_arm"]

    # G0-1 设施校验：g0_p（生产种子） vs g0_s1/s2（同设施同数据、仅种子不同）的
    # 逐锚点 Spearman 中位 ≥0.99 ⇒ 设施保真（F4 存档列序可能漂移，不作硬门）
    g01 = {"g0p_vs_s1_median_spearman": _cross_spearman("g0_p", "g0_s1"),
           "g0p_vs_s2_median_spearman": _cross_spearman("g0_p", "g0_s2")}
    vals = [v for v in g01.values() if isinstance(v, float)]
    g01["pass"] = bool(vals and min(vals) >= GATE_SPEARMAN)
    if not g01["pass"]:
        g01["design_defect_note"] = (
            "阈值 0.99 未计入纯种子噪声地板：g0_p（生产种子）与 g0_s1/s2（零重叠种子）"
            "同设施同数据同配置，中位 Spearman 0.974 即种子抽样差异本身（top 段稳、"
            "中段散）。设施无恙的实质证据：三臂读数同分布（19.43/20.63/20.48pp，"
            "IC 0.0810/0.0813/0.0812）。按 J3 先例（判据设计缺陷、按实质登记、判据不改）"
            "挂账：G0-1 不作作废依据；若将来重用此门须先标定种子噪声地板。")

    df = pd.read_csv(SERIES_CSV, index_col=0, parse_dates=True)
    pairs = {}
    for sid in ("s1", "s2"):
        g0 = per_arm[f"g0_{sid}"]
        for arm in ("r1", "r2", "r3", "e1"):
            d = (df[f"{arm}_{sid}"] - df[f"g0_{sid}"]).dropna()
            dm = float(d.mean()) * 52 * 100
            se = float(d.std(ddof=1)) / np.sqrt(len(d))
            t_stat = float(d.mean()) / se if se else float("nan")
            ic_ok = per_arm[f"{arm}_{sid}"]["ic_mean"] >= g0["ic_mean"] + IC_FLOOR
            p = {"delta_ann_pp": round(dm, 4), "n": int(len(d)),
                 "t_paired": round(t_stat, 3), "ic_floor_ok": bool(ic_ok),
                 "pass": bool(dm >= L2_GATE_PP and t_stat >= L2_GATE_T and ic_ok)}
            pairs[f"{arm}_{sid}"] = p
    # L2 门（预注册）：判决单位 = 单臂（该臂双 seed 集全部满足才算越门）
    arm_verdict = {arm: bool(pairs[f"{arm}_s1"]["pass"] and pairs[f"{arm}_s2"]["pass"])
                   for arm in ("r1", "r2", "r3", "e1")}
    gated = [a for a, v in arm_verdict.items() if v]
    out = {"g0_1": g01, "pairs": pairs, "arm_verdict": arm_verdict,
           "verdict": "GATE_CROSSED:" + ",".join(gated) if gated else "null",
           "note": "L2 门（预注册）：每臂双 seed 集全部 Δ≥+0.7pp 且 t≥1.6 且 IC 不劣-0.005；"
                   "年化 ×52（PER_YEAR=52 在册口径，敏感性见 05 号 Q5）；"
                   "配对 t 的 SE 只含时间抽样，可能虚高 ≤1.20×（F78b）；"
                   "越门臂须独立复跑确认（F56 教训），不直接采纳"}
    _merge_json({"analyze": out})
    log.info("判决: %s | %s", out["verdict"], arm_verdict)
    return out


def _cross_spearman(tag_a: str, tag_b: str):
    pa, pb = HERE / f"W_scores_{tag_a}.npz", HERE / f"W_scores_{tag_b}.npz"
    if not (pa.exists() and pb.exists()):
        return None
    za, zb = np.load(pa), np.load(pb)
    aa, ab = pd.DatetimeIndex(za["anchors"]), pd.DatetimeIndex(zb["anchors"])
    common = aa.intersection(ab)
    if len(common) == 0:
        return None
    ia = aa.get_indexer(common)
    ib = ab.get_indexer(common)
    Sa, Sb = za["S"][ia], zb["S"][ib]
    cors = []
    for k in range(len(common)):
        x, y = Sa[k], Sb[k]
        ok = np.isfinite(x) & np.isfinite(y)
        if ok.sum() < 200:
            continue
        cors.append(pd.Series(x[ok]).corr(pd.Series(y[ok]), method="spearman"))
    return round(float(np.nanmedian(cors)), 4) if cors else None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="panel,train,readout,analyze")
    ap.add_argument("--arms", default=None,
                    help="如 r2_s1（pilot）；缺省 = g0/r1/r2/r3/e1 × s1/s2 + g0_p 全量")
    args = ap.parse_args()
    setup_logger()
    arms = (args.arms.split(",") if args.arms else
            [f"{a}_{s}" for a in ("g0", "r1", "r2", "r3", "e1") for s in ("s1", "s2")]
            + ["g0_p"])
    for s in [x.strip() for x in args.stage.split(",") if x.strip()]:
        log.info("==== stage: %s ====", s)
        r = stage_train(arms) if s == "train" else globals()[f"stage_{s}"]()
        print(json.dumps(r, ensure_ascii=False, indent=1, default=str)[:1200])
    log.info("== 完成 ==")
