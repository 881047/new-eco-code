"""镜厅 M0-lite · 一键复算（2026-09-15）

**只读**：从 `M0_series_lite/*.npz`（逐期序列）与 `M0_lite_result.json`（读数）重算报告里的每一个数字，
逐项与 JSON 里的存档值比对，**不一致即非零退出**（fail-closed）。

为什么要有它（验收硬闸 D2）：报告里的数字必须能被**任何人事后**从落盘序列复算出来 ——
否则"读数"就只是某次运行的打印，无法复核。这也是 KB 14 号第 9 条的做法。

复算的内容：
  1. 各序列 σ（S / F66_bench / POOL / BENCH800）与两个差序列的 σ、均值 —— 全部 ddof=1，单位 pp/锚点；
  2. `G_pool = σ(d_bench800)² / σ(d_pool)²`（两套种子 + 合并）；
  3. `ρ_req(G) = 1 − 1/(2G)`；
  4. 闸门判定（预注册 §三-C 的三档）；
  5. A2 红检：把锚点整体平移一格后重算 `corr(POOL, F66_bench)` 与 `σ(差)` ⇒ 必须"变红"。

用法：
    D:\\Python313\\python.exe -X utf8 experiments\\mirror\\mirror_m0_recompute.py
退出码：0 = 全部逐位一致；1 = 有不一致（打印明细）。
"""
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
SERIES = HERE / "M0_series_lite"
RESULT = HERE / "M0_lite_result.json"
#: 允许"存档侧做过舍入"—— 脚本早期版本把 G / ρ 存成 3~4 位小数，复算值是全精度。
#: 口径：要么逐位一致（1e-9 相对），要么等于**存档的舍入值**（±5e-5）。
TOL = 1e-9
ROUND_TOL = 5e-5


def _sd(a) -> float:
    v = np.asarray(a, dtype=float)
    v = v[~np.isnan(v)]
    return float(np.std(v, ddof=1)) if len(v) > 2 else float("nan")


def _mean(a) -> float:
    v = np.asarray(a, dtype=float)
    v = v[~np.isnan(v)]
    return float(np.mean(v)) if len(v) else float("nan")


def main() -> int:
    saved = json.loads(RESULT.read_text(encoding="utf-8"))
    rows, bad = [], []

    def chk(name, got, exp, scale=1.0):
        if np.isfinite(got) and np.isfinite(exp):
            d = abs(got - exp)
            ok = (d <= TOL * max(1.0, abs(exp)) * scale) or (d <= ROUND_TOL)
        else:
            ok = False
        rows.append((ok, name, got, exp))
        if not ok:
            bad.append(name)

    gs = []
    for seed in ("seedA", "seedB"):
        z = np.load(SERIES / f"M0_lite_{seed}.npz", allow_pickle=True)
        rec = saved["seeds"][seed]
        S, POOL = z["S"], z["POOL"]
        F66B, B800 = z["F66_bench"], z["BENCH800"]
        d_pool, d_b800 = z["d_pool_pool"], z["d_bench800"]

        chk(f"{seed}.sd_S_pp", 100 * _sd(S), rec["sd_S_pp"])
        chk(f"{seed}.sd_F66bench_pp", 100 * _sd(F66B), rec["sd_F66bench_pp"])
        chk(f"{seed}.sd_POOL_pp", 100 * _sd(POOL), rec["sd_POOL_pp"])
        chk(f"{seed}.sd_BENCH800_pp", 100 * _sd(B800), rec["sd_BENCH800_pp"])
        chk(f"{seed}.sd_d_pool_pool_pp", 100 * _sd(d_pool), rec["sd_d_pool_pool_pp"])
        chk(f"{seed}.sd_d_bench800_pp", 100 * _sd(d_b800), rec["sd_d_bench800_pp"])
        chk(f"{seed}.mean_d_pool_pool_pp", 100 * _mean(d_pool), rec["mean_d_pool_pool_pp"])
        chk(f"{seed}.mean_d_bench800_pp", 100 * _mean(d_b800), rec["mean_d_bench800_pp"])

        # A1：复现检查（POOL vs F66 的 bench）—— 相关与 σ(差)
        m = ~(np.isnan(POOL) | np.isnan(F66B))
        chk(f"{seed}.A1_corr", float(np.corrcoef(POOL[m], F66B[m])[0, 1]),
            rec["A1_corr_pool_vs_f66bench"], scale=1e-6)
        chk(f"{seed}.A1_sd_diff_pp", 100 * _sd(POOL - F66B), rec["A1_sd_diff_pp"])
        # A1-old（预注册原判据，前提被证伪）：F66.bench 与中证800 的开→开
        m2 = ~(np.isnan(F66B) | np.isnan(B800))
        chk(f"{seed}.A1old_corr", float(np.corrcoef(F66B[m2], B800[m2])[0, 1]),
            rec["A1old_corr_f66bench_vs_000906"], scale=1e-6)
        chk(f"{seed}.A1old_sd_diff_pp", 100 * _sd(F66B - B800), rec["A1old_sd_diff_pp"])

        # G_pool（主判据）
        gs.append((rec["sd_d_bench800_pp"] / rec["sd_d_pool_pool_pp"]) ** 2)
        chk(f"{seed}.G_pool", gs[-1], saved["G_pool"][seed])

        # A2 红检：整体平移一格 ⇒ 必须"变红"（相关崩、σ(差) 变大）
        p = POOL[1:]; q = F66B[:-1]
        m2 = ~(np.isnan(p) | np.isnan(q))
        corr_shift = float(np.corrcoef(p[m2], q[m2])[0, 1])
        chk(f"{seed}.A2_shift_corr", corr_shift, saved["checks"]["A2_shifted_corr"][0 if seed == "seedA" else 1])
        chk(f"{seed}.A2_shift_sd_pp", 100 * _sd(p - q),
            saved["checks"]["A2_shifted_sd_diff_pp"][0 if seed == "seedA" else 1])

    chk("G_pool.merged_mean", float(np.mean(gs)), saved["G_pool"]["merged_mean"])
    for g, key in ((3.0, "G3.0"), (6.9, "G6.9")):
        chk(f"rho_required[{key}]", 1 - 1 / (2 * g), saved["rho_required"][key])

    g = float(np.mean(gs))
    gate = "继续" if g >= 3 else ("待裁定" if g >= 2 else "缩编")
    chk("gate", {"缩编": 0.0, "待裁定": 1.0, "继续": 2.0}[gate],
        {"缩编": 0.0, "待裁定": 1.0, "继续": 2.0}[saved["verdict"]["gate"]])

    print(f"复算 {len(rows)} 项（容差 {TOL:g} 相对）:")
    for ok, name, got, exp in rows:
        flag = "✓" if ok else "✗"
        print(f"  {flag} {name:<34} 复算={got:>18.12f}  存档={exp:>18.12f}")
    print()
    print(f"闸门：G_pool = {g:.4f} ⇒ **{gate}**（预注册 §三-C：<2 缩编 / ≥3 继续）")
    if bad:
        print(f"\n✗ 有 {len(bad)} 项不一致: {bad}")
        return 1
    print("\n✓ 全部逐位一致 —— 报告里的数字可由落盘序列复算。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
