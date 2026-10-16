"""新信息源回填(2026-09-11 芯片式架构 阶段 0a)

用法:
    python scripts/oneoff/backfill_new_sources.py --start 20200901   # 只拉死区窗口(快, ~30min)
    python scripts/oneoff/backfill_new_sources.py                    # 从 START_DATE(2013) 全量

为什么可以分两次拉: `sync_daily_bulk` 是**按缺失交易日**补的, 与水位无关 ——
先拉 2020-09 起的窗口拿到死区结论, 日后再不传 --start 补 2013~2020 那段,
增量逻辑自动衔接, 不会重复拉已有日期。

幂等, 可中断续跑。日志: D:/claude-home/tmp/backfill_newsrc.log
"""
import argparse
import logging
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import db                                    # noqa: E402
from app.datasource.updater import DataUpdater        # noqa: E402
from app.datasource import flow_client                # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None,
                    help="按日接口的日历下界 YYYYMMDD; 默认 START_DATE(2013-01-01) 全量")
    ap.add_argument("--end", default=None, help="截止日 YYYYMMDD, 默认今天")
    ap.add_argument("--only", default=None,
                    help="只跑一个源: margin/moneyflow/block_trade/hk_hold/"
                         "holder_num/index_weight/sw_industry")
    ap.add_argument("--skip-hk", action="store_true",
                    help="跳过 hk_hold(研究专用, 2024-08 后覆盖度断崖, 无需日更)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(r"D:/claude-home/tmp/backfill_newsrc.log",
                                      encoding="utf-8")],
    )
    db.init_db()
    u = DataUpdater()
    t0 = time.time()

    if args.only:
        fn = dict(flow_client.DAILY_SOURCES).get(args.only)
        if fn is not None:
            fn(u, start_date=args.start, end_date=args.end)
        elif args.only == "holder_num":
            flow_client.sync_holder_num(u)
        elif args.only == "index_weight":
            flow_client.sync_index_weight(u)
        elif args.only == "sw_industry":
            flow_client.sync_sw_industry(u)
        else:
            sys.exit(f"未知源: {args.only}")
    else:
        flow_client.sync_new_sources(u, start_date=args.start, end_date=args.end,
                                     skip_hk=args.skip_hk)

    # 对账: 每张表的行数 + 覆盖的交易日数
    conn = db.get_conn()
    print("\n===== 回填结果对账 =====")
    for t in ("margin", "moneyflow", "block_trade", "hk_hold", "holder_num",
              "index_weight", "sw_classify", "sw_member"):
        if not db.table_exists(conn, t):
            print(f"  {t:15s} 表不存在")
            continue
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        dcol = "end_date" if t == "holder_num" else "trade_date"
        rng = ""
        if dcol in [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]:
            r = conn.execute(f"SELECT MIN({dcol}), MAX({dcol}) FROM {t}").fetchone()
            rng = f"  {r[0]} ~ {r[1]}"
        print(f"  {t:15s} {n:>12,} 行{rng}")
    conn.close()
    print(f"\n耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
