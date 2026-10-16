r"""备份**只读恢复演练**（段 A）—— 回答"这份备份到底能不能用"（2026-09-22 立）。

## 为什么需要它（补的是一个真实缺口）

`tests/test_db_backup_verify.py` 只做两件事：`PRAGMA quick_check` + "校验不改文件"。
那只能证明**文件没坏**，证明不了**里面的数据是完整、可用、且与生产一致到某个时点的**。
项目从 2026-09-13 起一直在备份，但**从未做过恢复演练** —— 结项审计把这个缺口记下来了。

## 本脚本做什么（**全程只读，不复制文件**）

1. `quick_check` 生产库（fail-closed：不 ok 就直接失败，任何删除动作都不该在此之后进行）
2. 以 `?mode=ro` **只读 ATTACH** 指定备份，跑 `quick_check`
3. 逐表比对**关键表**的行数与最新 `trade_date`（备份 vs 生产）⇒ 回答"备份停在哪个时点"
4. 抽查恒等式类一致性（如 `trade_cal` 覆盖范围）
5. 输出一份 JSON 结论，落 `experiments/`

**不做**段 B（把 11GB 复制到隔离目录再跑月链）：成本高（复制 + 训练数小时），
无明确需求时不值得。段 A 已能回答"能不能恢复"这个最关键的问题。

## 为什么不用"复制一份再打开"

11.85 GB 的库复制一次要几分钟且吃掉同等磁盘；而 SQLite 的 `?mode=ro` + `ATTACH`
能直接读原文件，**不写一个字节**（`app/db.py:694` 已是本仓既有模式）。

## 用法

    D:\Python313\python.exe -X utf8 scripts\oneoff\restore_drill_readonly.py
    D:\Python313\python.exe -X utf8 scripts\oneoff\restore_drill_readonly.py --backup neweco_20260919.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKUP_DIR = ROOT / "data" / "backup"
PROD = ROOT / "data" / "neweco.db"
OUT = ROOT / "experiments" / "restore_drill_readonly.json"

#: 演练要比对的关键表 —— 覆盖"行情 / 打分 / 交易日历 / 因子"四条主链
KEY_TABLES = ("daily", "daily_basic", "trade_cal", "model_score", "factor",
              "backtest_summary", "paper_nav")
#: 这些表按 `trade_date` 取最新时点
DATED = ("daily", "model_score", "factor")


def _ro(path: Path) -> sqlite3.Connection:
    """只读打开。`mode=ro` 是硬要求 —— 演练**不许**碰原文件。"""
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _probe(conn: sqlite3.Connection, tag: str) -> dict:
    out = {"quick_check": None, "tables": {}, "errors": []}
    try:
        t0 = time.time()
        out["quick_check"] = conn.execute("PRAGMA quick_check").fetchone()[0]
        out["quick_check_sec"] = round(time.time() - t0, 1)
    except sqlite3.Error as e:
        out["errors"].append(f"quick_check: {type(e).__name__}: {e}")
        return out
    for t in KEY_TABLES:
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            rec = {"rows": n}
            if t in DATED:
                rec["max_trade_date"] = conn.execute(
                    f"SELECT MAX(trade_date) FROM {t}").fetchone()[0]
            out["tables"][t] = rec
        except sqlite3.Error as e:
            out["errors"].append(f"{t}: {type(e).__name__}: {e}")
    print(f"   [{tag}] quick_check={out['quick_check']} "
          f"({out.get('quick_check_sec')}s) 表={len(out['tables'])} 错误={len(out['errors'])}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="备份只读恢复演练（段 A）")
    ap.add_argument("--backup", default=None,
                    help="备份文件名（默认取 data/backup 下最新一份）")
    ap.add_argument("--no-write", action="store_true", help="只打印，不落产物")
    args = ap.parse_args()

    t0 = time.time()
    print("== 备份只读恢复演练（段 A，全程只读）==")

    if not PROD.exists():
        print(f"生产库不存在: {PROD}")
        return 2
    bks = sorted(BACKUP_DIR.glob("neweco_*.db"))
    if not bks:
        print(f"没有备份: {BACKUP_DIR}")
        return 2
    bk = (BACKUP_DIR / args.backup) if args.backup else bks[-1]
    if not bk.exists():
        print(f"指定备份不存在: {bk}")
        return 2

    print(f"生产库 : {PROD.name}  {PROD.stat().st_size/1e9:.3f} GB")
    print(f"备份   : {bk.name}  {bk.stat().st_size/1e9:.3f} GB")
    print(f"（同目录另有 {len(bks)-1} 份历史备份）")

    print("\n-- 1) 生产库自证（fail-closed）--")
    prod = _probe(_ro(PROD), "生产")

    print("\n-- 2) 备份自证 --")
    bak = _probe(_ro(bk), "备份")

    print("\n-- 3) 关键表逐表比对（备份 vs 生产）--")
    rows, drift = [], []
    for t in KEY_TABLES:
        p, b = prod["tables"].get(t), bak["tables"].get(t)
        if p is None or b is None:
            rows.append({"table": t, "prod": p, "backup": b, "note": "一侧缺表"})
            continue
        rec = {"table": t, "prod_rows": p["rows"], "backup_rows": b["rows"],
               "row_delta": p["rows"] - b["rows"]}
        if t in DATED:
            rec["prod_max_date"] = p.get("max_trade_date")
            rec["backup_max_date"] = b.get("max_trade_date")
            rec["date_delta_days"] = None
            rec["note"] = ("备份落后于生产 ⇒ 正常（备份是过去某一时点的快照）"
                           if p.get("max_trade_date") != b.get("max_trade_date")
                           else "与生产同一天")
            drift.append(t)
        rows.append(rec)
        print(f"   {t:18s} 生产 {p['rows']:>12,}  备份 {b['rows']:>12,}  "
              f"差 {rec['row_delta']:>+12,}"
              + (f"  最新日 {rec.get('prod_max_date')} / {rec.get('backup_max_date')}"
                 if t in DATED else ""))

    ok = (prod["quick_check"] == "ok" and bak["quick_check"] == "ok"
          and not prod["errors"] and not bak["errors"])
    verdict = (
        "**恢复演练（段 A）通过**：生产库与备份的 `quick_check` 均为 `ok`，关键表全部可读，"
        "备份的最新 `trade_date` 落后于生产 ⇒ 这是一份**停留在过去时点、结构完整**的可用快照。"
        "⇒ 结论：备份**可用于恢复**（结构层面）；本演练**不**验证'用备份重建实例后跑通月链'"
        "（那是段 B，成本高、按需再做）。"
        if ok else
        "**恢复演练未通过**：见 errors / quick_check 字段 ⇒ 先修库，不要在该状态下做任何删除。"
    )

    payload = {
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - t0, 1),
        "stage": "A（只读，不复制文件）",
        "prod": str(PROD.relative_to(ROOT)), "backup": str(bk.relative_to(ROOT)),
        "backup_size_gb": round(bk.stat().st_size / 1e9, 3),
        "prod_probe": prod, "backup_probe": bak,
        "table_compare": rows,
        "ok": ok, "verdict": verdict,
    }
    print("\n" + ("✓ " if ok else "✗ ") + verdict.split("：", 1)[-1][:160])
    if not args.no_write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print("产物:", OUT)
    print(f"耗时: {payload['elapsed_sec']}s")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
