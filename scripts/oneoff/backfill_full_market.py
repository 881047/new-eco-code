"""一次性回填: 全市场扩展(2026-09-07) —— 新纳入板块个股的完整历史行情
创业板/科创板/北交所(含退市股): 按股重拉 daily/adj_factor/daily_basic 全部历史。
幂等可中断: 已有行情的股票自动跳过, 重复运行续跑。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd

from app.logger import setup_logger
from app import db
from app.datasource.tushare_client import TushareClient
from app.datasource.updater import START_DATE, STOCK_DAILY_COLS, BASIC_COLS

END_DATE = "20260907"


def main():
    setup_logger()
    db.init_db()
    ts = TushareClient()
    conn = db.get_conn()
    uni = set(pd.read_sql("SELECT ts_code FROM stock_basic", conn)["ts_code"])
    have = set(pd.read_sql("SELECT DISTINCT ts_code FROM daily", conn)["ts_code"])
    conn.close()
    todo = sorted(uni - have)
    print(f"股票池 {len(uni)} 只, 无行情记录 {len(todo)} 只, 开始逐只回填")
    total = 0
    for i, code in enumerate(todo):
        added = 0
        for api, cols, table in (("daily", STOCK_DAILY_COLS, "daily"),
                                 ("adj_factor", ["ts_code", "trade_date", "adj_factor"], "adj_factor"),
                                 ("daily_basic", BASIC_COLS, "daily_basic")):
            try:
                df = ts.call(api, ts_code=code, start_date=START_DATE, end_date=END_DATE)
                st = ts.last_status
            except Exception as e:
                print(f"  {code} {api} 调用异常: {e}", flush=True)
                continue
            if st != "ok" or df is None or df.empty:
                if st != "empty":
                    print(f"  {code} {api}: 状态 {st}, 跳过(下次续跑)", flush=True)
                continue
            df = df[df["ts_code"] == code]
            conn = db.get_conn()
            n = db.upsert_df(conn, df[cols], table)
            conn.close()
            added += n
        total += added
        if (i + 1) % 50 == 0 or added == 0:
            print(f"  [{i+1}/{len(todo)}] {code} 新增 {added} 行 (累计 {total})", flush=True)
    print(f"回填完成, 累计写入 {total} 行", flush=True)


if __name__ == "__main__":
    main()
