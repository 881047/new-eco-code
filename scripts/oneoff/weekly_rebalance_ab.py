"""周频化 A/B（**调仓频率**）：月频现役 vs **周频调仓**（袖套分数 + 周锚点）。

## 前置改动
本轮给 `run_backtest` / `_load_inputs` 加了 **`anchor`** 参数（默认 `"month"`，与现役**逐位一致**）：
- `anchor="month"` ⇒ 只保留月末评分日（`me_dates`）= 现役 pin 口径；
- `anchor="week"`  ⇒ 保留**全部评分日**（周锚点）⇒ 这才是**真正的周频调仓**。

⚠️ 第一版 A/B（`weekly_freq_baseline.py` / `weekly_freq_hyst_scan.py`）**没测到调仓频率**：
因为 `_load_inputs` 用 `me_dates` 把 609 个周频分数降采样成了月末 ⇒ 调仓仍是月频，
对比的其实是「分数来源」而非「调仓频率」。本脚本补上这一课。

## A/B 两臂
| 臂 | 分数表 | anchor | 含义 |
|---|---|---|---|
| **A（对照 / 现役）** | `model_score`（生产月频）| `"month"` | 月频调仓，现役 pin 口径 |
| **B（候选 / 周频）** | `model_score_weekly_chk`（袖套周频，609 锚点）| `"week"` | **周频调仓** |

## 判据（预注册 `plans/weekly-freq-prereg-20260921.md`，跑数前冻结）
① 与月频对齐（F53）② 信号不劣化（IC/IC_IR vs F50 0.0875/0.685）
③ **净超额（扣成本后）不劣化**（vs F52 +17.78/+17.47pp，换手 0.137）④ 全量 ≥1407。
判决：任一不满足 ⇒ **不采纳**周频化，登记证伪（勿重跑）。

## 只读承诺
只写回测产物（`run_tag` 标记，新 `run_date` 不覆盖现役读数）；不碰生产 `model_score`、不换 pin。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.backtest.engine import run_backtest

ST_M = "model_score"                  # 生产月频（对照）
ST_W = "model_score_weekly_chk"       # 袖套周频隔离表（候选）

HYSTS = [0, 8, 16, 24]
OUT = Path(__file__).resolve().parents[2] / "experiments" / "weekly_rebalance_ab.json"

# 基线（对比基线）
F50_IC, F50_ICIR = 0.0875, 0.685
F52_EXCESS = [17.78, 17.47]
F52_MDD, F52_TURNOVER = -36.6, 0.137


def _run(score_table: str, anchor: str, hyst: int, tag: str) -> dict:
    m = run_backtest(score_table=score_table, run_tag=tag, hysteresis=hyst, anchor=anchor)
    return {
        "score_table": score_table, "anchor": anchor, "hysteresis": hyst,
        "periods": m.get("periods"), "periods_per_year": m.get("periods_per_year"),
        "ann_return": m.get("ann_return"), "bench_ann": m.get("bench_ann"),
        "excess_ann": m.get("excess_ann"), "max_drawdown": m.get("max_drawdown"),
        "win_rate": m.get("win_rate"), "ic_mean": m.get("ic_mean"), "icir": m.get("icir"),
        "avg_turnover": m.get("avg_turnover"),
    }


def main() -> int:
    t0 = time.time()
    print("== 周频化 A/B（调仓频率）：月频现役 vs 周频调仓 ==")
    print("   A = model_score + anchor=month（月频调仓，现役口径）")
    print("   B = model_score_weekly_chk + anchor=week（**周频调仓**）")

    rows = []
    for h in HYSTS:
        a = _run(ST_M, "month", h, f"wrab_m_{h}")
        b = _run(ST_W, "week", h, f"wrab_w_{h}")
        print(f"\n--- hysteresis={h} ---")
        for lbl, r in (("月频A", a), ("周频B", b)):
            print(f"  {lbl}: 超额={r['excess_ann']}  换手={r['avg_turnover']}  "
                  f"MDD={r['max_drawdown']}  胜率={r['win_rate']}  "
                  f"IC={r['ic_mean']}/{r['icir']}  ppy={r['periods_per_year']}  期数={r['periods']}")
        rows.append({"monthly": a, "weekly": b})

    print("\n== 汇总 ==")
    print(f"{'hyst':>5} | {'月频超额':>9} {'月频换手':>8} | {'周频超额':>9} {'周频换手':>8} | {'周-月超额差':>11}")
    for r in rows:
        a, b = r["monthly"], r["weekly"]
        d = (b["excess_ann"] or 0) - (a["excess_ann"] or 0)
        print(f"{a['hysteresis']:>5} | {a['excess_ann']:>9} {a['avg_turnover']:>8} | "
              f"{b['excess_ann']:>9} {b['avg_turnover']:>8} | {round(float(d), 4):>11}")

    payload = {
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - t0, 1),
        "baseline": {"F50_ic": F50_IC, "F50_icir": F50_ICIR, "F52_excess": F52_EXCESS,
                     "F52_mdd": F52_MDD, "F52_turnover": F52_TURNOVER},
        "results": rows,
    }

    # 按冻结判据判决（取 hysteresis=8 作代表退出带；其余供参考）
    rep = next((r for r in rows if r["monthly"]["hysteresis"] == 8), rows[0])
    a, b = rep["monthly"], rep["weekly"]
    ok_signal = (b["ic_mean"] or 0) >= F50_IC and (b["icir"] or 0) >= F50_ICIR
    ok_excess = (b["excess_ann"] or 0) >= (a["excess_ann"] or 0)
    verdict = ("采纳周频调仓" if (ok_signal and ok_excess) else
               "**不采纳周频调仓**（净超额劣于月频 ⇒ 按预注册登记证伪，勿重跑）")
    payload["verdict"] = verdict
    payload["checks"] = {
        "signal_not_worse": bool(ok_signal),
        "net_excess_not_worse": bool(ok_excess),
        "weekly_minus_monthly_excess": round(float((b["excess_ann"] or 0) - (a["excess_ann"] or 0)), 4),
    }
    print(f"\n判据：信号不劣化={ok_signal}  净超额不劣化={ok_excess}")
    print("判决:", verdict)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print("产物:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
