"""F74 · 镜厅：HAC 修正的排除线时钟（研究面，**报告仪器、不是闸门**）

**为什么要有它**：F74 的 `M0_report.md` §四 如实登记了"HAC 未做"，而预注册 B3 要求
「`mean(d)` 与其 HAC t 值如实报告」。本脚本把这条补齐，并把配对读数变成**每周看得见**的
输出形态 —— 最有用的那一句话是：**"当前已可排除 |α| < X pp/期"**（单侧 95% 置信下界）。

## 纪律（写死，输出里也会重复）

- **报告仪器，不是闸门**：不得用它做任何采纳决定（预注册 §三、M2.0 看视点纪律）。
- `z = 2.5 / 3.0` 只是"值得注意"标记；**叙事结论只允许在 6/9/12/15/18/24 月检视点提出**，
  且必须报"这是第 k 次检视"。
- **F70 红线**：`|t| < 1.6` 一律不得写成"发现"。
- 自相关未修正的朴素 `z` 与 HAC `z` **并列输出** —— 让读者看见修正量。

## 输入

`experiments/mirror/M0_series_lite/M0_lite_seed{A,B}.npz` 里的
`S`（袖套）、`POOL`（池等权）、`BENCH800`（中证800 开→开）、`date`。

## 公式（Newey-West / Bartlett，L 预注册 = 2）

    γ_l = (1/n)·Σ_{t>l} (d_t − d̄)(d_{t−l} − d̄)
    σ̂²_HAC = γ_0 + 2·Σ_{l=1..L} (1 − l/(L+1))·γ_l
    z = √n · d̄ / σ̂_HAC
    排除线（单侧 95%）= d̄ − 1.645·σ̂_HAC/√n      （>0 才有排除力）

## 自检（合成序列，已知自相关）

`d_t = μ + φ·d_{t−1} + η_t`。**这个自检我写错过两版，两版都被它自己打红，如实留痕：**

- 第一版断言"HAC 应最接近序列 σ_X"—— **错**：`σ_X = σ_η/√(1−φ²)` 描述的是**单个观测的离散度**，
  而 HAC 要估的是**均值的长期标准误**；二者相差 `√((1+φ)/(1−φ))`（φ=0.6 时整整 **2 倍**）。
- 第二版断言"`σ_naive ≤ σ_HAC ≤ σ_X`"—— **同样错**（HAC 本来就会大于 σ_X）。
- **正确断言（本版）**：`σ_naive ≈ √γ0`，且
  `截断求和 = σ_η·√((1+2Σ_{l=1,2}(1−l/3)φ^l)/(1−φ²))` ≤ `σ_HAC` ≤ `LRV = σ_η/(1−φ)`，
  容差 ±35%（n=2000 的估计噪声）。φ=0 时三者重合，是最强的对照。

用法：
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\mirror_hac_clock.py
"""
import json
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parents[2]
SERIES_DIR = BASE / "experiments" / "mirror" / "M0_series_lite"
OUT = BASE / "experiments" / "mirror" / "M0_clock.json"

LAG = 2                 # 预注册 L=2
Z95 = 1.645             # 单侧 95%
CHECKPOINTS_MONTHS = (6, 9, 12, 15, 18, 24)
PER_MONTH = 52.0 / 12.0


def hac(d: np.ndarray, lag: int = LAG) -> dict:
    """Newey-West(Bartlett) 长方差 + z + 排除线。返回各自 pp/期 与 t/z。"""
    d = np.asarray(d, dtype=float)
    d = d[~np.isnan(d)]
    n = len(d)
    if n < 4:
        return {"n": n}
    dbar = float(np.mean(d))
    dev = d - dbar
    g0 = float(np.dot(dev, dev) / n)
    s2 = g0
    gammas = []
    for l in range(1, lag + 1):
        g = float(np.dot(dev[l:], dev[:-l]) / n)
        gammas.append(g)
        s2 += 2 * (1 - l / (lag + 1)) * g
    s2 = max(s2, 1e-18)
    sd_naive = float(np.sqrt(g0))
    sd_hac = float(np.sqrt(s2))
    z_naive = np.sqrt(n) * dbar / sd_naive
    z_hac = np.sqrt(n) * dbar / sd_hac
    excl = dbar - Z95 * sd_hac / np.sqrt(n)      # 排除线（下界）
    return {"n": n, "mean_pp": 100 * dbar,
            "sd_naive_pp": 100 * sd_naive, "sd_hac_pp": 100 * sd_hac,
            "z_naive": float(z_naive), "z_hac": float(z_hac),
            "hac_inflation": float(sd_hac / sd_naive),
            "acf_lag1": float(gammas[0] / g0) if gammas else float("nan"),
            "acf_lag2": float(gammas[1] / g0) if len(gammas) > 1 else float("nan"),
            "exclusion_bound_pp": 100 * excl,
            "note": "exclusion_bound_pp = 单侧 95% 下界：当前可排除 |α| < 该值（>0 才有排除力）"}


def synthetic_selfcheck(seed: int = 20260915) -> list:
    """合成自检：d_t = μ + φ d_{t−1} + η；断言见模块 docstring（第三版）"""
    rng = np.random.default_rng(seed)
    out = []
    for phi in (0.0, 0.3, 0.6):
        n, mu, s_eta = 2000, 0.002, 0.02
        e = rng.normal(0, s_eta, n)
        d = np.zeros(n)
        for t in range(1, n):
            d[t] = mu + phi * d[t - 1] + e[t]
        r = hac(d[1:])
        trunc = s_eta * np.sqrt((1 + 2 * sum((1 - l / 3) * phi ** l for l in (1, 2))) / (1 - phi ** 2))
        lrv = s_eta / (1 - phi)
        naive, hac_v = r["sd_naive_pp"], r["sd_hac_pp"]
        lo, hi = 100 * trunc * 0.65, 100 * lrv * 1.35
        out.append({"phi": float(phi), "sd_naive_pp": naive, "sd_hac_pp": hac_v,
                    "hac_truncated_theory_pp": float(100 * trunc), "lrv_pp": float(100 * lrv),
                    "ordering_ok": bool(lo <= hac_v <= hi and naive <= hac_v * 1.05),
                    "hac_over_naive_ratio": float(hac_v / naive) if naive else float("nan")})
    return out


def main() -> int:
    res = {"lag": LAG, "z95": Z95, "synthetic_selfcheck": synthetic_selfcheck(),
           "checkpoints_months": list(CHECKPOINTS_MONTHS), "series": {}}
    print("=== 合成自检（断言：HAC 落在 截断求和 与 LRV 之间；且 ≥ 朴素）===")
    for row in res["synthetic_selfcheck"]:
        print("  φ=%.1f  朴素=%.2fpp  HAC=%.2fpp（×%.3f）  截断理论=%.2fpp  LRV=%.2fpp  夹逼成立=%s"
              % (row["phi"], row["sd_naive_pp"], row["sd_hac_pp"], row["hac_over_naive_ratio"],
                 row["hac_truncated_theory_pp"], row["lrv_pp"], row["ordering_ok"]))
    ok = all(r["ordering_ok"] for r in res["synthetic_selfcheck"])
    res["synthetic_ok"] = bool(ok)
    print("  ⇒ 自检", "通过" if ok else "**未通过（HAC 实现可疑，不许据此报数）**")

    for seed in ("seedA", "seedB"):
        z = np.load(SERIES_DIR / f"M0_lite_{seed}.npz", allow_pickle=True)
        d_pool = z["S"] - z["POOL"]
        d_800 = z["S"] - z["BENCH800"]
        rec = {"pool": hac(d_pool), "bench800": hac(d_800), "progress": {}}
        n = rec["pool"]["n"]
        for months in CHECKPOINTS_MONTHS:
            k = int(round(months * PER_MONTH))
            if k <= n:
                rec["progress"][f"{months}m"] = {
                    "n": k,
                    "exclusion_pool_pp": hac(d_pool[:k])["exclusion_bound_pp"],
                    "z_hac_pool": hac(d_pool[:k])["z_hac"],
                }
        res["series"][seed] = rec
        print(f"\n=== {seed}（n={n}）===")
        for tag, key in (("袖套−池等权", "pool"), ("袖套−中证800", "bench800")):
            r = rec[key]
            print(f"  {tag:12s} mean={r['mean_pp']:+.3f}pp  σ_朴素={r['sd_naive_pp']:.3f} "
                  f"σ_HAC={r['sd_hac_pp']:.3f}（×{r['hac_inflation']:.3f}）  "
                  f"z_朴素={r['z_naive']:+.2f}  z_HAC={r['z_hac']:+.2f}  "
                  f"排除线={r['exclusion_bound_pp']:+.3f}pp  [F70 红线 1.6: "
                  f"{'过' if abs(r['z_hac']) >= 1.6 else '不过'}]")
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n产物 → {OUT.relative_to(BASE)}")
    print("⚠️ 纪律：本时钟是**报告仪器**，不得用于采纳决定；叙事只限 6/9/12/15/18/24 月检视点。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
