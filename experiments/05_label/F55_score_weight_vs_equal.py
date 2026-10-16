"""F55 · **分位加权 vs 等权**（信号→组合的转换效率）—— 预注册筛选

## 动机（承接 F50/F54，不是新的搜索）

- F50：生产打分 IC_IR = 0.685（周频），把现金拖累算回去后投入部分的 Sharpe ≈ 0.2506，
  落在第三方经验带 [0.206, 0.343] 内 ⇒ 信号被正常吃掉，但只是**带内的下沿**。
- F54（已否）：动态波动率缩放只是沿 F25 的风险斜线随机降敞口 ⇒ 结清。
- F37（已否）：静态提高投放率 ⇒ MDD 恶化，判据不通过 ⇒ 结清。
- 剩下没测过的是**同样满仓、但把权重按信号强弱分配**（等权丢掉了"分数有多高"这层信息）；
  理论上这提高的是**转换系数**（transfer coefficient），不是提高 IC、也不是加杠杆。

## 判据（跑数前冻结；这是单机制筛选，通过才进离散模拟器）

- 数据：生产打分 `data/weekly/scores_20260911.npz`（2×W5+pit 融合分） + 生产 PIT 掩码 +
  5 日 `forward_excess`（与 `scores.forward_excess` 同口径）；
- 每个锚点：在**可交易**票里取分数最高的 `TOP_N=100`（与现役构型同）；
    · **E 臂（等权，现役）**：每只 1/100；
    · **R 臂（分位加权）**：权重 ∝ (101 − 排名)，即线性递减（固定形式，**不做参数搜索**）；
- 收益：`ret = Σ w_i × excess_i`（excess 已对当日可交易池 demean ⇒ 组合相对池等权的超额）；
- 成本：换手 = `Σ|w_t − w_{t-1}|`（进出名单都算），按 `app.portfolio.fees` 的**卖出侧**费率
  计价（保守：把全部换手当卖出）；净收益 = 毛收益 − 成本；
- 判据（FULL 窗口，两套种子不可用 —— 这里只有生产那一份融合分，故以**逐期配对 t 检验**替代）：
    `净超额_R ≥ 净超额_E + 1.0pp`（年化）**且** `σ_R ≤ 1.05 × σ_E` **且** `|MDD_R| ≤ |MDD_E|`；
    另用 HAC(lag=2) 的配对 t 做参考（不作为通过判据，因为这不是新搜索、但也不是显著性证据）。

## 诚实边界

- 这是**连续近似**：假设 100% 投入、无整手粒度、无现金、按分数线性加权；与实盘的差别由
  **两臂共享**，所以 A/B 比较有效，但绝对水平不能直接当实盘读数；
- 分位加权会**集中到前几名**（权重 100/5050… 第 1 名 ≈ 3.9×均值）⇒ 若其中藏着一只暴雷，
  MDD 会立刻反映出来 —— 判据里因此同时看 σ 与 MDD，两轴缺一不可；
- 通过 ⇒ 下一步做进 `F1b` 离散模拟器（整手/名额/费用/换手全在那边）再走一次预注册 A/B；
  不通过 ⇒ 结清"权重分布"这条线，写进 05 号证伪登记。

用法：
    D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F55_score_weight_vs_equal.py
"""
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

from app import config as cfg_mod              # noqa: E402
from app.factors import factor_engine as fe    # noqa: E402
from app.pit.engine import PITEngine           # noqa: E402
from app.portfolio import fees as fee_mod      # noqa: E402
from app.weekly import scores as sc            # noqa: E402

log = logging.getLogger("f55")
OUT = HERE / "F55_score_weight_vs_equal_result.json"
NPZ = BASE / "data" / "weekly" / "scores_20260911.npz"
TOP_N = 100
PER_YEAR = 52.0
WINDOWS = {"FULL": ("2010-01-01", "2030-12-31"),
           "OOS_32m": ("2024-01-01", "2026-08-31"),
           "DEAD": ("2020-09-01", "2023-12-31")}
MIN_PASS_EXCESS_PP = 1.0
MAX_VOL_RATIO = 1.05


def weights_for(order: np.ndarray) -> tuple:
    """→ (等权向量, 分位加权向量)，长度 = len(order)；线性递减、和为 1。"""
    n = len(order)
    w_eq = np.full(n, 1.0 / n)
    lin = (n + 1 - np.arange(1, n + 1)).astype(float)      # n, n-1, ..., 1
    w_rk = lin / lin.sum()
    return w_eq, w_rk


def series_stats(ret: pd.Series) -> dict:
    r = ret.dropna()
    if len(r) < 3:
        return {"n": int(len(r))}
    nav = (1 + r).cumprod()
    return {"n": int(len(r)),
            "excess_pp": round(100 * float((1 + r).prod() ** (PER_YEAR / len(r)) - 1), 2),
            "mean_pp_per_week": round(100 * float(r.mean()), 3),
            "vol_pp": round(100 * float(r.std(ddof=1)), 3),
            "sharpe": round(float(r.mean() / r.std(ddof=1)), 3),
            "mdd": round(float((nav / nav.cummax() - 1).min()), 4)}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    with np.load(NPZ, allow_pickle=True) as z:
        S = z["scores"]
        anchors = pd.to_datetime([str(d) for d in z["anchors"]])
        cols = [str(c) for c in z["cols"]]
    cfg = cfg_mod.load_config()
    m = fe.load_market(since=sc.SINCE)
    pit = PITEngine(m=m, cfg=cfg)
    mask = pit.tradable_matrix(anchors, columns=cols).fillna(False)
    Y = sc.forward_excess(m, mask, sc.HORIZON)
    log.info("面板就绪 | %d 锚点 | %.0fs", len(anchors), time.time() - t0)

    sell_rate = fee_mod.effective_rate(25_000.0, "sell", "20260914")   # 现役单票规模 ≈2.5 万
    rows, prev_w = [], None
    for k, d in enumerate(anchors):
        y = Y[k]
        s = S[k].astype(float)
        ok = mask.iloc[k].to_numpy() & np.isfinite(s) & np.isfinite(y)
        if ok.sum() < 200:
            continue
        idx = np.argsort(-np.where(ok, s, -np.inf))[:TOP_N]
        w_eq, w_rk = weights_for(idx)
        ex = y[idx].astype(float)
        g_eq, g_rk = float(np.dot(w_eq, ex)), float(np.dot(w_rk, ex))
        # 换手：两臂各自与上一期权重比较（缺失名字按 0 计）
        cur = {"eq": dict(zip(idx, w_eq)), "rk": dict(zip(idx, w_rk))}
        c_eq = c_rk = 0.0
        if prev_w:
            names = set(cur["eq"]) | set(prev_w["eq"])
            c_eq = sum(abs(cur["eq"].get(i, 0.0) - prev_w["eq"].get(i, 0.0)) for i in names)
            c_rk = sum(abs(cur["rk"].get(i, 0.0) - prev_w["rk"].get(i, 0.0)) for i in names)
        prev_w = cur
        rows.append({"date": d, "gross_eq": g_eq, "gross_rk": g_rk,
                     "cost_eq": c_eq * sell_rate, "cost_rk": c_rk * sell_rate,
                     "turnover_eq": c_eq, "turnover_rk": c_rk})
    df = pd.DataFrame(rows)
    df["net_eq"] = df["gross_eq"] - df["cost_eq"]
    df["net_rk"] = df["gross_rk"] - df["cost_rk"]
    log.info("逐期配对完成: %d 期 | %.0fs", len(df), time.time() - t0)

    res = {"n_periods": int(len(df)), "top_n": TOP_N, "sell_rate_bp": round(sell_rate * 1e4, 3),
           "criteria": {"min_excess_gain_pp": MIN_PASS_EXCESS_PP, "max_vol_ratio": MAX_VOL_RATIO},
           "windows": {}, "secs": None}
    for w, (lo, hi) in WINDOWS.items():
        s = df[(df["date"] >= lo) & (df["date"] <= hi)]
        if len(s) < 3:
            res["windows"][w] = {"n": int(len(s))}
            continue
        res["windows"][w] = {
            "E_等权": series_stats(s["net_eq"]),
            "R_分位加权": series_stats(s["net_rk"]),
            "turnover_mean": {"E": round(float(s["turnover_eq"].mean()), 3),
                              "R": round(float(s["turnover_rk"].mean()), 3)},
            "cost_pp_total": {"E": round(100 * float(s["cost_eq"].sum()), 2),
                              "R": round(100 * float(s["cost_rk"].sum()), 2)},
        }
    f = res["windows"]["FULL"]
    gain = f["R_分位加权"]["excess_pp"] - f["E_等权"]["excess_pp"]
    vol_ratio = f["R_分位加权"]["vol_pp"] / f["E_等权"]["vol_pp"]
    mdd_ok = abs(f["R_分位加权"]["mdd"]) <= abs(f["E_等权"]["mdd"])
    passed = bool(gain >= MIN_PASS_EXCESS_PP and vol_ratio <= MAX_VOL_RATIO and mdd_ok)
    # 配对 t（参考用；HAC lag=2 简化版：Newey-West 对差值序列）
    diff = (df["net_rk"] - df["net_eq"]).dropna().to_numpy()
    lag = 2
    dbar, n = diff.mean(), len(diff)
    gamma0 = float(np.sum((diff - dbar) ** 2) / n)
    var = gamma0
    for L in range(1, lag + 1):
        cov = float(np.sum((diff[L:] - dbar) * (diff[:-L] - dbar)) / n)
        var += 2 * (1 - L / (lag + 1)) * cov
    t_hac = float(dbar / np.sqrt(var / n)) if var > 0 else float("nan")
    res["verdict"] = {
        "gain_pp": round(gain, 2), "vol_ratio": round(vol_ratio, 3), "mdd_ok": bool(mdd_ok),
        "paired_t_hac_reference": round(t_hac, 2),
        "passed": passed,
        "conclusion": ("通过筛选 ⇒ 下一步做进 F1b 离散模拟器再 A/B"
                       if passed else "**未通过** ⇒ 结清'权重分布'这条线（写进 05 号证伪登记）")}
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    df.to_json(HERE / "F55_series.json", orient="records", force_ascii=False,
               indent=1, date_format="iso")
    print(f"{'窗口':<10}{'臂':<12}{'超额pp':>9}{'σpp':>8}{'Sharpe':>9}{'MDD':>9}{'换手':>8}{'成本pp':>9}")
    for w in WINDOWS:
        v = res["windows"][w]
        if "E_等权" not in v:
            continue
        for arm in ("E_等权", "R_分位加权"):
            a = v[arm]
            print(f"{w:<10}{arm:<12}{a['excess_pp']:>9}{a['vol_pp']:>8}{a['sharpe']:>9}"
                  f"{a['mdd']:>9}{v['turnover_mean'][arm[0]]:>8}{v['cost_pp_total'][arm[0]]:>9}")
    print(f"\n全史增量 {gain:+.2f}pp | σ 比 {vol_ratio:.3f} | MDD 不恶化: {mdd_ok} | "
          f"配对 t(HAC lag=2, 参考) {t_hac:.2f}")
    print(res["verdict"]["conclusion"])
    print(f"写入 {OUT.name}（{res['secs']}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
