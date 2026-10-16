"""KB 表覆盖检查（2026-09-14，一次性；**只读**）

## 为什么

当天我挖 `alpha_bank`（171 条已测因子）时才发现：**库里有实质证据、知识库从未写**这一类洞
（见 04 号"表清单与 KB 覆盖"）。随后全库点了一遍：**94 张表，KB 只按名提到 46 张，47 张有数据的
表在 KB 里查不到名字**。

本脚本把那次人工点检变成**可复跑**的：列出"有数据但未在 `D:\\know\\*.md` 中按名出现"的表，
供下一次往 KB 04 号登记（或确认它只是实验产物、无需登记）。

## 口径

- 数据侧：`data/neweco.db` 的全部表 + 行数（`sqlite_master`）；
- 文档侧：`D:\\know\\*.md` 全文（只匹配**表名字符串**，不做同义判断）⇒ 命中=按名提过；
- **只读**：不写任何表、不改 KB。

用法：`D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\kb_table_coverage.py [--list-all]`
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from app import db          # noqa: E402

KNOW = pathlib.Path(r"D:\know")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-all", action="store_true", help="也打印已覆盖的表")
    args = ap.parse_args()

    kb = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in sorted(KNOW.glob("*.md")))
    conn = db.get_conn()
    try:
        tabs = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name")]
        rows = []
        for t in tabs:
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception:                      # 视图/损坏表不该把检查带崩
                n = -1
            rows.append((t, n, t in kb))
    finally:
        conn.close()

    covered = [t for t, n, seen in rows if seen]
    missing = [(t, n) for t, n, seen in rows if not seen and n > 0]
    empty = [t for t, n, seen in rows if not seen and n == 0]
    print(f"库内表: {len(rows)} | KB 按名提及: {len(covered)} | "
          f"有数据但未提及: {len(missing)} | 空且未提及: {len(empty)}")
    print("\n有数据但 KB 未按名提及（应逐条登记或确认属实验产物）:")
    for t, n in sorted(missing, key=lambda x: -x[1]):
        print(f"  {t:34s} {n:>10,} 行")
    if args.list_all:
        print("\n已覆盖的表:", ", ".join(sorted(covered)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
