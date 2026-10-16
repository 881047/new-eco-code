"""一次性补库: 券商月度金股全史 → 表 `broker_recommend`(2026-09-13)

第 11 个数据族(卖方注意力/共识), 与量价、资金流、财务不同源。Tushare 该接口
**2021-01 起**才有数据(2010/2015/2018 实测为空), 全史约 70 个月 ⇒ 约 70 次调用、
25 秒(客户端已限速 187 次/分)。

**PIT 口径(因子层必须照此对齐)**: 接口只给 `month`, 不给发布日; 券商金股惯例在
上月末~本月初发布 ⇒ 一律按"**该月第一个交易日已知**"取用(保守, 不可能前视)。

只写**新表** `broker_recommend`, 不碰任何既有表 —— 这是"不碰实盘链路"不变量 #7 的
最低要求; 但仍是生产表写入, 故带 `_guard` 护栏(默认拒绝)。

运行:
    set ONEOFF_CONFIRM=backfill_broker_recommend.py
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\backfill_broker_recommend.py
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _guard import require_confirm                                  # noqa: E402

require_confirm(__file__, "写入新表 broker_recommend(不碰既有表; 幂等可重跑)")

from app.datasource import flow_client                              # noqa: E402
from app.datasource.updater import DataUpdater                      # noqa: E402


def main() -> int:
    u = DataUpdater()
    n = flow_client.sync_broker_recommend(u)
    print(f"broker_recommend 净写入 {n} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
