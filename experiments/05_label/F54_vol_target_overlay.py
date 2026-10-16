"""F54 · **波动率目标敞口**（vol-target overlay）筛选实验 —— 预注册

## 动机（说的是已核实的事，不是愿望）

- F50：生产打分的逐期 IC_IR = 0.685，第三方经验带给出周频 Sharpe ∈ [0.206, 0.343]；
  现役 0.1927 **除以投入率 0.769 ≈ 0.2506，落在带内** ⇒ 缺口几乎全来自"23% 现金没投出去"。
- F37（已测）：把投入率从 0.769 拉到 0.99 ⇒ 全史超额 +5.5pp，但 **MDD −32%→−41%**，判据不通过
  ⇒ "**静态**提高敞口只是沿同一条风险斜线滑动"（F25 的 excess≈−10.44+26.23w 也是同一条）。
- 那么剩下唯一没试过的机制是**动态**敞口：按预测波动缩放仓位（vol targeting）。
  文献两边都有：Moreira & Muir (2017, JF) 报显著改善；Cederburg et al. (2020, JFE) 质疑其稳健性
  —— 所以必须用自己的两轴判据在本数据上测，不许引用结论。

## 判据（跑数前冻结；这是**单机制筛选**，通过才进离散模拟器）

- 输入：F52 落盘的**生产构型**（1e6/top100/带8×/hold_bonus=0.0/w=1.0）逐期序列，两套种子；
- 预测：`vol_fc[t]` = 用 **t 之前**收益算的 EWMA 波动（λ=0.97，周频；来源：volatility-modeling skill
  的 RiskMetrics 口径）×√52 年化 ⇒ **无前视**；
- 目标：`target[t]` = 到 t-1 为止 `vol_fc` 的**扩张中位数**（warmup 26 期，warmup 前敞口=1.0）；
- 敞口：`e[t] = clip(target[t] / vol_fc[t], 0.5, 1.0)`（**不加杠杆**，下限 0.5）；
- 成本：调仓成本 = |e[t] − e[t-1]| × 单边费率（用 `app.portfolio.fees` 的真实费率，
  卖出含印花税 ⇒ 比买入贵）；**闲置部分的逆回购利息不计**（保守：低估 overlay）；
- 判据（**两套种子同时满足**才算通过）：
    `overlay 全史超额 ≥ 静态全史超额` 且 `|overlay 全史 MDD| ≤ 0.95 × |静态全史 MDD|`；
- 通过 ⇒ 下一步是把它做进离散模拟器（整手/名额/选股反馈都在那边）；不通过 ⇒ **结清这条线**，
  写进 05 号证伪登记。

## 诚实边界

这是**连续近似**：忽略整手粒度、忽略"敞口变了会改变候选与名额"、忽略闲置现金的逆回购利息。
三者方向不一致，所以结论只用于**筛掉**明显没戏的机制；真要上线必须过离散模拟器与预注册 A/B。

用法：
    D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F54_vol_target_overlay.py
"""
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent
sys.path.insert(0, str(BASE))

from app.portfolio import fees as fee_mod          # noqa: E402

OUT = HERE / "F54_vol_target_overlay_result.json"
SEEDS = ("seedA", "seedB")
LAMBDA = 0.97          # 周频 EWMA（RiskMetrics；见 volatility-modeling skill）
WARMUP = 26
EXPO_FLOOR, EXPO_CAP = 0.5, 1.0
PER_YEAR = 52.0
WINDOWS = {"FULL": ("2010-01-01", "2030-12-31"),
           "OOS_32m": ("2024-01-01", "2026-08-31"),
           "DEAD": ("2020-09-01", "2023-12-31")}


def ewma_vol(ret: np.ndarray, lam: float = LAMBDA) -> np.ndarray:
    """vol_fc[t] = sqrt(EWMA 方差，只用 r[0..t-1]) —— 决策期 t 不许看到 r[t]。"""
    n = len(ret)
    var = np.full(n, np.nan)
    v = 0.0
    for t in range(1, n):
        v = lam * v + (1 - lam) * ret[t - 1] ** 2
        var[t] = v
    return np.sqrt(var)


def build_overlay(d: pd.DataFrame) -> pd.DataFrame:
    ret = d["ret"].to_numpy(dtype=float)
    vol = ewma_vol(np.nan_to_num(ret, nan=0.0)) * math.sqrt(PER_YEAR)
    target = np.full(len(d), np.nan)
    for t in range(len(d)):
        hist = vol[:t]
        hist = hist[np.isfinite(hist)]
        if len(hist) >= WARMUP:
            target[t] = float(np.median(hist))
    expo = np.where(np.isfinite(target) & np.isfinite(vol) & (vol > 0),
                    np.clip(target / vol, EXPO_FLOOR, EXPO_CAP), 1.0)
    # 单边费率：卖出含印花税(2023-08-28 后 0.05%)、买入不含 ⇒ 用真实费用模型
    buy_rate = fee_mod.effective_rate(1_000_000.0, "buy", "20260914")   # 大额 ⇒ 逼近费率地板
    sell_rate = fee_mod.effective_rate(1_000_000.0, "sell", "20260914")
    prev = np.concatenate([[1.0], expo[:-1]])          # warmup 前敞口=1.0
    delta = np.abs(expo - prev)
    side_cost = np.where(expo > prev, buy_rate, sell_rate)
    cost = delta * side_cost
    out = d[["date"]].copy()
    out["bench"] = d["bench_ret"].to_numpy(dtype=float)      # 超额必须减基准，不能只报策略自身年化
    out["ret_static"] = ret
    out["expo"] = expo
    out["cost"] = cost
    out["ret_overlay"] = expo * ret - cost
    return out


def stats(o: pd.DataFrame, lo: str, hi: str) -> dict:
    s = o[(o["date"] >= lo) & (o["date"] <= hi)]
    if len(s) < 3:
        return {"n": int(len(s))}
    def ann(r):
        return float((1 + r).prod() ** (PER_YEAR / len(r)) - 1)
    def mdd(r):
        nav = (1 + r).cumprod()
        return float((nav / nav.cummax() - 1).min())
    bench = ann(s["bench"])
    ex_s, ex_o = 100 * (ann(s["ret_static"]) - bench), 100 * (ann(s["ret_overlay"]) - bench)
    mdd_s, mdd_o = mdd(s["ret_static"]), mdd(s["ret_overlay"])
    return {"n": int(len(s)),
            "excess_static_pp": round(ex_s, 2), "excess_overlay_pp": round(ex_o, 2),
            "excess_static_no_cost_pp": round(100 * (ann(s["expo"] * s["ret_static"]) - bench), 2),
            "mdd_static": round(mdd_s, 4), "mdd_overlay": round(mdd_o, 4),
            "ratio_static": round(abs(ex_s / (100 * mdd_s)), 3),      # 超额 ÷ |MDD|（两轴比）
            "ratio_overlay": round(abs(ex_o / (100 * mdd_o)), 3),
            "mean_expo": round(float(s["expo"].mean()), 3),
            "pct_at_floor": round(float((s["expo"] <= EXPO_FLOOR + 1e-9).mean()), 3),
            "pct_at_cap": round(float((s["expo"] >= EXPO_CAP - 1e-9).mean()), 3),
            "cost_pp_total": round(100 * float(s["cost"].sum()), 2)}


def main() -> int:
    res = {"lambda": LAMBDA, "warmup": WARMUP, "expo_floor": EXPO_FLOOR, "expo_cap": EXPO_CAP,
           "betas": {"buy_rate": fee_mod.effective_rate(1_000_000.0, "buy", "20260914"),
                     "sell_rate": fee_mod.effective_rate(1_000_000.0, "sell", "20260914")},
           "windows": WINDOWS, "seeds": {}, "verdict": {}}
    rows = {}
    for seed in SEEDS:
        p = HERE / f"F52_series_{seed}_A0.json"
        if not p.exists():
            print(f"缺 {p.name}，先跑 F52 --seed AB")
            return 2
        d = pd.read_json(p)
        d["date"] = pd.to_datetime(d["date"])
        o = build_overlay(d)
        rows[seed] = o
        res["seeds"][seed] = {w: stats(o, *wh) for w, wh in WINDOWS.items()}
        o.to_json(HERE / f"F54_overlay_series_{seed}.json", orient="records",
                  force_ascii=False, indent=1, date_format="iso")

    passed, detail = True, {}
    for seed in SEEDS:
        f = res["seeds"][seed]["FULL"]
        ok = (f["excess_overlay_pp"] >= f["excess_static_pp"]
              and abs(f["mdd_overlay"]) <= 0.95 * abs(f["mdd_static"]))
        detail[seed] = {"overlay_beats_static_excess": f["excess_overlay_pp"] >= f["excess_static_pp"],
                        "overlay_cuts_mdd_5pct": abs(f["mdd_overlay"]) <= 0.95 * abs(f["mdd_static"]),
                        "pass": bool(ok)}
        passed = passed and ok
    res["verdict"] = {"passed": bool(passed), "per_seed": detail,
                      "conclusion": ("通过筛选 ⇒ 下一步做进离散模拟器（整手/名额/选股反馈）再 A/B"
                                     if passed else
                                     "**未通过** ⇒ 结清这条线：写进 05 号证伪登记（动态敞口在本数据上"
                                     "没能同时改善两轴）")}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"{'seed':<7}{'全史 静态':>12}{'全史 overlay':>14}{'MDD 静态':>10}{'MDD overlay':>12}"
          f"{'均敞口':>9}{'地板%':>8}{'成本pp':>9}")
    for seed in SEEDS:
        f = res["seeds"][seed]["FULL"]
        print(f"{seed:<7}{f['excess_static_pp']:>12}{f['excess_overlay_pp']:>14}"
              f"{f['mdd_static']:>10}{f['mdd_overlay']:>12}{f['mean_expo']:>9}"
              f"{f['pct_at_floor']:>8}{f['cost_pp_total']:>9}")
    print(f"\n判据（两套种子同时）：{res['verdict']['conclusion']}")
    print(f"写入 {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
