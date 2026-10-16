"""场内基金打分(动量+流动性) + 场外基金名单(排行快照)"""
import logging

import pandas as pd

from app import db

log = logging.getLogger(__name__)


def score_etfs(top_n: int = 10, fund_type: str = None) -> pd.DataFrame:
    """场内 ETF/LOF/封基: 60/120日动量 + 20日均成交额≥1000万元 + 上市满1年"""
    # 2026-09-13 独立审查 §3.2: 改 conn_ctx()（原来三段读完后才 close，中间抛错即漏连接）
    with db.conn_ctx() as conn:
        basic = pd.read_sql(
            "SELECT ts_code, name, fund_type, invest_type, list_date FROM fund_basic "
            "WHERE market='E' AND delist_date IS NULL", conn)
        fd = pd.read_sql("SELECT ts_code, trade_date, close, amount FROM fund_daily", conn)
        fa = pd.read_sql("SELECT ts_code, trade_date, adj_factor FROM fund_adj", conn)
    if basic.empty or fd.empty:
        log.warning("场内基金数据为空")
        return pd.DataFrame()

    c = fd.pivot(index="trade_date", columns="ts_code", values="close").sort_index()
    # 动量/波动用复权价(2026-09-04 审查 P0-7: ETF 分红会让未复权价"假跌", 动量系统性低估;
    # 债券型 ETF 分红尤其频繁)。复权因子缺失(新股/未同步)回退未复权价
    if not fa.empty:
        adj_p = fa.pivot(index="trade_date", columns="ts_code", values="adj_factor").sort_index()
        adj_p = adj_p.reindex(index=c.index, columns=c.columns)
        # 2026-09-06 第十轮 N10-14: 单只 ETF 要么全用复权、要么整只回退 —— 原逐点
        # fillna(1.0) 在"前半缺失=1、后半有真值"的基金上拼接处 adj 跳变产生假动量
        # (股票侧是"整只缺失才降级")。按列广播: 全覆盖列保留真值, 部分覆盖列整列置 1
        full_cov = adj_p.notna() | c.isna()
        use = full_cov.all()
        adj_p = adj_p * use + (~use)
        adj_p = adj_p.fillna(1.0)
        c = c * adj_p
    amt = fd.pivot(index="trade_date", columns="ts_code", values="amount").sort_index()
    # 2026-09-05 审查修复: 行情不足 121 天时 iloc[-61]/[-121] IndexError 崩断月度 plan 阶段;
    # 必须返回空 DataFrame —— builder 调用 etfs.empty/iterrows, list 会 AttributeError
    # (诊断报告核查确认: builder.py:107)
    if len(c) < 121:
        log.warning("场内基金行情不足 121 天(现 %d 天), 跳过基金打分", len(c))
        return pd.DataFrame()
    last_date = c.index[-1]
    cutoff = (pd.Timestamp(last_date) - pd.Timedelta(days=365)).strftime("%Y%m%d")

    mom60 = c.iloc[-1] / c.iloc[-61] - 1
    mom120 = c.iloc[-1] / c.iloc[-121] - 1
    vol20 = c.pct_change().tail(20).std()
    amt20 = amt.tail(20).mean()

    df = pd.DataFrame({"mom60": mom60, "mom120": mom120, "vol20": vol20, "amt20": amt20})
    df = df.join(basic.set_index("ts_code")[["name", "fund_type", "invest_type", "list_date"]])
    df = df[df["list_date"].notna() & (df["list_date"] <= cutoff)]
    df = df[(df["amt20"] >= 1e4) & df["mom60"].notna()]  # 20日均额≥1000万元(千元单位)
    if fund_type:
        sub = df[df["fund_type"] == fund_type]
        if len(sub) >= 3:
            df = sub

    def z(s):
        return (s - s.mean()) / s.std()

    df["score"] = 0.6 * z(df["mom60"]) + 0.4 * z(df["mom120"]) - 0.2 * z(df["vol20"])
    out = df.nlargest(top_n, "score").reset_index()
    out = out[["ts_code", "name", "fund_type", "mom60", "mom120", "amt20", "score"]]
    return out


def otc_fund_picks(n: int = 5, plan_date: str = None) -> dict:
    """场外基金名单: 最新排行快照, 按类别取近3年收益前 n (缺失则用近1年)。
    2026-09-04 审查 P2-6:
    - QDII/FOF 排行数据不再"拉而不用", 一并参与推荐
    - 加入成立满 3 年过滤(新基金无历史业绩可验证, 纯追涨风险高; fund_basic.list_date=成立日)
    注: 最大回撤/波动/规模过滤需基金净值历史(fund_nav 未同步), 属后续数据层工作
    """
    import datetime

    with db.conn_ctx() as conn:                     # 2026-09-13: 同 §3.2（连接卫生）
        fr = pd.read_sql(
            "SELECT * FROM fund_rank WHERE snap_date=(SELECT MAX(snap_date) FROM fund_rank)", conn)
        fb = pd.read_sql("SELECT ts_code, list_date FROM fund_basic", conn)
    if fr.empty:
        log.warning("场外基金排行快照为空, 请先运行基金排行更新")
        return {}
    fb = fb.copy()
    fb["code"] = fb["ts_code"].str.split(".").str[0]
    # 2026-09-06 第十轮 N10-29: 成立年限按方案锚定日(plan_date)而非今天 —— 补建历史
    # 方案时用今天会轻微前视(仅建议名单不进自动交易, 影响小但口径应一致)
    ref = pd.Timestamp(plan_date) if plan_date else pd.Timestamp.now()
    cutoff = (ref - datetime.timedelta(days=365 * 3)).strftime("%Y%m%d")
    out = {}
    for t in ("债券型", "混合型", "股票型", "指数型", "QDII", "FOF"):
        sub = fr[fr["fund_type"] == t].copy()
        if sub.empty:
            continue
        sub = sub.merge(fb[["code", "list_date"]], on="code", how="left")
        sub = sub[sub["list_date"].notna() & (sub["list_date"] <= cutoff)]
        if sub.empty:
            continue
        sort_col = "ret_3y" if sub["ret_3y"].notna().any() else "ret_1y"
        sub = sub.sort_values(sort_col, ascending=False)
        out[t] = sub.head(n)[["code", "name", "ret_1y", "ret_3y"]].to_dict("records")
    return out
