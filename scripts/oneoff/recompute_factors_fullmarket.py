"""一次性重算: 全市场扩展(2026-09-07)后全历史月末因子重算
新板块(创业/科创/北交)个股的历史月末因子行, 供 116 期重训使用。
运行前请停服务(load_market 内存压力, 2026-09-04 教训)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.logger import setup_logger
from app import db
from app.factors.factor_engine import compute_all


def main():
    setup_logger()
    db.init_db()
    n = compute_all()
    print(f"因子重算完成: {n} 行", flush=True)


if __name__ == "__main__":
    main()
