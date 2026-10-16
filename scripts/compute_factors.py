"""因子计算脚本: python scripts/compute_factors.py [--fina-only]"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.logger import setup_logger
from app import db
from app.factors.factor_engine import compute_all


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fina-only", action="store_true", help="只重算财务因子(财务数据补齐后增量重算)")
    parser.add_argument("--only", help="只重算指定因子, 逗号分隔, 如 beta_250,debt_to_assets")
    args = parser.parse_args()
    setup_logger()
    db.init_db()
    only = [s.strip() for s in args.only.split(",")] if args.only else None
    compute_all(only=only, fina_only=args.fina_only)


if __name__ == "__main__":
    main()
