"""种子方差 → **验证分辨率**刻度（2026-09-21，阶段 2「验证方法改进」）。

## 输入（实测，非假设）
F76b（`experiments/05_label/F76b_uniqueness_ab_result.json`，05 号台账已登记）：
  同配置 `base_A` vs `base_B` **全窗年化差 5.9pp**。
⇒ 两 seed 之差的标度 σ_diff ≈ 5.9pp ⇒ 单 seed 标准差 σ_single ≈ 5.9 / √2 ≈ 4.17pp。

## 统计
N 个 seed **平均**的标准误 SE = σ_single / √N。
要分辨效应 Δ，通常要求 SE ≤ Δ / 2（效应至少 2 倍标准误）⇒ N = (2·σ_single / Δ)²。

## 用途
判断「加 seed 降方差」在**合理计算成本内**能否验证候选效应。
若某候选的预期 Δ 很小（如 F76b 的 +0.04pp），本脚本会直接给出所需的 seed 数（往往不现实）。
"""
import json
from pathlib import Path

DIFF_PP = 5.9                      # F76b 实测：base_A vs base_B 全窗年化差(pp)
SIGMA_SINGLE = DIFF_PP / (2 ** 0.5)  # 单 seed 标准差 ≈ 4.17pp

OUT = Path(__file__).resolve().parents[2] / "experiments" / "seed_resolution.json"


def seeds_needed(delta_pp: float, k: float = 2.0) -> int:
    """分辨效应 delta_pp 所需的 seed 数（k=2 ⇒ SE ≤ Δ/2）。"""
    if delta_pp <= 0:
        return -1
    return int(round((k * SIGMA_SINGLE / delta_pp) ** 2))


def main() -> int:
    print("== 种子方差 → 验证分辨率刻度 ==")
    print(f"  实测两 seed 年化差      : {DIFF_PP} pp  (F76b)")
    print(f"  推算单 seed 标准差 σ    : {SIGMA_SINGLE:.2f} pp")
    print(f"  分辨准则                : SE = σ/√N ≤ Δ/2\n")
    print(f"  {'目标效应 Δ(pp)':>14} | {'需要 seed 数':>12} | {'2 seed 时 SE(pp)':>16}")
    print("  " + "-" * 50)
    rows = []
    for d in (0.04, 0.1, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0):
        n = seeds_needed(d)
        se2 = SIGMA_SINGLE / (2 ** 0.5)
        rows.append({"delta_pp": d, "seeds_needed": n, "se_at_2_seeds_pp": round(se2, 2)})
        print(f"  {d:>14} | {n:>12} | {se2:>16.2f}")

    print("\n解读：")
    print("  · 现状(2 seed) 的 SE ≈ 2.95pp ⇒ 只能分辨约 3pp 以上的效应；")
    print("  · 提到 4 seed ⇒ SE ≈ 2.09pp ⇒ 可分辨约 2pp；提到 8 seed ⇒ SE ≈ 1.47pp ⇒ 约 1.5pp；")
    print("  · 但候选效应若为 0.04pp(F76b 实测) ⇒ 需要上万 seed ⇒ **任何合理成本都不可行**。")
    print("⇒ 结论：加 seed 能改善分辨率，但**无法**把 0.x pp 级的候选变成可判定；")
    print("   若候选效应 < ~2pp，正确做法不是堆 seed，而是改用配对/分层等降方差设计，或承认不可判定。")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "input_diff_pp": DIFF_PP,
        "sigma_single_pp": round(SIGMA_SINGLE, 3),
        "rows": rows,
        "note": "SE = sigma/sqrt(N); 分辨准则 SE <= delta/2",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n产物:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
