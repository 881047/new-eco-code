r"""alpha_pool 历史挖掘因子筛查（研究层）—— "把以前挖的因子都加进去会更好吗"的第一阶段测量。

跑法（cwd = 仓根）：
    .venv\\Scripts\\python.exe -X utf8 -m scripts.screen_alpha_pool

## 回答什么

`data/alpha_pool/` 有历史挖掘的因子面板（2026-09-28 实点 **247** 个；各研究轮次 gate 批次的产物，
数字随门禁批次增长，对账以 `ls data/alpha_pool/*.npz | wc -l` 为准）。⚠️ 口径注记（2026-09-28）：
本脚本内嵌的 PROD_FACTORS=31 是**因子库全集**；第 6 次重钉起生产消费侧为 26（`factor_exclusions`
剔 5 个 S5 判否因子），冗余去重对已剔除 5 因子仍算 ρ ⇒ 偏保守、方向无害。"全部加进去"是错误版本（冗余+过拟合——13 号 §五补 门禁 v2
就是为此拍板）；**正确版本 = 先筛后加**：189 个里有多少
  (a) 全史显著：|rank IC| ≥ 0.02 且 t ≥ 2（对 21 日前瞻收益，trainer 同式标签）；
  (b) 近期存活：强度保持比 |近12月|/|长窗| ≥ 0.5；
  (c) 非冗余：与现役 31 因子的截面秩相关 |ρ| ≤ 0.7（W2-F38 同款阈值），且互不冗余；
→ 幸存者 = "值得进 Phase 2（重训 A/B）"的候选清单。

## 口径

- 标签与 rank IC：与 `factor_lifecycle_monitor` 完全同式（21 开市日复权收益、
  逐月 Spearman）；本文档新增的只有**对现役 31 因子的相关性去重**；
- 相关性 = 近 24 个月逐月截面 Spearman 秩相关的均值（面板交集上）。

⚠️ 只读诊断：不改生产打分、不构成上线判决——Phase 2（31+N 重训 walk-forward A/B）
才是"是否更好"的最终测量。本脚本回答的是"有没有值得重训的料"。
"""
import glob
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.factor_lifecycle_monitor import forward_returns, load_close_grid, load_factor_panel

DB = r"data/neweco.db"
POOL = Path("data/alpha_pool")
H = 21
GATE_IC = 0.02
GATE_T = 2.0
GATE_DECAY = 0.5
GATE_RHO = 0.7
PROD_FACTORS = ["amount_log", "beta_250", "bias_20", "bias_60", "bp", "debt_to_assets",
                "dv_ttm", "ep", "eps_yoy", "grossprofit_margin", "high_52w", "illiq",
                "lhb_cnt20", "lhb_net20", "lhb_org_net20", "low_52w", "mom_120", "mom_20",
                "mom_250", "mom_60", "netprofit_yoy", "or_yoy", "ret_max20", "rev_5",
                "roe", "size", "skew_20", "turnover_20", "turnover_chg", "vol_20", "vol_60"]


def main():
    t0 = time.time()
    files = sorted(glob.glob(str(POOL / "*.npz")))
    conn = sqlite3.connect(DB)
    # 月份网格 = alpha_pool 日期并集（与前向收益网格同源）
    all_dates = set()
    panels = {}
    for f in files:
        d = np.load(f, allow_pickle=True)
        name = Path(f).stem
        ds = [str(x) for x in d["dates"].tolist()]
        codes = [str(x) for x in d["codes"].tolist()]
        vals = d["values"].astype(float)
        panels[name] = pd.DataFrame(vals, index=ds, columns=codes)
        all_dates |= set(ds)
    months = sorted(all_dates)
    print(f"alpha_pool {len(panels)} 面板｜月份并集 {len(months)} 期（{months[0]}..{months[-1]}）",
          flush=True)

    fwd = forward_returns(load_close_grid(conn, months), months)
    print(f"前向收益网格 {fwd.shape[0]} 期 × {fwd.shape[1]} 票", flush=True)

    # 现役 31 因子面板（同月网格）
    prod = {}
    for f in PROD_FACTORS:
        prod[f] = load_factor_panel(conn, f, months)
    print("现役 31 因子面板就绪", flush=True)

    # 2026-09-28 审查(36补): IC 读数裁剪到 **PIT 可交易域** —— 原在全市场交集上算 rank IC,
    # 与门禁 evaluate 的域不同(07 号 P0-05 同族: 门禁域与读数域不同 ⇒ 读数不可比)。
    # 冗余 ρ(xcorr)仍在全交集上算(域偏宽 ⇒ 偏保守, 方向无害), 故意不动。
    from app.factors.factor_engine import load_market
    from app.pit.engine import PITEngine
    _pit = PITEngine(m=load_market())

    def monthly_ic(panel: pd.DataFrame) -> pd.Series:
        ics = {}
        for m in panel.index:
            if m not in fwd.index:
                continue
            x = panel.loc[m]
            mask = _pit.tradable_mask(pd.Timestamp(str(m)))
            x = x[[c for c in x.index if mask.get(c, False)]]
            y = fwd.loc[m].reindex(x.index)
            ok = x.notna() & y.notna() & np.isfinite(x) & np.isfinite(y)
            if ok.sum() < 100:
                continue
            ics[m] = float(x[ok].rank().corr(y[ok].rank()))
        return pd.Series(ics).sort_index()

    def xcorr(a: pd.DataFrame, b: pd.DataFrame, months24: list) -> float:
        """近 24 个月逐月截面秩相关均值（交集上）。"""
        cs = []
        for m in months24:
            if m not in a.index or m not in b.index:
                continue
            x = a.loc[m]
            y = b.loc[m].reindex(x.index)
            ok = x.notna() & y.notna() & np.isfinite(x) & np.isfinite(y)
            if ok.sum() < 100:
                continue
            c = float(x[ok].rank().corr(y[ok].rank()))
            if c == c:
                cs.append(c)
        return float(np.mean(cs)) if cs else float("nan")

    months24 = months[-24:]
    prod_ic = {f: monthly_ic(p) for f, p in prod.items()}
    print("现役因子基线 IC 就绪；开始筛查 189 个挖掘因子…", flush=True)

    out = {}
    for i, (name, panel) in enumerate(sorted(panels.items())):
        ic = monthly_ic(panel)
        if len(ic) < 36:
            out[name] = {"verdict": "数据不足", "n_months": int(len(ic))}
            continue
        lr = float(ic.mean())
        se = float(ic.std(ddof=1) / np.sqrt(len(ic)))
        t_ = lr / se if se else np.nan
        trail = ic.iloc[-12:]
        base = ic.iloc[:-12] if len(ic) > 24 else ic
        decay = abs(trail.mean()) / abs(base.mean()) if abs(base.mean()) > 0 else np.nan
        rho_prod = {}
        for f in PROD_FACTORS:
            c = xcorr(panel, prod[f], months24)
            if c == c:
                rho_prod[f] = c
        max_rho = max((abs(v) for v in rho_prod.values()), default=np.nan)
        passed = (abs(lr) >= GATE_IC and (t_ == t_ and abs(t_) >= GATE_T)
                  and decay == decay and abs(decay) >= GATE_DECAY
                  and max_rho == max_rho and max_rho <= GATE_RHO)
        out[name] = {"ic": round(lr, 4), "ic_t": round(t_, 2) if t_ == t_ else None,
                     "decay": round(decay, 2) if decay == decay else None,
                     "max_rho_vs_prod": round(max_rho, 3) if max_rho == max_rho else None,
                     "rho_top_factor": (max(rho_prod, key=lambda k: abs(rho_prod[k]))
                                        if rho_prod else None),
                     "passed": bool(passed)}
        if i % 25 == 0 or i + 1 == len(panels):
            print(f"  [{i + 1}/{len(panels)}] 已筛（存活 {sum(1 for v in out.values() if v.get('passed'))}）",
                  flush=True)

    survivors = sorted(((k, v) for k, v in out.items() if v.get("passed")),
                       key=lambda kv: abs(kv[1]["ic"]), reverse=True)
    print(f"\n== 筛查结果：{len(survivors)} / {len(panels)} 通过门槛 ==")
    for k, v in survivors:
        print(f"  {k:24s} |IC| {abs(v['ic']):.4f}（t {v['ic_t']:+.2f}）｜保持比 {v['decay']}"
              f"｜max|ρ| vs 31 {v['max_rho_vs_prod']}（最高相关: {v['rho_top_factor']}）")
    if not survivors:
        print("  （无 —— 历史挖掘因子中没有'存活且非冗余'的可加项；"
              "Phase 2 重训 A/B 不开工）")

    q = Path("data") / f"alpha_pool_screen_{datetime.now().strftime('%Y%m%d')}.json"
    q.write_text(json.dumps({
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "alpha_pool 189 面板筛查（只读）：门槛 |IC|≥0.02 & t≥2 & 保持比≥0.5 & "
                "max|ρ|vs现役31≤0.7（探索性阈值，对齐 W2-F38 去重口径）；"
                "幸存者只是 Phase 2 重训 A/B 的候选，不构成上线判决。",
        "gate": {"ic": GATE_IC, "t": GATE_T, "decay": GATE_DECAY, "rho": GATE_RHO},
        "results": out, "survivors": [k for k, _ in survivors],
        "runtime_sec": round(time.time() - t0, 1)},
        ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n读数入库：{q}（{round(time.time() - t0, 1)}s）")


if __name__ == "__main__":
    main()
