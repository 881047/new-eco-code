"""F48 · **用第三方独立实现复核 F38 的 DSR**（2026-09-13，交付前守门）

## 为什么做

F38 判"DSR < 0.95 ⇒ 全史超额不得当显著读数"，这是**挡住对外报数**的那一道闸。
而它是我们自己写的实现 —— 本项目已经吃过"自证"的亏（F6 独立重写复现过另一条线）。
2026-09-13 按用户要求扫 GitHub 时找到 `quantskills/skill-backtest-overfit`
（GPL-3.0，**只作外部工具，代码不进本仓库**），其 DSR/PSR/MinTRL 公式与
Bailey & López de Prado (2014/2012) 一致；本实验**只用它的函数**在 F38 的同一组输入上复算。

## 判据（跑数前冻结）

- 同一输入下两套实现的 `sr0`（每期）与 `DSR` 必须一致到 **|Δ| ≤ 5e-4**（四舍五入误差量级）；
- 若不一致 ⇒ **以谨慎者为准**（更低的 DSR），并登记差异原因；
- 另补 F38 没算的 **MinTRL**（Bailey & López de Prado 2012）：让当前 SR 显著于 0 所需的最少期数。

## 输入

`F38_multiplicity_haircut_result.json`（T / 每期 SR / 偏度 / **非超额**峰度 / 试验间年化方差 / N）。
skill 路径默认 `D:\\codex-home\\skills\\skill-backtest-overfit\\scripts`，可用环境变量
`BACKTEST_OVERFIT_SKILL` 覆盖；**skill 不存在时本实验报"跳过"而不是伪造数字**。

用法：
    D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F48_independent_dsr_check.py
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
F38 = HERE / "F38_multiplicity_haircut_result.json"
OUT = HERE / "F48_independent_dsr_check_result.json"
SKILL = Path(os.environ.get("BACKTEST_OVERFIT_SKILL",
                            r"D:\codex-home\skills\skill-backtest-overfit\scripts"))
PER_YEAR = 52.0            # 与 F38 同为周频口径
TOL = 5e-4


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod        # @dataclass 会去 sys.modules 找 cls.__module__（不注册就 AttributeError）
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    if not F38.exists():
        print(f"找不到 F38 结果: {F38}")
        return 2
    ds = SKILL / "deflated_sharpe.py"
    if not ds.exists():
        print(f"跳过：没装第三方 skill（{SKILL}）—— 不伪造数字。装法见 docs/know/06 号文件。")
        return 3
    dsp = _load("dsp", ds)
    f38 = json.loads(F38.read_text(encoding="utf-8"))

    res = {"source": "F38_multiplicity_haircut_result.json",
           "skill": {"path": str(ds), "license": "GPL-3.0（外部工具，代码未入库）"},
           "tolerance": TOL, "seeds": {}, "verdict": {}}
    worst = 0.0
    for seed, e in f38["seeds"].items():
        sr = float(e["sr_week"])
        T = int(e["T"])
        sk, ku = float(e["skew"]), float(e["kurtosis"])
        var_period = float(e["var_trials_doc_ann"]) / PER_YEAR      # 年化方差 → 每期
        row = {"inputs": {"T": T, "sr_week": sr, "skew": sk, "kurtosis": ku,
                          "var_trials_ann": e["var_trials_doc_ann"]}}
        for tag, n, ref in (("n7", 7, e["dsr_n7"]), ("n25", 25, e["dsr_n25"])):
            sr0 = dsp.expected_max_sharpe(var_period, n)
            dsr = dsp.probabilistic_sharpe_ratio(sr, sr0, T, sk, ku)
            d_sr0 = abs(sr0 - float(ref["sr0_period"]))
            d_dsr = abs(dsr - float(ref["dsr"]))
            worst = max(worst, d_sr0, d_dsr)
            row[tag] = {"sr0_period_skill": round(sr0, 6), "sr0_period_f38": ref["sr0_period"],
                        "sr0_annual_skill": round(sr0 * (PER_YEAR ** 0.5), 3),
                        "dsr_skill": round(dsr, 4), "dsr_f38": ref["dsr"],
                        "delta_sr0": round(d_sr0, 6), "delta_dsr": round(d_dsr, 6)}
        # F38 没算的：让当前 SR 显著于 0（单期基准 0）所需的最少期数
        row["mintrl_vs_zero"] = {
            "observations_needed": round(dsp.minimum_track_record_length(sr, 0.0, sk, ku, 0.95), 1),
            "weeks_have": T,
            "note": "MinTRL > T ⇒ 当前样本长度还不足以让这个 Sharpe 显著于 0（与 DSR 是两问）"}
        res["seeds"][seed] = row

    res["verdict"] = {
        "worst_abs_delta": round(worst, 6),
        "agree_within_tolerance": bool(worst <= TOL),
        "f38_dsr_n7_min": f38["verdict"]["dsr_n7_min"],
        "f38_pass_95": f38["verdict"]["pass_95"],
        "note": ("两套独立实现同输入同结论" if worst <= TOL else
                 "**不一致**：以更低（更谨慎）的 DSR 为准，并登记差异原因")}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"{'seed':<8}{'T':>5}{'SR/周':>9}{'skew':>8}{'kurt':>9}"
          f"{'sr0技能':>10}{'sr0 F38':>10}{'DSR技能':>10}{'DSR F38':>10}{'ΔDSR':>9}")
    for seed, r in res["seeds"].items():
        i = r["inputs"]
        for tag, n in (("n7", 7), ("n25", 25)):
            b = r[tag]
            print(f"{seed + '/' + tag:<8}{i['T']:>5}{i['sr_week']:>9.4f}{i['skew']:>8.2f}"
                  f"{i['kurtosis']:>9.2f}{b['sr0_period_skill']:>10.4f}{b['sr0_period_f38']:>10.4f}"
                  f"{b['dsr_skill']:>10.4f}{b['dsr_f38']:>10.4f}{b['delta_dsr']:>9.5f}")
    print()
    for seed, r in res["seeds"].items():
        m = r["mintrl_vs_zero"]
        print(f"  MinTRL({seed}): 需 {m['observations_needed']:.0f} 期(周), 现有 {m['weeks_have']} 期")
    print(f"\n最大绝对差 = {worst:.6f}  ⇒  {'两套实现一致' if worst <= TOL else '不一致, 取谨慎者'}"
          f"（判据 {TOL}）；写入 {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
