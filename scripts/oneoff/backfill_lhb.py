"""一次性回填: AkShare 龙虎榜 2015-01 至今 (约 2800 交易日, 预计 15~30 分钟)
幂等可中断, 重复运行自动续跑; 建议运行前先冷备 DB (scripts/backup_db.py)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.logger import setup_logger
from app import db
from app.datasource import lhb_client


def main():
    setup_logger()
    db.init_db()
    done = lhb_client.sync_lhb_ak()
    print(f"龙虎榜回填完成, 本次新增 {done} 个交易日")


if __name__ == "__main__":
    main()
