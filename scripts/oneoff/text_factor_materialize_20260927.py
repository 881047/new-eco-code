# -*- coding: utf-8 -*-
"""文本因子 materialize 进生产 factor 表 —— `plans/text-factor-llm-labeling-prereg-20260927.md`
§十四 步骤 1 的执行体。

把 text_n1m/text_novel3m（factors_v1.parquet，已按生产口径中性化）写进生产 `factor` 表
（ts_code / trade_date YYYYMMDD / factor / value）。幂等：先 DELETE 本两因子全部行再 INSERT。
写后核对：factor 表 DISTINCT factor 应为 33（31 在池 + 2 文本）。
护栏：_guard.require_confirm（写生产表）。运行前必须 backup_db.py。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from app import db

ROOT = Path(__file__).resolve().parents[2]
FV1 = ROOT / "data" / "text_corpus" / "factors_v1.parquet"
TEXT_FACTORS = ["text_n1m", "text_novel3m"]


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    import _guard
    _guard.require_confirm(Path(__file__).name,
                           "写生产 factor 表：materialize text_n1m/text_novel3m "
                           "(DELETE 旧行 + INSERT ~108 万行)")
    long = pd.read_parquet(FV1)
    long = long[long["factor"].isin(TEXT_FACTORS)]
    long["trade_date"] = pd.to_datetime(long["trade_date"]).dt.strftime("%Y%m%d")
    rows = long[["ts_code", "trade_date", "factor", "value"]].dropna(subset=["value"])
    log(f"待写入 {len(rows):,} 行（{len(rows)//2//5806} 锚点 × 2 因子）")

    conn = db.get_conn()
    try:
        n0 = conn.execute("SELECT COUNT(DISTINCT factor) FROM factor").fetchone()[0]
        for f in TEXT_FACTORS:
            conn.execute("DELETE FROM factor WHERE factor=?", (f,))
        conn.executemany("INSERT INTO factor (ts_code, trade_date, factor, value) "
                         "VALUES (?, ?, ?, ?)",
                         rows.itertuples(index=False, name=None))
        conn.commit()
        n1 = conn.execute("SELECT COUNT(DISTINCT factor) FROM factor").fetchone()[0]
        per = {f: conn.execute("SELECT COUNT(*), MIN(trade_date), MAX(trade_date) "
                               "FROM factor WHERE factor=?", (f,)).fetchone()
               for f in TEXT_FACTORS}
    finally:
        conn.close()
    log(f"factor 表因子集 {n0} → {n1}（应为 33）")
    for f, (cnt, dmin, dmax) in per.items():
        log(f"  {f}: {cnt:,} 行, {dmin} → {dmax}")
    summary = {"ran_at": datetime.now().isoformat(timespec="seconds"),
               "factors_before": n0, "factors_after": n1,
               "rows": {f: {"count": c, "min": dmin, "max": dmax}
                        for f, (c, dmin, dmax) in per.items()}}
    (ROOT / "experiments" / "text_factor_materialize_20260927.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = n1 == 33 and all(c > 500_000 for c, _, _ in per.values())
    log("核对通过" if ok else "核对未通过——检查后再继续")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
