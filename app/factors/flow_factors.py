"""资金类因子: 融资融券 / 个股资金流 / 北向持股 / 大宗交易
(2026-09-11 「芯片式异构架构」阶段 0b)

**为什么单开一个模块**: 这四类信息与现有 31 个量价/财务因子**正交** —— 它们是
「谁在买、用什么钱买」, 而不是「价格怎么走」。芯片架构要让多个 die 去相关,
靠的就是口粮不同(见 plans/abstract-sleeping-rainbow.md §0)。

**单位约定**(踩过的坑, 记录以免后人改错):
    daily.amount              千元
    daily_basic.circ_mv       万元
    margin.rzye/rqye/rzmre    元
    moneyflow.*_amount        万元
    block_trade.vol           万股   (price 为元 → price*vol = 万元)
**因此归一化一律优先用无量纲比值**(如「净流入 / 总买入」), 而不是靠乘以 1e4/1e3
去凑单位 —— 后者一旦上游改单位就会静默算错, 且错得「看起来还挺合理」。

**缺失语义**: 两融标的外、龙虎榜未上榜、无大宗交易的股票为 **NaN(不是 0)** ——
"不是两融标的"与"融资余额为 0"是两回事, 填 0 会把前者错当成后者。LightGBM 原生
处理缺失, 所以保持 NaN 即可。

⚠️ `hk_*` 系列是**研究专用**: hk_hold 在 2024-08-19 交易所停止披露后覆盖度从
4042 只断崖跌到 793 只, 是「含义在 2024-08 静默改变」的特征。方案 §3 不变量 #6
禁止它进入生产特征集 —— 本模块把它单独隔离成一组, 便于按前缀排除。
"""
import logging
from typing import Optional

import numpy as np
import pandas as pd

from app import db
from app import timeutil

log = logging.getLogger(__name__)

# 生产侧排除的前缀(不变量 #6): 见模块 docstring 的 hk_hold 断崖说明
RESEARCH_ONLY_PREFIX = ("hk_",)

# 融资余额下界(元): 低于此值视为「无有意义的杠杆持仓」→ NaN, 见 compute_margin_factors
# 内的小分母清洗说明。两融标的的 rzye 中位数 3.02 亿元, 100 万元下界不会误伤真值。
MARGIN_RZYE_MIN = 1e6


def _pivot(conn, table: str, cols: list, dates: pd.DatetimeIndex) -> dict:
    """按 (trade_date, ts_code) 透视并对齐到完整日频索引。

    与 factor_engine._pivot_multi 的区别: 这里**必须 reindex 到 dates** ——
    两融/大宗是稀疏的(非全市场每日都有行), 不 reindex 会导致 rolling 窗口按
    「有数据的行」计数而不是按交易日计数, 把「20 交易日变化」算成「20 个观测变化」。

    2026-09-12 审查 P2-19: 补 `trade_date >= dates[0]` 下界。原为无 WHERE 全表读入 ——
    输出既然要 reindex 到 dates, **窗口外的行本来就被丢掉**, 却要先在内存里整表透视
    一遍(moneyflow 约 1250 万行 ⇒ 全历史面板, 再砍掉窗口外的行)。工厂/实验按 warmup
    窗口加载(alpha/factory.py 的 `load_market(since=warm)`)再喂进来, 峰值内存与耗时
    几乎全花在算完就扔的数据上。
    """
    # 先归一成 DatetimeIndex: 调用方传 Series 时 dates[0] 会被当成**标签**查找(KeyError: -1),
    # 归一后 [0] 才是位置取值
    dates = pd.DatetimeIndex(dates)
    if len(dates) == 0:
        return {c: pd.DataFrame(index=dates, dtype=np.float32) for c in cols}
    q = ", ".join(["trade_date", "ts_code"] + cols)
    df = pd.read_sql(f"SELECT {q} FROM {table} WHERE trade_date>=?", conn,
                     params=(dates[0].strftime("%Y%m%d"),))
    # 只在窗口内有行的股票, 透视出来会**少若干列**(退市/长期停牌: 全历史表里有行, 窗口内没有)。
    # 这些列在窗口里本来整列 NaN, 但**整列 NaN 也占面板宽度** —— neutralize 的 docstring 已
    # 实测「多出若干列(哪怕全场 NaN)会改变 groupby 累加分块 ⇒ 每个截面平移 1~2 ulp」。
    # 故列集按全历史补齐(只做一次 DISTINCT 索引扫描, 不把行读进内存), 让窗口化取值与原
    # 全表读法**逐位相同** —— 这是纯性能修复, 不得顺带改任何下游读数。
    want_cols = pd.read_sql(f"SELECT DISTINCT ts_code FROM {table} ORDER BY ts_code",
                            conn)["ts_code"]
    out = {}
    for c in cols:
        pv = df.pivot(index="trade_date", columns="ts_code", values=c).sort_index()
        pv.index = pd.to_datetime(pv.index)
        out[c] = pv.reindex(index=dates, columns=want_cols).astype(np.float32)
    return out


def _roll_chg(x: pd.DataFrame, win: int = 20) -> pd.DataFrame:
    """N 交易日变化率。分母为 0(如复牌前 rzye=0)时给 NaN 而非 inf。"""
    prev = x.shift(win)
    return x / prev.replace(0, np.nan) - 1.0


# --------------------------------------------------------------------------
def compute_margin_factors(m: dict, dates: pd.DatetimeIndex) -> dict:
    """融资融券因子。覆盖约 1/3 市场(两融标的), 其余为 NaN。"""
    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        if not db.table_exists(conn, "margin"):
            conn.close()
            log.warning("margin 表不存在, 跳过两融因子")
            return {}
        d = _pivot(conn, "margin", ["rzye", "rqye", "rzmre", "rzche"], dates)
    if d["rzye"].dropna(how="all").empty:
        log.warning("margin 数据为空, 跳过两融因子")
        return {}

    amt = m["amount"]                      # 千元
    circ = m["circ_mv"]                    # 万元
    rzye, rqye = d["rzye"], d["rqye"]      # 元
    rzmre, rzche = d["rzmre"], d["rzche"]  # 元
    # 成交额(元) = amount(千元) * 1e3; 流通市值(元) = circ_mv(万元) * 1e4
    amt_y = amt * 1e3
    circ_y = circ * 1e4

    # ⚠️ 小分母清洗(2026-09-11 实测): rzye 的最小值是 **0**, 0.1% 分位仅 13 万元 ——
    # 这些是刚被调入两融名单/已接近平仓的极小额余额。以它们为分母的比率会爆炸
    # (rzmre-rzche 除以 130 元 → 上万倍), 实测把 margin_net_to_rzye 的均值拉到 **-3.51**
    # (正常应接近 0)。设 100 万元下界并置 NaN: 「余额小到没有意义」在语义上就是缺失,
    # 而不是"一个很大的比率"。两融标的的余额中位数是 3.02 亿元, 该下界不会误伤真值。
    rzye = rzye.where(rzye >= MARGIN_RZYE_MIN)
    rqye = rqye.where(rzye.notna())
    rzmre = rzmre.where(rzye.notna())
    rzche = rzche.where(rzye.notna())

    out = {
        # 融资余额 / 流通市值 = 杠杆持仓存量强度(慢变量)
        "margin_rzye_ratio": (rzye / circ_y.replace(0, np.nan)).astype(np.float32),
        # 融券余额 / 流通市值 = 看空存量强度
        "margin_rqye_ratio": (rqye / circ_y.replace(0, np.nan)).astype(np.float32),
        # 融资买入额 / 成交额 = 当日杠杆买入强度(快变量)
        "margin_buy_share": (rzmre / amt_y.replace(0, np.nan)).astype(np.float32),
        # 融资净买入 / 成交额 = 杠杆净流入
        "margin_net_share": ((rzmre - rzche) / amt_y.replace(0, np.nan)).astype(np.float32),
        # 融资余额 20 交易日变化(趋势)
        "margin_rzye_chg20": _roll_chg(rzye, 20).astype(np.float32),
        # 融资净买入占融资余额 = 相对自身存量的加杠杆速度(与市值无关, 便于跨股比较)
        "margin_net_to_rzye": ((rzmre - rzche) / rzye.replace(0, np.nan)).astype(np.float32),
    }
    return out


def compute_moneyflow_factors(m: dict, dates: pd.DatetimeIndex) -> dict:
    """个股资金流因子(大/中/小/特大单)。全市场每日都有, 覆盖度最好。"""
    cols = ["buy_sm_amount", "buy_md_amount", "buy_lg_amount", "buy_elg_amount",
            "sell_sm_amount", "sell_md_amount", "sell_lg_amount", "sell_elg_amount"]
    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        if not db.table_exists(conn, "moneyflow"):
            conn.close()
            log.warning("moneyflow 表不存在, 跳过资金流因子")
            return {}
        d = _pivot(conn, "moneyflow", cols, dates)
    if d["buy_lg_amount"].dropna(how="all").empty:
        log.warning("moneyflow 数据为空, 跳过资金流因子")
        return {}

    # 用资金流自身的「总买入」做分母 —— 无量纲, 不依赖上游的万元约定
    tot = (d["buy_sm_amount"] + d["buy_md_amount"]
           + d["buy_lg_amount"] + d["buy_elg_amount"]).replace(0, np.nan)
    main_net = ((d["buy_lg_amount"] + d["buy_elg_amount"])
                - (d["sell_lg_amount"] + d["sell_elg_amount"]))
    elg_net = d["buy_elg_amount"] - d["sell_elg_amount"]
    sm_net = d["buy_sm_amount"] - d["sell_sm_amount"]
    lg_net = d["buy_lg_amount"] - d["sell_lg_amount"]

    out = {
        # 主力(大+特大)净流入占比
        "mf_main_net": (main_net / tot).astype(np.float32),
        # 特大单净流入占比(机构/大户)
        "mf_elg_net": (elg_net / tot).astype(np.float32),
        # 大单净流入占比
        "mf_lg_net": (lg_net / tot).astype(np.float32),
        # 小单净流入占比 —— 散户方向, 常作**反向**信号
        "mf_sm_net": (sm_net / tot).astype(np.float32),
        # 主力净流入 20 日滚动均值(去单日噪声)
        "mf_main_net20": (main_net / tot).rolling(20, min_periods=10).mean().astype(np.float32),
        # 主力/散户背离: 主力净买而散户净卖 = 筹码由散转集
        "mf_main_sm_div": ((main_net - sm_net) / tot).astype(np.float32),
    }
    return out


def compute_hk_factors(m: dict, dates: pd.DatetimeIndex) -> dict:
    """北向持股因子。⚠️ **研究专用** —— 2024-08 覆盖度断崖, 见模块 docstring。"""
    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        if not db.table_exists(conn, "hk_hold"):
            conn.close()
            return {}
        d = _pivot(conn, "hk_hold", ["ratio"], dates)
    if d["ratio"].dropna(how="all").empty:
        return {}
    r = d["ratio"]
    return {
        # 持股比例水平 —— 存量(外资偏好)
        "hk_ratio": r.astype(np.float32),
        # 20 交易日持股比例变化(百分点) —— 增量(外资加减仓), 比水平更有信息
        "hk_ratio_chg20": (r - r.shift(20)).astype(np.float32),
    }


def compute_block_factors(m: dict, dates: pd.DatetimeIndex) -> dict:
    """大宗交易因子。稀疏(~300 只/日), 未发生的股票为 NaN。"""
    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        if not db.table_exists(conn, "block_trade"):
            conn.close()
            log.warning("block_trade 表不存在, 跳过大宗因子")
            return {}
        # 已按 (trade_date, ts_code) 聚合, 见 datasource/flow_client._agg_block_trade
        d = _pivot(conn, "block_trade", ["n_trades", "total_amount", "avg_price"], dates)
    if d["n_trades"].dropna(how="all").empty:
        log.warning("block_trade 数据为空, 跳过大宗因子")
        return {}

    close = m["close"]
    n = d["n_trades"]
    out = {
        # 折溢价率: 加权均价 vs 当日收盘。负=折价甩卖(常见), 正=溢价接盘(罕见, 信号强)
        "block_discount": (d["avg_price"] / close.replace(0, np.nan) - 1.0).astype(np.float32),
        # 20 交易日上榜次数(该股被大宗交易的频率)
        "block_cnt20": n.rolling(20, min_periods=1).count().astype(np.float32),
        # 单笔均额(对数, 取对数压尾)
        "block_avg_size": np.log1p(
            (d["total_amount"] / n.replace(0, np.nan)).clip(lower=0)).astype(np.float32),
    }
    return out


def compute_flow_factors(m: dict, dates: pd.DatetimeIndex, include_research: bool = False) -> dict:
    """入口: 汇总全部资金类因子。

    include_research=False(默认) 时**不返回** hk_* 系列 —— 生产链路不得使用,
    见模块 docstring。研究/体检脚本显式传 True。

    ⚠️ broker_recommend(券商金股) **刻意不在这里**: 本函数被
    `factor_engine.build_all`(生产因子表)与 `alpha/factory` 调用, 往里加族 = 改生产
    特征集(须换 pin + A/B)。新族只由 `compute_broker_factors` 单独暴露, 供研究脚本调。
    """
    out = {}
    out.update(compute_margin_factors(m, dates))
    out.update(compute_moneyflow_factors(m, dates))
    out.update(compute_block_factors(m, dates))
    if include_research:
        out.update(compute_hk_factors(m, dates))
    if out:
        log.info("资金类因子: %d 个 (%s)", len(out), ",".join(sorted(out)))
    return out


def _rating_score(s: pd.Series) -> pd.Series:
    """把卖方评级文本映射成序数（研究用；未识别的评级 → NaN，不猜）。

    口径来自 A 股卖方惯例：买入/强烈推荐/推荐 = 2；增持/审慎推荐/优于大市/跑赢行业 = 1；
    中性/持有/观望/同步大市 = 0；减持/卖出/弱于大市/跑输行业 = −1。
    """
    txt = s.fillna("").astype(str)
    out = pd.Series(np.nan, index=s.index, dtype=float)
    rules = (("买入|强烈推荐|推荐", 2.0), ("增持|审慎|优于大市|跑赢", 1.0),
             ("中性|持有|观望|同步", 0.0), ("减持|卖出|弱于大市|跑输", -1.0))
    for pat, val in rules:
        hit = txt.str.contains(pat, regex=True, na=False) & out.isna()
        out[hit] = val
    return out


# --------------------------------------------------------------------------
# 分析师研报预测(第 12 个数据族, 2026-09-13) —— **研究专用**, 不进生产特征集
# --------------------------------------------------------------------------
RC_WINDOW = "30D"          # 滚动窗: 近 30 自然日
RC_UPSIDE_BOUNDS = (-0.5, 2.0)   # 目标价空间只在 [−50%, +200%] 内采信(滤掉脏目标价)
# 补库完成度判据（门禁前的硬闸）：74 个月 × 每月约 5.6k 行 ≈ 41 万行，留两成余量；
# 且最新研报日期不能离"今天"太远（否则说明补库中途停了）。
REPORT_RC_MIN_ROWS = 400_000
REPORT_RC_MAX_STALE_DAYS = 10
# 东财研报（主力源）的完成度门槛：实测每月 0.6k~4k 行（2020-08 起 74 个月）
# ⇒ 全史约 11 万行。门槛取 9 万（留余量），**真正的完成信号是"最新研报够新"**那一条
# —— 起初照抄 Tushare 的 40 万行会把门禁永久卡死（2026-09-13 实测发现）。
RESEARCH_EM_MIN_ROWS = 90_000


def _analyst_frame(conn, source: str) -> pd.DataFrame:
    """把两个源的原始表统一成 `[ts_code, report_date, org_name, rating, target]`。

    `target`（目标价）两源口径不同：Tushare 用 `tp`（缺则 `max_price`）；东财给的是区间
    `aim_price_t`/`aim_price_l` ⇒ **区间取中点**（分析师的中心观点），只有单边就用那一边。
    """
    if source == "em":
        df = pd.read_sql("SELECT ts_code, report_date, org_name, rating, "
                         "aim_price_t, aim_price_l FROM research_report_em", conn)
        t = pd.to_numeric(df["aim_price_t"], errors="coerce")
        lo = pd.to_numeric(df["aim_price_l"], errors="coerce")
        df["target"] = np.where(t.notna() & lo.notna(), (t + lo) / 2.0, t.fillna(lo))
        return df[["ts_code", "report_date", "org_name", "rating", "target"]]
    df = pd.read_sql("SELECT ts_code, report_date, org_name, rating, tp, max_price "
                     "FROM report_rc", conn)
    t = pd.to_numeric(df["tp"], errors="coerce")
    m2 = pd.to_numeric(df["max_price"], errors="coerce")
    df["target"] = t.where(t > 0, m2.where(m2 > 0))
    return df[["ts_code", "report_date", "org_name", "rating", "target"]]


def analyst_source(conn) -> Optional[str]:
    """选主力源：**东财优先**（Tushare 该接口实测 10 次/天，补不动），行数不够再退回 Tushare。"""
    def _rows(tbl):
        if not db.table_exists(conn, tbl):
            return 0
        return int(conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0] or 0)
    if _rows("research_report_em") >= RESEARCH_EM_MIN_ROWS:
        return "em"
    if _rows("report_rc") >= REPORT_RC_MIN_ROWS:
        return "rc"
    return None


def analyst_ready(min_rows_em: int = RESEARCH_EM_MIN_ROWS,
                  min_rows_rc: int = REPORT_RC_MIN_ROWS,
                  max_stale_days: int = REPORT_RC_MAX_STALE_DAYS, today=None) -> tuple:
    """分析师族是否可以纳入门禁判决 ⇒ `(ok, 原因, source)`。

    半张表跑门禁会得到"覆盖不足 ⇒ 因子无效"的**假结论**（本项目头号教训：假结论会被写进台账、
    污染后续所有判断），所以这里按行数 + 最新研报新鲜度双条件拦一道。
    """
    # 2026-09-29 收编 timeutil(东八区): 裸 date.today() 换非东八区机器时新鲜度判定漂一天
    today = pd.Timestamp(today or timeutil.date8())
    conn = db.get_conn()
    try:
        rows = {}
        for tbl in ("research_report_em", "report_rc"):
            rows[tbl] = (int(conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0] or 0)
                         if db.table_exists(conn, tbl) else 0)
            mx = (conn.execute(f"SELECT MAX(report_date) FROM {tbl}").fetchone()[0]
                  if rows[tbl] else None)
            rows[tbl + ":max"] = mx
    finally:
        conn.close()
    em, rc = rows["research_report_em"], rows["report_rc"]
    if em >= min_rows_em:
        src, n, mx = "em", em, rows["research_report_em:max"]
    elif rc >= min_rows_rc:
        src, n, mx = "rc", rc, rows["report_rc:max"]
    else:
        return False, (f"两源都未就绪: 东财 {em:,}/{min_rows_em:,} 行, "
                       f"Tushare {rc:,}/{min_rows_rc:,} 行"), None
    try:
        stale = (today - pd.Timestamp(str(mx))).days
    except Exception:
        return False, f"{src} 最新研报日期不可解析: {mx!r}", src
    if stale > max_stale_days:
        return False, f"{src} 最新研报在 {mx}(距今 {stale} 天 > {max_stale_days}) ⇒ 补库中途停了", src
    return True, f"{src}: {n:,} 行, 最新 {mx}", src




def compute_report_rc_factors(m: dict, dates: pd.DatetimeIndex, source: str = "auto") -> dict:
    """分析师研报预测因子（`report_rc` 表）。返回**按交易日**索引的面板。

    **PIT 口径（硬约束）**：一律按 `report_date`（研报发布日）对齐 —— 第 d 天的因子只用
    `report_date <= d` 的研报；`create_time`（入库时间，历史回填时会整体后移）与 `quarter`
    （报告期，内容口径）**都不许用**，否则就是前视。

    本期只上三个**可精确定义**的因子（更花哨的构造——评级上调比例、EPS 修正、机构分歧度——
    刻意留到这三个判完再谈，避免第一轮就做重假设搜索）：
      `rc_cnt_30`     近 30 日研报**篇数**（注意力；空窗填 0，属真实信息）
      `rc_rating_30`  近 30 日**平均评级分**（买=2 增=1 中=0 减=−1；未识别评级不计入）
      `rc_upside_30`  近 30 日**目标价中位数 ÷ 当日收盘 − 1**（目标价取 `tp`，缺失用 `max_price`；
                      只在 [−50%, +200%] 内采信，越界记 NaN 而不是硬截断）

    ⚠️ 覆盖依赖补库：`report_rc` 由 9 小时限速补库填充（见 09 号），表未填满时因子覆盖会
    明显偏低 —— 门禁脚本会先查覆盖再决定要不要纳入判决（不许用半张表得出结论）。
    """
    conn = db.get_conn()
    try:
        src = analyst_source(conn) if source == "auto" else source
        if src is None:
            log.warning("分析师研报两源都没就绪(东财 research_report_em / Tushare report_rc), 跳过")
            return {}
        df = _analyst_frame(conn, src)
    finally:
        conn.close()
    if df.empty:
        log.warning("%s 表为空, 跳过分析师研报因子（补库未完成?）", src)
        return {}
    df["d"] = pd.to_datetime(df["report_date"], errors="coerce")
    df = df.dropna(subset=["d", "ts_code"])
    if df.empty:
        return {}
    df["rating_s"] = _rating_score(df["rating"])
    df["target"] = pd.to_numeric(df["target"], errors="coerce")
    # 按 (研报发布日, 票) 聚合：篇数 / 平均评级 / 目标价中位数
    g = df.groupby(["d", "ts_code"])
    per_day = pd.DataFrame({
        "cnt": g.size(),
        "rating": g["rating_s"].mean(),
        "target": g["target"].median(),
    }).reset_index()
    idx = pd.DatetimeIndex(dates)
    # 列对齐到**全市场面板**：没被任何研报覆盖的票，"篇数=0"是真实信息（无人覆盖），
    # 不能整列缺失 —— 否则截面只剩"被覆盖过的票"，头部十分位就不是全市场十分位
    # （第 11 族当时就吃了这个亏，见 05 号 F57 的口径限定）。
    cols = pd.Index([str(c) for c in m["close"].columns]) if m else pd.Index(sorted(set(per_day["ts_code"])))
    grid = {}
    for k in ("cnt", "rating", "target"):
        piv = per_day.pivot(index="d", columns="ts_code", values=k)
        # 目标价是"发布时点"的信息 ⇒ 先按日 ffill（旧研报仍然有效）再滚动；篇数/评级按当日原值
        grid[k] = piv.reindex(index=piv.index.union(idx), columns=cols).ffill().reindex(idx) \
            if k == "target" else piv.reindex(index=idx, columns=cols)
    out = {}
    roll = RC_WINDOW
    out["rc_cnt_30"] = (grid["cnt"].fillna(0).rolling(roll, min_periods=1).sum()
                        .reindex(columns=cols, fill_value=0.0).astype(np.float32))
    out["rc_rating_30"] = grid["rating"].rolling(roll, min_periods=1).mean().astype(np.float32)
    out["rc_rating_30"] = out["rc_rating_30"].reindex(columns=cols)
    close = m["close"].reindex(index=idx, columns=cols) if m else None
    if close is not None:
        # 目标价是"发布时点"的信息 ⇒ 先按日 ffill(旧研报仍然有效), 再取近 30 日中位数
        tgt_daily = grid["target"]
        med = tgt_daily.rolling(roll, min_periods=1).median()
        up = med / close.replace(0, np.nan) - 1.0
        lo, hi = RC_UPSIDE_BOUNDS
        out["rc_upside_30"] = (up.where((up >= lo) & (up <= hi))
                               .reindex(columns=cols).astype(np.float32))
    log.info("分析师研报因子(%s): %d 个 (%s) | 研报 %d 行 / %d 票 / %s~%s",
             src, len(out), ",".join(sorted(out)), len(df), len(cols),
             str(df["d"].min().date()), str(df["d"].max().date()))
    return out


# --------------------------------------------------------------------------
# 券商月度金股(第 11 个数据族, 2026-09-13) —— **研究专用**, 不进生产特征集
# --------------------------------------------------------------------------
TOP10_MIN_ROWS = 700_000          # 全史约 110 万行(25 期 × 约 44k)
TOP10_MAX_STALE_DAYS = 120        # 季报披露最长滞后期(年报最晚 4 月底) ⇒ 120 天算新鲜
_INST_KEYWORDS = ("基金", "社保", "保险", "QFII", "券商", "信托", "资管", "银行", "证券")


def holder_ready(min_rows: int = TOP10_MIN_ROWS,
                 max_stale_days: int = TOP10_MAX_STALE_DAYS, today=None) -> tuple:
    """第 13 族（十大流通股东）补库是否足以支撑门禁 —— `(ok, 原因)`。

    与分析师族同一条纪律：**半张表跑门禁会产出假结论并写进台账**。
    新鲜度按 `ann_date`（公告日）判：季报最长滞后约 4 个月 ⇒ 门槛放到 120 天。
    """
    # 2026-09-29 收编 timeutil(东八区): 裸 date.today() 换非东八区机器时新鲜度判定漂一天
    today = pd.Timestamp(today or timeutil.date8())
    conn = db.get_conn()
    try:
        if not db.table_exists(conn, "top10_holder"):
            return False, "top10_holder 表不存在"
        n, mx = conn.execute("SELECT COUNT(*), MAX(ann_date) FROM top10_holder").fetchone()
    finally:
        conn.close()
    if not n:
        return False, "top10_holder 为空(补库未开始)"
    if n < min_rows:
        return False, f"只有 {n:,} 行(需 ≥ {min_rows:,}) ⇒ 补库未完成"
    try:
        stale = (today - pd.Timestamp(str(mx))).days
    except Exception:
        return False, f"最新公告日不可解析: {mx!r}"
    if stale > max_stale_days:
        return False, f"最新公告在 {mx}(距今 {stale} 天 > {max_stale_days}) ⇒ 补库中途停了"
    return True, f"{n:,} 行, 最新公告 {mx}"


def compute_top10_holder_factors(m: dict, dates: pd.DatetimeIndex) -> dict:
    """十大流通股东因子 —— **持仓结构类**（本项目第一类"谁在持有"的信息）。

    **PIT 硬口径**：一律按 `ann_date`（公告日）对齐：第 d 天只用**公告日 ≤ d** 的报告期数据。
    绝不许用 `end_date`（报告期）—— 季报有 1~4 个月滞后，用报告期就是提前知道还没公告的持仓。

    因子（比例都换算成"占流通股的比例"这个 0~1 的小数）：
      `ih_conc`       十大流通股东合计持股比例（集中度水平）
      `ih_fund_ratio` 其中**机构类**（基金/社保/保险/QFII/券商/信托/资管…）合计比例
      `ih_conc_chg`   合计比例相对**上一个报告期**的变动（正 = 十大股东整体在增持）

    为什么加这一族：前 12 族里"注意力类"（研报/券商金股）已证明是弱负向，而持仓结构
    与它们不同源；且第 13 族便宜（25 期 × 8 页 ≈ 8 分钟，无严格限速）。
    """
    conn = db.get_conn()
    try:
        if not db.table_exists(conn, "top10_holder"):
            log.warning("top10_holder 表不存在, 跳过十大流通股东因子")
            return {}
        df = pd.read_sql("SELECT ts_code, ann_date, end_date, hold_float_ratio, holder_type "
                         "FROM top10_holder", conn)
    finally:
        conn.close()
    if df.empty:
        log.warning("top10_holder 表为空, 跳过（补库未完成?）")
        return {}
    df["ratio"] = pd.to_numeric(df["hold_float_ratio"], errors="coerce")
    df = df.dropna(subset=["ratio", "ts_code", "end_date", "ann_date"])
    if df.empty:
        return {}
    ht = df["holder_type"].fillna("").astype(str)
    df["is_inst"] = ht.str.contains("|".join(_INST_KEYWORDS), regex=True, na=False)
    df["ratio_inst"] = df["ratio"].where(df["is_inst"], 0.0)   # 非机构记 0，便于直接求和
    g = (df.groupby(["ts_code", "end_date"])
         .agg(conc=("ratio", "sum"), fund_ratio=("ratio_inst", "sum"),
              ann=("ann_date", "max"))
         .reset_index())
    g["conc"] = g["conc"] / 100.0
    g["fund_ratio"] = g["fund_ratio"] / 100.0
    g = g.sort_values(["ts_code", "end_date"])
    g["conc_chg"] = g.groupby("ts_code")["conc"].diff()
    # ⚠️ 必须把公告日转成 datetime 再 pivot/ffill：直接从 sqlite 读出来是 TEXT（"20260430"），
    # 与 DatetimeIndex 混在一起 reindex/ffill 会**把公告日之前也算出值** = 前视（2026-09-13 测试抓到）。
    g["ann_d"] = pd.to_datetime(g["ann"], format="%Y%m%d", errors="coerce")
    g = g.dropna(subset=["ann_d"])
    if g.empty:
        return {}
    # ⚠️ 同一个公告日可能有**多个报告期**（A 股常见"年报+一季报同日披露"）⇒ pivot 会报
    # "Index contains duplicate entries"。同日多期取**最新报告期**（信息更新、口径一致）；
    # 必须在算完 conc_chg 之后再 dedup（环比要的是完整期序）。2026-09-13 实测踩到。
    g = g.sort_values(["ts_code", "end_date"]).drop_duplicates(
        subset=["ts_code", "ann_d"], keep="last")
    idx = pd.DatetimeIndex(dates)
    cols = pd.Index([str(c) for c in m["close"].columns]) if m else pd.Index(sorted(set(g["ts_code"])))
    out = {}
    for name, key in (("ih_conc", "conc"), ("ih_fund_ratio", "fund_ratio"),
                      ("ih_conc_chg", "conc_chg")):
        piv = g.pivot(index="ann_d", columns="ts_code", values=key)
        wide = (piv.reindex(index=piv.index.union(idx), columns=cols)
                .ffill().reindex(index=idx, columns=cols))
        out[name] = wide.astype(np.float32)
    log.info("十大流通股东因子: %d 个 (%s) | %d 行 / %d 期 / %s~%s",
             len(out), ",".join(sorted(out)), len(df), g["end_date"].nunique(),
             str(g["end_date"].min()), str(g["end_date"].max()))
    return out


def compute_broker_factors(dates: pd.DatetimeIndex) -> dict:
    """券商月度金股因子。返回**按交易日**索引的面板(值取"该交易日所在月"的名单)。

    **PIT 口径(必须照此读)**: Tushare broker_recommend 只给 `month`, 不给发布日;
    券商金股惯例在上月末~本月初发布 ⇒ 本项目一律把该月名单视为
    **"该月第一个交易日已知"**。于是月末截面用**本月**名单 = 零前视(最多晚看 1~2 天),
    这正是阶段 0c 门禁要检验的方向。

    因子(都随月份取同一份名单, 与月内哪一天无关):
      `br_cnt`   当月推荐该股的**券商家数**(注意力/共识强度)
      `br_share` 家数 ÷ **当月最大家数** —— 去掉"当月整体推荐量"漂移
                 (实测 202101 只有 194 行、202103 有 315 行, 绝对计数会被月度强度主导)
      `br_chg`   家数 − 上月家数(上月缺行按 0 ⇒ 当月新进即 +n; 首月为 NaN 预热)
      `br_new`   当月有推荐且上月没有 = 1(纯新进, 事件型)

    ⚠️ 未上榜股票取 **0**(不是 NaN): "没被推荐"是真实信息(注意力为零), 全截面排名才成立。
    """
    conn = db.get_conn()
    try:
        if not db.table_exists(conn, "broker_recommend"):
            log.warning("broker_recommend 表不存在, 跳过券商金股因子")
            return {}
        df = pd.read_sql("SELECT month, broker, ts_code FROM broker_recommend", conn)
    finally:
        conn.close()
    if df.empty:
        log.warning("broker_recommend 表为空, 跳过券商金股因子")
        return {}
    uniq = df.drop_duplicates(["month", "broker", "ts_code"])
    cnt = (uniq.groupby(["month", "ts_code"]).size()
           .unstack(fill_value=0).sort_index().astype(np.float32))
    cnt = cnt.reindex(columns=sorted(cnt.columns))
    prev = cnt.shift(1)
    chg = (cnt - prev.fillna(0)).astype(np.float32)
    new = ((cnt > 0) & (prev.fillna(0) == 0)).astype(np.float32)
    mx = cnt.max(axis=1).replace(0, np.nan)
    share = cnt.div(mx, axis=0).astype(np.float32)
    # 首月是预热: 环比/新进没有上一个月可比, 置 NaN 而不是当成"全部新进"
    if len(cnt):
        chg.iloc[0] = np.nan
        new.iloc[0] = np.nan

    idx = pd.DatetimeIndex(dates)
    mkey = pd.Index(idx.strftime("%Y%m"))
    out = {}
    for nm, wide in (("br_cnt", cnt), ("br_share", share), ("br_chg", chg), ("br_new", new)):
        w = wide.reindex(mkey)
        w.index = idx
        out[nm] = w.astype(np.float32)
    log.info("券商金股因子: %d 个 (%s), 覆盖 %d 个月 / %d 只票",
             len(out), ",".join(sorted(out)), len(cnt), cnt.shape[1])
    return out
