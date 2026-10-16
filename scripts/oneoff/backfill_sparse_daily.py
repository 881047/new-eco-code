"""一次性回填: 覆盖率不足个股的 daily/adj_factor/daily_basic 逐只历史 (2026-09-07 完整性检查)

机制: 初始全量回填按"交易日"拉取, 之后才纳入股票池的股票(尤其是退市股 list_status=D
后补进 stock_basic)在已拉过的日期上没有行; sync_daily_bulk 只补缺失"日期"(该日期已在
have 集合中) → 个股级缺口永不自愈。逐只按代码重拉整段历史, 幂等 upsert, 源端确实没有
的行(停牌日)保持缺失属正常。
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
COVERAGE = 0.80


def main():
    setup_logger()
    db.init_db()
    ts = TushareClient()
    conn = db.get_conn()
    cal = pd.read_sql(
        "SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date>=? AND cal_date<=? ORDER BY cal_date",
        conn, params=(START_DATE, END_DATE))["cal_date"].tolist()
    rows = pd.read_sql("SELECT ts_code, list_date, delist_date FROM stock_basic", conn)
    have = pd.read_sql("SELECT ts_code, COUNT(*) AS n FROM daily GROUP BY ts_code", conn) \
        .set_index("ts_code")["n"]
    poor = []
    for _, r in rows.iterrows():
        start = max(str(r["list_date"] or START_DATE), START_DATE)
        end = min(str(r["delist_date"] or END_DATE), END_DATE)
        exp = sum(1 for d in cal if start <= d <= end)
        n = int(have.get(r["ts_code"], 0))
        if exp > 20 and n / exp < COVERAGE:
            poor.append((r["ts_code"], exp, n))
    conn.close()
    print(f"覆盖率 < {COVERAGE:.0%} 的个股: {len(poor)} 只, 开始逐只回填")
    total = 0
    for i, (code, exp, n) in enumerate(poor):
        for api, cols, table in (("daily", STOCK_DAILY_COLS, "daily"),
                                 ("adj_factor", ["ts_code", "trade_date", "adj_factor"], "adj_factor"),
                                 ("daily_basic", BASIC_COLS, "daily_basic")):
            try:
                df = ts.call(api, ts_code=code, start_date=START_DATE, end_date=END_DATE)
                st = ts.last_status
            except Exception as e:
                print(f"  {code} {api} 调用异常: {e}")
                continue
            if st != "ok" or df is None or df.empty:
                print(f"  {code} {api}: 状态 {st}, 跳过")
                continue
            df = df[df["ts_code"] == code]
            conn = db.get_conn()
            n_add = db.upsert_df(conn, df[cols], table)
            conn.close()
            total += n_add
        print(f"  [{i+1}/{len(poor)}] {code} 预期 {exp} 天 / 原有 {n} 行")
    print(f"回填完成, 累计写入 {total} 行")


if __name__ == "__main__":
    main()
