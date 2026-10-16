# -*- coding: utf-8 -*-
"""factor 表月末行跨日漂移诊断 —— "日频链覆盖月末行"假说的直接检验（2026-09-29）。

背景（R5：全量重训跨日不可复现，09-16 diag 只比了原始行情表并推翻"行情修订"假说，
但 **factor 表本身从未被对比过**）：

月度链(compute_all)每月写月末行（全史整块中性化路径）；日频链(daily_engine)每天
对"最近 30 个交易日"的每一天 upsert factor 行 —— **包括月末日**（逐日单行中性化
路径）。两条路径"当前形状逐位相同"是数据形状碰巧不是不变量（neutralize docstring
实测：列集/行数变化 ⇒ 1~2 ulp 漂移）。若两路径有差异，月末行的值 = "谁最后写"，
且月度链增量从不重写旧月末 ⇒ 漂移永久化 ⇒ 跨日重训读到的特征不同 ⇒ 分数不可复现。

本脚本用 09-22 周备份(第一现场) vs 当前库，对比 factor 表**月末行**（≤20260928 的
已完结月末，含 20260731[>30 日窗外,应稳定] 与 20260831[30 日窗内,日频链天天覆盖]）：
  · 20260831 变而 20260731 不变 ⇒ 日频覆盖漂移实锤（假说成立）;
  · 两者都不变 ⇒ factor 漂移排除，转向 LightGBM 确定性 / 其他输入。
只读（ATTACH ro + query_only）。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd
import numpy as np

from app import db
from app.logger import setup_logger

setup_logger()

BACKUP = Path(__file__).resolve().parents[2] / "data" / "backup" / "neweco_20260922.db"
OUT = Path(__file__).resolve().parents[2] / "experiments" / "05_label" / "diag_factor_row_drift_0929.json"

# 已完结月末（评估窗内; 20260831 在日频链 30 日窗内, 0731/0630 在窗外作对照）
MONTH_ENDS = ("20260831", "20260731", "20260630", "20260529")


def main():
    if not BACKUP.exists():
        print("备份库不存在:", BACKUP)
        return 2
    conn = db.get_conn()
    try:
        conn.execute("PRAGMA query_only=1")
        conn.execute(f"ATTACH DATABASE '{BACKUP.as_posix()}' AS bak")
        summary = {}
        for md in MONTH_ENDS:
            cur = pd.read_sql(
                "SELECT ts_code, factor, value FROM main.factor WHERE trade_date=?",
                conn, params=(md,))
            bak = pd.read_sql(
                "SELECT ts_code, factor, value FROM bak.factor WHERE trade_date=?",
                conn, params=(md,))
            merged = cur.merge(bak, on=["ts_code", "factor"], how="outer",
                               suffixes=("_cur", "_bak"), indicator=True)
            n_cur_only = int((merged["_merge"] == "left_only").sum())
            n_bak_only = int((merged["_merge"] == "right_only").sum())
            both = merged[merged["_merge"] == "both"].copy()
            both["diff"] = ~(both["value_cur"].to_numpy() == both["value_bak"].to_numpy())
            by_factor = (both[both["diff"]].groupby("factor").size()
                         .sort_values(ascending=False).head(10))
            # 浮点: 完全相等才算"未漂移"; 另报 ulp 级差异统计
            a = both["value_cur"].to_numpy(dtype=float)
            b = both["value_bak"].to_numpy(dtype=float)
            finite = np.isfinite(a) & np.isfinite(b)
            exact = int((a[finite] == b[finite]).sum())
            n_fin = int(finite.sum())
            rel = (abs(a[finite] - b[finite]) /
                   np.maximum(np.abs(b[finite]), 1e-12))
            summary[md] = {
                "cur_rows": len(cur), "bak_rows": len(bak),
                "n_cur_only": n_cur_only, "n_bak_only": n_bak_only,
                "n_value_diff": int(both["diff"].sum()),
                "n_exact": exact, "n_finite": n_fin,
                "max_rel_diff": float(np.nanmax(rel)) if n_fin else None,
                "p99_rel_diff": float(np.nanpercentile(rel, 99)) if n_fin else None,
                "top_factors": {str(k): int(v) for k, v in by_factor.items()},
            }
            print(f"{md}: 行 cur={len(cur)} bak={len(bak)} | 值不同 {int(both['diff'].sum())} "
                  f"(max_rel={summary[md]['max_rel_diff']}) | 仅cur {n_cur_only} 仅bak {n_bak_only}")
            if len(by_factor):
                print("   top:", dict(by_factor))
    finally:
        conn.close()
    OUT.write_text(json.dumps({
        "asof": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "backup": BACKUP.name,
        "hypothesis": "daily_engine 覆盖月末行(逐日单行中性化路径) vs 月度链(整块路径) 漂移",
        "reading": "20260831 变而 20260731/0630 不变 ⇒ 假说成立; 全不变 ⇒ factor 漂移排除",
        "month_ends": summary,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n结果已写", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
