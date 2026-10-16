"""数据修订源定位 —— 09-13 周备份 vs 当前库, 找出漂移的修订到底在哪张表(2026-09-16)

背景（复核请求：把漂移诊断从「机制推断」升级到「根因定位」）
----------------------------------------------------------
verify_tm36_retrain / exp_holdout_recent 的诊断说：09-14/15/16 三次日更修订了历史因子面板，
导致全量重训不可跨日复现。本脚本用 09-13 08:43 的周备份(neweco_20260913.db, 含 09-11 收盘
数据)与当前库做**逐表历史行对比**，回答"到底哪张表、哪些日期的行变了"。

只比**会进月度因子面板的表**：daily / adj_factor / daily_basic / fina_indicator /
stock_basic / index_daily / trade_cal / stk_limit(若存在)。关键口径：
  · 只统计 trade_date(或报告期/公告日) **< 20260914** 的历史行 —— 09-14 之后的新增行是
    正常增量，不算修订；
  · "修订" = 同一主键下任一对比列的值不同，或镜像侧行在备份侧消失(回删)。
只读：当前库只读查询；备份库 ATTACH 只读(file:...?mode=ro)。不写任何表。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from app import db
from app.logger import setup_logger

setup_logger()

import os
BACKUP = Path(os.environ.get("DRIFT_BACKUP") or (Path(__file__).resolve().parents[2] / "data" / "backup" / "neweco_20260913.db"))
CUT = os.environ.get("DRIFT_CUT", "20260914")          # 此日期之后的行是正常增量，不算修订

# 表 → (主键列, 对比列[None=全部共有的非主键列], 日期列)
TABLES = {
    "daily":          ("ts_code, trade_date", ["close", "vol", "amount"], "trade_date"),
    "adj_factor":     ("ts_code, trade_date", ["adj_factor"], "trade_date"),
    "daily_basic":    ("ts_code, trade_date", ["close", "turnover_rate", "pe", "pb", "total_mv"], "trade_date"),
    "fina_indicator": ("ts_code, end_date, ann_date", None, "ann_date"),
    "stock_basic":    ("ts_code", ["name", "industry", "list_date", "delist_date"], None),
    "index_daily":    ("ts_code, trade_date", ["close"], "trade_date"),
    "trade_cal":      ("cal_date", ["is_open", "pretrade_date"], "cal_date"),
    "stk_limit":      ("ts_code, trade_date", ["up_limit", "down_limit"], "trade_date"),
}


def _cols_of(conn, table, alias="main"):
    # PRAGMA 不吃 "alias.table" 限定名 —— 限定名放在 pragma 关键字上: PRAGMA bak.table_info(x)
    # 返回列名列表（cursor 行的第 2 列是 name）
    return [r[1] for r in conn.execute(f"PRAGMA {alias}.table_info({table})").fetchall()]


def main():
    if not BACKUP.exists():
        print("备份库不存在:", BACKUP)
        return 2
    conn = db.get_conn()
    try:
        conn.execute("PRAGMA query_only=1")     # 整连接禁写：当前库与备份库都只许读
        conn.execute(f"ATTACH DATABASE '{BACKUP.as_posix()}' AS bak")
        print(f"{'表':<16} {'历史行修订':>10} {'历史行回删':>10} {'新增(正常)':>10}  主要变动日期(前5)")
        print("-" * 100)
        summary = {}
        for tbl, (pk, cols, date_col) in TABLES.items():
            try:
                cur_cols = _cols_of(conn, tbl, "main")
                bak_cols = _cols_of(conn, tbl, "bak")
            except Exception:
                print(f"{tbl:<16} 表缺失，跳过")
                continue
            common = [c for c in (cols or [c for c in cur_cols if c not in pk.split(", ")])
                      if c in cur_cols and c in bak_cols]
            if not common:
                print(f"{tbl:<16} 无可比列，跳过")
                continue
            # 括号必须有：否则 OR/AND 优先级让日期过滤只约束最后一个 OR 项（2026-09-16 复核修）
            cmp_expr = "(" + " OR ".join(
                f"NOT (a.{c} IS NOT DISTINCT FROM b.{c})" for c in common) + ")"
            where = f"WHERE a.{date_col} < '{CUT}'" if date_col else ""
            try:
                chg = pd.read_sql(f"""
                    SELECT COUNT(*) n FROM main.{tbl} a JOIN bak.{tbl} b ON {pk_join(pk)}
                    WHERE {cmp_expr} {and_where(where)}
                """, conn)
                gone = pd.read_sql(f"""
                    SELECT COUNT(*) n FROM bak.{tbl} b
                    WHERE NOT EXISTS (SELECT 1 FROM main.{tbl} a WHERE {pk_exists(pk)})
                    {gone_date_filter(date_col, CUT)}
                """, conn)
                new = pd.read_sql(f"""
                    SELECT COUNT(*) n FROM main.{tbl} a
                    WHERE a.{date_col} >= '{CUT}'
                """, conn) if date_col else pd.DataFrame({"n": [0]})
                top = pd.read_sql(f"""
                    SELECT a.{date_col} d, COUNT(*) n FROM main.{tbl} a JOIN bak.{tbl} b ON {pk_join(pk)}
                    WHERE {cmp_expr} {and_where(where)}
                    GROUP BY a.{date_col} ORDER BY n DESC LIMIT 5
                """, conn) if date_col else pd.DataFrame()
            except Exception as e:
                print(f"{tbl:<16} 查询失败: {type(e).__name__}: {e}")
                continue
            n_chg, n_gone = int(chg.iloc[0]["n"]), int(gone.iloc[0]["n"])
            n_new = int(new.iloc[0]["n"]) if not new.empty else 0
            tops = ", ".join(f"{r['d']}({r['n']})" for _, r in top.iterrows()) or "-"
            print(f"{tbl:<16} {n_chg:>10} {n_gone:>10} {n_new:>10}  {tops}")
            summary[tbl] = {"changed": n_chg, "deleted": n_gone, "new_normal": n_new,
                            "top_dates": [{"date": str(r["d"]), "n": int(r["n"])} for _, r in top.iterrows()]}
    finally:
        conn.close()

    out = Path(__file__).resolve().parents[2] / "experiments" / "05_label" / "diag_data_drift_0916_result.json"
    out.write_text(json.dumps({
        "date": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "baseline_backup": str(BACKUP.name), "cutoff": CUT,
        "note": "只统计日期 < cutoff 的历史行；新增行属正常增量。修订行=同主键值不同；回删=备份有而今无。",
        "tables": summary,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n结果已写", out)
    return 0


def pk_join(pk):
    return " AND ".join(f"a.{c}=b.{c}" for c in pk.split(", "))


def pk_exists(pk):
    return " AND ".join(f"a.{c}=b.{c}" for c in pk.split(", "))


def and_where(where):
    return (" AND " + where[len("WHERE "):]) if where else ""


def gone_date_filter(date_col, cut):
    return f"AND b.{date_col} < '{cut}'" if date_col else ""


if __name__ == "__main__":
    sys.exit(main())
