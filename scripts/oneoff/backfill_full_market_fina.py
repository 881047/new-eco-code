"""一次性回填: 全市场扩展(2026-09-07) —— 新板块个股的财务指标与历史名称
财务按股拉取(免费档按股可用, 一次返回全部报告期), 名称按股拉改名记录。
幂等: 已有记录的股票自动跳过(recent_only 增量口径), 可中断续跑。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.logger import setup_logger
from app import db
from app.datasource.updater import DataUpdater


def main():
    setup_logger()
    db.init_db()
    u = DataUpdater()
    print("== 财务指标增量(新板块按股) ==", flush=True)
    u.init_fina(recent_only=True)
    print("== 历史名称增量(新板块按股) ==", flush=True)
    u.init_namechange(recent_only=True)
    print("回填完成", flush=True)


if __name__ == "__main__":
    main()
