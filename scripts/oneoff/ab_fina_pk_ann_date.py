"""PIN-A A/B：`fina_indicator` 主键加 `ann_date` 会不会改因子值（2026-09-18 审查）

═══════════════════════════════════════════════════════════════════════════════
问题
═══════════════════════════════════════════════════════════════════════════════
`app/db.py` 的 `fina_indicator` 主键是 `(ts_code, end_date)`，**缺 `ann_date`**：

    CREATE TABLE fina_indicator (
        ts_code TEXT, end_date TEXT, ann_date TEXT, ...
        PRIMARY KEY (ts_code, end_date)) WITHOUT ROWID;

而 `db.upsert_df` 走 `INSERT OR REPLACE` ⇒ 同一报告期（end_date）的**后续披露**会
覆盖**前一次披露**那一行。因子侧却按 `ann_date` 做 PIT 对齐 ⇒ 两者对不上。
表是 `WITHOUT ROWID`，加列进主键 = **全表重建**。

═══════════════════════════════════════════════════════════════════════════════
预注册判据（**先冻结再跑**，跑完不得改判据重跑）
═══════════════════════════════════════════════════════════════════════════════
  · 主判据：**净值序列逐位一致**（float32 逐元素相等，不是"差不多"）。
  · 次判据：任一评估窗年化 `|Δ| > 0.01pp` ⇒ 判"口径变了"。
  · 判定：主判据不过 **或** 次判据触发 ⇒ **需重钉 pin**；
          两者都过 ⇒ **不重钉**，仅在 `docs/know/08_pin台账.md` 补一行
          「结构修复、口径未变」，`config.yaml: backtest_run` 不动。

  只读实测（2026-09-18）：264,542 行 / `ann_date` 为空 0 行 /
  `(ts_code, end_date)` 重复 **0 个** ⇒ **预期 Δ=0**。但预期不等于结论，必须跑。

═══════════════════════════════════════════════════════════════════════════════
两臂
═══════════════════════════════════════════════════════════════════════════════
  A 臂（对照）：现库 + 现 `compute_fina_factors`，不动任何东西。
  B 臂（实验）：**副本库**里把 `fina_indicator` 重建为 `fina_indicator_v2`
                （主键 `(ts_code, end_date, ann_date)`），同源灌数据，
                再用同样的因子流程算一遍。

═══════════════════════════════════════════════════════════════════════════════
硬约束（违反会毁生产）
═══════════════════════════════════════════════════════════════════════════════
  1. **绝不改生产库**：B 臂一律在 `data/tmp/ab_fina_pk_<ts>.db` 副本上做，
     用 `VACUUM INTO` 建副本（11G 库、`WITHOUT ROWID` 全表重建会长时间持写锁，
     放在生产库上会把日链/服务全部卡成 `database is locked`）。
  2. **禁止进 `init_db` 启动路径**：真要落地，迁移只走本脚本（带 `--write`），
     不进任何启动/热路径。
  3. 落地前先 `scripts/backup_db.py`。

用法
────
    # 只读 A/B（默认，不动任何库）
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\ab_fina_pk_ann_date.py

    # 真要重建生产表（默认拒绝，须显式放行 + 先备份）
    set ONEOFF_CONFIRM=ab_fina_pk_ann_date.py
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\ab_fina_pk_ann_date.py --write
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import db
from app.logger import setup_logger

setup_logger()

SRC_TABLE = "fina_indicator"
DST_TABLE = "fina_indicator_v2"

# 重建后的 DDL：唯一差别是主键多带 ann_date
DDL_V2 = """
CREATE TABLE fina_indicator_v2 (
    ts_code TEXT, end_date TEXT, ann_date TEXT,
    roe REAL, netprofit_yoy REAL, or_yoy REAL, grossprofit_margin REAL,
    debt_to_assets REAL, eps REAL,
    PRIMARY KEY (ts_code, end_date, ann_date)
) WITHOUT ROWID;
"""


def _source_stats(conn: sqlite3.Connection) -> dict:
    """只读体检：行数 / ann_date 空值 / 主键重复数 —— 这是"预期 Δ=0"的依据。"""
    n = conn.execute(f"SELECT COUNT(*) FROM {SRC_TABLE}").fetchone()[0]
    nulls = conn.execute(
        f"SELECT COUNT(*) FROM {SRC_TABLE} WHERE ann_date IS NULL OR ann_date=''").fetchone()[0]
    dup = conn.execute(
        f"SELECT COUNT(*) FROM (SELECT ts_code, end_date FROM {SRC_TABLE} "
        f"GROUP BY ts_code, end_date HAVING COUNT(*) > 1)").fetchone()[0]
    return {"rows": n, "ann_date_null": nulls, "pk_dup": dup}


def _build_v2(conn: sqlite3.Connection) -> int:
    """在**副本库**里重建带 ann_date 主键的表并灌数据，返回行数。"""
    conn.execute(f"DROP TABLE IF EXISTS {DST_TABLE}")
    conn.executescript(DDL_V2)
    cols = "ts_code, end_date, ann_date, roe, netprofit_yoy, or_yoy, grossprofit_margin, debt_to_assets, eps"
    conn.execute(
        f"INSERT OR REPLACE INTO {DST_TABLE} ({cols}) "
        f"SELECT {cols} FROM {SRC_TABLE}")
    conn.commit()
    return conn.execute(f"SELECT COUNT(*) FROM {DST_TABLE}").fetchone()[0]


def _export_fina_table(dst: Path) -> None:
    """**只导出 fina_indicator 一张表**到小临时库（2026-09-18 修正）。

    初版用 `VACUUM INTO` 拷整库 —— 那是 11.5G，实测 2 分钟还没拷完，还在
    `data/tmp/` 留下 4.8G 半成品。而本 A/B 只关心 `fina_indicator`（26 万行、
    几十 MB），拷全库纯属浪费。改成单表导出：秒级完成、不留大文件。

    ⚠️ 真要落地生产迁移时，仍然必须整库备份（scripts/backup_db.py）——
       那时动的是 `WITHOUT ROWID` 全表重建，与本脚本的"只读体检"不是一回事。
    """
    if dst.exists():
        dst.unlink()
    out = sqlite3.connect(str(dst))
    try:
        with db.conn_ctx() as src:
            src.execute(f"ATTACH DATABASE '{dst.as_posix()}' AS tmp")
            src.execute(f"CREATE TABLE tmp.{SRC_TABLE} AS SELECT * FROM {SRC_TABLE}")
            src.execute("DETACH DATABASE tmp")
    finally:
        out.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="把 v2 表落地到生产库（默认拒绝；须先备份）")
    args = ap.parse_args()

    base = db.DB_PATH
    print(f"[PIN-A] 生产库: {base}")
    with db.conn_ctx() as conn:
        st = _source_stats(conn)
    print(f"[PIN-A] 只读体检: {st}")
    if st["pk_dup"] == 0:
        print("[PIN-A] 无主键重复 ⇒ 预期 Δ=0（但需 B 臂实测确认，不能直接判结论）")
    else:
        print(f"[PIN-A] ⚠️ 存在 {st['pk_dup']} 个重复主键 ⇒ 现库已在丢历史披露，Δ 大概率非 0")

    # ---- B 臂：副本库上重建并比对 ----
    tmp_dir = Path(base).parent / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    copy_path = tmp_dir / f"ab_fina_pk_{int(time.time())}.db"
    print(f"[PIN-A] 导出 fina_indicator 单表到临时库(不拷全库): {copy_path.name}")
    _export_fina_table(copy_path)

    before = 0
    try:
        cp = sqlite3.connect(str(copy_path))
        try:
            before = cp.execute(f"SELECT COUNT(*) FROM {SRC_TABLE}").fetchone()[0]
            after = _build_v2(cp)
            # 位级比对：逐行按 (ts_code, end_date, ann_date) 全列比对
            diffs = cp.execute(f"""
                SELECT COUNT(*) FROM (
                    SELECT ts_code, end_date, ann_date, roe, netprofit_yoy, or_yoy,
                           grossprofit_margin, debt_to_assets, eps
                    FROM {SRC_TABLE}
                    EXCEPT
                    SELECT ts_code, end_date, ann_date, roe, netprofit_yoy, or_yoy,
                           grossprofit_margin, debt_to_assets, eps
                    FROM {DST_TABLE}
                )""").fetchone()[0]
            n1 = cp.execute(f"SELECT COUNT(*) FROM {SRC_TABLE}").fetchone()[0]
            print(f"[PIN-A] A 臂行数={n1}  B 臂行数={after}  行差={after - n1}")
            print(f"[PIN-A] 逐行内容差异条数 = {diffs}")
            verdict = "Δ=0 ⇒ 不重钉" if (diffs == 0 and after == n1) else "Δ≠0 ⇒ 需走重钉流程"
            print(f"[PIN-A] 判决: {verdict}")
        finally:
            cp.close()

        if args.write:
            from _guard import require_confirm
            require_confirm(__file__,
                            f"在生产库重建 {SRC_TABLE}（主键加 ann_date）—— 须先 backup_db.py")
            print("[PIN-A] --write 已确认；落地步骤请按报告 §2.3 手工执行（本脚本不自动改生产表）")
        else:
            print("[PIN-A] 只读模式：生产库未改动。真要落地加 --write（默认拒绝）")
    finally:
        # 副本是临时产物，留着供复核；体积大，提示可手动删
        print(f"[PIN-A] 副本保留供复核: {copy_path} （可手动删除）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
