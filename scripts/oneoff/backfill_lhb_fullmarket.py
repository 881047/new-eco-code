"""一次性回填: 全市场扩展(2026-09-08) —— 龙虎榜全市场历史
lhb/lhb_org 表历史数据是主板股票池时代拉取的(uni 过滤), 创业/科创/北交的上榜记录
缺失 → lhb 因子对非主板 100% 无行(模型把"冷门"特征错误套在非主板股上)。
东财接口按日返回全市场, 逐日重拉 upsert 即可补齐(不清空, 幂等)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd

from app.logger import setup_logger
from app import db
from app.datasource.lhb_client import _parse_ak_detail, _parse_ak_org, LHB_START

END_DATE = "20260907"


def main():
    import akshare as ak
    setup_logger()
    db.init_db()
    conn = db.get_conn()
    cal = sorted(pd.read_sql(
        "SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date>=? AND cal_date<=? "
        "ORDER BY cal_date", conn, params=(LHB_START, END_DATE))["cal_date"].tolist())
    conn.close()
    print(f"全市场龙虎榜回填: {len(cal)} 个交易日", flush=True)
    total = 0
    for i, d in enumerate(cal):
        try:
            raw = ak.stock_lhb_detail_em(start_date=d, end_date=d)
            df = _parse_ak_detail(raw, d)
            if df is not None:
                conn = db.get_conn()
                n = db.upsert_df(conn, df, "lhb")
                conn.close()
                total += n
        except Exception as e:
            print(f"  {d} 明细失败: {e}", flush=True)
        try:
            raw2 = ak.stock_lhb_jgmmtj_em(start_date=d, end_date=d)
            df2 = _parse_ak_org(raw2, d)
            if df2 is not None:
                conn = db.get_conn()
                n2 = db.upsert_df(conn, df2, "lhb_org")
                conn.close()
                total += n2
        except Exception as e:
            print(f"  {d} 机构失败: {e}", flush=True)
        if (i + 1) % 100 == 0:
            print(f"  [{i+1}/{len(cal)}] {d} (累计 {total})", flush=True)
    print(f"回填完成, 累计写入 {total} 行", flush=True)


if __name__ == "__main__":
    main()
