"""调仓方案生成脚本: python scripts/build_plan.py [plan_date]"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.logger import setup_logger
from app import db
from app.portfolio.builder import build_plan


def main():
    setup_logger()
    db.init_db()
    plan_date = sys.argv[1] if len(sys.argv) > 1 else None
    summary = build_plan(plan_date)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
