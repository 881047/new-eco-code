"""拉 `stk_limit`(涨跌停价) —— 实验路线图 D06/D07 的数据基础 (2026-09-11)

## 为什么要这张表

路线图阶段一是**硬门禁**: D06「涨停不可买」/ D07「跌停不可卖」, 通过标准写的是
「实际成交率 / 涨停不可买 / 跌停不可卖」。而本项目当前
`PITEngine.tradable_mask` 的六层过滤里**没有这一层**:

    ① 上市满 60 日  ② 未停牌(vol>0)  ③ 非 ST/退(时点名)  ④ 板块
    ⑤ 市值下界      ⑥ 近 20 日均额下限

策略在月末按**收盘价**买入。若该股当日封涨停, 收盘前是买不到的 —— 回测按收盘价成交
= 系统性高估。这与「旧基线买了生产买不到的 ST/退市股, 高估 28.78pp」是**同一类错误**,
只是那一类已被修掉、这一类还没。

## 口径

- 接口: Tushare `stk_limit`(权限已探明可用), 一次一个交易日返回全市场
- 列: `trade_date / ts_code / up_limit / down_limit`, 与 `daily.close` 同尺度(未复权)
- 区间: `START` 起 —— 选 2017-01-01 是为了同时覆盖 pin 的两个评估窗
  (prod=2019-07-22~ 与 full=2017-01-01~), 而非只覆盖其中一个
- 复用 `updater.sync_daily_bulk`: 只拉缺失日期, 断点续跑, 权限错误即终止

## 用法

    ./.venv/Scripts/python.exe scripts/oneoff/pull_stk_limit.py            # 增量
    ./.venv/Scripts/python.exe scripts/oneoff/pull_stk_limit.py --check    # 只看覆盖率
"""
import argparse
import io
import sys
from pathlib import Path

_ORIG_STDOUT = sys.stdout
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(_ORIG_STDOUT.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import db                                       # noqa: E402
from app.datasource.updater import DataUpdater           # noqa: E402
from app.logger import setup_logger                      # noqa: E402

START = "20170101"


def check() -> None:
    conn = db.get_conn()
    try:
        if not db.table_exists(conn, "stk_limit"):
            print("stk_limit 表不存在 —— 先跑不带 --check 的同步")
            return
        r = conn.execute("SELECT COUNT(*), COUNT(DISTINCT trade_date), "
                         "MIN(trade_date), MAX(trade_date), "
                         "SUM(up_limit IS NULL), SUM(down_limit IS NULL) "
                         "FROM stk_limit").fetchone()
        print("stk_limit: %d 行 | %d 个交易日 | %s ~ %s | up_limit NULL %d | down_limit NULL %d"
              % r)
        print("\n逐年覆盖(交易日数 / 行数):")
        for y, nd, n in conn.execute(
                "SELECT substr(trade_date,1,4) y, COUNT(DISTINCT trade_date), COUNT(*) "
                "FROM stk_limit GROUP BY y ORDER BY y"):
            print("   %s  %3d 日  %8d 行" % (y, nd, n))
        # 与 daily 对账: 同一天 stk_limit 的股票数应约等于 daily 的股票数
        print("\n与 daily 对账(抽样 3 天):")
        for d in [r[0] for r in conn.execute(
                "SELECT DISTINCT trade_date FROM stk_limit ORDER BY trade_date DESC LIMIT 3")]:
            a = conn.execute("SELECT COUNT(*) FROM stk_limit WHERE trade_date=?", (d,)).fetchone()[0]
            b = conn.execute("SELECT COUNT(*) FROM daily WHERE trade_date=?", (d,)).fetchone()[0]
            print("   %s  stk_limit %5d  |  daily %5d  |  差 %+d (%.2f%%)"
                  % (d, a, b, a - b, 100.0 * (a - b) / b if b else float("nan")))
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只报告覆盖, 不拉取")
    ap.add_argument("--start", default=START)
    args = ap.parse_args()

    setup_logger()
    db.init_db()                      # 建出 stk_limit 表(SCHEMA 里已声明)
    if args.check:
        check()
        return

    u = DataUpdater()
    n = u.sync_daily_bulk("stk_limit", "stk_limit", start_date=args.start)
    print("\n同步完成, 本次写入 %s 行" % n)
    check()


if __name__ == "__main__":
    main()
