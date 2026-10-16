"""周频化 A/B：**退出带（hysteresis）扫描** —— 让周频与月频在**同一退出带**下公平对比。

## 为什么需要这一步
首次周频回测（`weekly_freq_baseline.py`，`hysteresis_buffer=0` 无退出带）得到：
  IC 0.0951 / ICIR 0.787（**优于** F50 的 0.0875 / 0.685）⇒ 信号确实变好；
  但换手 **0.9258**、超额 **−6.54%** ⇒ 被交易成本吞噬。

但 `config.yaml` L72 `hysteresis_buffer: 0`（回测默认无退出带），而实盘袖套走 `pick_shape` 的
`band`（1e6 档 = **8.0** 退出带倍数，SHAPES L47-53）⇒ **首次回测构型与实盘不一致，换手被高估**。

⇒ 本脚本：对**同一 hysteresis** 分别跑月频（生产 `model_score`）与周频（隔离表 `model_score_weekly_chk`），
   扫描多个退出带 ⇒ 看两者在**同退出带**下的 换手 / 超额 / MDD / 胜率 ⇒ 判断是否"周频成本 > 周频收益"。

## 只读承诺
只写回测产物（`run_tag` 标记，新 run_date 不覆盖现役读数）与隔离表；不碰生产 `model_score`、不换 pin。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.backtest.engine import run_backtest

ST_W = "model_score_weekly_chk"      # 周频隔离表（由 weekly_freq_baseline.py 导入）
ST_M = "model_score"                  # 生产月频（对照，**只读不写**）

HYSTS = [0, 8, 16, 24]
OUT = Path(__file__).resolve().parents[2] / "experiments" / "weekly_freq_hyst_scan.json"


def _one(score_table: str, hyst: int, tag: str) -> dict:
    m = run_backtest(score_table=score_table, run_tag=tag, hysteresis=hyst)
    return {
        "hysteresis": hyst,
        "periods": m.get("periods"),
        "ann_return": m.get("ann_return"),
        "bench_ann": m.get("bench_ann"),
        "excess_ann": m.get("excess_ann"),
        "max_drawdown": m.get("max_drawdown"),
        "win_rate": m.get("win_rate"),
        "ic_mean": m.get("ic_mean"),
        "icir": m.get("icir"),
        "avg_turnover": m.get("avg_turnover"),
        "periods_per_year": m.get("periods_per_year"),
    }


def main() -> int:
    t0 = time.time()
    print("== 周频化 A/B：退出带扫描（月频 vs 周频，同一 hysteresis）==")

    rows = []
    for h in HYSTS:
        print(f"\n--- hysteresis={h} ---")
        m = _one(ST_M, h, f"hysthscan_m_{h}")
        print(f"  月频: 超额={m['excess_ann']}  换手={m['avg_turnover']}  MDD={m['max_drawdown']}  "
              f"胜率={m['win_rate']}  IC={m['ic_mean']}/{m['icir']}  ppy={m['periods_per_year']}")
        w = _one(ST_W, h, f"hysthscan_w_{h}")
        print(f"  周频: 超额={w['excess_ann']}  换手={w['avg_turnover']}  MDD={w['max_drawdown']}  "
              f"胜率={w['win_rate']}  IC={w['ic_mean']}/{w['icir']}  ppy={w['periods_per_year']}")
        rows.append({"monthly": m, "weekly": w})

    payload = {
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - t0, 1),
        "hysteresis_scanned": HYSTS,
        "results": rows,
    }

    print("\n== 汇总（超额% / 换手）==")
    print(f"{'hyst':>5} | {'月频超额':>10} {'月频换手':>9} | {'周频超额':>10} {'周频换手':>9}")
    for r in rows:
        m, w = r["monthly"], r["weekly"]
        print(f"{m['hysteresis']:>5} | {m['excess_ann']:>10} {m['avg_turnover']:>9} | "
              f"{w['excess_ann']:>10} {w['avg_turnover']:>9}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print("\n产物:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
