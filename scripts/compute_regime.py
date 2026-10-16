"""市场状态计算脚本: python scripts/compute_regime.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.logger import setup_logger
from app import db
from app.models.regime import compute_regime


def main():
    setup_logger()
    db.init_db()
    compute_regime()


if __name__ == "__main__":
    main()
