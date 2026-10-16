"""F62 把第 11/12/13 族当**特征**喂给周频模型（2026-09-13，生产构型真 A/B）

**用户指令**："这个逻辑应该没问题，继续寻找方法" + "如果这个方法可行也要把模型层面改造适配"。
前面 F59/F60/F61 测的都是**手工筛选/倾斜**（在打分外面套规则）；**模型层适配**这条路还没试过：
把这些新因子当作**特征**交给 GBDT，让它自己去学"哪边好"（树模型天然能学负向关系，
所以不需要我们再手工翻符号）。

与 F16 的三点区别（F16 当时测的是资金流/事件族 17 个因子，**不含**这三族）：
  ① 特征换成第 11/12/13 族的 **10 个新因子**（`br_*` 4 / `rc_*` 3 / `ih_*` 3），中性化后拼接；
  ② **两臂都重训**（基础 31 vs 扩展 41，同种子同流程）⇒ 真 A/B，不复用旧存档（避免把"构型差异"
     混进"特征增益"里）；
  ③ 回测用**生产构型**（`top_n=100, rank_band=8.0, hold_bonus=0.0`，= `plan.pick_shape(1e6)`），
     而 F16 用的是研究构型 `hyst=0.2` ⇒ **本脚本读数不要与 F16 直接比大小**。

**预注册判据（与 F16 同门槛，便于跨实验比较）**：
  ① `ic_ok`：扩展臂 IC ≥ 基础臂 IC − 0.002
  ② `excess_ok`：扩展+M 的净超额 ≥ 基础+M + **0.5pp**
  ③ `mdd_ok`：扩展+M 的 MDD 不比基础+M 差 10% 以上（`mdd_ext ≥ mdd_base × 1.10`）
  三條全过才 `adopt`。⚠️ 与 F16 一样，这里的 "+M" = 与生产月频模型做均值秩融合（消除"只用周频单臂"的偏差）。

运行：``D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F62_newfamilies_as_features.py``
"""
import hashlib
import importlib.util
import json
import logging
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

from app import config as cfg_mod                                     # noqa: E402
from app import db                                                    # noqa: E402
from app.factors import factor_engine as fe                           # noqa: E402
from app.factors.flow_factors import (compute_broker_factors,         # noqa: E402
                                      compute_report_rc_factors,
                                      compute_top10_holder_factors)
from app.pit.engine import PITEngine                                  # noqa: E402
from app.weekly import scores as wsc                                  # noqa: E402

OUT = HERE / "F62_newfamilies_as_features_result.json"
CAPITAL, TOP_N, BAND, HYST = 1_000_000.0, 100, 8.0, 0.0     # hyst=0.0 = 生产构型
log = logging.getLogger("f62")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    pit = PITEngine(m=m, cfg=cfg)
    cols = list(m["close"].columns)
    mask = f1.tradable_frame(pit, wk, cols)
    log.info("周锚点 %d | 票池均值 %.0f", len(wk), mask.sum(axis=1).mean())
    panel = wsc.build_panel(m, wk, cfg)
    log.info("基础面板 %s", panel["F"].shape)
    conn = db.get_conn()
    try:
        ind = fe.industry_map(conn)
    finally:
        conn.close()
    t1 = time.time()
    new = {}
    new.update(compute_broker_factors(dates))
    new.update(compute_report_rc_factors(m, dates, source="em"))
    new.update(compute_top10_holder_factors(m, dates))
    log.info("新族因子 %d 个 (%.0fs): %s", len(new), time.time() - t1, sorted(new))
    arrs, names = [panel["F"]], list(panel["names"])
    for nm, df in new.items():
        sub = fe.neutralize(df.reindex(index=wk, columns=cols), ind)
        arrs.append(sub.to_numpy(dtype=np.float32)[:, :, None])
        names.append(nm)
    Fext = np.concatenate(arrs, axis=2)
    log.info("扩展面板 %s（新增 %d 个特征）", Fext.shape, Fext.shape[2] - panel["F"].shape[2])

    # ---- 两臂都重训（同种子、同流程）：真 A/B ----
    S_base = wsc.train(panel, cfg, seeds=wsc.SEEDS, horizon=5,
                       progress=lambda i, n, s: log.info("  基础臂 %d/%d (%.0fs)", i, n, s))
    Wbase = pd.DataFrame(S_base, index=wk, columns=cols)
    ext = dict(panel)
    ext["F"], ext["names"] = Fext, names
    S_ext = wsc.train(ext, cfg, seeds=wsc.SEEDS, horizon=5,
                      progress=lambda i, n, s: log.info("  扩展臂 %d/%d (%.0fs)", i, n, s))
    Wext = pd.DataFrame(S_ext, index=wk, columns=cols)
    M = wsc.production_score_on(wk, cols)

    lab = f1b.forward_excess_open(m, mask, 5)
    Yr = f1._rank(lab)
    okv = lab.notna().to_numpy()
    ic = {}
    for tag, Sdf in (("基础31", Wbase), ("扩展41", Wext)):
        Xr = f1._rank(Sdf)
        s = pd.Series(f1.row_ic(Xr, Yr, okv & np.isfinite(Xr)), index=wk).dropna()
        ic[tag] = {"n": int(len(s)), "ic": round(float(s.mean()), 4),
                   "t": round(f1b.hac_t(s.to_numpy(), 2), 2),
                   "DEAD": f1b.zone_stat_hac(s, "DEAD", 2),
                   "ACTIVE": f1b.zone_stat_hac(s, "ACTIVE", 2)}
        log.info("[%s] IC %+.4f (t=%.2f) | 死区 %s | 活跃区 %s", tag, ic[tag]["ic"], ic[tag]["t"],
                 ic[tag]["DEAD"], ic[tag]["ACTIVE"])

    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    mask_np = mask.to_numpy()
    arms = {"基础31": Wbase, "扩展41": Wext,
            "基础31+M": wsc._mean_rank(Wbase, M), "扩展41+M": wsc._mean_rank(Wext, M)}
    series = {}
    for tag, Sdf in arms.items():
        df = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, {**cfg, "capital": CAPITAL}, pit, 0,
                                       score_matrix=pd.DataFrame(Sdf, index=wk, columns=cols),
                                       top_n=TOP_N, rank_band=BAND, hyst=HYST)
        if df is None:
            continue
        bench, prev_exec, prev_ap = [], None, None
        for _, r in df.iterrows():
            e = pos_of[r["date"]]
            if prev_exec is None or prev_ap is None:
                bench.append(np.nan)
            else:
                good = (mask_np[int(prev_ap)] & np.isfinite(op_adj[prev_exec])
                        & np.isfinite(op_adj[e]) & (op_adj[prev_exec] > 0))
                p0, p1 = op_adj[prev_exec], op_adj[e]
                bench.append(float(np.mean(p1[good] / p0[good] - 1)) if good.sum() >= 200 else np.nan)
            prev_exec, prev_ap = e, int(r["anchor_pos"])
        df["bench_ret"] = bench
        series[tag] = df[["date", "ret", "bench_ret"]].copy()
        log.info("  [%s] 回测完成 %d 期 (%.0fs)", tag, len(df), time.time() - t0)

    common = None
    for tag, s in series.items():
        ds = set(pd.to_datetime(s.loc[s["ret"].notna() & s["bench_ret"].notna(), "date"]))
        common = ds if common is None else (common & ds)
    common = sorted(common or [])
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "shape": {"capital": CAPITAL, "top_n": TOP_N, "rank_band": BAND, "hyst": HYST},
           "n_features_base": int(panel["F"].shape[2]), "n_features_ext": int(Fext.shape[2]),
           "common_window": {"n": len(common),
                             "start": str(common[0].date()) if common else None,
                             "end": str(common[-1].date()) if common else None},
           "ic": ic, "arms": {}, "verdict": {}}
    for tag, s in series.items():
        s2 = s.copy()
        s2["date"] = pd.to_datetime(s2["date"])
        s2 = s2[s2["date"].isin(set(common))].dropna(subset=["ret", "bench_ret"])
        ann_s, ann_b = f1b.ann(s2["ret"], 52.0), f1b.ann(s2["bench_ret"], 52.0)
        nav = (1 + s2["ret"]).cumprod()
        res["arms"][tag] = {"n": int(len(s2)), "ann": round(ann_s, 4), "bench": round(ann_b, 4),
                            "excess_pp": round(100 * (ann_s - ann_b), 2),
                            "mdd": round(float((nav / nav.cummax() - 1).min()), 4)}
        d = res["arms"][tag]
        log.info("  [%s] 年化 %+.2f%% 超额 %+.2pp | MDD %.1f%%", tag, 100 * d["ann"],
                 d["excess_pp"], 100 * d["mdd"])
    b0, e0 = res["arms"]["基础31+M"], res["arms"]["扩展41+M"]
    res["verdict"] = {
        "ic_ok": bool(ic["扩展41"]["ic"] >= ic["基础31"]["ic"] - 0.002),
        "excess_gain_pp": round(e0["excess_pp"] - b0["excess_pp"], 2),
        "excess_ok": bool(e0["excess_pp"] >= b0["excess_pp"] + 0.5),
        "mdd_ok": bool(e0["mdd"] >= b0["mdd"] * 1.10)}
    res["verdict"]["adopt"] = bool(res["verdict"]["ic_ok"] and res["verdict"]["excess_ok"]
                                   and res["verdict"]["mdd_ok"])
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 判决: 采纳三族作特征 = %s (超额增益 %+.2fpp, IC_ok=%s, MDD_ok=%s) | %.0fs → %s ==",
             res["verdict"]["adopt"], res["verdict"]["excess_gain_pp"], res["verdict"]["ic_ok"],
             res["verdict"]["mdd_ok"], res["secs"], OUT.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
