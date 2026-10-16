"""SQLite 本地数据仓库"""
import contextlib
import logging
import sqlite3
from pathlib import Path
from typing import Iterator

import pandas as pd

from app import timeutil
from app.config import DATA_DIR

log = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "neweco.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_cal (
    cal_date TEXT PRIMARY KEY,
    is_open INTEGER
);
CREATE TABLE IF NOT EXISTS stock_basic (
    ts_code TEXT PRIMARY KEY,
    symbol TEXT, name TEXT, area TEXT, industry TEXT, market TEXT,
    list_date TEXT, delist_date TEXT
);
CREATE TABLE IF NOT EXISTS daily (
    ts_code TEXT, trade_date TEXT,
    open REAL, high REAL, low REAL, close REAL,
    pre_close REAL, change REAL, pct_chg REAL, vol REAL, amount REAL,
    PRIMARY KEY (ts_code, trade_date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS adj_factor (
    ts_code TEXT, trade_date TEXT, adj_factor REAL,
    PRIMARY KEY (ts_code, trade_date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS daily_basic (
    ts_code TEXT, trade_date TEXT,
    turnover_rate REAL, turnover_rate_f REAL, volume_ratio REAL,
    pe REAL, pe_ttm REAL, pb REAL, dv_ratio REAL, dv_ttm REAL,
    total_mv REAL, circ_mv REAL,
    PRIMARY KEY (ts_code, trade_date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS index_daily (
    ts_code TEXT, trade_date TEXT,
    open REAL, high REAL, low REAL, close REAL, pre_close REAL,
    pct_chg REAL, vol REAL, amount REAL,
    PRIMARY KEY (ts_code, trade_date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fund_basic (
    ts_code TEXT PRIMARY KEY,
    name TEXT, management TEXT, fund_type TEXT, market TEXT,
    invest_type TEXT, list_date TEXT, delist_date TEXT, benchmark TEXT,
    m_fee REAL, c_fee REAL
);
CREATE TABLE IF NOT EXISTS fund_daily (
    ts_code TEXT, trade_date TEXT,
    open REAL, high REAL, low REAL, close REAL,
    pre_close REAL, pct_chg REAL, vol REAL, amount REAL,
    PRIMARY KEY (ts_code, trade_date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fund_adj (
    ts_code TEXT, trade_date TEXT, adj_factor REAL,
    PRIMARY KEY (ts_code, trade_date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fund_nav (
    ts_code TEXT, nav_date TEXT, unit_nav REAL, accum_nav REAL,
    PRIMARY KEY (ts_code, nav_date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fund_rank (
    snap_date TEXT, code TEXT, name TEXT, fund_type TEXT,
    ret_1y REAL, ret_3y REAL,
    PRIMARY KEY (snap_date, code)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS repo_rate (
    trade_date TEXT PRIMARY KEY, overnight REAL, r7d REAL, r14d REAL
);
CREATE TABLE IF NOT EXISTS fina_indicator (
    ts_code TEXT, end_date TEXT, ann_date TEXT,
    roe REAL, netprofit_yoy REAL, or_yoy REAL, grossprofit_margin REAL,
    debt_to_assets REAL, eps REAL,
    PRIMARY KEY (ts_code, end_date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS lhb (
    trade_date TEXT, ts_code TEXT, name TEXT,
    close REAL, pct_chg REAL,
    net_buy REAL, buy_value REAL, sell_value REAL,
    lhb_amount REAL, market_amount REAL,
    net_ratio REAL, amount_ratio REAL,
    turnover REAL, float_mv REAL, reason TEXT,
    PRIMARY KEY (trade_date, ts_code)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS namechange (
    ts_code TEXT, name TEXT, start_date TEXT, end_date TEXT,
    ann_date TEXT, change_reason TEXT,
    PRIMARY KEY (ts_code, start_date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS lhb_org (
    trade_date TEXT, ts_code TEXT, name TEXT,
    org_buy_num REAL, org_sell_num REAL,
    org_buy_value REAL, org_sell_value REAL, org_net_value REAL,
    org_net_ratio REAL, reason TEXT,
    PRIMARY KEY (trade_date, ts_code)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS lhb_ths (
    trade_date TEXT, thscode TEXT, name TEXT,
    change REAL, net_value REAL, buy_value REAL, sell_value REAL,
    hot_rank INTEGER, org_net_value REAL, org_net_rate REAL,
    org_buy_num INTEGER, org_sell_num INTEGER,
    limit_reason TEXT, concepts TEXT, range_days INTEGER,
    PRIMARY KEY (trade_date, thscode, range_days)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS lhb_ths_hm (
    trade_date TEXT, hm_name TEXT, thscode TEXT, name TEXT,
    net_value REAL, buying REAL,
    PRIMARY KEY (trade_date, hm_name, thscode)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS factor (
    ts_code TEXT, trade_date TEXT, factor TEXT, value REAL,
    PRIMARY KEY (ts_code, trade_date, factor)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS model_score (
    ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL,
    PRIMARY KEY (ts_code, trade_date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS daily_score (
    ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL,
    PRIMARY KEY (ts_code, trade_date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS plan (
    plan_date TEXT, plan_type TEXT, ts_code TEXT, name TEXT,
    weight REAL, shares INTEGER, industry TEXT, reason TEXT,
    PRIMARY KEY (plan_date, plan_type, ts_code)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS backtest_summary (
    run_date TEXT PRIMARY KEY, metrics TEXT
);CREATE TABLE IF NOT EXISTS backtest_nav (
    run_date TEXT, trade_date TEXT, nav REAL, sys_nav REAL, bench_nav REAL,
    PRIMARY KEY (run_date, trade_date)
) WITHOUT ROWID;
-- 2026-09-06 R18-1: 估值层 PE 序列落库 —— 接口不可用时降级用库内最近一次有效值
-- ffill 保持 7 分制连续, 价格层照常更新(不再整层冻结)
CREATE TABLE IF NOT EXISTS valuation_pe (
    trade_date TEXT PRIMARY KEY, pe_ttm REAL
) WITHOUT ROWID;
-- 2026-09-06 R14-4/D7: 回测日频净值(月内盯市), 只观察不改交易; 月频回撤低估月内回撤,
-- 日频序列给出实盘口径的诚实回撤(mdd_daily)。月频表保持不动, 历史对比不破坏
CREATE TABLE IF NOT EXISTS backtest_nav_daily (
    run_date TEXT, trade_date TEXT, nav REAL, sys_nav REAL, bench_nav REAL,
    PRIMARY KEY (run_date, trade_date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS model_importance (
    train_date TEXT, factor TEXT, importance REAL,
    PRIMARY KEY (train_date, factor)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS regime (
    trade_date TEXT PRIMARY KEY, state TEXT, score REAL, drawdown REAL, vol REAL,
    pool_state TEXT, pool_score REAL, pool_drawdown REAL, pool_vol REAL
);
CREATE TABLE IF NOT EXISTS pool_index (
    trade_date TEXT PRIMARY KEY, ret REAL, close REAL, n INTEGER
);
CREATE TABLE IF NOT EXISTS signal (
    signal_date TEXT, ts_code TEXT, name TEXT, action TEXT, reason TEXT,
    PRIMARY KEY (signal_date, ts_code, action)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS data_status (
    name TEXT PRIMARY KEY, last_date TEXT, updated_at TEXT, rows INTEGER
);
CREATE TABLE IF NOT EXISTS paper_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash REAL, created_at TEXT, last_applied_plan TEXT, initial_capital REAL
);
CREATE TABLE IF NOT EXISTS paper_position (
    ts_code TEXT PRIMARY KEY, name TEXT, shares INTEGER, avg_cost REAL, entry_adj REAL
);
CREATE TABLE IF NOT EXISTS paper_order (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_date TEXT, ts_code TEXT, name TEXT, side TEXT, shares INTEGER,
    reason TEXT, status TEXT DEFAULT 'pending', first_attempt TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS paper_trade (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exec_date TEXT, ts_code TEXT, name TEXT, side TEXT, shares INTEGER,
    price REAL, cost REAL, amount REAL, reason TEXT
);
CREATE TABLE IF NOT EXISTS paper_nav (
    trade_date TEXT PRIMARY KEY, nav REAL, cash REAL, market_value REAL, bench_nav REAL
);
CREATE TABLE IF NOT EXISTS paper_interest (
    trade_date TEXT PRIMARY KEY, amount REAL
);
CREATE TABLE IF NOT EXISTS notify_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT, tag TEXT, ok INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS pipeline_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, ended_at TEXT,
    stage TEXT, run_date TEXT, status TEXT, rows INTEGER, error TEXT
);
-- ============================================================================
-- 2026-09-11 「芯片式异构架构」阶段 0a: 新信息源接入
-- 目的: 现有 4 个集成种子吃的是**同一份** 31 因子, 误差高度相关 —— 换算法族
-- 也不解决, 只有换「口粮」(信息源) 才能让多个 die 真正去相关。
-- 下列数据家族均经实调确认有 Tushare 权限, 且**完整覆盖死区 2020-09~2023-12**
-- (该 39 个月现行模型超额为负, 是唯一还有空间的地块; 见 plans/abstract-sleeping-rainbow.md)。
-- 来源接口见 app/datasource/flow_client.py; 拉取状态见 data_status 同名记录。
-- ============================================================================
-- 融资融券明细(2010-03 起, 按日全市场; 两融标的外为缺行, 非 0)
CREATE TABLE IF NOT EXISTS margin (
    trade_date TEXT, ts_code TEXT,
    rzye REAL, rqye REAL, rzmre REAL, rqyl REAL, rzche REAL,
    rqchl REAL, rqmcl REAL, rzrqye REAL,
    PRIMARY KEY (trade_date, ts_code)
) WITHOUT ROWID;
-- 个股资金流(2010-01 起, 按日全市场; 大/中/小/特大单买卖额)
CREATE TABLE IF NOT EXISTS moneyflow (
    ts_code TEXT, trade_date TEXT,
    buy_sm_vol REAL, buy_sm_amount REAL, sell_sm_vol REAL, sell_sm_amount REAL,
    buy_md_vol REAL, buy_md_amount REAL, sell_md_vol REAL, sell_md_amount REAL,
    buy_lg_vol REAL, buy_lg_amount REAL, sell_lg_vol REAL, sell_lg_amount REAL,
    buy_elg_vol REAL, buy_elg_amount REAL, sell_elg_vol REAL, sell_elg_amount REAL,
    net_mf_vol REAL, net_mf_amount REAL,
    PRIMARY KEY (trade_date, ts_code)
) WITHOUT ROWID;
-- 沪深股通持股(2017-03 起)。⚠️ 2024-08-19 交易所停止披露: 覆盖度从 4042 只
-- 断崖跌到 793 只(2026-09-11 实测), 是**含义在 2024-08 静默改变**的特征。
-- 硬性不变量: 只作死区研究, 不得进入生产特征集(否则模型学到的是另一个分布)。
CREATE TABLE IF NOT EXISTS hk_hold (
    trade_date TEXT, ts_code TEXT, name TEXT,
    vol REAL, ratio REAL, exchange TEXT,
    PRIMARY KEY (trade_date, ts_code)
) WITHOUT ROWID;
-- 大宗交易(2010-01 起, 稀疏: 约 300 只/日)。原始接口一笔一行(同股同日可多笔),
-- 落库前**按 (trade_date, ts_code) 聚合**为一行: 笔数/总量/总额/加权均价。
-- 折价率(avg_price vs 当日 close)在因子层算, 避免此处依赖 daily 的可用性。
CREATE TABLE IF NOT EXISTS block_trade (
    trade_date TEXT, ts_code TEXT,
    n_trades INTEGER, total_vol REAL, total_amount REAL, avg_price REAL,
    PRIMARY KEY (trade_date, ts_code)
) WITHOUT ROWID;
-- 股东户数(2016-03 起, 报告期)。带 ann_date = 真正的 PIT 披露日 —— 因子必须按
-- ann_date 而非 end_date 对齐, 否则用了尚未公布的季报(前视)。PK 含 ann_date
-- 以容纳同一报告期的更正披露。
CREATE TABLE IF NOT EXISTS holder_num (
    ts_code TEXT, end_date TEXT, ann_date TEXT, holder_num REAL,
    PRIMARY KEY (ts_code, end_date, ann_date)
) WITHOUT ROWID;
-- 券商月度金股(2026-09-13 新增, Tushare broker_recommend)。**第 11 个数据族**, 也是本项目
-- 唯一一类"卖方注意力/共识"信息(与量价、资金流、财务都不同源)。
-- ⚠️ **PIT 口径**: 接口只给 month(如 202609), **不给发布日**; 券商金股惯例在**上月末~本月初**
-- 发布 ⇒ 本项目一律按"**该月第一个交易日已知**"对齐(保守: 绝不可能前视, 代价是最多晚看 1~2 天)。
-- ⚠️ 覆盖起点: 接口 2021-01 起才有数据(2018/2015/2010 实测为空) ⇒ 死区只能取 2021-01~2023-12。
-- 主键含 broker: 同一券商在同一月可能重复推荐同一票(多次发布), upsert 幂等去重。
CREATE TABLE IF NOT EXISTS broker_recommend (
    month TEXT, broker TEXT, ts_code TEXT, name TEXT,
    PRIMARY KEY (month, broker, ts_code)
) WITHOUT ROWID;
-- 分析师研报预测(2026-09-13 新增, Tushare report_rc)。**第 12 个数据族** —— 本项目里最接近
-- "卖方一致预期"的一类信息(评级 / 目标价 / EPS 预测 / 机构)，与量价、资金流、财务都不同源。
-- ⚠️ **PIT 必须用 `report_date`**(研报发布日)：不许用 `create_time`(入库时间, 历史回填时会整体
-- 后移到回填当天) 或 `quarter`(报告期, 属内容口径) —— 两者都会造成前视。
-- ⚠️ 限速: 该接口 **1 次/分钟 + 10 次/小时**(2026-09-13 实测两种文案) ⇒ 全史(2020-08 起 74 个月、
-- 每月约 5600 行、单次上限 5000 行)约 83 次调用 ≈ 9 小时 ⇒ 只能由
-- `scripts/oneoff/backfill_report_rc.py` 断点续跑(检查点 `data/.report_rc_backfill.json`)。
-- 主键 (report_date, ts_code, org_name, report_title)：接口没有唯一 ID；同一机构同日多篇研报
-- 标题不同即不同行，用这四列做幂等键(重复拉取不会翻倍)。
CREATE TABLE IF NOT EXISTS report_rc (
    ts_code TEXT, name TEXT, report_date TEXT, report_title TEXT, report_type TEXT,
    classify TEXT, org_name TEXT, author_name TEXT, quarter TEXT,
    op_rt REAL, op_pr REAL, tp REAL, np REAL, eps REAL, pe REAL, rd REAL, roe REAL,
    ev_ebitda REAL, rating TEXT, max_price REAL, min_price REAL, imp_dg TEXT,
    create_time TEXT,
    PRIMARY KEY (report_date, ts_code, org_name, report_title)
) WITHOUT ROWID;
-- 东财研报(AkShare `stock_research_report_em`, 2026-09-13 新增)。**报告期补源**: Tushare 的
-- `report_rc` 实测限速是 **10 次/天**(不是 1 次/分钟) ⇒ 74 个月要 8 天多, 单靠它做不完研究;
-- 东财同源数据无此限额(单只票 4.6 秒拿到 2017 起的整段历史), 且额外带**东财评级/机构/盈利预测**。
-- ⚠️ PIT: 同 `report_rc`, 一律按 `report_date`(研报日期) 对齐。
-- ⚠️ **盈利预测的年份是滚动的**(今天是 2026/2027/2028, 明年就变) ⇒ 按**位置**存
-- `fy1_*/fy2_*/fy3_*`, 并把年份标签单独写进 `fy_years`(否则跨年重跑会对着错的年份读)。
-- 主键 (ts_code, report_date, org_name, report_title): 东财列表无唯一 ID, 这四列做幂等键。
CREATE TABLE IF NOT EXISTS research_report_em (
    ts_code TEXT, name TEXT, report_date TEXT, report_title TEXT, org_name TEXT,
    rating TEXT, rating_prev TEXT, rating_change TEXT, industry TEXT, fy_years TEXT,
    fy1_eps REAL, fy1_pe REAL, fy2_eps REAL, fy2_pe REAL, fy3_eps REAL, fy3_pe REAL,
    aim_price_t REAL, aim_price_l REAL, report_type TEXT, author TEXT,
    info_code TEXT, encode_url TEXT,
    PRIMARY KEY (info_code)
) WITHOUT ROWID;
-- 十大流通股东(Tushare `top10_floatholders`, 2026-09-13 新增)。**第 13 个数据族**, 也是本项目
-- 第一类"**机构持仓结构**"信息(前 10 大流通股东名单/比例/变动/类型) —— 与注意力类(研报/金股)
-- 和资金流类都不同源。
-- ⚠️ **PIT 必须用 `ann_date`**(公告日, 随季报披露)：`end_date` 是报告期，用它就是提前知道还没
-- 公告的持仓。同一 (end_date, ts_code, holder_name) 可能因更正公告再次出现 ⇒ 主键不含 ann_date,
-- 重拉即覆盖成最新公告日(幂等)。
-- ⚠️ 单次返回上限 6000 行、`offset` 翻页可用；2026-09-13 实测**无明显限速**(3 次/9 秒全通)。
CREATE TABLE IF NOT EXISTS top10_holder (
    ts_code TEXT, ann_date TEXT, end_date TEXT, holder_name TEXT,
    hold_amount REAL, hold_ratio REAL, hold_float_ratio REAL, hold_change REAL,
    holder_type TEXT,
    PRIMARY KEY (end_date, ts_code, holder_name)
) WITHOUT ROWID;
-- 业绩预告(2013Q1 起, 东方财富/akshare)。**PIT 的关键是 ann_date**: 同一报告期会在不同
-- 日期披露/更正, 因子必须按 ann_date 对齐, 否则等于提前知道还没公告的业绩(前视)。
-- ⚠️ 覆盖度随年份剧烈变化(2020Q1 有 1649 只、2025Q1 只有 276 只)—— 因为"要不要预告"
-- 本身由业绩好坏触发(强制预告偏坏消息) ⇒ 这是**业绩选择后的子集**, 不是全市场面板。
-- 因此: 因子只能在**已披露子集内**取截面(不得把没预告的填 0, 那是把"没开会"当成"中性")。
CREATE TABLE IF NOT EXISTS forecast (
    ts_code TEXT, end_date TEXT, ann_date TEXT, metric TEXT,
    fc_type TEXT, chg_mid REAL, profit_mid REAL,
    PRIMARY KEY (ts_code, end_date, ann_date, metric)
) WITHOUT ROWID;
-- 限售解禁(2013 起, 东财/akshare)。**这是"事先已知的供给冲击"**: 解禁日程在招股书/公告里
-- 提前几个月就公开 ⇒ 在锚点用"未来 N 天解禁股数"是合法的 PIT 用法。
-- ⚠️ 只存**股数与日程**; 上游表里的"实际解禁市值/占流通市值比例"依赖解禁日前一交易日收盘价,
-- 在解禁**之前**用它是轻微前视 ⇒ 压力因子一律用 `解禁股数 ÷ 当日流通股本` 自己算。
-- ⚠️ 没有公告日字段(接口不提供) ⇒ 用"解禁日 − N 天"当可见日是最保守的近似。
CREATE TABLE IF NOT EXISTS restricted_release (
    ts_code TEXT, release_date TEXT, share_type TEXT, release_shares REAL,
    PRIMARY KEY (ts_code, release_date, share_type)
) WITHOUT ROWID;
-- 董监高持股变动(2013 起, 东财/akshare)。⚠️ 接口只给**变动日期**, 不给披露日:
-- 按深沪规则董监高交易需在 2 个交易日内公告 ⇒ 因子侧一律把可见日**推后 3 个自然日**
-- (保守), 不然后视镜里会"提前知道高管在卖"。
CREATE TABLE IF NOT EXISTS insider_trade (
    ts_code TEXT, change_date TEXT, person TEXT, reason TEXT,
    delta_shares REAL, price REAL, amount REAL, ratio REAL,
    PRIMARY KEY (ts_code, change_date, person, reason, delta_shares)
) WITHOUT ROWID;
-- 股票回购(2005 起, 东财/akshare)。**只存"计划"侧字段**(方案起始日 + 计划金额区间/进度):
-- 「已回购金额/数量」是**当前快照的累计值**, 拿它回填历史就是前视 ⇒ 一律不入库。
-- ⚠️ 这是**方案清单**不是公告流水: 同一家公司多年多次回购会有多行(按起始日区分)。
CREATE TABLE IF NOT EXISTS repurchase (
    ts_code TEXT, start_date TEXT, amount_floor REAL, amount_cap REAL,
    PRIMARY KEY (ts_code, start_date)
) WITHOUT ROWID;
-- 增发(2010 起, 东财/akshare)。发行效应(issuance effect)是文献里最稳的负向事件之一。
-- 用 `发行日期` 作可见日(比"预案公告"晚, **保守**)。
CREATE TABLE IF NOT EXISTS seo_issue (
    ts_code TEXT, issue_date TEXT, method TEXT, shares REAL, price REAL, list_date TEXT,
    PRIMARY KEY (ts_code, issue_date, method)
) WITHOUT ROWID;
-- 指数成分权重(2010-01 起, 月频)。既是 PIT 成分股(图/基准修复), 也是
-- 「指数成分共现图」的边来源(阶段 4 图 die)。
CREATE TABLE IF NOT EXISTS index_weight (
    index_code TEXT, con_code TEXT, trade_date TEXT, weight REAL,
    PRIMARY KEY (con_code, index_code, trade_date)
) WITHOUT ROWID;
-- 涨跌停价(2010-01 起, 按日全市场, 约 5400 只/日)。**实验路线图 D06/D07 的数据基础**:
-- 「涨停不可买 / 跌停不可卖」。此前 `PITEngine.tradable_mask` 六层里**没有这一层** ——
-- 策略在月末按**收盘价**买入, 若该股当日封涨停, 生产根本买不到, 回测却照样成交 = 高估。
-- 与 `suspend_d`(无权限)不同, 本接口有权限且覆盖完整; 停牌仍用 `vol>0` 近似, 不假装有它。
-- 主键 trade_date 打头 ⇒ 按日取截面走主键, 无需二级索引(同 margin/moneyflow)。
CREATE TABLE IF NOT EXISTS stk_limit (
    trade_date TEXT, ts_code TEXT, up_limit REAL, down_limit REAL,
    PRIMARY KEY (trade_date, ts_code)
) WITHOUT ROWID;
-- 申万行业分类静态表(L1/L2/L3)
CREATE TABLE IF NOT EXISTS sw_classify (
    index_code TEXT PRIMARY KEY, industry_name TEXT, level TEXT,
    industry_code TEXT, parent_code TEXT, src TEXT
);
-- 申万成分股 —— **PIT 行业分类的唯一来源**。
-- 修掉一个已登记的数据缺陷: stock_basic.industry 是**当前快照**, 拿它做历史
-- 行业中性化/标签中性化 = 用了未来信息; 且 333 只 industry 为 NULL 被 fillna("未知")
-- 当成同一个行业参与中性化。in_date/out_date 给出真实的时点归属区间
-- (out_date 为 NULL/空 = 至今仍在), 可重建任意历史日的行业映射。
CREATE TABLE IF NOT EXISTS sw_member (
    con_code TEXT, index_code TEXT, in_date TEXT, out_date TEXT, is_new TEXT,
    PRIMARY KEY (con_code, index_code, in_date)
) WITHOUT ROWID;

-- 模型元信息(2026-09-12 审查 P2-09): 记录「上次训练这张表用的是哪套因子」的指纹。
-- 增量训练只补缺失月份, 而 factor_names 取自**当前** factor 表 —— 期间新增/删除过因子时,
-- 同一张 model_score 会静默混入两代特征, 回测与对比跨口径且无任何标记。
CREATE TABLE IF NOT EXISTS model_meta (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
"""


def get_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # 2026-09-06 第十轮 N10-24: journal_mode 是持久级设置, 原每次连接都无条件 SET,
    # 现先查后设(仅当非 WAL 时设置)
    if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")  # 写锁冲突时等待而不是报错
    # 2026-09-18 审查 PERF-01: 库已 11G（page_count 约 281 万），而三个关键参数全是
    # SQLite 默认值 —— cache_size=-2000(仅 2MB 页缓存) / mmap_size=0(不内存映射) /
    # temp_store=0(ORDER BY、pivot 的中间结果落磁盘临时表)。实测这三项加起来让
    # 全史 load_market 的 pivot 花 142s、trainer 全表 pivot 231s。
    # 以下三个都是**连接级**设置（不写库、不改变任何查询结果，只改 IO 与排序路径）：
    #   · cache_size=-262144 → 256MB 页缓存（负数=KB）
    #   · mmap_size=268435456 → 256MB 只读内存映射
    #   · temp_store=MEMORY → 中间结果走内存而非磁盘临时文件
    # ⚠️ 不要加 ANALYZE 到启动路径：它写 sqlite_stat1（持久、会改查询计划），
    #    属"改口径"，须先备份再单独跑（见审查报告 PERF-02）。
    conn.execute("PRAGMA cache_size=-262144")
    conn.execute("PRAGMA mmap_size=268435456")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


@contextlib.contextmanager
def conn_ctx() -> Iterator[sqlite3.Connection]:
    """`with db.conn_ctx() as conn:` —— 离开块时**必定** close, 异常路径也一样。

    2026-09-12 审查 P2-12: 全项目百来处 `conn = get_conn(); ...; conn.close()` 都没包
    try/finally, 中途抛异常就漏一个连接 (长驻服务里累积句柄; `init_db` 里更糟 —— 迁移
    中途失败会留下**持写锁**的连接, 让其他进程全部 `database is locked`)。

    ⚠️ 别写成 `with db.get_conn() as conn:` —— sqlite3.Connection 的上下文管理器管的是
    **事务**(正常退出 commit、异常回滚), 它**不关连接**。这是本项目里最容易踩的那个坑,
    所以这里专门提供一个真正 close 的入口。
    """
    c = get_conn()
    try:
        yield c
    finally:
        try:
            c.close()
        except Exception:
            log.exception("关闭数据库连接失败")


def _add_column(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db() -> None:
    # 2026-09-12 审查 P2-12: 原为 "conn = get_conn() ... conn.commit(); conn.close()",
    # 中途任何一步抛错都会留下一个**持写锁**的连接 —— 启动期迁移失败时, 其他进程
    # (服务/脚本)会全部卡在 `database is locked`, 且看不出根因是这里。
    with conn_ctx() as conn:
        _init_db(conn)


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # 迁移: 早期版本 repo_rate 列名不同(无数据, 直接重建)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(repo_rate)").fetchall()]
    if cols and "overnight" not in cols:
        conn.execute("DROP TABLE repo_rate")
        conn.execute(
            "CREATE TABLE repo_rate (trade_date TEXT PRIMARY KEY, overnight REAL, r7d REAL, r14d REAL)"
        )
    # 迁移: regime 池子分支列(2026-09-10 风格防守) —— 风格基准/择时输入, 关闭时不写值
    _add_column(conn, "regime", "pool_state", "TEXT")
    _add_column(conn, "regime", "pool_score", "REAL")
    _add_column(conn, "regime", "pool_drawdown", "REAL")
    _add_column(conn, "regime", "pool_vol", "REAL")
    # 迁移: plan 行业列 / paper_position 复权因子列 (2026-09-03 优化)
    _add_column(conn, "plan", "industry", "TEXT")
    _add_column(conn, "plan", "fund_type", "TEXT")  # ETF 行类型 (2026-09-04 审查 P2-9)
    _add_column(conn, "paper_position", "entry_adj", "REAL")
    # 迁移: 挂单首次尝试日 —— 过期时钟从首次尝试起算, 停机补跑不误作废整月订单 (2026-09-04)
    _add_column(conn, "paper_order", "first_attempt", "TEXT")
    # 迁移: 挂单创建时间 —— 撮合执行闸(2026-09-05): 只执行成交日晚于创建日的订单,
    # 根治"当日补跑把次日挂单按当日开盘价成交"类事故; 旧订单回填为挂单日
    _add_column(conn, "paper_order", "created_at", "TEXT")
    conn.execute(
        "UPDATE paper_order SET created_at = order_date || ' 00:00:00' WHERE created_at IS NULL")
    # 迁移: paper_order/paper_trade 主键改为自增 id (2026-09-04 审查 P0-3)
    # 原主键 (order_date/exec_date, ts_code, side) 让跨信号日两笔同向订单同日成交时
    # INSERT OR REPLACE 覆盖第一笔成交, 而现金/持仓按两笔变动, 账实不一致
    _migrate_paper_pk(conn, "paper_order",
                      "order_date TEXT, ts_code TEXT, name TEXT, side TEXT, shares INTEGER, "
                      "reason TEXT, status TEXT DEFAULT 'pending', first_attempt TEXT, "
                      "created_at TEXT")  # created_at 必须随迁移重建, 否则升级后新表缺列(审查发现)
    _migrate_paper_pk(conn, "paper_trade",
                      "exec_date TEXT, ts_code TEXT, name TEXT, side TEXT, shares INTEGER, "
                      "price REAL, cost REAL, amount REAL, reason TEXT")
    # 迁移: paper_account 初始资金 (2026-09-04 审查 P0-5, 前端累计盈亏基准)
    # 回填: 无卖出时精确; 有卖出时卖出额含复权口径的累计分红, 结果会低估分红部分(一次性近似)
    _add_column(conn, "paper_account", "initial_capital", "REAL")
    conn.execute(
        "UPDATE paper_account SET initial_capital = cash "
        " + COALESCE((SELECT SUM(amount + COALESCE(cost, 0)) FROM paper_trade WHERE side='buy'), 0)"
        " - COALESCE((SELECT SUM(amount) FROM paper_trade WHERE side='sell'), 0) "
        "WHERE id=1 AND (initial_capital IS NULL OR initial_capital <= 0)")
    # 索引: 千万行大表按 trade_date 查询的二级索引 (2026-09-04 审查 P1-1)
    # 原 WITHOUT ROWID 主键 (ts_code, trade_date) 按日过滤只能全表扫描(实测 ~1s/次)
    # index_weight 的主键是 (con_code, index_code, trade_date), 按日取截面需二级索引
    # (2026-09-11 阶段 0a)。margin/moneyflow/hk_hold/block_trade 主键已是 trade_date 打头,
    # 无需重复建索引。
    for _tbl in ("daily", "daily_basic", "adj_factor", "factor", "model_score",
                 "daily_score", "index_weight"):
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_tbl}_date ON {_tbl}(trade_date)")
    conn.commit()


def _migrate_paper_pk(conn: sqlite3.Connection, table: str, cols_decl: str) -> None:
    """paper_order/paper_trade 旧表(业务三元组主键) → 新表(自增 id 主键), 保留全部数据。
    2026-09-08 审查 P2: 四步 DDL 包进事务(SQLite DDL 可事务) + 结尾提交 —— 原 RENAME
    与 CREATE 之间崩溃(断电/强杀)时, 下次启动 CREATE IF NOT EXISTS 重建空表、"id" in
    cols_now 提前 return, _old 表数据永久孤儿(挂单/成交静默丢失)。事务内崩溃整体回滚"""
    cols_now = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if "id" in cols_now:
        return
    copy_cols = [c.split()[0] for c in cols_decl.split(", ")]
    copy_cols = [c for c in copy_cols if c in cols_now]
    try:
        # 2026-09-19 审查: 本函数**自带**事务，调用点不得留未提交的 DML —— `_init_db` 在
        # 这里之前刚执行过 `UPDATE paper_order SET created_at=…`(隐式 BEGIN，到函数尾才
        # commit)，sqlite3 默认 isolation_level 下再 `BEGIN` 必抛
        # "cannot start a transaction within a transaction" → rollback → raise → 整个
        # init_db 崩、服务起不来。生产库早已带 id 列(上面提前 return)所以平时不发作，
        # 只在"升级旧库/从旧备份恢复"这条 DR 路径上炸 —— 恰是最需要它能跑的时候。
        # 回归测试: tests/test_db_legacy_migration.py(去掉下面这行必红, 已实测)
        conn.commit()
        conn.execute("BEGIN")
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY AUTOINCREMENT, {cols_decl})")
        conn.execute(
            f"INSERT INTO {table} ({','.join(copy_cols)}) "
            f"SELECT {','.join(copy_cols)} FROM {table}_old")
        conn.execute(f"DROP TABLE {table}_old")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def upsert_df(conn: sqlite3.Connection, df: pd.DataFrame, table: str, chunk_size: int = 20000) -> int:
    """分块写入, 避免长事务独占写锁(与其他进程并发时更友好)。
    2026-09-06 第十三轮 R13-9 契约注: INSERT OR REPLACE 整行重插 —— 调用方 df 必须
    包含目标表全部非缺省列(缺列会被写成 NULL 覆盖旧值), 所有调用点已按此核对"""
    if df is None or len(df) == 0:
        return 0
    obj = df.astype(object)
    obj = obj.where(obj.notna(), None)
    cols = list(df.columns)
    rows = [tuple(r) for r in obj.itertuples(index=False, name=None)]
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    for i in range(0, len(rows), chunk_size):
        conn.executemany(sql, rows[i:i + chunk_size])
        conn.commit()
    return len(rows)


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """写 model_meta(key → value)。缺表时静默跳过 —— 元信息不该让主流程失败。"""
    try:
        conn.execute(
            "INSERT INTO model_meta (key, value, updated_at) VALUES "
            "(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, timeutil.stamp()))
    except sqlite3.OperationalError:
        log.warning("model_meta 表不存在或不可写, 跳过元信息 %s", key)


def get_meta(conn: sqlite3.Connection, key: str):
    """读 model_meta; 表不存在/键不存在 → None"""
    try:
        row = conn.execute("SELECT value FROM model_meta WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def open_days_since(conn: sqlite3.Connection, name: str = "daily",
                    today: str = None):
    """`data_status` 里某张表的最新数据日之后, 到今天为止有**多少个开市日**。

    为什么按交易日历而不是日历日：春节/国庆长假若用"日历日 ≥7"判落后, 年年长假狼来了
    （2026-09-16 审查 COR-02 的原由, 当时只实现在 `web/api.py` 的 status 里）。
    2026-09-18 邮件优化提到数据层共用 —— 晨间待办邮件与看板行动中枢必须同口径,
    否则同一份数据两处说"落后 N 天"会说出不一样的数。

    返回 `None` = 表里没有这个 name / 交易日历缺失 ⇒ 调用方自己退回兜底口径。
    **不要把 None 当 0**（"算不出"和"不落后"是两件事）。
    """
    try:
        last = conn.execute(
            "SELECT MAX(last_date) FROM data_status WHERE name=?", (name,)).fetchone()[0]
        if not last:
            return None
        if today is None:
            from app import timeutil
            today = timeutil.date8()
        return int(conn.execute(
            "SELECT COUNT(*) FROM trade_cal WHERE is_open=1 "
            "AND cal_date>? AND cal_date<=?", (str(last), str(today))).fetchone()[0])
    except Exception:
        return None


# ---------- 管线阶段状态 (2026-09-04 审查 P1-7) ----------
def stage_start(conn: sqlite3.Connection, stage: str, run_date: str) -> int:
    """记录阶段开始, 返回本次运行 id

    2026-09-12 审查 P2-18: 时间戳改走 app.timeutil(东八区), 不再用 SQLite 的
    `datetime('now','localtime')`(跟机器时区走)—— 运行记录里的 run_date 本来就来自
    东八区, 两者不同源时, 非东八区部署下重跑窗口会错日。本机(东八区)逐字符不变。
    """
    cur = conn.execute(
        "INSERT INTO pipeline_run (started_at, stage, run_date, status) "
        "VALUES (?,?,?,'running')", (timeutil.stamp(), stage, run_date))
    conn.commit()
    return cur.lastrowid


def stage_finish(conn: sqlite3.Connection, sid: int, status: str,
                 rows: int = None, error: str = None) -> None:
    conn.execute(
        "UPDATE pipeline_run SET ended_at=?, status=?, rows=?, error=? "
        "WHERE id=?", (timeutil.stamp(), status, rows,
                       (str(error)[:300] if error else None), sid))
    conn.commit()


def set_status(conn: sqlite3.Connection, name: str, last_date: str, rows: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO data_status (name, last_date, updated_at, rows) VALUES (?,?,?,?)",
        (name, last_date, timeutil.stamp(), rows),
    )
    conn.commit()


def backup_db() -> str:
    """在线备份 SQLite → data/backup/neweco_YYYYMMDD.db (WAL 安全, 运行中可执行; P3-4)
    先写临时文件再原子改名, 避免中断留下残缺备份。
    跳过规则(2026-09-04 审查 P1-6): 今日已备份过就跳过; 不再用"目标≥源80%"判定 ——
    那会让缩小后的库(如 VACUUM)永远不刷新备份。备份后按保留策略清理过期文件。"""
    import logging
    log = logging.getLogger(__name__)
    out_dir = DATA_DIR / "backup"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 2026-09-15（外部审查 P2-6）：裸 `date.today()` → timeutil（东八区）。
    # 备份文件名/新鲜度判定与 run_date 同源，避免换时区部署时"今天"差一天。
    target = out_dir / f"neweco_{timeutil.today().strftime('%Y%m%d')}.db"
    if target.exists():
        # 新鲜度只看 **mtime 与本地钟同源**（文件系统写时间）：东八区"今天"与文件本地钟
        # 若不是同一时区，比较两者反而会在午夜附近误判 —— 故这里刻意不用 timeutil，
        # 文件名（对外可见的"这是我们东八区第几天的备份"）才是必须统一的那一半。
        import datetime
        if datetime.date.fromtimestamp(target.stat().st_mtime) == datetime.date.today():
            log.info("今日备份已存在(%.1f MB), 跳过", target.stat().st_size / 1e6)
            _prune_backups(out_dir)
            return str(target)
        target.unlink()  # 昨日及更早的旧文件: 重做今日备份(库可能已显著变化)
    tmp = target.with_suffix(".tmp")
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(tmp)
    # 2026-09-19 审查: 这里是裸 connect，拿不到 get_conn() 的 PRAGMA busy_timeout=30000，
    # 于是"有并发写者"直接变成 `database is locked` 异常 —— 而唯一的周备份档(周日 09:15)
    # 对 backup_db() 这一行没有 try，档整个死掉且这周从此没有备份。等 30 秒再报错。
    src.execute("PRAGMA busy_timeout=30000")
    dst.execute("PRAGMA busy_timeout=30000")
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    tmp.replace(target)
    log.info("数据库备份完成: %s (%.1f MB)", target.name, target.stat().st_size / 1e6)
    _prune_backups(out_dir)
    return str(target)


def verify_backup(path) -> tuple:
    """校验一份备份**到底能不能读**：只读打开 + `PRAGMA quick_check`。返回 `(ok, detail)`。

    为什么要这一步（2026-09-13）：备份"文件在"不等于"能恢复" —— 从没验过的备份只是一个**假设**。
    本库是全项目唯一的真相源（十几年日线 + 因子 + 打分 + 账本引用），真出事那天才发现备份坏了
    是不可接受的。实测口径：10.33 GB 备份 `quick_check` **21 秒**（完整 `integrity_check` 要 339 秒，
    所以日常用 quick_check；完整版留给人工年度体检）。

    `mode=ro` 保证**绝不会**因为"顺手打开一下"就改动备份本身。
    """
    p = Path(path)
    if not p.exists():
        return False, "备份文件不存在"
    try:
        con = sqlite3.connect(p.as_uri() + "?mode=ro", uri=True)
        try:
            row = con.execute("PRAGMA quick_check").fetchone()
        finally:
            con.close()
    except Exception as e:
        return False, f"打开/校验失败: {e}"
    detail = str(row[0]) if row else "无返回"
    return detail.lower() == "ok", detail


def _prune_backups(out_dir, keep_days: int = 7) -> None:
    """备份保留策略(2026-09-04 审查 P1-6): 最近 N 天全留 + 每月保留该月最新 1 份, 其余删除。
    2026-09-06 第十六轮 R16-2: 日期改用正则 neweco_(\\d{8}) 提取 —— 原 rsplit 对非标准
    命名(如 neweco_20260904_pre_ens)取到后缀"ens", 字符串比较恒大于 cutoff 永远删不掉;
    提取不到日期的文件不自动删(人工处理)

    2026-09-12 体检报告跟进: 原先只 unlink `.db` 本体 —— 而 SQLite 以 WAL 模式被打开过
    一次就会在旁边留 `-shm`(32K)/`-wal`, 于是**每清一份备份就永久留两个孤儿边车文件**
    (2026-09-12 手工清理 0903/0904/0904_pre_ens 时实测: 三份 .db 旁边躺着 6 个边车,
    时间戳全部相同, 来自同一次批量打开)。同时清掉边车 —— 它们**只对同名 .db 有意义**,
    本体没了就是纯垃圾。注意不要 glob 成 `neweco_*.db*`: 那会连别的库一起匹配。"""
    import datetime
    import logging
    import re
    log = logging.getLogger(__name__)
    cutoff = (datetime.date.today() - datetime.timedelta(days=keep_days)).strftime("%Y%m%d")
    files = sorted(out_dir.glob("neweco_*.db"))

    def _d8(p):
        m = re.search(r"neweco_(\d{8})", p.stem)
        return m.group(1) if m else None

    month_latest = {}
    for p in files:
        d8 = _d8(p)
        if not d8:
            continue  # 非标准命名: 不参与自动清理
        m = d8[:6]
        if m not in month_latest or p.name > month_latest[m].name:
            month_latest[m] = p
    keep = set(month_latest.values())
    for p in files:
        d8 = _d8(p)
        if not d8:
            continue
        if p in keep or d8 >= cutoff:
            continue
        p.unlink()
        # 边车文件(-shm/-wal)只对同名 .db 有意义, 本体删了就是孤儿
        for side in ("-shm", "-wal"):
            s = p.with_name(p.name + side)
            if s.exists():
                s.unlink()
        log.info("清理过期备份: %s", p.name)
