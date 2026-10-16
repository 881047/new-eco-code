"""seed 方差**结构**分解（2026-09-21，阶段 2「验证方法改进」续篇）

## 为什么写这个
上一支脚本 `seed_resolution.py` 把 F76b 的「同配置 base_A vs base_B 全窗年化差 5.9pp」
当成**两个独立 seed 的一次抽样差**来反推 σ_single ≈ 5.9/√2 ≈ 4.17pp，并据此建了分辨率表。

本脚本复核它的**两个前提**，并回答「降方差到底该用什么杠杆」：

  前提① **两臂是否独立**？
    生产 `trainer.ENSEMBLE_SEEDS = [42, 7, 123, 2024]`（4 seed 集成，逐期训练 K 个模型取分位排名均值）。
    F76b 里 `base_A` = 生产种子集 {42,7,123,2024}，`base_B` = {7,2024,99,555} ⇒ **共享 2/4 个 seed**。
    共享 ⇒ 两臂**不独立** ⇒ 差值的方差 = 2σ²(1−ρ)，ρ 由共享比例决定（4 取 2 ⇒ ρ≈0.5）
    ⇒ σ_single = 5.9/√(2(1−ρ))，ρ=0 ⇒ 4.17pp；ρ=0.5 ⇒ **5.90pp**。4.17pp 是**乐观下限**。

  前提② **噪声是"持续性"还是"逐期"**？这决定哪个杠杆有效：
    A_s = μ_config + b_s + ē_s
      b_s : seed 集决定的**持续性**分量（各期同向 ⇒ 不随时间平均消失），SD = σ_b
      ē_s : **逐期**噪声的 T 期平均，SD = σ_e/√T —— 这一项**能**被更多期数抹掉
    ⇒ σ_time 由 F76b 的配对逐期差序列直接测（n=114，**测得很准**）
    ⇒ σ_b = √(σ_total² − σ_time²)（σ_total 只有 1 次观测 ⇒ 区间极宽，本脚本如实给出）

## 冻结协议（先写死再算；只读、不重训、不读库、不碰 pin）
- 唯一数据源：`experiments/05_label/F76b_uniqueness_ab_result.json`（已归档，不重算）。
- σ_time := `paired_monthly.sd_pp / √n × 12`（pp/年）；研究层用 `research.per_seed.*.sd_pp / √n × 12`。
- σ_total := `|两臂年化差| / √(2(1−ρ))`，ρ ∈ {0.0, 0.5} 两档并列（不替它拍板）。
- σ_b := √(max(σ_total² − σ_time², 0))。
- 置信区间：σ_total 只有 1 个独立差值 ⇒ (n−1)s²/σ² ~ χ²₁ ⇒ σ 的 95% 区间跨越两个数量级，如实写。
- **不改任何判据**：本脚本只量仪器，不判任何候选。
"""
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "experiments" / "05_label" / "F76b_uniqueness_ab_result.json"
OUT = ROOT / "experiments" / "seed_variance_decomposition.json"

# 生产集成种子数（trainer.ENSEMBLE_SEEDS 长度）
K_NOW = 4
# F76b 两臂种子集：{42,7,123,2024} vs {7,2024,99,555} ⇒ 共享 2/4
SHARED, TOTAL_SEEDS = 2, 4


def main() -> int:
    d = json.loads(SRC.read_text(encoding="utf-8"))
    ps = d["portfolio"]["per_seed"]
    rs = d["research"]["per_seed"]
    runs = {(r["arm"], r["win"]): r for r in d["portfolio"]["runs"]}

    print("== seed 方差结构分解（F76b 现成产物，只读）==")
    print(f"生产集成: K={K_NOW} seed；F76b 两臂共享 {SHARED}/{TOTAL_SEEDS} 个 seed\n")

    # ---------- 1) σ_time：逐期噪声的时间平均分量（n≈114，测得准） ----------
    sig_time_pf, sig_time_rs = {}, {}
    for k, v in ps.items():
        pm = v["paired_monthly"]
        sig_time_pf[k] = pm["sd_pp"] / math.sqrt(pm["n"]) * 12
    for k, v in rs.items():
        f = v["segments"]["full"]
        sig_time_rs[k] = f["sd_pp"] / math.sqrt(f["n"]) * 12
    st_pf = sum(sig_time_pf.values()) / len(sig_time_pf)
    st_rs = sum(sig_time_rs.values()) / len(sig_time_rs)
    # SD 的相对标准误 ≈ 1/√(2(n-1))
    n_pf = ps["seed_A"]["paired_monthly"]["n"]
    rel = 1.0 / math.sqrt(2 * (n_pf - 1))

    print("1) σ_time —— 逐期噪声被 T 期平均后的残差（**能被更多期数抹掉**）")
    for k, v in sig_time_pf.items():
        print(f"   组合层 {k}: {v:.2f} pp/年   (sd={ps[k]['paired_monthly']['sd_pp']:.4f} pp/月, n={n_pf})")
    for k, v in sig_time_rs.items():
        print(f"   研究层 {k}: {v:.2f} pp/年")
    print(f"   ⇒ 组合层 σ_time ≈ {st_pf:.2f} ± {st_pf * rel:.2f} pp/年（±{rel * 100:.1f}%，n={n_pf} ⇒ 测得准）")
    print(f"   ⇒ 研究层 σ_time ≈ {st_rs:.2f} pp/年")
    print(f"   ⇒ **研究层分辨率是组合层的 {st_pf / st_rs:.1f} 倍**（同一候选，先在研究层筛）\n")

    # ---------- 2) σ_total：跨 seed 集离散度（只有 1 次观测） ----------
    diff_bb = (runs[("base_A", "full")]["sys_ann"] - runs[("base_B", "full")]["sys_ann"]) * 100
    diff_uw = ps["seed_A"]["delta_ann_pp"] - ps["seed_B"]["delta_ann_pp"]
    # χ²₁：单差值估计 σ 的 95% 区间
    chi_hi, chi_lo = 5.0239, 0.0009821
    lo_f, hi_f = math.sqrt(1 / chi_hi), math.sqrt(1 / chi_lo)

    print("2) σ_total —— 跨 seed 集离散度（**只有 1 个独立差值**，区间必然极宽）")
    print(f"   base_A − base_B 年化差 = {diff_bb:.2f} pp（同配置、不同种子集）")
    print(f"   uw−base 效应跨种子集差 = {diff_uw:.2f} pp")
    tot = {}
    for rho in (0.0, 0.5):
        s_bb = abs(diff_bb) / math.sqrt(2 * (1 - rho))
        s_uw = abs(diff_uw) / math.sqrt(2 * (1 - rho))
        tot[rho] = {"from_base_pair": round(s_bb, 2), "from_uw_effect": round(s_uw, 2)}
        print(f"   ρ={rho}: σ_total ≈ {s_bb:.2f} pp（base 对） / {s_uw:.2f} pp（uw 效应）")
    print(f"   ⇒ 95% 区间（χ²₁, 1 个差值）: σ ∈ [{lo_f:.2f}×, {hi_f:.1f}×] 点估计")
    print("   ⇒ 上一支脚本取的 4.17pp 是 ρ=0 的**乐观下限**；ρ=0.5（实际共享 2/4 seed）⇒ 5.90pp\n")

    # ---------- 3) σ_b：持续性分量（不能被期数抹掉） ----------
    print("3) σ_b —— 持续性分量（**加期数抹不掉**，只能靠 K 或 N）")
    decomp = {}
    for rho in (0.0, 0.5):
        row = {}
        for name, s_tot in (("base对", tot[rho]["from_base_pair"]), ("uw效应", tot[rho]["from_uw_effect"])):
            s_b = math.sqrt(max(s_tot ** 2 - st_pf ** 2, 0.0))
            share = s_b ** 2 / (s_tot ** 2) if s_tot > 0 else 0.0
            row[name] = {"sigma_total": s_tot, "sigma_b": round(s_b, 2), "b_share_of_var": round(share, 3)}
            print(f"   ρ={rho} {name}: σ_total={s_tot:.2f} ⇒ σ_b={s_b:.2f} pp（占方差 {share * 100:.0f}%）")
        decomp[str(rho)] = row
    print("   ⇒ 持续性分量占方差 **55~70%** ⇒ 主导项。配对/加期数只能削掉剩下那 30~45%\n")

    # ---------- 4) 三个杠杆的实际分辨率 ----------
    print("4) 三个杠杆能把分辨率提到多少（Δ_min = 2·SE，沿用 seed_resolution.py 的准则）")
    s_tot_ref = tot[0.5]["from_base_pair"]   # ρ=0.5，实测共享 seed 的最现实口径
    s_b_ref = math.sqrt(max(s_tot_ref ** 2 - st_pf ** 2, 0.0))

    lev = {"lever_T": [], "lever_K": [], "lever_N": []}
    print("   ① 加期数 T（只削 σ_time；σ_b 不动）")
    for T in (114, 491, 608):
        s = math.sqrt(s_b_ref ** 2 + st_pf ** 2 * 114 / T)
        lev["lever_T"].append({"T": T, "sigma_pp": round(s, 2), "delta_min_pp": round(2 * s, 2)})
        print(f"      T={T:>4}: σ={s:.2f} pp ⇒ 可辨 Δ ≥ {2 * s:.2f} pp")
    print("   ② 加集成 seed 数 K（σ ∝ 1/√K，**同时改善模型本身**，不只是测量）")
    for K in (4, 8, 16, 32):
        s = s_tot_ref * math.sqrt(K_NOW / K)
        lev["lever_K"].append({"K": K, "sigma_pp": round(s, 2), "delta_min_pp": round(2 * s, 2)})
        print(f"      K={K:>3}: σ={s:.2f} pp ⇒ 可辨 Δ ≥ {2 * s:.2f} pp")
    print("   ③ 独立重复 N 次取平均（SE = σ/√N，只改善测量，**不改善模型**）")
    for N in (1, 2, 4, 8):
        s = s_tot_ref / math.sqrt(N)
        lev["lever_N"].append({"N": N, "sigma_pp": round(s, 2), "delta_min_pp": round(2 * s, 2)})
        print(f"      N={N:>3}: SE={s:.2f} pp ⇒ 可辨 Δ ≥ {2 * s:.2f} pp")

    print("\n   对照：上一支脚本（σ=4.17pp）给的 2 seed ⇒ 可辨 ~2.95pp / 4 seed ⇒ ~2.09pp")
    print("   本脚本（ρ=0.5 ⇒ σ=5.90pp）: 1 次 ⇒ 可辨 ~11.8pp，8 次独立重复 ⇒ ~4.2pp")
    print("   ⇒ **上一支的分辨率表偏乐观约 2 倍**，但两者都受制于同一个问题：σ 只有 1 次观测。")

    # ---------- 5) 对既有「双 seed 配对 t 检验」的含义 ----------
    exp_spread = math.sqrt(2) * st_pf          # 若持续性分量被同种子集配对抵消 ⇒ 期望离散
    obs_spread = abs(diff_uw)                  # uw−base 效应跨两套种子集的实测离散
    ratio = obs_spread / exp_spread if exp_spread else float("nan")
    print("\n5) 对既有「双 seed 配对 t 检验」的含义（**本脚本最要紧的一条**）")
    print("   F76b 的配对 t 用的是**逐期序列**标准误（σ_time ≈ 2.85pp）——它只计入时间抽样，")
    print("   把 seed 抽样分量当成「固定的未知均值」。若持续性分量没被同种子集配对抵消，")
    print("   真实的 SE 更大 ⇒ **t 被系统性高估**。")
    print(f"   若抵消干净 ⇒ 两套估计的期望离散 = √2·σ_time = {exp_spread:.2f} pp")
    print(f"   实测离散 = {obs_spread:.2f} pp ⇒ 比值 {ratio:.2f}×")
    print(f"   ⇒ 提示 t 最多被高估 {ratio:.2f}×（F76b seed_B 的 t=1.894 ⇒ 约 {1.894 / ratio:.2f}）")
    print("   ⇒ **n=2 定不了论**；但凡是 t 贴近门限的判决都有翻档风险 ⇒ 需标定 + 回头复核台账")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "source": str(SRC.relative_to(ROOT)).replace("\\", "/"),
        "assumptions": {
            "K_now": K_NOW, "shared_seeds": SHARED, "rho_if_shared": SHARED / TOTAL_SEEDS,
            "model": "A_s = mu + b_s(sigma_b, 持续) + ebar_s(sigma_e/sqrt(T), 逐期)",
            "sigma_time_is_well_measured": f"n={n_pf}, 相对标准误 ±{rel * 100:.1f}%",
            "sigma_total_is_one_observation": f"95%CI = [{lo_f:.2f}x, {hi_f:.1f}x] 点估计 (chi2_1)",
        },
        "sigma_time_pp": {"portfolio": {k: round(v, 2) for k, v in sig_time_pf.items()},
                          "portfolio_mean": round(st_pf, 2),
                          "research": {k: round(v, 2) for k, v in sig_time_rs.items()},
                          "research_mean": round(st_rs, 2),
                          "research_over_portfolio_gain": round(st_pf / st_rs, 2)},
        "sigma_total_pp": tot,
        "sigma_b_pp": decomp,
        "levers": lev,
        "paired_t_deflation": {
            "expected_spread_pp": round(exp_spread, 2),
            "observed_spread_pp": round(obs_spread, 2),
            "ratio": round(ratio, 2),
            "f76b_seedB_t_reported": 1.894,
            "f76b_seedB_t_adjusted": round(1.894 / ratio, 2),
            "note": "双 seed 配对 t 只计入时间抽样；若持续性分量未抵消 ⇒ t 高估至多 ratio 倍。n=2 定不了论。",
        },
        "caveat": "σ_total 仅 1 个独立差值 ⇒ 区间跨两个数量级；σ_time 由 n=114 测得准，是 σ 的硬下界。",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n产物:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
