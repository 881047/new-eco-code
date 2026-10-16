"""月频纸面账户(模拟盘)的**只读归档** —— 重置前必须先留痕(2026-09-14)

## 为什么有这个模块

`scripts/oneoff/reset_paper.py` 会清空模拟盘 5 张业务表并把现金重置为初始资金 —— 一次
**不可逆的账务重启**。2026-09-10 12:03 的那一次**没有留痕**: 重置前的持仓/成交/净值只剩
4.6 GB 的全库备份 `data/backup/neweco_20260909.db` 可以反查(KB 02/14 记着这笔账)。

本模块把重置前的状态导成**小而完整**的 JSON: 逐表逐行 + 行数 + 内容 sha256 + 库指纹,
外加一段人能直接读的摘要。它是**纯只读**的: SQLite 以 `mode=ro` 打开, 只 SELECT,
不碰任何业务表 —— 归档本身永远不该有副作用。

## 三道保证

1. **只读**: `mode=ro` 打开; 表不存在按 0 行处理(新装机也能归档, 不报错)。
2. **可逐位复核**: `sha256` 覆盖 `{counts, tables}`(不含时间戳/库指纹) ⇒ `verify()` 能在
   任何一天重算出同一个值; 摘要字段则由 `summarize()` 从内容**重新推导**再比对。
3. **写坏等于没归档**: `write()` 原子写(同目录 tmp + `os.replace`), `archive()` 写完立刻
   `verify()` 读回自检, 不符就抛。

## 已知的坑(2026-09-14 真踩过)

第一版归档脚本用 `dict(...)` 套在游标结果上, 把 6 张表全部写成了
`{"error": "cannot convert dictionary update sequence element #0 ..."}`, 文件只有 636 字节
却**看起来像成功了**。本模块的做法: 列名一律取**同一个游标**的 `description`,
行按 `{列: 值}` 显式构造, 并且写完必须读回自检(那次错误会被第 3 条当场拦下)。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import datetime
from pathlib import Path

from app import timeutil

SCHEMA = "paper_archive/v1"

#: 重置会清空的表(顺序 = 归档 JSON 里的键顺序, 也是报数顺序)
TABLES = (
    "paper_account", "paper_position", "paper_order",
    "paper_trade", "paper_nav", "paper_interest",
)


def _connect_ro(db_path) -> sqlite3.Connection:
    """只读打开(URI `mode=ro`): 归档过程不可能改到生产库"""
    uri = "file:" + str(Path(db_path)).replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, table: str) -> list:
    """表内容 → `[{列: 值}]`。列名取**同一个游标**的 description(2026-09-14 那个 bug 的修复点)。"""
    try:
        cur = conn.execute("SELECT * FROM %s" % table)
    except sqlite3.OperationalError:
        return []  # 表不存在(新装机/测试临时库): 按 0 行处理
    cols = [d[0] for d in cur.description]
    return [{c: r[i] for i, c in enumerate(cols)} for r in cur.fetchall()]


def canonical_sha256(counts: dict, tables: dict) -> str:
    """内容指纹: 只看行数与逐行内容, 不含时间戳 ⇒ 任何时候重算都相同"""
    blob = json.dumps({"counts": counts, "tables": tables}, sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def summarize(payload: dict) -> dict:
    """从**内容**推导摘要(verify 会重算一次与文件里的比 —— 内容与摘要必须自洽)"""
    t = payload.get("tables") or {}
    acc = (t.get("paper_account") or [{}])[0]
    navs = sorted(t.get("paper_nav") or [], key=lambda r: r.get("trade_date") or "")
    pos = t.get("paper_position") or []
    cost = sum((r.get("shares") or 0) * (r.get("avg_cost") or 0) for r in pos)
    return {
        "initial_capital": acc.get("initial_capital"),
        "cash": acc.get("cash"),
        "account_created_at": acc.get("created_at"),
        "last_applied_plan": acc.get("last_applied_plan"),
        "positions": len(pos),
        "positions_cost": round(cost, 2),
        "orders": len(t.get("paper_order") or []),
        "trades": len(t.get("paper_trade") or []),
        "nav_points": len(navs),
        "nav_first": navs[0].get("trade_date") if navs else None,
        "nav_last": navs[-1].get("trade_date") if navs else None,
        "nav_last_value": navs[-1].get("nav") if navs else None,
        "interest_total": round(sum((r.get("amount") or 0)
                                    for r in (t.get("paper_interest") or [])), 2),
    }


def dump(db_path) -> dict:
    """读库 → 归档 payload(不写文件)"""
    p = Path(db_path)
    if not p.exists():
        raise FileNotFoundError("归档失败: 库不存在 %s" % p)
    st = p.stat()
    conn = _connect_ro(p)
    try:
        tables = {t: _rows(conn, t) for t in TABLES}
    finally:
        conn.close()
    counts = {t: len(v) for t, v in tables.items()}
    payload = {
        "schema": SCHEMA,
        "dumped_at": timeutil.stamp(),
        "db": {
            "path": str(p),
            "size": st.st_size,
            "mtime": _fmt_mtime(st.st_mtime),
        },
        "counts": counts,
        "tables": tables,
    }
    payload["summary"] = summarize(payload)
    payload["sha256"] = canonical_sha256(counts, tables)
    return payload


def _fmt_mtime(mtime: float) -> str:
    return datetime.datetime.fromtimestamp(mtime, timeutil.TZ).strftime("%Y-%m-%d %H:%M:%S")


def default_path(db_path) -> Path:
    """归档默认落在库同级的 `backup/` 下, 文件名带秒级时间戳"""
    p = Path(db_path)
    return p.parent / "backup" / ("paper_archive_%s.json"
                                  % timeutil.now().strftime("%Y%m%d_%H%M%S"))


def write(path, payload) -> Path:
    """原子写: 同目录 tmp + os.replace(半截文件不会顶掉上一份归档)"""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8", newline="\n")
    os.replace(tmp, out)
    return out


def verify(path) -> tuple:
    """复验归档文件: schema / 行数 / sha256 / 摘要自洽。返回 `(ok, 说明)`。"""
    p = Path(path)
    if not p.exists():
        return False, "归档文件不存在: %s" % p
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # JSON 坏掉 = 归档坏掉
        return False, "归档文件不是合法 JSON: %s" % e
    if data.get("schema") != SCHEMA:
        return False, "schema 不符(期望 %s, 实为 %r)" % (SCHEMA, data.get("schema"))
    counts, tables = data.get("counts") or {}, data.get("tables") or {}
    missing = [t for t in TABLES if t not in tables]
    if missing:
        return False, "缺表: %s" % missing
    bad = [t for t in TABLES if counts.get(t) != len(tables.get(t) or [])]
    if bad:
        return False, "行数与内容不一致(写坏?): %s" % bad
    sha = canonical_sha256(counts, tables)
    if sha != data.get("sha256"):
        return False, "sha256 不符(文件被改写或写坏): 重算 %s != 文件中 %s" % (
            sha[:16], str(data.get("sha256"))[:16])
    if summarize(data) != data.get("summary"):
        return False, "摘要与内容不一致: %r vs %r" % (data.get("summary"), summarize(data))
    return True, "OK 行数=%s sha256=%s" % (counts, sha[:16])


def archive(db_path, out=None) -> Path:
    """归档 + 写回自检(自检失败即抛 —— 宁可中止重置, 也不要一份假归档)"""
    payload = dump(db_path)
    path = write(out or default_path(db_path), payload)
    ok, detail = verify(path)
    if not ok:
        raise RuntimeError("归档写回自检失败(这份归档不可信): %s" % detail)
    return path


def summary_line(payload: dict) -> str:
    """一行人能读的摘要(重置脚本与 CLI 都打这一行, 日志里留痕)"""
    s = payload["summary"]
    return ("账户: 初始资金 %s / 现金 %s / 建仓于 %s | 持仓 %d 只(成本合计 %s) / "
            "挂单 %d 行 / 成交 %d 笔 / 净值 %d 点(最新 %s: %s) / 计息合计 %s"
            % (s["initial_capital"], s["cash"], s["account_created_at"],
               s["positions"], s["positions_cost"], s["orders"], s["trades"],
               s["nav_points"], s["nav_last"], s["nav_last_value"], s["interest_total"]))
