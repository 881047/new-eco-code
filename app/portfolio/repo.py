"""逆回购建议: 现金仓位 + 日历规则(月末/季末/节前锁长, 平时滚 GC001)"""
import datetime
import logging

import pandas as pd

from app import db
from app import timeutil

log = logging.getLogger(__name__)

FALLBACK_RATE = 1.5  # 无利率数据时的假设年化(%)


def latest_rate(term_days: int = 1) -> float:
    """最近一期的对应期限年化利率: 期限必须匹配利率列 —— 2026-09-05 审查修复:
    原无论推荐 7 天还是 14 天都返回隔夜利率, 方案/邮件把隔夜利率标在"锁7天"上"""
    col = {1: "overnight", 7: "r7d", 14: "r14d"}.get(term_days, "overnight")
    with db.conn_ctx() as conn:                     # 2026-09-13: 连接卫生（独立审查 §3.2）
        r = pd.read_sql(f"SELECT {col} FROM repo_rate ORDER BY trade_date DESC LIMIT 1", conn)
    if r.empty or pd.isna(r[col].iloc[0]):
        return FALLBACK_RATE
    return float(r[col].iloc[0])


def repo_advice(amount: float, today: str = None) -> dict:
    """根据现金金额与日历给出逆回购期限建议"""
    # 2026-09-29 收编 timeutil(东八区): 生产唯一调用方(builder)显式传 plan_date,
    # 缺省路径只服务脚本/手工调用 —— 裸 date.today() 换非东八区机器部署时日历窗
    # 起点漂一天。本机逐位无影响。
    today = today or timeutil.date8()
    with db.conn_ctx() as conn:                     # 2026-09-13: 连接卫生
        cal = pd.read_sql(
            "SELECT cal_date FROM trade_cal WHERE cal_date>=? AND is_open=1 "
            "ORDER BY cal_date LIMIT 10", conn, params=(today,))
    days = cal["cal_date"].tolist()
    if len(days) >= 2:
        gap = (datetime.datetime.strptime(days[1], "%Y%m%d")
               - datetime.datetime.strptime(days[0], "%Y%m%d")).days
    else:
        gap = 1
    dt = datetime.datetime.strptime(today, "%Y%m%d")
    same_month = [d for d in days if d[:6] == today[:6]]
    is_month_end = len(same_month) <= 2          # 本月只剩≤2个交易日
    is_quarter_end = dt.month in (3, 6, 9, 12) and is_month_end

    # 2026-09-04 审查 P2-5 口径修正:
    # 国债逆回购按资金实际占用天数计息 —— 节前最后交易日做 GC001 隔夜即可吃满假期利息,
    # 无需锁 14 天牺牲流动性; 沪深自 2022-10 起均为 1000 元起
    if gap >= 3:
        term = 1
        desc = f"节前休市{gap}天, GC001 隔夜即可吃满假期利息(按实际占用天数计息)"
    elif is_quarter_end:
        term = 14
        desc = "季末资金面紧利率冲高, 锁14天"
    elif is_month_end:
        term = 7
        desc = "月末利率冲高, 锁7天"
    else:
        term = 1
        desc = "日常滚动 GC001/R-001(沪深均1000元起)"
    rate = latest_rate(term)  # 与推荐期限匹配的利率
    return {"amount": round(amount, 2), "rate": rate, "term": term, "desc": desc}
