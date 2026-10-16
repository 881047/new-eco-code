"""F53 · **月度口径**的生产构型 vs 研究构型（2026-09-13）

## 为什么做

用户的目标函数是"**月**收益超额"。而唯一那份月度读数（F45，转引 F36 的聚合函数）走的是
`f29._run_series`，其 `HYST=0.2` —— 与 F25/F41 同源，**不是生产在跑的构型**（生产 `hold_bonus=0.0`，见 F52）。
F52 已经用两套种子量出：全史年化超额差 0.17~0.22pp（容差内），但**换手差 ~17%、留出集差 ~1pp**。
月度是用户真正看的口径 ⇒ 必须单独给一份**生产构型**的月度读数，不能拿研究构型顶上。

## 做法（不重跑模拟器、不写第二份口径）

F52 已把两构型 × 两套种子的**逐期序列**落盘（date/ret/bench_ret/turnover/invested_ratio/n_positions）。
本实验只做聚合：直接复用 `F36.to_monthly` / `F36.stats` / `F36.WINDOWS`（与 F36/F45 逐字同口径），
对四个序列各算一遍月度统计，并排贴出 F45 记录的研究构型读数。

## 判据（跑数前冻结）

- 只做**同口径并排**，不产生新的显著性证据（留出集仍是 F28 已开封那段）；
- 结论写明：**生产构型**的 OOS 月均 = 多少、是否仍在 +0.83pp/月（≈+10pp/年）之上，
  以及"月样本量不足以分辨"这条不变。

用法：
    D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F53_monthly_live_vs_research_shape.py
"""
import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent
sys.path.insert(0, str(BASE))
OUT = HERE / "F53_monthly_live_vs_research_shape_result.json"
ARMS = {"A0_生产构型(hyst=0.0)": "A0", "A1_研究构型(hyst=0.2)": "A1"}
SEEDS = ("seedA", "seedB")
F45_QUOTED = {"full": {"mean_pp": [1.35, 1.30]},
              "oos": {"mean_pp": [0.84, 0.93]},
              "source": "F45（研究构型 hyst=0.2）"}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    f36 = _load("f36", HERE / "F36_monthly_excess_distribution.py")
    res = {"question": "生产构型(hold_bonus=0.0) 的月度读数 vs 研究构型(hyst=0.2)",
           "windows": f36.WINDOWS, "results": {}, "f45_quoted": F45_QUOTED, "verdict": {}}
    for arm, tag in ARMS.items():
        res["results"][arm] = {}
        for seed in SEEDS:
            p = HERE / f"F52_series_{seed}_{tag}.json"
            if not p.exists():
                print(f"缺 {p.name}，先跑 F52 --seed AB")
                return 2
            d = pd.read_json(p)
            g = f36.to_monthly(d)
            res["results"][arm][seed] = {
                w: f36.stats(g[(g.index >= pd.Period(lo[:7], "M"))
                               & (g.index <= pd.Period(hi[:7], "M"))])
                for w, (lo, hi) in f36.WINDOWS.items()}

    a0, a1 = res["results"]["A0_生产构型(hyst=0.0)"], res["results"]["A1_研究构型(hyst=0.2)"]
    oos0 = [a0[s]["OOS_32m"]["mean_pp"] for s in SEEDS]
    oos1 = [a1[s]["OOS_32m"]["mean_pp"] for s in SEEDS]
    full0 = [a0[s]["FULL"]["mean_pp"] for s in SEEDS]
    full1 = [a1[s]["FULL"]["mean_pp"] for s in SEEDS]
    mean_oos0 = round(sum(oos0) / len(oos0), 3)
    res["verdict"] = {
        "live_full_mean_pp": round(sum(full0) / len(full0), 3),
        "research_full_mean_pp": round(sum(full1) / len(full1), 3),
        "live_oos_mean_pp": mean_oos0,
        "research_oos_mean_pp": round(sum(oos1) / len(oos1), 3),
        "live_oos_per_seed": oos0,
        "above_c3_monthly_line(0.83pp)": bool(mean_oos0 >= 0.83),
        "note": "留出集是 F28 已开封那 32 个月 ⇒ 只作方向；月样本量远不足以分辨 +0.83pp/月（约需 70 个月）",
    }
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"{'构型':<26}{'FULL 月均':>12}{'OOS 月均':>12}{'OOS 命中':>10}")
    for arm in res["results"]:
        f = res["results"][arm]
        fu = [f[s]["FULL"]["mean_pp"] for s in SEEDS]
        oo = [f[s]["OOS_32m"]["mean_pp"] for s in SEEDS]
        hit = [f[s]["OOS_32m"]["hit"] for s in SEEDS]
        print(f"{arm:<26}{str(fu):>12}{str(oo):>12}{str(hit):>10}")
    print(f"\nF45 记录（研究构型）: full {F45_QUOTED['full']['mean_pp']} | oos {F45_QUOTED['oos']['mean_pp']}")
    print(f"生产构型 OOS 月均均值 {mean_oos0}pp ⇒ "
          f"{'仍在 +0.83pp/月之上' if res['verdict']['above_c3_monthly_line(0.83pp)'] else '**低于 +0.83pp/月**'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
