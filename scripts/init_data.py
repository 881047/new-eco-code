"""数据初始化/增量脚本

用法:
    python scripts/init_data.py            # 全量初始化(可中断续跑)
    python scripts/init_data.py --fast     # 只同步核心股票行情
    python scripts/init_data.py --daily    # 每日增量(含基金)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.logger import setup_logger
from app import db
from app.datasource.updater import DataUpdater


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", action="store_true", help="只同步核心股票行情, 跳过基金/财务/回购")
    parser.add_argument("--daily", action="store_true", help="每日增量模式(默认全量)")
    args = parser.parse_args()

    setup_logger()
    db.init_db()
    u = DataUpdater()
    if args.daily:
        u.run_daily(full=True)
    else:
        u.init_all(fast=args.fast)


if __name__ == "__main__":
    main()
