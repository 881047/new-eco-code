"""F74 · 镜厅：孪生 ρ 单向模拟（决定"造不造孪生机器"）

**预注册**：`plans/mirror-twin-rho-prereg.md`（判据先冻结：ρ 网格、换手带 ±0.20、三档判决）。
**性质**：纯研究、**零生产改动** —— 只读行情缓存 + 只读分数存档；产物只落 `experiments/mirror/`。

## 一句话

F74 已证"池等权参照"比"中证800 参照"紧 2.11×。本脚本回答**下一个问题**：
**同池随机孪生**（随机游走选股）与袖套的相关 ρ 有多高？—— 配平后 `σ_d = σ(S−T)` 若显著小于
`σ(d_800)=2.889pp`，孪生机器才值得造（判据见预注册 §三）。

## 实现要点（与预注册逐条对应）

- 池 = 当期分数存档 `cols` ∩ **当期锚点掩码**；选股 = `mask & 分数 top100`，保留带 = 现持有者 rank ≤ 800。
- 随机数：`ε = Φ⁻¹(uniform(blake2b(f"{seed}|{ts_code}|{date8}") 前 8 字节))`，**禁内置 hash()**；float64。
- 状态：`s_t = ρ·s_{t−1} + √(1−ρ²)·ε_t`；**首见抽样、掉出池冻结**（重回池沿用旧 s）。
- 收益：**执行日复权开盘 → 下一执行日复权开盘**、等权、无费用（与 F74 的 `POOL` 同一把尺）。
- 两阶段：先用**前 104 期**校准 ρ（换手带 ±0.20，基准中位 0.1200），再用它跑**主测**（暖机 52 期后的 439 期）。

用法：
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\mirror_twin_rho.py [--recompute]
"""
import argparse
import hashlib
import json
import logging
import sqlite3
import statistics as st
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from app.factors import factor_engine as fe        # noqa: E402
from app.pit.engine import PITEngine               # noqa: E402
from app import config as cfg_mod                  # noqa: E402

log = logging.getLogger("mirror_twin_rho")

LABEL = "experiments/05_label"
OUT_DIR = BASE / "experiments" / "mirror" / "M0_twin_rho"
PREREG = BASE / "plans" / "mirror-twin-rho-prereg.md"
SEEDS = ("seedA", "seedB")
SEED_FILE = {"seedA": "F4_scores_seedA.npz", "seedB": "F4_scores_seedB.npz"}
SEED_SERIES = {"seedA": "F66_series_seedA.json", "seedB": "F66_series_seedB.json"}

RHO_GRID = (0.5, 0.7, 0.8, 0.9, 0.95, 0.98)
TOPN = 100
BAND = 8.0                     # 保留带 8×
WARMUP = 52                    # 暖机期数（不参与测量）
CALIB_N = 104                  # 校准窗（含暖机）
TURNOVER_BAND = 0.20           # |中位(换手T/换手S) − 1| ≤ 0.20
SD_D800_PP = 2.889             # F74 修正后的 σ(袖套 − 中证800)，pp/期（同尺分母）


def _sha16(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def _norm_draw(seed: str, code: str, date8: str) -> float:
    """确定性标准正态抽样（禁内置 hash：跨进程不稳定）"""
    h = hashlib.blake2b(f"{seed}|{code}|{date8}".encode(), digest_size=8).digest()
    u = int.from_bytes(h, "big") / float(2 ** 64)
    u = min(max(u, 1e-12), 1 - 1e-12)
    from statistics import NormalDist
    return float(NormalDist().inv_cdf(u))


def _load_by_path(name: str, path: Path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def build_pool_and_returns(m, dates, wk, cols, mask, exec_dates):
    """与 F74 同口径：池等权（开→开、复权、无费用）+ 指数开→开（000906）"""
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64)
    pos_of = pd.Series(np.arange(len(dates)), index=dates)
    pool = []
    prev_e = None
    for t in exec_dates:
        e = int(pos_of.asof(t)) if t in pos_of.index else int(np.searchsorted(dates.values, np.datetime64(t), side="right") - 1)
        if prev_e is None:
            pool.append(np.nan)
        else:
            good = np.isfinite(op_adj.iloc[prev_e].values) & np.isfinite(op_adj.iloc[e].values)
            good &= (op_adj.iloc[prev_e].values > 0)
            pool.append(float(np.mean(op_adj.iloc[e].values[good] / op_adj.iloc[prev_e].values[good] - 1))
                        if good.sum() >= 200 else np.nan)
        prev_e = e
    conn = sqlite3.connect(f"file:{BASE / 'data' / 'neweco.db'}?mode=ro", uri=True)
    ipx = pd.read_sql("SELECT trade_date, open FROM index_daily WHERE ts_code='000906.SH' ORDER BY trade_date", conn)
    conn.close()
    ix = pd.Series(ipx["open"].values, index=pd.to_datetime(ipx["trade_date"])).astype(float)
    ix = ix.reindex(pd.DatetimeIndex(exec_dates)).ffill()
    b800 = (ix / ix.shift(1) - 1.0).values
    return np.array(pool), b800, op_adj, pos_of


def run_twin(seed: str, rho: float, exec_dates, wk, mask, op_adj, pos_of, cols) -> dict:
    """单向推进孪生：返回逐期收益、换手、以及持有状态轨迹（用于验证）"""
    state = {}                       # code -> 分数
    avail_prev = np.zeros(len(cols), dtype=bool)
    held = set()
    rets, tos, n_pool_hist = [], [], []
    prev_e = None
    wk_arr = wk.values
    col_index = {c: i for i, c in enumerate(cols)}
    for k, t in enumerate(exec_dates):
        e = int(pos_of.asof(t)) if t in pos_of.index else int(np.searchsorted(pd.DatetimeIndex(exec_dates).values, np.datetime64(t), side="right") - 1)
        ap = int(np.searchsorted(wk_arr, np.datetime64(t), side="right") - 1)
        pool_codes = np.flatnonzero(mask[ap])
        avail = np.zeros(len(cols), dtype=bool)
        avail[pool_codes] = True
        # ε 与状态演化（首见抽样、掉出池冻结）
        eps = np.zeros(len(cols))
        d8 = pd.Timestamp(t).strftime("%Y%m%d")
        for i in pool_codes:
            eps[i] = _norm_draw(seed, cols[i], d8)
        for i in pool_codes:
            s_prev = state.get(cols[i])
            state[cols[i]] = eps[i] if (s_prev is None or not avail_prev[i]) else rho * s_prev + np.sqrt(1 - rho ** 2) * eps[i]
        # 选股：分数 top100（池内）；保留带：现持有者 rank ≤ band*topn
        s_vec = np.array([state.get(c, -np.inf) for c in cols], dtype=float)
        s_vec[~avail] = -np.inf
        order = np.argsort(-s_vec, kind="stable")          # 稳定排序 ⇒ 同分按列序（确定性）
        rank = np.empty(len(cols), dtype=int)
        rank[order] = np.arange(len(cols))
        keep = [c for c in held if avail[col_index[c]] and rank[col_index[c]] < BAND * TOPN]
        picks = list(keep)
        for i in order:
            if len(picks) >= TOPN:
                break
            c = cols[i]
            if avail[i] and c not in picks:
                picks.append(c)
        new_held = set(picks)
        if prev_e is None:
            rets.append(np.nan)
        else:
            idx = [col_index[c] for c in new_held]
            p0 = op_adj.iloc[prev_e].values[idx]
            p1 = op_adj.iloc[e].values[idx]
            ok = np.isfinite(p0) & np.isfinite(p1) & (p0 > 0)
            rets.append(float(np.mean(p1[ok] / p0[ok] - 1)) if ok.sum() else np.nan)
        tos.append(1 - len(held & new_held) / max(1, len(new_held)) if prev_e is not None else np.nan)
        held = new_held
        avail_prev = avail
        prev_e = e
        n_pool_hist.append(int(avail.sum()))
    return {"ret": np.array(rets), "turnover": np.array(tos), "n_pool": n_pool_hist,
            "n_held": len(held)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recompute", action="store_true", help="只从落盘 npz 复算，不重跑模拟")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if a.recompute:
        res = json.loads((OUT_DIR / "result.json").read_text(encoding="utf-8"))
        g = res["main"]["G_twin"]
        verdict = ("可谈 M1" if (res["main"]["rho_TS"] >= 0.83 and g >= 3)
                   else ("待裁定" if (res["main"]["rho_TS"] >= 0.50 or g >= 2) else "孪生线关闭"))
        print(f"[recompute] ρ={res['main']['rho_TS']:.4f}  G_twin={g:.3f}  ⇒ {verdict}")
        return 0

    cfg = cfg_mod.load_config()
    m = fe.load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    f1 = _load_by_path("f1", BASE / LABEL / "F1_weekly_ic_decay.py")
    wk = f1.week_end_dates(dates)
    cols = list(m["close"].columns)
    pit = PITEngine(m=m, cfg=cfg)
    mask_df = f1.tradable_frame(pit, wk, cols)
    mask = mask_df.fillna(False).to_numpy()
    log.info("面板就绪 | 锚点 %d | 票池 %d | %.0fs", len(wk), len(cols), time.time() - t0)

    out = {"script_sha16": _sha16(Path(__file__)),
           "prereg_sha16": _sha16(PREREG) if PREREG.exists() else None,
           "config": {"top_n": TOPN, "band": BAND, "warmup": WARMUP, "calib_n": CALIB_N,
                      "grid": list(RHO_GRID), "turnover_band": TURNOVER_BAND, "sd_d800_pp": SD_D800_PP},
           "seeds": {}}
    for seed in SEEDS:
        ser = json.loads((BASE / LABEL / SEED_SERIES[seed]).read_text(encoding="utf-8"))
        s_ret = pd.Series([x["ret"] for x in ser], index=pd.to_datetime([x["date"] for x in ser], unit="ms"))
        s_turn = pd.Series([x["turnover"] for x in ser], index=s_ret.index)
        exec_dates = pd.DatetimeIndex(s_ret.index)
        pool, b800, op_adj, pos_of = build_pool_and_returns(m, dates, wk, cols, mask, exec_dates)
        # 校准：前 CALIB_N 期
        calib = exec_dates[:CALIB_N]
        rows = []
        for rho in RHO_GRID:
            tw = run_twin(seed, rho, calib, wk, mask, op_adj, pos_of, cols)
            to_t = np.nanmedian(tw["turnover"][WARMUP:])
            to_s = np.nanmedian(s_turn.values[WARMUP:CALIB_N])
            rows.append((rho, float(to_t), float(to_t / to_s - 1) if to_s else float("nan")))
            log.info("  校准 %s ρ=%.2f 换手T=%.4f 换手S=%.4f 偏差=%+.3f", seed, rho, to_t, to_s, rows[-1][2])
        ok = [r for r in rows if abs(r[2]) <= TURNOVER_BAND]
        if not ok:
            out["seeds"][seed] = {"calibration": rows, "status": "DESIGN_FAILURE",
                                  "note": "ρ 网格内无解（换手带 ±0.20）⇒ 按预注册停，不许放宽"}
            (OUT_DIR / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
            log.error("设计失败：网格内无解 —— 已如实落盘")
            return 2
        rho_star = min(ok, key=lambda r: abs(r[2]))[0]
        # 主测：全窗（含暖机）
        tw = run_twin(seed, rho_star, exec_dates, wk, mask, op_adj, pos_of, cols)
        meas = slice(WARMUP, len(exec_dates))
        S_meas = s_ret.values[meas]
        T_meas = tw["ret"][meas]
        m_ok = ~(np.isnan(S_meas) | np.isnan(T_meas))
        rho_ts = float(np.corrcoef(S_meas[m_ok], T_meas[m_ok])[0, 1])
        d = S_meas[m_ok] - T_meas[m_ok]
        sd_d = float(np.std(d, ddof=1))
        g_twin = (SD_D800_PP / (100 * sd_d)) ** 2
        out["seeds"][seed] = {
            "calibration": rows, "rho_star": rho_star,
            "turnover_T_median": float(np.nanmedian(tw["turnover"][meas])),
            "turnover_S_median": float(np.nanmedian(s_turn.values[meas])),
            "rho_TS": rho_ts, "n_measure": int(m_ok.sum()),
            "sd_T_pp": float(100 * np.std(T_meas[m_ok], ddof=1)),
            "sd_S_pp": float(100 * np.std(S_meas[m_ok], ddof=1)),
            "sd_d_pp": float(100 * sd_d), "G_twin": float(g_twin),
            "n_pool_median": int(np.median(tw["n_pool"])),
        }
        np.savez_compressed(OUT_DIR / f"series_{seed}.npz",
                            date=exec_dates.values.astype("datetime64[ns]"),
                            S=s_ret.values, T=tw["ret"], turnover_T=tw["turnover"],
                            turnover_S=s_turn.values, pool=pool, bench800=b800,
                            n_pool=np.array(tw["n_pool"]))
        log.info("  %s: ρ*=%.2f  ρ_TS=%.4f  σ_d=%.3fpp  G_twin=%.3f", seed, rho_star, rho_ts, 100 * sd_d, g_twin)

    rhos = [out["seeds"][s]["rho_TS"] for s in SEEDS if "rho_TS" in out["seeds"][s]]
    gs = [out["seeds"][s]["G_twin"] for s in SEEDS if "G_twin" in out["seeds"][s]]
    out["main"] = {"rho_TS": float(np.mean(rhos)), "G_twin": float(np.mean(gs))}
    # ⚠️ 支配性自检（2026-09-15 补，不属预注册判据但**必须并列报告**）：
    # 预注册的判据只问"孪生是否打过**中证800**"；但 F74 已经证明**池等权线**更紧。
    # 若孪生的 σ_d 反而 > 池线的 σ（实测：1.53 vs 1.37），那么**孪生被免费参照物支配** ——
    # 此时"可谈 M1"只是字面通过，工程上仍不该造机器。
    pool_sd_pp = 1.367   # F74 实测（seedA 1.367 / seedB 1.373），同尺：执行日开→开
    sd_d_mean = float(np.mean([out["seeds"][s]["sd_d_pp"] for s in SEEDS]))
    out["dominance_check"] = {
        "sd_d_twin_pp": sd_d_mean, "sd_d_pool_pp": pool_sd_pp,
        "twin_dominated_by_pool": bool(sd_d_mean > pool_sd_pp),
        "note": "孪生被免费参照物支配 ⇒ 即便字面通过预注册判据，也不应造机器（机器更贵且更松）",
    }
    out["verdict"] = {"rule": "ρ≥0.83 且 G_twin≥3 ⇒ 可谈 M1 / 0.5≤ρ<0.83 或 G∈[2,3) ⇒ 待裁定 / ρ<0.5 或 G<2 ⇒ 孪生线关闭",
                      "value": ("可谈 M1" if (out["main"]["rho_TS"] >= 0.83 and out["main"]["G_twin"] >= 3)
                                else ("待裁定" if (out["main"]["rho_TS"] >= 0.50 or out["main"]["G_twin"] >= 2)
                                      else "孪生线关闭"))}
    out["verdict"]["final"] = ("不造（被池线支配）"
                               if out["dominance_check"]["twin_dominated_by_pool"]
                               else out["verdict"]["value"])
    (OUT_DIR / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"main": out["main"], "verdict": out["verdict"],
                      "seeds": {s: {k: v for k, v in out["seeds"][s].items() if k != "calibration"}
                                for s in SEEDS}}, ensure_ascii=False, indent=1))
    log.info("产物 → %s（%.0fs）", OUT_DIR, time.time() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
