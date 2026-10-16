"""F58 「负向因子能不能反着用」—— 三族 9 因子的**机制量化**（2026-09-13，诊断，不判决）

用户提问：既然有些因子是**负向**的（值越大未来越差），能不能反过来用它们的负面信息
**筛掉差股票**、从而产生正向作用？

这不是新问题 —— F57 已经对**最强**的那个负向因子（券商金股 `br_cnt`，死区 −1.64%/月、t=−4.66）
试过"反向剔除器"：四条冻结判据全过（全史 +0.31pp、活跃区 +0.52pp、MDD 与换手不变），
**但机制读数判它不可行动** —— 模型 top100 里平均只有 **0.1 只/期**落在"券商头部"。

本脚本把这件事**系统化**：对第 11/12/13 族共 9 个因子，逐期测三件事 ——
  ① 坏侧（前 10%）与好侧（后 10%）相对全池均值的**头部超额**（死区 / 活跃区分开算）；
  ② 模型 top100 与**坏侧**的重叠（=剔除器能作用的对象数）；
  ③ 模型 top100 与**好侧**的重叠（=因子与模型一致的比例）。

**为什么这决定"能不能反着用"**：剔除器只在"模型本来就买、而因子说它坏"的时候才起作用；
若重叠≈0，那么再强的负向信号也**无从下手**（这正是 F57 的处境）。

只读（生产打分 + PIT 掩码 + 5 日 forward_excess + 三族因子面板），不写任何生产产物。
运行：``D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F58_negative_factor_overlap.py``
"""
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from app import config as cfg_mod                                    # noqa: E402
from app.factors import factor_engine as fe                          # noqa: E402
from app.factors.flow_factors import (compute_broker_factors,        # noqa: E402
                                      compute_report_rc_factors,
                                      compute_top10_holder_factors)
from app.pit.engine import PITEngine                                 # noqa: E402
from app.weekly import scores as sc                                  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "F58_negative_factor_overlap_result.json"
NPZ = BASE_DIR / "data" / "weekly" / "scores_20260911.npz"
TOP_N = 100
DECILE = 0.10
WINDOWS = {"DEAD": ("2020-09-01", "2023-12-31"),
           "ACTIVE": ("2024-01-01", "2026-08-31"),
           "ALL": ("2010-01-01", "2030-12-31")}
log = logging.getLogger("f58")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    with np.load(NPZ, allow_pickle=True) as z:
        S = np.asarray(z["scores"], dtype=float)
        anchors = pd.to_datetime([str(d) for d in z["anchors"]])
        cols = [str(c) for c in z["cols"]]
    m = fe.load_market(since=sc.SINCE)
    pit = PITEngine(m=m, cfg=cfg)
    mask = pit.tradable_matrix(anchors, columns=cols).fillna(False)
    Y = sc.forward_excess(m, mask, sc.HORIZON)
    daily = pd.to_datetime(m["close"].index)
    panels = {}
    panels.update(compute_broker_factors(daily))
    panels.update(compute_report_rc_factors(m, daily, source="em"))
    panels.update(compute_top10_holder_factors(m, daily))
    log.info("面板就绪: %d 个因子 | %.0fs", len(panels), time.time() - t0)

    rows = []
    for name, panel in sorted(panels.items()):
        P = panel.reindex(index=anchors, columns=cols).to_numpy(dtype=float)
        for k, d in enumerate(anchors):
            y, s = Y[k], P[k]
            ok = mask.iloc[k].to_numpy() & np.isfinite(s) & np.isfinite(y)
            if ok.sum() < 200:
                continue
            sc_k = S[k]
            tradable = mask.iloc[k].to_numpy() & np.isfinite(sc_k)
            if tradable.sum() < TOP_N:
                continue
            order = np.argsort(-np.where(tradable, sc_k, -np.inf))[:TOP_N]
            vals = s[ok]
            hi = np.quantile(vals, 1 - DECILE)
            lo = np.quantile(vals, DECILE)
            bad = ok & (s >= hi)
            good = ok & (s <= lo)
            base = float(y[ok].mean())
            rows.append({
                "date": d, "factor": name,
                "bad_head": float(y[bad].mean() - base) if bad.any() else np.nan,
                "good_head": float(y[good].mean() - base) if good.any() else np.nan,
                "overlap_bad": int(np.isin(order, np.where(bad)[0]).sum()),
                "overlap_good": int(np.isin(order, np.where(good)[0]).sum()),
                "n_ok": int(ok.sum()), "n_bad": int(bad.sum()), "n_good": int(good.sum()),
            })
    df = pd.DataFrame(rows)
    res = {"n_anchors": int(df["date"].nunique()), "factors": {}}
    for name, g in df.groupby("factor"):
        entry = {}
        for tag, (a, b) in WINDOWS.items():
            w = g[(g["date"] >= a) & (g["date"] <= b)].dropna(subset=["bad_head"])
            if w.empty:
                continue
            entry[tag] = {
                "n": int(len(w)),
                "bad_head_pp": round(100 * float(w["bad_head"].mean()), 3),
                "bad_t": round(float(w["bad_head"].mean() /
                                     (w["bad_head"].std(ddof=1) / np.sqrt(len(w)))), 2),
                "good_head_pp": round(100 * float(w["good_head"].mean()), 3),
                "good_t": round(float(w["good_head"].mean() /
                                      (w["good_head"].std(ddof=1) / np.sqrt(len(w)))), 2),
                "overlap_bad_per_period": round(float(w["overlap_bad"].mean()), 2),
                "overlap_good_per_period": round(float(w["overlap_good"].mean()), 2),
                "mean_bad_set_size": round(float(w["n_bad"].mean()), 0),
            }
        res["factors"][name] = entry
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    print(f"\n锚点 {res['n_anchors']} 期（模型 top100 与因子十分位对比，超额单位为 %/期）")
    print(f"{'因子':<16}{'窗口':<8}{'坏侧超额':>10}{'t':>7}{'好侧超额':>10}{'t':>7}"
          f"{'坏侧∩top100':>12}{'好侧∩top100':>12}")
    for name, e in res["factors"].items():
        for tag in ("DEAD", "ACTIVE"):
            v = e.get(tag)
            if not v:
                continue
            print(f"{name:<16}{tag:<8}{v['bad_head_pp']:>10.3f}{v['bad_t']:>7.2f}"
                  f"{v['good_head_pp']:>10.3f}{v['good_t']:>7.2f}"
                  f"{v['overlap_bad_per_period']:>12.2f}{v['overlap_good_per_period']:>12.2f}")
    print(f"\n结果 → {OUT.name} | {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
