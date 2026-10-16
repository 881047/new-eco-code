"""F68 四因子风格归因：超额里还剩多少 alpha？（2026-09-14 早，理论层 23 号 §可检验假设 1）

**问题**：F24 早就算出"毛超额一半以上是风格（size 主导）"，F67 又把量级钉死（剔最小 30% 市值后
超额只剩 18%、留出集转负）。本实验做**标准做法**：把袖套逐期超额对四个风格多空价差回归 ——
`excess_t = α + β_size·SMB_t + β_value·HML_t + β_mom·MOM_t + β_lowvol·LOWVOL_t + ε_t`
⇒ **α（截距）才是"扣掉风格后剩下的本事"**，并看它在**留出集是否转负**。

**四因子怎么造**（全部用**本项目自己的面板**，不引外部因子库；逐期在**可交易域内**排名，避免前视）：
  SMB = 小市值组 − 大市值组（`size`，五分位 Q1−Q5，size 越小越好 ⇒ 小减大）
  HML = 高 BP 组 − 低 BP 组（`bp`，Q5−Q1）
  MOM = 强动量组 − 弱动量组（`mom_120`，Q5−Q1）
  LOWVOL = 低波动组 − 高波动组（`vol_60`，Q1−Q5）
  每组等权、按**锚点后首个开盘价 → 第 6 个交易日开盘价**计算（与生产成交口径一致，`h=5`）；
  风格值取 `factor` 表里**不晚于该锚点**的最近月末值（基本面类本就季频，ffill 是标准做法）。

**判据（预注册）**：① 全史 α 的 HAC(2) t ≥ 2（正） ② 留出集 α > 0。两条都过 ⇒ "超额不是纯风格"；
否则 ⇒ 登记为"**风格可解释**"，按 14 号 §三补七 的红线**不得调参把它调回来**。
⚠️ 只做解释，不改生产。

运行：``D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F68_style_attribution.py``
"""
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

from app import config as cfg_mod                                     # noqa: E402
from app import db                                                    # noqa: E402
from app.factors import factor_engine as fe                           # noqa: E402
from app.pit.engine import PITEngine                                  # noqa: E402

OUT = HERE / "F68_style_attribution_result.json"
SPECS = {"SMB": ("size", False), "HML": ("bp", True), "MOM": ("mom_120", True),
         "LOWVOL": ("vol_60", False)}          # (因子名, 值越大越好?)
Q = 0.20
SERIES = {"seedA": HERE / "F52_series_seedA_A0.json", "seedB": HERE / "F52_series_seedB_A0.json"}
FULL = ("2010-01-01", "2030-12-31")
ACTIVE = ("2024-01-01", "2026-08-31")
log = logging.getLogger("f68")


def _ols(y: np.ndarray, Xs: list) -> dict:
    X = np.column_stack([np.ones(len(y))] + Xs)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    e = y - X @ beta
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - float((e ** 2).sum()) / ss_tot if ss_tot > 0 else float("nan")
    # α 的 HAC(2) t：用残差自协方差（对多元情形近似，仅作读数）
    XtX_inv = np.linalg.inv(X.T @ X)
    S = np.zeros((X.shape[1], X.shape[1]))
    for lag in range(3):
        w = 1.0 - lag / 3.0
        for t in range(lag, len(e)):
            u = np.outer(X[t] * e[t], X[t - lag] * e[t - lag])
            S += w * (u + (u.T if lag > 0 else 0))
    cov = XtX_inv @ S @ XtX_inv
    se0 = float(np.sqrt(max(cov[0, 0], 1e-18)))
    return {"alpha_pp": round(100 * float(beta[0]), 3),
            "alpha_t": round(float(beta[0]) / se0, 2),
            "betas": {k: round(float(b), 3) for k, b in zip(SPECS, beta[1:])},
            "r2": round(float(r2), 3), "n": int(len(y))}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    wk = pd.DatetimeIndex(sorted({pd.Timestamp(x) for x in
                                  pd.to_datetime([r["date"] for r in
                                                  json.loads(SERIES["seedA"].read_text(encoding="utf-8"))],
                                                 unit="ms")}))
    cols = list(m["close"].columns)
    pit = PITEngine(m=m, cfg=cfg)
    mask = pit.tradable_matrix(wk, columns=cols).fillna(False)
    op = (m["open"] * m["adj"]).reindex(index=dates, columns=cols)
    pos_of = {d: i for i, d in enumerate(dates)}
    conn = db.get_conn()
    try:
        facs = {}
        for fname in {s[0] for s in SPECS.values()}:
            q = ("SELECT ts_code, trade_date, value FROM factor WHERE factor=? "
                 "AND trade_date <= ?")
            df = pd.read_sql(q, conn, params=(fname, wk[-1].strftime("%Y%m%d")))
            piv = df.pivot_table(index="trade_date", columns="ts_code", values="value")
            piv.index = pd.to_datetime(piv.index)
            # 对齐到**全票池列**：factor 表里某因子缺行的票不会出现在 pivot 里（实测 5812 vs 5819），
            # 不对齐会在 `ok0 & isfinite(v)` 上广播失败
            facs[fname] = (piv.reindex(index=piv.index.union(wk)).ffill()
                           .reindex(index=wk, columns=cols))
    finally:
        conn.close()
    log.info("因子面板就绪（%s）| %.0fs", ",".join(sorted(facs)), time.time() - t0)

    fwd = op.shift(-6) / op.shift(-1) - 1.0        # 锚点后首个开盘 → 第 6 个交易日开盘（h=5）
    spreads = {k: [] for k in SPECS}
    for d in wk:
        i = pos_of[d]
        ok0 = mask.loc[d].to_numpy() if d in mask.index else None
        if ok0 is None:
            for k in spreads:
                spreads[k].append(np.nan)
            continue
        fut = fwd.iloc[i].to_numpy(dtype=float)
        for k, (fname, higher_better) in SPECS.items():
            v = facs[fname].loc[d].to_numpy(dtype=float) if d in facs[fname].index else None
            if v is None:
                spreads[k].append(np.nan)
                continue
            ok = ok0 & np.isfinite(v) & np.isfinite(fut)
            if ok.sum() < 100:
                spreads[k].append(np.nan)
                continue
            hi = v[ok] >= np.quantile(v[ok], 1 - Q)
            lo = v[ok] <= np.quantile(v[ok], Q)
            f_ok, v_ok = fut[ok], v[ok]
            good = hi if higher_better else lo
            bad = lo if higher_better else hi
            spreads[k].append(float(f_ok[good].mean() - f_ok[bad].mean()))
    sp = pd.DataFrame(spreads, index=wk)
    res = {"protocol": {"specs": {k: {"factor": v[0], "higher_is_better": v[1]} for k, v in SPECS.items()},
                        "quintile": Q, "h": 5, "windows": {"full": FULL, "active": ACTIVE},
                        "criteria": "① 全史 α_t≥2（正） ② 留出集 α>0"},
           "factor_stats": {k: {"mean_pp": round(100 * float(sp[k].mean()), 3),
                                "t": round(float(sp[k].mean() / (sp[k].std(ddof=1) /
                                                                np.sqrt(sp[k].notna().sum()))), 2)}
                            for k in SPECS}, "seeds": {}}
    for seed, path in SERIES.items():
        rows = [r for r in json.loads(path.read_text(encoding="utf-8"))
                if r.get("ret") is not None and r.get("bench_ret") is not None]
        s = pd.DataFrame({"date": pd.to_datetime([r["date"] for r in rows], unit="ms"),
                          "ex": [float(r["ret"]) - float(r["bench_ret"]) for r in rows]}).set_index("date")
        j = s.join(sp, how="inner").dropna()
        entry = {}
        for tag, (lo, hi) in (("FULL", FULL), ("ACTIVE", ACTIVE)):
            w = j[(j.index >= lo) & (j.index <= hi)]
            if len(w) < 20:
                entry[tag] = {"n": int(len(w))}
                continue
            entry[tag] = _ols(w["ex"].to_numpy(), [w[k].to_numpy() for k in SPECS])
        entry["alpha_ok"] = bool(entry["FULL"].get("alpha_t", 0) >= 2
                                 and entry["FULL"].get("alpha_pp", -1) > 0)
        entry["active_ok"] = bool(entry["ACTIVE"].get("alpha_pp", -1) > 0)
        entry["pass"] = bool(entry["alpha_ok"] and entry["active_ok"])
        res["seeds"][seed] = entry
        log.info("[%s] 全史 α %+.3fpp/期 (t=%.2f) β=%s | 留出集 α %+.3fpp (t=%.2f)",
                 seed, entry["FULL"]["alpha_pp"], entry["FULL"]["alpha_t"],
                 entry["FULL"]["betas"], entry["ACTIVE"].get("alpha_pp", float("nan")),
                 entry["ACTIVE"].get("alpha_t", float("nan")))
    res["verdict"] = {"both_seeds_pass": bool(all(res["seeds"][s]["pass"] for s in SERIES))}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n== 四因子风格归因（袖套逐期超额 ~ SMB + HML + MOM + LOWVOL）==")
    print("  风格价差均值（pp/期）:", {k: v["mean_pp"] for k, v in res["factor_stats"].items()})
    for seed, e in res["seeds"].items():
        f, a = e["FULL"], e.get("ACTIVE", {})
        print(f"  {seed}: 全史 α {f['alpha_pp']:+.3f}pp/期 (t={f['alpha_t']}, R²={f['r2']})"
              f" β={f['betas']}")
        print(f"          留出集 α {a.get('alpha_pp', float('nan')):+.3f}pp/期"
              f" (t={a.get('alpha_t')}) ⇒ {'扣掉风格仍有 α' if e['pass'] else '风格可解释'}")
    print(f"  两套种子同时通过? {res['verdict']['both_seeds_pass']}")
    print(f"结果 → {OUT.name} | {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
