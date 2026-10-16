# -*- coding: utf-8 -*-
"""20260831 中性化截面的源表漂移定位（2026-09-29）。

前置发现（diag_factor_row_drift_0929）：factor 表 20260831 月末行跨日（09-22 备份 →
现库）75% 的行值变（p99 相对差 0.30% —— 真实值变化，非 ulp 级路径差）；窗外月末
零变化。中性化是纯截面操作 ⇒ 源头必是"8/31 截面的某张源表原始行变了"。

09-16 的 diag_data_drift_0916 比过 daily/adj/daily_basic/fina/stock_basic（结论：零修订），
但 **漏了 lhb 族**（龙虎榜，sync_lhb 补缺失日 = 历史行可后补），且那是 09-13→09-21 窗口。
本脚本对 09-22 备份 vs 现库，专查 8/31 截面的全部源表（lhb 窗口含 20 日滚动窗）。
只读。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from app import db
from app.logger import setup_logger

setup_logger()

BACKUP = Path(__file__).resolve().parents[2] / "data" / "backup" / "neweco_20260922.db"
OUT = Path(__file__).resolve().parents[2] / "experiments" / "05_label" / "diag_source_row_drift_0929.json"

# 表 → (主键, 对比列, 日期过滤 SQL 片段[{d}=相关窗口])
QUERIES = {
    "daily": ("ts_code, trade_date", ["open", "high", "low", "close", "pre_close", "vol", "amount"],
              "trade_date IN ('20260831','20260828','20260827')"),
    "adj_factor": ("ts_code, trade_date", ["adj_factor"],
                   "trade_date IN ('20260831','20260828','20260827')"),
    "daily_basic": ("ts_code, trade_date", ["turnover_rate", "pe_ttm", "pb", "total_mv", "circ_mv"],
                    "trade_date IN ('20260831','20260828','20260827')"),
    "lhb": ("ts_code, trade_date", ["net_buy", "net_ratio", "reason"],
            "trade_date >= '20260812' AND trade_date <= '20260831'"),
    "lhb_org": ("ts_code, trade_date", ["org_net_value", "org_net_ratio"],
                "trade_date >= '20260812' AND trade_date <= '20260831'"),
    "fina_indicator": ("ts_code, end_date, ann_date", None,
                       "ann_date <= '20260922'"),   # 8/31 截面可见的全部财报行
    "stock_basic": ("ts_code", ["name", "industry", "list_date", "delist_date"], None),
}


def main():
    if not BACKUP.exists():
        print("备份库不存在:", BACKUP)
        return 2
    conn = db.get_conn()
    try:
        conn.execute("PRAGMA query_only=1")
        conn.execute(f"ATTACH DATABASE '{BACKUP.as_posix()}' AS bak")
        summary = {}
        for tbl, (pk, cols, filt) in QUERIES.items():
            try:
                cur_cols = [r[1] for r in conn.execute(f"PRAGMA main.table_info({tbl})")]
                bak_cols = [r[1] for r in conn.execute(f"PRAGMA bak.table_info({tbl})")]
            except Exception:
                print(f"{tbl}: 表缺失，跳过")
                continue
            common = [c for c in (cols or [c for c in cur_cols if c not in pk.split(", ")])
                      if c in cur_cols and c in bak_cols]
            if not common:
                print(f"{tbl}: 无可比列，跳过")
                continue
            cmp_expr = " OR ".join(f"a.{c} IS NOT b.{c}" for c in common)
            pkj = " AND ".join(f"a.{c}=b.{c}" for c in pk.split(", "))
            # 日期过滤必须带 a. 前缀: JOIN 后两库同名列歧义(首跑实测 ProgrammingError)
            def _prefixed(alias: str) -> str:
                w = filt
                for col in ("trade_date", "ann_date"):
                    w = w.replace(col, f"{alias}.{col}")
                return w
            where_a = f"AND {_prefixed('a')}" if filt else ""
            where_b = f"AND {_prefixed('b')}" if filt else ""
            try:
                chg = pd.read_sql(f"""
                    SELECT COUNT(*) n FROM main.{tbl} a JOIN bak.{tbl} b ON {pkj}
                    WHERE ({cmp_expr}) {where_a}
                """, conn)
                gone = pd.read_sql(f"""
                    SELECT COUNT(*) n FROM bak.{tbl} b WHERE NOT EXISTS (
                        SELECT 1 FROM main.{tbl} a WHERE {pkj}) {where_b}
                """, conn)
                newr = pd.read_sql(f"""
                    SELECT COUNT(*) n FROM main.{tbl} a WHERE NOT EXISTS (
                        SELECT 1 FROM bak.{tbl} b WHERE {pkj}) {where_a}
                """, conn)
            except Exception as e:
                print(f"{tbl}: 查询失败 {type(e).__name__}: {e}")
                continue
            n_chg, n_gone, n_new = int(chg.iloc[0]["n"]), int(gone.iloc[0]["n"]), int(newr.iloc[0]["n"])
            print(f"{tbl:<16} 修订 {n_chg:>8} | 回删 {n_gone:>6} | 新增 {n_new:>6}")
            summary[tbl] = {"changed": n_chg, "deleted": n_gone, "added": n_new}
            if n_chg and tbl == "stock_basic":
                det = pd.read_sql(f"""
                    SELECT a.ts_code, a.name an, b.name bn, a.industry ai, b.industry bi
                    FROM main.{tbl} a JOIN bak.{tbl} b ON {pkj} WHERE ({cmp_expr}) LIMIT 10
                """, conn)
                print(det.to_string())
    finally:
        conn.close()
    OUT.write_text(json.dumps({
        "asof": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "backup": BACKUP.name,
        "question": "20260831 中性化截面 75% 行跨日漂移的源表定位",
        "tables": summary,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n结果已写", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
