"""数据资产台账（只读）：逐个数据源列出行数、时间覆盖、以及**处置结论**。

为什么要它：本项目的核心结论之一是"周频缺口不在新数据"（10 个数据族全部测否）。
这句话要经得起审计，就必须能回答"**库里每一类数据，到底试过没有、结论是什么**"。
本脚本给出**事实**（行数/覆盖），处置结论写在 `plans/weekly-terminal-plan-20260912.md`
的「数据资产台账」一节里（人工维护，改数据源必须同步）。

用法：
    ./.venv/Scripts/python.exe -X utf8 scripts/data_inventory.py            # markdown 表
    ./.venv/Scripts/python.exe -X utf8 scripts/data_inventory.py --json     # 机器可读
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# (表名, 用于报时间覆盖的列) —— 只列**数据类**表；模型分数/回测结果/账户等另见计划文档
SOURCES = [
    ("daily", "trade_date"), ("daily_basic", "trade_date"), ("adj_factor", "trade_date"),
    ("stk_limit", "trade_date"), ("stock_basic", "list_date"), ("namechange", "start_date"),
    ("trade_cal", "cal_date"), ("index_daily", "trade_date"), ("index_weight", "trade_date"),
    ("fina_indicator", "ann_date"), ("moneyflow", "trade_date"), ("margin", "trade_date"),
    ("block_trade", "trade_date"), ("holder_num", "ann_date"), ("hk_hold", "trade_date"),
    ("lhb", "trade_date"), ("lhb_org", "trade_date"), ("lhb_ths", "trade_date"),
    ("lhb_ths_hm", "trade_date"), ("sw_member", "in_date"), ("sw_classify", None),
    ("repo_rate", "trade_date"), ("valuation_pe", "trade_date"),
    # 2026-09-13 新增的 5 个事件族
    ("forecast", "ann_date"), ("restricted_release", "release_date"),
    ("insider_trade", "change_date"), ("repurchase", "start_date"), ("seo_issue", "issue_date"),
]


def inventory(db_path: Path = None) -> list:
    p = db_path or (BASE / "data" / "neweco.db")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    out = []
    try:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, date_col in SOURCES:
            if table not in have:
                out.append({"table": table, "rows": None, "first": None, "last": None,
                            "note": "表不存在"})
                continue
            n = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            first = last = None
            if date_col:
                try:
                    first, last = conn.execute(
                        f'SELECT MIN("{date_col}"), MAX("{date_col}") FROM "{table}"').fetchone()
                except sqlite3.Error:
                    pass
            out.append({"table": table, "rows": int(n), "first": first, "last": last,
                        "note": None})
    finally:
        conn.close()
    return out


def main() -> int:
    rows = inventory()
    if "--json" in sys.argv:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    print("| 表 | 行数 | 覆盖 |")
    print("|---|---|---|")
    for r in rows:
        cov = (f"{r['first']} ~ {r['last']}" if r["first"] else (r["note"] or "—"))
        n = f"{r['rows']:,}" if r["rows"] is not None else "—"
        print(f"| `{r['table']}` | {n} | {cov} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
