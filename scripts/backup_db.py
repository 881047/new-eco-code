"""在线备份脚本: python scripts/backup_db.py  →  data/backup/neweco_YYYYMMDD.db"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.logger import setup_logger
from app import db


def main():
    setup_logger()
    target = db.backup_db()
    print(target)


if __name__ == "__main__":
    main()
