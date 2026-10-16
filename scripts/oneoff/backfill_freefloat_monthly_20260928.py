"""backfill_freefloat_monthly_20260928.py — 月末自由流通股本快照回填（只读拉取）

预注册：plans/freefloat-backfill-rule-v3-prereg-20260928.md（判据跑数前冻结）。
tushare `daily_basic` 的 `free_share`（自由流通股本，万股）是 v2 获胜选股规则
weight/circ_mv 的可计算孪生（ff_ratio = free_share/float_share）的核心字段——
仓库 daily_basic 表建表时未存。本脚本按月末交易日全市场拉取 2019-01~2026-09 快照，
落地 data/satellite_rebal/freefloat_monthly.parquet（gitignore，可重拉再生）。

用法：
    ./.venv/Scripts/python.exe -X utf8 scripts/oneoff/backfill_freefloat_monthly_20260928.py
"""
import json
import logging
import sys
import time
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfg_mod  # noqa: E402,F401
from app import db  # noqa: E402

log = logging.getLogger("ff_backfill")
OUT = BASE / "data" / "satellite_rebal" / "freefloat_monthly.parquet"
FIELDS = "ts_code,trade_date,total_share,float_share,free_share,circ_mv,total_mv"
START, END = "20190101", "20260930"


def month_end_tds() -> list:
    """全市场日历里的每月最后一个交易日。"""
    con = db.get_conn()
    try:
        rows = con.execute(
            "SELECT trade_date FROM index_daily WHERE ts_code='000300.SH' "
            "AND trade_date BETWEEN ? AND ? ORDER BY trade_date", (START, END)).fetchall()
    finally:
        con.close()
    s = pd.Series([r[0] for r in rows])
    return s.groupby(s.str[:6]).max().tolist()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    import tushare as ts
    import os
    pro = ts.pro_api(os.environ.get("TUSHARE_TOKEN") or os.environ.get("TUSHARE_KEY"))
    tds = month_end_tds()
    log.info("月末交易日 %d 个（%s ~ %s）", len(tds), tds[0], tds[-1])
    frames = []
    for i, td in enumerate(tds):
        for attempt in range(3):
            try:
                df = pro.daily_basic(trade_date=td, fields=FIELDS)
                frames.append(df)
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("%s 尝试%d 失败: %s", td, attempt + 1, str(exc)[:100])
                time.sleep(2.0)
        else:
            raise SystemExit(f"daily_basic 拉取失败 {td}")
        if (i + 1) % 12 == 0:
            log.info("进度 %d/%d（累计 %d 行）", i + 1, len(tds), sum(len(f) for f in frames))
        time.sleep(0.35)
    out = pd.concat(frames, ignore_index=True)
    for c in ("total_share", "float_share", "free_share", "circ_mv", "total_mv"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    bad = int((out["free_share"] > out["float_share"]).sum())
    cov = float(out["free_share"].notna().mean())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUT)
    meta = {"months": len(tds), "rows": int(len(out)), "free_le_float_violations": bad,
            "free_share_coverage": cov, "start": tds[0], "end": tds[-1]}
    (OUT.parent / "freefloat_monthly_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("完成：%s | 行 %d | free≤float 违例 %d | 覆盖 %.3f", OUT.name,
             len(out), bad, cov)


if __name__ == "__main__":
    main()
