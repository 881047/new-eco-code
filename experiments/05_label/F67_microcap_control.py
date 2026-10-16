"""F67 微盘对照：剔除最小 30% 市值后，生产构型的超额还剩多少？（2026-09-13 夜，预注册）

**来自理论层 23 号 §可检验假设 2**（原话："剔除最小 30% 市值后重跑 F41/F45"）。
为什么值得做：本项目毛超额有相当部分来自小盘暴露（F24：一半以上是风格，size 主导），
微盘股在 A 股又受**壳价值/流动性/退市新规**影响 —— 若超额主要来自微盘段，那么：
① 容量极小（自有资金无所谓，但**容量一放大就消失**）；② 未来小盘β逆转时超额会跟着没。

**预注册判据（跑数前冻结）**——三条同时满足才算"**超额不依赖微盘**"：
  ① **全史超额保留率 ≥ 70%**（相对同构型但**不剔微盘**的基线，基线取 F52 A0 逐期序列）
  ② **留出集（2024-01~2026-08）超额 > 0**
  ③ **|MDD| 不劣化超 2pp**
  否则 ⇒ 如实登记为"**部分依赖微盘**"，并写进 `02_口径与当前读数.md` 的"必须与超额一起读的限定"。
  ⚠️ 本实验**只做解释**，不据此调参（红线见 14 号台账 §三补七）。

**口径**：生产构型（100 万 / top100 / 8× 出带 / `hyst=0.0` / 整手 / 逐笔费用 / 涨跌停闸门），
两套种子（F4_scores_seedA/B）；剔除口径 = **每个锚点内**、在可交易域上按 `total_mv` 分位剔除最小 30%
（逐期重算，不用全样本分位 —— 后者会引入未来信息）。

运行：``D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F67_microcap_control.py``（两套种子各一次模拟，约 5~10 分钟）
"""
import importlib.util
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BASE_DIR = HERE.parent.parent
sys.path.insert(0, str(BASE_DIR))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


f1 = _load("f1", HERE / "F1_weekly_ic_decay.py")
f1b = _load("f1b", HERE / "F1b_weekly_open_overlap.py")

from app import config as cfg_mod                                     # noqa: E402
from app.factors import factor_engine as fe                           # noqa: E402
from app.pit.engine import PITEngine                                  # noqa: E402

OUT = HERE / "F67_microcap_control_result.json"
CAPITAL, TOP_N, BAND, HYST = 1_000_000.0, 100, 8.0, 0.0
CUT = 0.30
SEEDS = {"seedA": "F4_scores_seedA.npz", "seedB": "F4_scores_seedB.npz"}
BASE_SERIES = {"seedA": "F52_series_seedA_A0.json", "seedB": "F52_series_seedB_A0.json"}
FULL = ("2010-01-01", "2030-12-31")
ACTIVE = ("2024-01-01", "2026-08-31")
MIN_KEEP = 200          # 剔除后至少留这么多只，否则该锚点放弃剔除(避免把票池削到没得选)
log = logging.getLogger("f67")


def _excess(series: list, lo: str, hi: str) -> dict:
    # 跳过 null 行（F52 序列首期没有收益；不跳过会在 float(None) 上炸）
    rows = [r for r in series
            if r.get("ret") is not None and r.get("bench_ret") is not None
            and lo <= pd.Timestamp(r["date"], unit="ms").strftime("%Y-%m-%d") <= hi]
    if not rows:
        return {"n": 0}
    ex = [float(r["ret"]) - float(r["bench_ret"]) for r in rows]
    nav, peak = 1.0, 1.0
    dds = []
    for r in rows:
        nav *= (1 + float(r["ret"]))
        peak = max(peak, nav)
        dds.append(nav / peak - 1)
    return {"n": len(rows), "excess_pp": round(100 * ((1 + np.mean(ex)) ** 52 - 1), 2),
            "mdd": round(float(min(dds)), 4),
            "turnover": round(float(np.mean([r.get("turnover") or 0 for r in rows])), 4)}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    cols = list(m["close"].columns)
    pit = PITEngine(m=m, cfg=cfg)
    mask0 = f1.tradable_frame(pit, wk, cols)
    mv = m["total_mv"].reindex(index=wk, columns=cols).astype(float)
    m0 = mask0.fillna(False).to_numpy()
    mv_np = mv.to_numpy()
    keep = np.zeros_like(m0)
    dropped = []
    for k in range(len(wk)):
        ok = m0[k] & np.isfinite(mv_np[k])
        if ok.sum() < MIN_KEEP:
            keep[k] = m0[k]
            dropped.append(0)
            continue
        thr = np.quantile(mv_np[k][ok], CUT)
        sel = ok & (mv_np[k] >= thr)
        if sel.sum() < MIN_KEEP:               # 退化保护：宁可少剔也别把池子削空
            sel = ok
        keep[k] = sel
        dropped.append(int((m0[k] & ~sel).sum()))
    mask1 = pd.DataFrame(keep, index=wk, columns=cols)
    log.info("微盘剔除就绪：平均剔 %.0f 只/期（可交易池 %.0f）| %.0fs",
             float(np.mean(dropped)), float(m0.sum(axis=1).mean()), time.time() - t0)

    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    mask_np = mask1.fillna(False).to_numpy()
    res = {"protocol": {"cut": CUT, "shape": {"capital": CAPITAL, "top_n": TOP_N,
                                              "rank_band": BAND, "hyst": HYST},
                        "seeds": list(SEEDS), "baseline": "F52 A0（同构型不剔微盘）",
                        "criteria": "① 全史超额保留率≥70% ② 留出集超额>0 ③ |MDD| 不劣化超2pp",
                        "mean_dropped_per_period": round(float(np.mean(dropped)), 0)},
           "arms": {}, "verdict": {}}
    for seed, fn in SEEDS.items():
        z = np.load(HERE / fn, allow_pickle=True)
        a = pd.to_datetime([str(x) for x in z["anchors"]])
        c = [str(x) for x in z["cols"]]
        W = pd.DataFrame(z["scores"], index=a, columns=c).reindex(index=wk, columns=cols)
        df = f1b.run_overlap_portfolio(m, {}, wk, mask1, 5, {**cfg, "capital": CAPITAL}, pit, 0,
                                       score_matrix=W, top_n=TOP_N, rank_band=BAND, hyst=HYST)
        if df is None:
            log.error("%s 回测失败", seed)
            return 2
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
        series = df[["date", "ret", "bench_ret", "turnover"]].dropna().to_dict("records")
        base = json.loads((HERE / BASE_SERIES[seed]).read_text(encoding="utf-8"))
        cur = {tag: _excess(series, *w) for tag, w in (("FULL", FULL), ("ACTIVE", ACTIVE))}
        ref = {tag: _excess(base, *w) for tag, w in (("FULL", FULL), ("ACTIVE", ACTIVE))}
        keep_ratio = (cur["FULL"]["excess_pp"] / ref["FULL"]["excess_pp"]
                      if ref["FULL"]["excess_pp"] else None)
        v = {"keep_ratio": round(float(keep_ratio), 3) if keep_ratio is not None else None,
             "keep_ok": bool(keep_ratio is not None and keep_ratio >= 0.70),
             "active_ok": bool(cur["ACTIVE"]["excess_pp"] > 0),
             "mdd_ok": bool(abs(cur["FULL"]["mdd"]) <= abs(ref["FULL"]["mdd"]) + 0.02),
             "excess_full_pp": cur["FULL"]["excess_pp"], "excess_full_base_pp": ref["FULL"]["excess_pp"],
             "excess_active_pp": cur["ACTIVE"]["excess_pp"], "excess_active_base_pp": ref["ACTIVE"]["excess_pp"],
             "mdd": cur["FULL"]["mdd"], "mdd_base": ref["FULL"]["mdd"],
             "turnover": cur["FULL"]["turnover"], "turnover_base": ref["FULL"]["turnover"]}
        v["pass"] = bool(v["keep_ok"] and v["active_ok"] and v["mdd_ok"])
        res["arms"][seed] = {"current": cur, "baseline": ref}
        res["verdict"][seed] = v
        log.info("[%s] 剔微盘后全史 %+.2fpp（基线 %+.2f，保留 %.0f%%）| 留出集 %+.2fpp（基线 %+.2f）"
                 "| MDD %.1f%%（基线 %.1f%%）",
                 seed, v["excess_full_pp"], v["excess_full_base_pp"], 100 * (keep_ratio or 0),
                 v["excess_active_pp"], v["excess_active_base_pp"], 100 * v["mdd"], 100 * v["mdd_base"])
    res["verdict"]["strong"] = bool(res["verdict"]["seedA"]["pass"] and res["verdict"]["seedB"]["pass"])
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n== 微盘对照（剔除最小 30% 市值，生产构型，两套种子）==")
    for seed, v in res["verdict"].items():
        if seed == "strong":
            continue
        print(f"  {seed}: 全史 {v['excess_full_base_pp']:+.2f} → {v['excess_full_pp']:+.2f}pp"
              f"（保留 {100*(v['keep_ratio'] or 0):.0f}%）| 留出集 {v['excess_active_base_pp']:+.2f}"
              f" → {v['excess_active_pp']:+.2f}pp | MDD {100*v['mdd_base']:.1f}% → {100*v['mdd']:.1f}%"
              f" ⇒ {'不依赖微盘' if v['pass'] else '部分依赖微盘'}")
    print(f"  两套种子同时通过? {res['verdict']['strong']}")
    print(f"结果 → {OUT.name} | {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
