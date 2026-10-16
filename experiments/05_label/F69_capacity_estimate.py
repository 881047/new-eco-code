"""F69 容量估计：这个袖套最多能装多少钱？（2026-09-14 早，预注册）

**为什么现在做**：F67 把"超额是微盘范围内的选股 alpha"钉死了 ⇒ 容量必然受限。
**容量是这条策略能不能继续放大的硬约束**（1 万块随便做，1 千万就要看盘口）。

**预注册口径（跑数前冻结，不搜索）**：
  · **单票单次成交 ≤ p × 该票 20 日日均成交额**（ADV20，用 `daily.amount`，**单位千元**→已实测核实：
    600000.SH 20260911 的 amount=604,625.882 ≈ 6.05 亿元 ✓）；主口径 **p = 10%**，
    另报 5% 与 20% 作敏感度；
  · 每只票的**单期最坏成交额按"整仓换手"估**：`trade_i = C × w_i`（等权 w_i ≈ 1/N，N = 方案票数）
    —— 这是**上界式保守估计**（实际单期换手只有 ~0.14~0.18 倍仓）；
  · 容量 = `min_i (p × ADV20_i) / w_i`，并**分别报中位票与最差十分位票**（后者才是真正的约束）；
  · 作用域：用**当前生产方案**（`data/weekly/plan_20260911.json`，今天 09:32 要执行的那批）的票，
    ADV20 取锚点 20260911；同时报这批票的市值/成交额分位，把"为什么容量受限"与 F67 对上。

**不变量**：只看不改（只读方案与行情库），不写任何生产产物。
运行：``D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F69_capacity_estimate.py``
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from app import db                                                    # noqa: E402

PLAN = BASE_DIR / "data" / "weekly" / "plan_20260911.json"
ANCHOR = "20260911"
P_MAIN = 0.10
P_SENS = (0.05, 0.20)
OUT = Path(__file__).resolve().parent / "F69_capacity_estimate_result.json"


def main() -> int:
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    rows = plan.get("rows") or plan.get("holdings") or []
    codes = [r["ts_code"] for r in rows]
    n = len(codes)
    capital = float(plan.get("capital") or 1_000_000.0)
    print(f"方案 {PLAN.name}：锚点 {plan.get('anchor')} | {n} 只 | 资本 ¥{capital:,.0f} | "
          f"投入率 {plan.get('invested_ratio')}")
    ph = ",".join("?" * n)
    conn = db.get_conn()
    try:
        q = (f"SELECT ts_code, AVG(amount) AS adv20, AVG(total_mv) AS mv FROM "
             f"(SELECT ts_code, amount, total_mv FROM daily d LEFT JOIN daily_basic b "
             f" USING (ts_code, trade_date) WHERE d.trade_date <= ? AND d.ts_code IN ({ph}) "
             f"ORDER BY d.trade_date DESC LIMIT {n * 20}) t GROUP BY ts_code")
        df = pd.read_sql(q, conn, params=[ANCHOR] + codes)
    finally:
        conn.close()
    df = df.set_index("ts_code").reindex(codes)
    adv = df["adv20"].astype(float)          # 千元
    mv = df["mv"].astype(float)              # 万元
    w = np.full(n, 1.0 / n)
    # ⚠️ 单位：`amount` 是**千元**（已实测核实）⇒ 转成元要 ×1000。
    # 2026-09-14 首版漏了这一步，把"13.8 亿"印成"138 万"（差 1000×），自查时发现并修正。
    ADV_YUAN = adv * 1000.0
    res = {"plan": PLAN.name, "anchor": ANCHOR, "n": n, "capital_yuan": capital,
           "p_main": P_MAIN, "p_sensitivity": list(P_SENS),
           "rule": "容量 = min_i (p × ADV20_i) / w_i；单票单次最坏成交 = C × w_i（整仓换手上界）",
           "adv20_qian_yuan": {"p10": round(float(adv.quantile(0.10)), 1),
                               "median": round(float(adv.median()), 1),
                               "p90": round(float(adv.quantile(0.90)), 1)},
           "total_mv_wan": {"p10": round(float(mv.quantile(0.10)), 0),
                            "median": round(float(mv.median()), 0),
                            "p90": round(float(mv.quantile(0.90)), 0)},
           "capacity_yuan": {}, "binding_names": []}
    for p in (P_MAIN,) + P_SENS:
        c = P_MAIN if p == P_MAIN else p
        caps = c * ADV_YUAN / w
        res["capacity_yuan"][f"p={c:.2f}"] = {
            "median_name": round(float(caps.median())),
            "worst_decile_name": round(float(caps.quantile(0.10))),
            "min_name": round(float(caps.min()))}
    worst = (P_MAIN * ADV_YUAN / w).sort_values().head(5)
    res["binding_names"] = [{"ts_code": k, "adv20_qian_yuan": round(float(adv[k]), 1),
                             "implied_capacity_yuan": round(float(v))} for k, v in worst.items()]
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nADV20（千元）分位: p10 {res['adv20_qian_yuan']['p10']:,.0f} | "
          f"中位 {res['adv20_qian_yuan']['median']:,.0f} | p90 {res['adv20_qian_yuan']['p90']:,.0f}")
    print(f"总市值（万元）分位: p10 {res['total_mv_wan']['p10']:,.0f} | "
          f"中位 {res['total_mv_wan']['median']:,.0f} | p90 {res['total_mv_wan']['p90']:,.0f}")
    print("\n容量估计（单票单期整仓换手的最坏情形）:")
    for k, v in res["capacity_yuan"].items():
        print(f"  {k}: 中位票 {v['median_name']/1e4:,.0f} 万 | 最差十分位票 "
              f"{v['worst_decile_name']/1e4:,.0f} 万 | 最小票 {v['min_name']/1e4:,.0f} 万")
    print("\n最吃容量的 5 只:", [f"{b['ts_code']}({b['implied_capacity_yuan']/1e4:,.0f}万)"
                                 for b in res["binding_names"]])
    cap_worst = res["capacity_yuan"]["p=0.10"]["worst_decile_name"]
    print(f"\n结论：按 p=10% 口径（单票单次 ≤10% ADV20、整仓换手上界），**最保守那档（最差十分位票）**"
          f"的容量约 **{cap_worst/1e8:.2f} 亿元**（中位票 {res['capacity_yuan']['p=0.10']['median_name']/1e8:.2f} 亿元）；"
          f"当前资本 {capital/1e4:,.0f} 万元只占该容量的 **{100*capital/cap_worst:.2f}%**")
    print(f"结果 → {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
