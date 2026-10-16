"""新信息源接入(2026-09-11 「芯片式异构架构」阶段 0a)

**为什么需要这个模块**: 现行集成是 4 个种子跑**同一份** 31 因子, 误差高度相关 ——
换成不同算法族也不解决, 只有换「口粮」才能让多个 die 真正去相关。本模块拉的是
四类**与量价/财务正交**的信息: 杠杆资金、逐笔资金流、外资持股、大宗交易, 外加
股东户数与 PIT 行业分类。

**权限与覆盖**(2026-09-11 实调, 见 plans/abstract-sleeping-rainbow.md §1a):
    接口                起点        死区 2020-09~2023-12   形态
    margin_detail       ~2010-03    ✅                   按日全市场
    moneyflow           2010-01     ✅                   按日全市场
    block_trade         2010-01     ✅(稀疏 ~300只/日)   按日全市场
    hk_hold             2017-03     ⚠️ 见下              按日全市场
    stk_holdernumber    2016-03     ✅                   按报告期
    index_weight        2010-01     ✅                   月频
    index_member        1993        ✅                   PIT!

⚠️ **hk_hold 的硬约束**: 2024-08-19 交易所停止披露后覆盖度从 4042 只断崖跌到
793 只, 此后稳定在 800~960。这是**含义在 2024-08 静默改变**的特征, 模型会学到
完全不同的分布。**只作死区研究, 不得进入生产特征集**(方案 §3 不变量 #6)。

⚠️ **holder_num 的 PIT 纪律**: 必须按 `ann_date` 而非 `end_date` 对齐 —— 季报在
报告期结束后数周才披露, 按 end_date 对齐 = 用了尚未公布的数据(前视)。

上游列名漂移的一贯处理沿用 lhb_client 的口径: 明确列出目标列, 缺列由
`sync_daily_bulk` 的 cols 检查拦截并跳过该日(次日强制重拉窗自愈), 不做静默补 NULL。
"""
import logging
from typing import Optional

import pandas as pd

from app import db
from app import timeutil

log = logging.getLogger(__name__)

# 按日接口的目标列(必须与 app/db.py 的 DDL 逐字一致 —— upsert_df 的契约是
# 「df 必须含目标表全部非缺省列」, 缺列会被写成 NULL 覆盖旧值)
MARGIN_COLS = ["trade_date", "ts_code", "rzye", "rqye", "rzmre", "rqyl",
               "rzche", "rqchl", "rqmcl", "rzrqye"]
MONEYFLOW_COLS = ["ts_code", "trade_date",
                  "buy_sm_vol", "buy_sm_amount", "sell_sm_vol", "sell_sm_amount",
                  "buy_md_vol", "buy_md_amount", "sell_md_vol", "sell_md_amount",
                  "buy_lg_vol", "buy_lg_amount", "sell_lg_vol", "sell_lg_amount",
                  "buy_elg_vol", "buy_elg_amount", "sell_elg_vol", "sell_elg_amount",
                  "net_mf_vol", "net_mf_amount"]
HK_HOLD_COLS = ["trade_date", "ts_code", "name", "vol", "ratio", "exchange"]

# 指数成分: 覆盖大盘/中盘/小盘/创业板/科创, 供「指数成分共现图」(阶段 4) 与基准修复
INDEX_WEIGHT_CODES = ["000300.SH", "000905.SH", "000852.SH", "000906.SH",
                      "399006.SZ", "000688.SH"]
# 从 2019 起(而非 2010): 本项目的评估窗起于 2019-07-22, 更早的成分快照对
# 「训练域 = 选股域」没有增量价值, 但会把拉取请求数翻三倍。
INDEX_WEIGHT_START_YEAR = 2019

# 股东户数: 报告期起点(Tushare 该接口 2016 起有数据)
HOLDER_NUM_START = "20160331"

# 券商月度金股(第 11 个数据族, 2026-09-13): Tushare broker_recommend 实测 **2021-01 起**才有
# 数据(2010/2015/2018 均为空) ⇒ 死区只能取 2021-01~2023-12(36 期, 够走阶段 0c 门禁)。
# ⚠️ PIT: 接口只给 month, 不给发布日 ⇒ 因子层一律按"该月第一个交易日已知"对齐(保守方向)。
BROKER_RECOMMEND_START = "202101"
BROKER_RECOMMEND_COLS = ["month", "broker", "ts_code", "name"]

# 申万行业分类: SW2021 版。L1 = 31 个一级行业(对应 stock_basic.industry 的粒度),
# L2 更细, 一并拉取供图 die 用。
SW_SRC = "SW2021"
SW_LEVELS = ("L1", "L2")


# --------------------------------------------------------------------------
# 按日接口 (复用 DataUpdater.sync_daily_bulk: 缺失日补跑 + 水位 + 盘后重拉)
# --------------------------------------------------------------------------
def sync_margin(u, start_date: Optional[str] = None, end_date: Optional[str] = None) -> int:
    """融资融券明细 → 表 margin。两融标的外为**缺行**(不是 0), 因子层需按此处理。"""
    return u.sync_daily_bulk("margin", "margin_detail", cols=MARGIN_COLS,
                             start_date=start_date, end_date=end_date)


def sync_moneyflow(u, start_date: Optional[str] = None, end_date: Optional[str] = None) -> int:
    """个股资金流(大/中/小/特大单买卖) → 表 moneyflow"""
    return u.sync_daily_bulk("moneyflow", "moneyflow", cols=MONEYFLOW_COLS,
                             start_date=start_date, end_date=end_date)


def sync_hk_hold(u, start_date: Optional[str] = None, end_date: Optional[str] = None) -> int:
    """沪深股通持股 → 表 hk_hold。⚠️ 研究专用: 2024-08 起覆盖度断崖, 见模块 docstring。

    2026-09-17 每日拉取审计: start_date 缺省时 sync_daily_bulk 从全局 START_DATE(2013)
    起扫缺失, 而 hk_hold 数据 **2017-03 才开始** ⇒ 每天在 2013~2017 年代的空数据上
    触发 fail_streak(连续 10 天无数据)熔断, **从未走到当前日期** —— 日更自上线起就是
    死的(水位长期停在最后一次手动回填)。修正: 缺省起点钉到 20170301(表自身首日)。"""
    return u.sync_daily_bulk("hk_hold", "hk_hold", cols=HK_HOLD_COLS,
                             start_date=start_date or "20170301", end_date=end_date)


def _agg_block_trade(df: pd.DataFrame) -> pd.DataFrame:
    """大宗交易: 上游一笔一行(同股同日可多笔) → 按 (trade_date, ts_code) 聚合成一股一行。

    avg_price 用**成交量加权**而非 total_amount/total_vol —— 后者依赖上游 vol/amount
    的单位约定(万股/万元), 一旦上游改单位会静默算错折价率; 加权均价只用 price 与 vol,
    只要两者同比例缩放就不影响结果。
    """
    df = df.copy()
    for c in ("price", "vol"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["price", "vol"])
    if df.empty:
        return df
    df["_amt"] = df["price"] * df["vol"]
    g = df.groupby(["trade_date", "ts_code"], as_index=False).agg(
        n_trades=("price", "size"),
        total_vol=("vol", "sum"),
        total_amount=("_amt", "sum"),
    )
    g["avg_price"] = g["total_amount"] / g["total_vol"].replace(0, pd.NA)
    # total_amount 内部口径 = price*vol; 对外保持「成交额」语义, 单位随上游 vol 单位而定,
    # 因子层只用 avg_price 与 n_trades, 不依赖该列的量纲。
    return g[["trade_date", "ts_code", "n_trades", "total_vol", "total_amount", "avg_price"]]


def sync_block_trade(u, start_date: Optional[str] = None, end_date: Optional[str] = None) -> int:
    """大宗交易 → 表 block_trade(已聚合)。cols 必须为 None: 让 transform 的输出直接入库。"""
    return u.sync_daily_bulk("block_trade", "block_trade", cols=None,
                             transform=_agg_block_trade,
                             start_date=start_date, end_date=end_date)


# --------------------------------------------------------------------------
# 按期频接口 (不走 sync_daily_bulk: 它们的「日期」不是交易日历上的日)
# --------------------------------------------------------------------------
def _quarter_ends(start: str, end: str) -> list:
    """[start, end] 区间内的自然季度末 YYYYMMDD 列表"""
    per = pd.period_range(pd.Timestamp(start), pd.Timestamp(end), freq="Q")
    return [p.end_time.strftime("%Y%m%d") for p in per]


def _months(start: str, end: str) -> list:
    """[start, end] 区间内的自然月 **YYYYMM** 列表(输入容错: 202101 或 2021-01 都可)。

    ⚠️ 必须是 `strftime("%Y%m")`: 新版 pandas 的 `str(Period)` 给的是 **"2021-01"**,
    而 Tushare 的 `month` 参数只认 `202101` —— 传错格式接口**不报错、直接返回空**,
    于是补库"成功写出 0 行"(2026-09-13 实测踩到)。这类静默空转由本函数 + 调用方的
    "全月为空即告警"一起挡。
    """
    s = pd.Period(pd.Timestamp(start if len(start) > 6 else start + "01"), freq="M")
    e = pd.Period(pd.Timestamp(end if len(end) > 6 else end + "01"), freq="M")
    return [p.strftime("%Y%m") for p in pd.period_range(s, e, freq="M")]


def sync_broker_recommend(u, start: str = BROKER_RECOMMEND_START,
                          end: Optional[str] = None) -> int:
    """券商月度金股 → 表 broker_recommend(月频, 全史约 70 次调用 ≈ 25 秒)。

    最近 2 个月**无条件重拉**(券商会在月内追加/更正名单), 更早只补缺口 —— 与
    `sync_holder_num` 的"最近 4 期强制重拉"同一思路。返回净写入行数。
    """
    end = end or timeutil.today().strftime("%Y%m")
    months = _months(start, end)
    if not months:
        return 0
    # 2026-09-19 审查: 原 `get_conn() … conn.close()` 没有 try/finally —— 中间任何一步抛错就漏
    # 一个连接(长驻服务里累积句柄, 且它可能正持着写锁)。改 conn_ctx(), 与本仓 2026-09-13
    # 起的连接卫生口径一致。
    have = set()
    with db.conn_ctx() as conn:
        if db.table_exists(conn, "broker_recommend"):
            have = set(pd.read_sql("SELECT DISTINCT month FROM broker_recommend", conn)["month"])
    todo = sorted(set([m for m in months if m not in have] + months[-2:]))
    total, n_ok = 0, 0
    conn = db.get_conn()
    try:
        for m in todo:
            try:
                df = u.ts.call("broker_recommend", month=m)
            except Exception as e:
                log.error("broker_recommend %s 失败: %s, 跳过(下次续跑)", m, e)
                continue
            if u.ts.last_status == "perm":
                log.error("broker_recommend 接口权限不足, 终止本轮(已到 %s)", m)
                break
            if df is None or df.empty:
                continue
            n_ok += 1
            miss = [c for c in BROKER_RECOMMEND_COLS if c not in df.columns]
            if miss:
                log.error("broker_recommend %s 缺列 %s, 跳过(先修客户端)", m, miss)
                continue
            total += db.upsert_df(conn, df[BROKER_RECOMMEND_COLS], "broker_recommend")
            conn.commit()
    finally:
        conn.close()
    if todo and not n_ok:
        # 全月为空 = 可疑(格式/权限/接口改名), 不许静默通过
        log.warning("broker_recommend: %d 个月份全部返回空 —— 检查 month 格式(须 YYYYMM)"
                    "与接口权限, 本次未写入任何行", len(todo))
    else:
        log.info("broker_recommend: %d/%d 月有数据, 净写入 %d 行", n_ok, len(todo), total)
    return total


def sync_holder_num(u, start: str = HOLDER_NUM_START,
                    end: Optional[str] = None) -> int:
    """股东户数 → 表 holder_num。

    每次重拉**最近 4 个报告期**再回填历史缺口: 户数会在后续报告期里被更正披露,
    只按「哪个 end_date 还没有行」判增量会把更正永久漏掉(与 sync_daily_bulk 的
    「盘后强制重拉最后两日」同一思路)。
    """
    end = end or timeutil.today().strftime("%Y%m%d")
    periods = _quarter_ends(start, end)
    if not periods:
        return 0
    have = set()
    with db.conn_ctx() as conn:      # 同上: 连接卫生(2026-09-19 审查)
        if db.table_exists(conn, "holder_num"):
            have = set(pd.read_sql("SELECT DISTINCT end_date FROM holder_num", conn)["end_date"])
    # 最近 4 期无论如何重拉(更正披露), 更早的只补缺口
    todo = [p for p in periods if p not in have] + periods[-4:]
    todo = sorted(set(todo))
    cols = ["ts_code", "end_date", "ann_date", "holder_num"]
    total = 0
    conn = db.get_conn()
    try:
        for p in todo:
            try:
                df = u.ts.call("stk_holdernumber", enddate=p)
            except Exception as e:
                log.error("holder_num %s 失败: %s, 跳过(下次续跑)", p, e)
                continue
            if u.ts.last_status == "perm":
                log.error("stk_holdernumber 接口权限不足, 终止本轮(已到 %s)", p)
                break
            if df is None or df.empty:
                continue
            miss = [c for c in cols if c not in df.columns]
            if miss:
                log.warning("holder_num %s 缺列 %s(上游字段变更?), 跳过", p, miss)
                continue
            df = df[df["ann_date"].notna() & (df["ann_date"] != "")]
            if df.empty:
                continue
            total += db.upsert_df(conn, df[cols], "holder_num")
        if total:
            db.set_status(conn, "holder_num", todo[-1], total)
    finally:
        conn.close()
    log.info("股东户数同步结束, 本次 %d 行 (%d 个报告期)", total, len(todo))
    return total


def sync_index_weight(u, codes=None, start_year: int = INDEX_WEIGHT_START_YEAR) -> int:
    """指数成分权重 → 表 index_weight。

    ⚠️ **必须按年显式指定 start_date/end_date** —— 2026-09-11 实测: 不带日期范围时
    上游只返回近 ~2 年(7000 行/指数 ≈ 23 个月), 静默丢失更早历史。显式给区间才给全量。
    上游按月发布成分快照, 幂等覆盖, 可随时重跑。
    """
    codes = codes or INDEX_WEIGHT_CODES
    end_year = timeutil.today().year
    cols = ["index_code", "con_code", "trade_date", "weight"]
    total = 0
    conn = db.get_conn()
    try:
        # 2026-09-16 数据线审查 DATA-07: 原来每天 6 指数 × 全历史逐年 = 48 次调用全量重拉。
        # 上游按月发布、幂等覆盖 ⇒ 库内 max(trade_date) 已达去年底/今年的指数只需拉本年，
        # 没有该指数任何行时才拉全历史（首次初始化路径不变）。
        have_max = dict(conn.execute(
            "SELECT index_code, MAX(trade_date) FROM index_weight "
            "WHERE index_code IN (%s) GROUP BY index_code"
            % ",".join("?" * len(codes)), tuple(codes)).fetchall())
        for code in codes:
            n_code = 0
            yr0 = start_year
            mx = have_max.get(code)
            if mx:
                yr0 = max(start_year, int(str(mx)[:4]))
                if int(str(mx)[:6]) >= (end_year - 1) * 100 + 12:
                    yr0 = end_year          # 上年末之后的数据齐备 ⇒ 只拉本年
            for yr in range(yr0, end_year + 1):
                try:
                    df = u.ts.call("index_weight", index_code=code,
                                   start_date=f"{yr}0101", end_date=f"{yr}1231")
                except Exception as e:
                    log.error("index_weight %s %d 失败: %s, 跳过该年", code, yr, e)
                    continue
                if u.ts.last_status == "perm":
                    log.error("index_weight 接口权限不足, 终止本轮")
                    return total
                if df is None or df.empty:
                    continue
                miss = [c for c in cols if c not in df.columns]
                if miss:
                    log.warning("index_weight %s %d 缺列 %s, 跳过", code, yr, miss)
                    continue
                n_code += db.upsert_df(conn, df[cols], "index_weight")
            total += n_code
            log.info("index_weight %s: %d 行", code, n_code)
        if total:
            db.set_status(conn, "index_weight", "", total)
    finally:
        conn.close()
    log.info("指数成分同步结束, 本次 %d 行", total)
    return total


def sync_sw_industry(u, force_members: bool = False) -> int:
    """申万行业分类 + 成分股 → 表 sw_classify / sw_member。

    **这是 PIT 行业分类的唯一来源**, 用于修掉一个已登记的数据缺陷:
    原 `stock_basic.industry` 是**当前快照**, 拿它做历史行业中性化/标签中性化等于
    用了未来信息; 且 333 只 industry 为 NULL 被 fillna("未知") 当成同一个行业参与中性化。
    `sw_member.in_date/out_date` 给出真实的时点归属区间, 可重建任意历史日的行业映射。
    """
    total_c, total_m = 0, 0
    # 2026-09-16 数据线审查 DATA-07②: sw_member 每天全量重拉 = 160+ 次 index_member
    # 调用/天。成员表是时点归属(in_date/out_date)——每周一全量刷新一次足以承接成员
    # 变动, 其余 6 天跳过(省 ~85% 配额; classify 只有 2 次调用, 保持每日)。
    _skip_members = (timeutil.today().weekday() != 0) and not force_members
    if _skip_members:
        log.info("申万行业: 非周一, 跳过成员全量重拉(每周一刷新, DATA-07)")
    conn = db.get_conn()
    try:
        for lvl in SW_LEVELS:
            try:
                cls = u.ts.call("index_classify", level=lvl, src=SW_SRC)
            except Exception as e:
                log.error("index_classify %s 失败: %s, 跳过", lvl, e)
                continue
            if u.ts.last_status == "perm":
                log.error("index_classify 接口权限不足, 终止本轮")
                break
            if cls is None or cls.empty:
                continue
            ccols = ["index_code", "industry_name", "level", "industry_code",
                     "parent_code", "src"]
            miss = [c for c in ccols if c not in cls.columns]
            if miss:
                log.warning("index_classify %s 缺列 %s, 跳过", lvl, miss)
                continue
            total_c += db.upsert_df(conn, cls[ccols], "sw_classify")
            codes = cls["index_code"].tolist()
            if _skip_members:
                continue
            log.info("申万 %s: %d 个行业, 开始拉成分股", lvl, len(codes))
            mcols = ["con_code", "index_code", "in_date", "out_date", "is_new"]
            for ic in codes:
                try:
                    mem = u.ts.call("index_member", index_code=ic)
                except Exception as e:
                    log.error("index_member %s 失败: %s, 跳过", ic, e)
                    continue
                if u.ts.last_status == "perm":
                    log.error("index_member 接口权限不足, 终止")
                    break
                if mem is None or mem.empty:
                    continue
                miss = [c for c in mcols if c not in mem.columns]
                if miss:
                    log.warning("index_member %s 缺列 %s, 跳过", ic, miss)
                    continue
                total_m += db.upsert_df(conn, mem[mcols], "sw_member")
        if total_c:
            db.set_status(conn, "sw_classify", "", total_c)
        if total_m:
            db.set_status(conn, "sw_member", "", total_m)
    finally:
        conn.close()
    log.info("申万行业同步结束: 分类 %d 行, 成分 %d 行", total_c, total_m)
    return total_m


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------
DAILY_SOURCES = (("margin", sync_margin), ("moneyflow", sync_moneyflow),
                 ("block_trade", sync_block_trade), ("hk_hold", sync_hk_hold))


def sync_new_sources(u, start_date: Optional[str] = None,
                     end_date: Optional[str] = None,
                     skip_hk: bool = False) -> None:
    """拉全部新信息源, 单个失败不阻塞其余(与 DataUpdater.sync_lhb 同一纪律)。

    start_date: 按日接口的日历下界。传 2020-09 可先拿死区窗口的结论,
                日后再不传(从 START_DATE)补齐全历史 —— 增量逻辑自动衔接。
    skip_hk:    hk_hold 是研究专用且 2024-08 后覆盖度断崖, 无需日更。
    """
    for name, fn in DAILY_SOURCES:
        if skip_hk and name == "hk_hold":
            continue
        try:
            fn(u, start_date=start_date, end_date=end_date)
        except Exception:
            log.exception("%s 同步异常, 不影响其余源", name)
    for name, fn in (("holder_num", sync_holder_num), ("index_weight", sync_index_weight),
                     ("sw_industry", sync_sw_industry)):
        try:
            fn(u)
        except Exception:
            log.exception("%s 同步异常, 不影响其余源", name)
    # 券商金股是**月频**: 日更只刷最近 3 个月(名单会被追加/更正), 历史靠一次性补库
    # (`python scripts/oneoff/backfill_broker_recommend.py`, 全史约 70 次调用)
    try:
        recent = (pd.Timestamp(timeutil.today()).normalize() - pd.DateOffset(months=3)).strftime("%Y%m")
        sync_broker_recommend(u, start=recent)
    except Exception:
        log.exception("broker_recommend 同步异常, 不影响其余源")
    # 业绩预告(2026-09-13 新增, 第 6 个数据族): **只重拉最近 4 个报告期** ——
    # 预告会在报告期后持续披露/更正几个月, 天天全量重拉 54 期毫无必要;
    # 首次用 `python -m app.datasource.event_client` 补全历史(约 4 分钟)。
    try:
        from app.datasource import event_client
        # 上界要往后放: 当期(还没到期)的报告期此时正在密集披露预告
        recent = event_client._quarter_ends(
            (pd.Timestamp(timeutil.today()) - pd.Timedelta(days=400)).strftime("%Y%m%d"),
            (pd.Timestamp(timeutil.today()) + pd.Timedelta(days=130)).strftime("%Y%m%d"))[-4:]
        if recent:
            event_client.sync_forecast(start=recent[0], end=recent[-1])
    except Exception:
        log.exception("forecast 同步异常, 不影响其余源")
