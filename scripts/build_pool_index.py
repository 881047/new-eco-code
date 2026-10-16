"""池子指数(等权全A)重算脚本: python scripts/build_pool_index.py

全量重算(一次 GROUP BY, 晚到的数据修正会自愈), 结果落 pool_index 表。
日链/月度链已在 compute_regime 之前自动调用, 本脚本供手工重建/核对使用。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.logger import setup_logger
from app import db
from app.models.pool_index import build_pool_index


def main():
    setup_logger()
    db.init_db()
    n = build_pool_index()
    print("池子指数写入 {} 行".format(n))


if __name__ == "__main__":
    main()
