"""每日动态链路(手动触发): 日频因子 → 每日推分 → 持仓体检 → 信号

# STATUS: 手工入口, 与生产日链**不等价**(2026-09-12 独立审计 #25):
# 本脚本跑 compute_daily → score_today → run_monitor, 而生产链 app/scheduler.py:332 会在
# 这三步之前先跑 build_pool_index 与 compute_regime。手工调用会用到过期的池/市况读数。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.logger import setup_logger
from app import db
from app.factors.daily_engine import compute_daily
from app.models.daily_score import score_today
from app.portfolio.monitor import run_monitor


def main():
    setup_logger()
    db.init_db()
    compute_daily()
    score_today()
    sig = run_monitor()
    print(json.dumps(sig, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
