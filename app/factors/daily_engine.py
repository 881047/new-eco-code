"""日频因子引擎: 每日收盘后计算量价因子 + 财务因子前向填充, 截面中性化后存入 factor 表

- 量价因子: 复用 compute_price_factors(全量日频), 只对最近 keep_days 个交易日做中性化并入库
- 财务因子: 从 factor 表取月度值(已中性化), 按股票前向填充到每个交易日
- 清理: 删除非月末的过期日频行, 控制表体积
"""
import logging

import pandas as pd

from app import db
from app.factors.factor_engine import (compute_lhb_factors, compute_price_factors,
                                       load_market, neutralize, industry_map)

log = logging.getLogger(__name__)

KEEP_DAYS = 30   # 保留最近 N 个交易日的日频因子
LOOKBACK_DAYS = 300  # 加载窗口: keep_days + 最长滚动窗(high_52w=252) + 缓冲 (2026-09-04 P1-2)
FINA_FIELDS = ["roe", "netprofit_yoy", "or_yoy", "grossprofit_margin", "debt_to_assets", "eps_yoy"]


def _load_monthly_fina(conn, cutoff: str) -> pd.DataFrame:
    """月度财务因子(已中性化)长表。2026-09-05 审查修复: 加 trade_date 下界走
    idx_factor_date 索引 —— 原无下界全表扫描 14.5M 行 × 6 因子(350 万行进 pandas
    做全历史 pivot 再丢掉 ~90%)"""
    q = ",".join("'" + f + "'" for f in FINA_FIELDS)
    return pd.read_sql(
        f"SELECT ts_code, trade_date, factor, value FROM factor WHERE factor IN ({q}) "
        "AND trade_date>=?", conn, params=(cutoff,))


def compute_daily(end_date: str = None, keep_days: int = KEEP_DAYS) -> int:
    log.info("== 日频因子计算开始 ==")
    # 窗口化加载(2026-09-04 审查 P1-2): 只取最近 LOOKBACK_DAYS 个交易日,
    # 原每次全历史 12 矩阵约 1~1.5GB、2-3 分钟; 300 天足以覆盖 keep_days+最长 252 日滚动窗
    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        db_last = conn.execute("SELECT MAX(trade_date) FROM daily").fetchone()[0]
        if db_last is None:
            # 2026-09-05 审查修复: daily 空表时原 min(end_date, None) 抛 TypeError
            # (脚本直跑 fatal), 应提前跳过等待行情初始化
            # (2026-09-29: 块内不手动 close —— conn_ctx 离开块时必定关, 双 close 属脆弱写法)
            log.warning("daily 表为空, 跳过日频因子(先初始化行情数据)")
            return 0
        last = min(end_date, db_last) if end_date else db_last
        cut_row = conn.execute(
            "SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date<=? "
            "ORDER BY cal_date DESC LIMIT 1 OFFSET ?",
            (last, LOOKBACK_DAYS)).fetchone()
        cutoff = cut_row[0] if cut_row else None
        # 月末保留清单改用交易日历全量(原从已加载 dates 推, 窗口化后会只剩 300 天,
        # 窗口外的历史月末行会被误删 —— 月末行是模型训练集, 必须永久保留)
        # 2026-09-08 审查 P2: 并集 daily 表实际月末 —— 某月最后一交易日数据源缺勤时,
        # 该月因子行日期不在交易日历清单, 会被 DELETE 静默删除(训练丢截面且无告警)
        md_all = set(r[0] for r in conn.execute(
            "SELECT MAX(cal_date) FROM trade_cal WHERE is_open=1 AND cal_date<=? "
            "GROUP BY substr(cal_date,1,6)", (last,)).fetchall())
        md_all |= set(r[0] for r in conn.execute(
            "SELECT MAX(trade_date) FROM daily WHERE trade_date<=? "
            "GROUP BY substr(trade_date,1,6)", (last,)).fetchall())
        md_all = sorted(md_all)
        ind = industry_map(conn)
        fina_long = _load_monthly_fina(conn, cutoff or "00000000")

    m = load_market(since=cutoff)
    dates = pd.to_datetime(m["close"].index)
    if len(dates) == 0:
        # 2026-09-05 审查修复: 防御性跳过(load_market 窗口内无数据时 dates[-1] IndexError)
        log.warning("load_market 窗口内无行情数据, 跳过日频因子")
        return 0
    today = pd.Timestamp(end_date) if end_date else dates[-1]

    # 最近 keep_days 个交易日
    recent = dates[dates <= today][-keep_days:]
    if len(recent) == 0:
        # 2026-09-12 审查 P2-21: end_date 早于加载窗口起点时 recent 为空, 下面第 75/120/130
        # 行的 recent[0] 直接 IndexError —— 日链是「每天必跑」的, 崩在这里会让当天整条链断掉,
        # 而真实原因只是调用方给了个落在窗口之前的日期。按「无目标区间」跳过。
        log.warning("加载窗口 %s~%s 内没有 <= %s 的交易日, 跳过日频因子",
                    dates[0].date(), dates[-1].date(), today.date())
        return 0
    log.info("目标区间: %s ~ %s (%d 天)", recent[0].date(), recent[-1].date(), len(recent))

    # 1) 量价因子(全量日频计算, 只取近期做中性化)
    price_f = compute_price_factors(m)
    for name in price_f:
        price_f[name].index = dates

    # 2) 财务因子: 月度值前向填充到每日
    fina_f = {}
    if not fina_long.empty:
        for f in FINA_FIELDS:
            sub = fina_long[fina_long["factor"] == f]
            pv = sub.pivot(index="trade_date", columns="ts_code", values="value").sort_index()
            pv.index = pd.to_datetime(pv.index)
            # 按交易日历重索引 + ffill
            full = pv.reindex(dates).ffill()
            fina_f[f] = full

    # 2.5) 龙虎榜因子: 近20交易日滚动和(未上榜=NaN), 与量价因子一样逐日中性化
    lhb_f = compute_lhb_factors(dates)

    # 3) 逐日中性化 + 入库
    # 量价/龙虎榜因子: 当日截面中性化(与月度流程一致)
    # 财务因子: 存储值已是月度中性化结果, 直接前向填充使用(不能二次中性化, 否则与月度打分不一致)
    # 2026-09-12 审查 P2-21 **只修了边界**, 没改成「整块中性化」—— 试过, 不能保证逐位相同:
    # 直觉上 neutralize 每步都是按行的(截面内完成), 行数不该影响结果, 其 docstring 也这么写。
    # 但实测该结论**不普遍成立**: 同一批列、只把行数从 1 改成 6, 3000 列纯随机的 float32 面板
    # 6/6 行都出现 ~1e-8 的差; 面板里只要有一个 NaN/inf 格就 5/6 行有差。机制是 dtype 漂移 ——
    # `clip` 对多行帧会上转 float64、单行帧保持 float32, 于是两条路径的中间运算精度不同。
    # 生产面板(301 行 × 5626 列, 25 因子)当前两法确实逐位相同, 但那是数据形状碰巧,
    # 不是不变量 ⇒ 不值得拿「入库日频因子值」去换这点开销(值与今日相同、来日未必)。
    # 详见 factor_engine.neutralize docstring 的实测记录。
    frames = []
    for d in recent:
        for name, df in {**price_f, **lhb_f}.items():
            neu = neutralize(df.loc[[d]], ind)
            s = neu.loc[d].dropna()
            if not s.empty:
                frames.append(pd.DataFrame({
                    "ts_code": s.index, "trade_date": d.strftime("%Y%m%d"),
                    "factor": name, "value": s.values.astype(float)}))
        for name, df in fina_f.items():
            s = df.loc[d].dropna()
            if not s.empty:
                frames.append(pd.DataFrame({
                    "ts_code": s.index, "trade_date": d.strftime("%Y%m%d"),
                    "factor": name, "value": s.values.astype(float)}))
    out = pd.concat(frames, ignore_index=True)

    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        n = db.upsert_df(conn, out, "factor")
        # 清理: 删除早于保留窗口且非月末的日频行(月末行供模型训练, 永久保留);
        # 月末清单来自交易日历(全历史), 窗口化后不再依赖已加载 dates
        recent_start = recent[0].strftime("%Y%m%d")
        if md_all:
            # 2026-09-05 审查修复: 加下界(窗口起点前 60 天), 原无下界 DELETE 每天扫 ~1170 万
            # 行索引条目且稳态删 0 行
            lb = (pd.to_datetime(recent[0]) - pd.Timedelta(days=90)).strftime("%Y%m%d")
            conn.execute(
                "DELETE FROM factor WHERE trade_date >= ? AND trade_date < ? "
                "AND trade_date NOT IN ({})".format(",".join("?" * len(md_all))),
                (lb, recent_start, *md_all))
        conn.commit()
        db.set_status(conn, "daily_factor", recent[-1].strftime("%Y%m%d"), n)
    log.info("== 日频因子完成: %d 行 (最新 %s) ==", n, recent[-1].date())
    return n
