"""周频化 A/B（**先袖套**路径第一步）：用已是周频的袖套分数跑周频回测，建立**周频基线**。

## 背景（2026-09-21 核实，纠正了原计划的错误前提）
- `app/models/trainer.py` L370-373 明确：`horizon` **只改「模型预测多远」，不改「多久调仓」** —— 调仓频率由 `md_all`（月末截面）决定。⇒ **改 `HORIZON=21→5` 不产生周频化**（原计划阶段 1 的技术前提有误）。
- 周频袖套 `app/weekly/scores.py`：`HORIZON=5` + 周锚点（`week_end_dates`）+ `EMBARGO=3` + 651 锚点 ⇒ **已是周频**，是现成的周频信号源。

## 本脚本做什么
1. 读袖套打分存档 `scores_<asof>.npz`（`weekly.scores.latest()`）。
2. 导入**隔离表** `model_score_weekly_chk`（长表 `ts_code/trade_date/score`）—— **不碰生产 `model_score`**。
3. 跑 `run_backtest(score_table=ST, run_tag="weekly_chk")` ⇒ 由于隔离表的 `trade_date` 是**周锚点**，回测的调仓频率 = **周频** ⇒ 得到真正的周频回测指标。
4. 与月频基线对比（F52 生产构型 / F53 月均命中 / F50 周频 IC）⇒ 验证冻结判据：**对齐 / 不劣化 / 净超额（扣成本）**。

## 只读承诺
只写隔离表 + 回测产物（`run_tag="weekly_chk"` 标记，新 `run_date` 不覆盖现役读数）；**不改生产打分、不换 pin、不删任何生产数据**。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app import db
from app.backtest.engine import run_backtest
from app.weekly import scores as W

ST = "model_score_weekly_chk"
TAG = "weekly_chk"
OUT = Path(__file__).resolve().parents[2] / "experiments" / "weekly_freq_baseline.json"

# 月频基线（对比基线；来源 F52 / F53 / F50）
BASE = {
    "F52_excess_ann": [17.78, 17.47],      # 生产构型全史超额 pp（两套种子）
    "F52_mdd": -36.6,                       # MDD %
    "F52_turnover": 0.137,                  # 生产换手（研究构型 0.118）
    "F53_monthly_excess": [1.34, 1.31],     # 全史月均超额 pp
    "F53_holdout_hit": [65.6, 65.6],        # 留出集月命中率 %
    "F50_ic_mean": 0.0875,                  # 周频 IC mean（生产分数+生产掩码，不重训）
    "F50_ic_ir": 0.685,                     # 周频 IC_IR
}


def main() -> int:
    t0 = time.time()
    print("== 周频化 A/B（先袖套）：建立周频基线 ==")

    # 1) 读袖套打分存档
    S, anchors, cols, _names, arc = W.latest()
    print(f"袖套存档: {arc.name}  {S.shape[0]} 锚点 × {S.shape[1]} 只")
    df = pd.DataFrame(S, index=anchors, columns=cols)
    df.index.name = "trade_date"
    # 长表（ts_code, trade_date, score）
    long = (df.stack().rename("score").reset_index())
    long.columns = ["trade_date", "ts_code", "score"]
    long["trade_date"] = pd.to_datetime(long["trade_date"]).dt.strftime("%Y%m%d")
    long = long.dropna(subset=["score"])
    print(f"长表行数: {len(long)}  锚点数: {long['trade_date'].nunique()}")

    # 2) 导入隔离表（不碰生产 model_score）
    conn = db.get_conn()
    try:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {ST} ("
                     "ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL, "
                     "PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID")
        conn.execute(f"DELETE FROM {ST}")
        conn.commit()
        long.assign(pred_ret=np.nan)[["ts_code", "trade_date", "score", "pred_ret"]] \
            .to_sql(ST, conn, if_exists="append", index=False)
        conn.commit()
        n = conn.execute(f"SELECT COUNT(*) FROM {ST}").fetchone()[0]
        print(f"已导入隔离表 {ST}: {n} 行")
    finally:
        conn.close()

    # 3) 跑周频回测（隔离表的 trade_date = 周锚点 ⇒ 调仓 = 周频）
    print("跑回测（周频）…")
    m = run_backtest(score_table=ST, run_tag=TAG)

    payload = {
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - t0, 1),
        "archive": arc.name,
        "score_table": ST,
        "run_tag": TAG,
        "weekly_metrics": {k: m.get(k) for k in (
            "start", "end", "periods", "ann_return", "bench_ann", "excess_ann",
            "max_drawdown", "vol_ann", "sharpe", "win_rate", "ic_mean", "ic_std",
            "icir", "avg_turnover", "mdd_daily", "sys_ann_return", "avg_stocks")},
        "baseline_monthly": BASE,
    }

    print("\n== 周频回测结果 ==")
    for k in ("periods", "ann_return", "bench_ann", "excess_ann", "max_drawdown",
              "win_rate", "ic_mean", "icir", "avg_turnover"):
        print(f"  {k:<14}: {m.get(k)}")
    print("\n== 月频基线（对照）==")
    print(f"  F52 excess_ann   : {BASE['F52_excess_ann']} pp")
    print(f"  F52 MDD          : {BASE['F52_mdd']} %")
    print(f"  F52 turnover     : {BASE['F52_turnover']}")
    print(f"  F50 ic/icir      : {BASE['F50_ic_mean']} / {BASE['F50_ic_ir']}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print("\n产物:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
