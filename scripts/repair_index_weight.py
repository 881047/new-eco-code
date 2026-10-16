"""补齐 `index_weight` 的缺失月份（2026-09-13）

## 背景

F20 想测"指数调样（纳入/剔除）作为**外生信息源**"，但本地 `index_weight` 不完整：

    000906.SH(中证800)   71/92 个月  缺 21（缺月集中在**每年年初**：2019-01~03、2020-01~03 …）
    000852.SH(中证1000)  56/92 个月  缺 36（同上）
    000688.SH(科创50)    74/92 个月  缺 18（**正常**：该指数 2020-07-22 才发布）
    000300/000905/399006 92/92 个月  ✓ 完整

缺月导致差分把"部分快照"当真实调样：600 只(部分) → 800 只(完整) 被算成"进 200 / 出 0"，
每年 5 月凭空多出 200 个假纳入事件 ⇒ F20 第一次读出 +73.5%/年、第二次 +1.97 万亿 %（数值爆炸）。

## 本脚本

对每个指数逐月检查，只补**缺失的月份**（不重抓已有月份，避免无谓 API 调用）：

    pro.index_weight(index_code=..., start_date=YYYYMM01, end_date=YYYYMMDD)

写入前打印将写行数；写入用 `db.upsert_df`（主键去重，幂等）。跑完自动复核月数。
限流由 `TushareClient` 内置（0.32s/次 + 重试），因此约 1~2 秒/月。

用法：
    ./.venv/Scripts/python.exe -X utf8 scripts/repair_index_weight.py --dry-run
    ./.venv/Scripts/python.exe -X utf8 scripts/repair_index_weight.py
"""
import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import db                     # noqa: E402
from app.datasource.tushare_client import TushareClient  # noqa: E402

log = logging.getLogger("repair_iw")


def missing_months(conn, index_code: str, start="2019-01", end="2026-12") -> list:
    have = {str(d) for d in pd.read_sql(
        "SELECT DISTINCT trade_date FROM index_weight WHERE index_code=?", conn,
        params=(index_code,))["trade_date"]}
    have_m = {f"{d[:4]}-{d[4:6]}" for d in have}
    allm = [str(p) for p in pd.period_range(start, end, freq="M")]
    return [m for m in allm if m not in have_m]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只列出缺月, 不调用 API")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    conn = db.get_conn()
    try:
        idx = [r[0] for r in conn.execute(
            "SELECT DISTINCT index_code FROM index_weight ORDER BY index_code")]
        plan = {c: missing_months(conn, c) for c in idx}
        for c, ms in plan.items():
            log.info("%s 缺 %d 个月: %s", c, len(ms), ms[:8] + (["..."] if len(ms) > 8 else []))
        if a.dry_run:
            log.info("dry-run: 共缺 %d 个月, 未调用 API", sum(len(v) for v in plan.values()))
            return
        cli = TushareClient()
        total = 0
        for c, ms in plan.items():
            for m in ms:
                y, mm = m.split("-")
                nxt = (pd.Period(m, freq="M") + 1).strftime("%Y%m%d")
                try:
                    df = cli.call("index_weight", index_code=c,
                                  start_date=f"{y}{mm}01", end_date=nxt)
                except Exception as e:
                    log.warning("  %s %s 拉取失败: %s", c, m, e)
                    continue
                if df is None or df.empty:
                    log.info("  %s %s 无数据(指数未发布/无权限)", c, m)
                    continue
                keep = [x for x in ("index_code", "con_code", "trade_date", "weight") if x in df.columns]
                df = df[keep]
                n = db.upsert_df(conn, df, "index_weight")
                total += n
                log.info("  %s %s: +%d 行 (共 %d)", c, m, n, total)
        conn.commit()
        log.info("=== 写入 %d 行, 复核 ===", total)
        for c in idx:
            n = pd.read_sql("SELECT COUNT(DISTINCT trade_date) n FROM index_weight "
                            "WHERE index_code=?", conn, params=(c,))["n"].iloc[0]
            log.info("  %s: %s 个月", c, n)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
