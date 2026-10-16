"""股东结构因子 + PIT 行业映射 (2026-09-11 「芯片式异构架构」阶段 0b)

两部分:
  1. `compute_holder_factors` —— 股东户数变化(筹码集中度)。户数是**季度**数据,
     必须按 `ann_date` 而非 `end_date` 对齐到日频截面 —— 季报在报告期结束后数周才
     披露, 按 end_date 对齐 = 用了尚未公布的数据(前视)。本模块复用 factor_engine
     的 `shift_to_next_trading_day`(已有审查记录: A 股财报多在收盘后披露, 当期截面
     直接用等于 1 日前视)。

  2. `sw_industry_map_pit` —— **PIT 行业分类映射**, 修掉一个已登记的数据缺陷:
     原 `factor_engine.industry_map` 读 `stock_basic.industry`, 那是**当前快照**:
     拿它给 2018 年的截面做行业中性化, 等于用 2026 年的行业归属去中性化 2018 年 ——
     期间发生过分拆/更名/重分类的股票全部错配。另: 333 只 industry 为 NULL 的股票
     被 `fillna("未知")` 归成同一个"行业"参与中性化, 把一组无关股票当成同业。
     `sw_member.in_date/out_date` 给出真实的时点归属区间, 可精确重建历史行业。
"""
import logging

import numpy as np
import pandas as pd

from app import db

log = logging.getLogger(__name__)

# 股东户数下界(见 compute_holder_factors 内的清洗说明): 上游有 11,052 行 holder_num<100
# 的占位值(最小为 1), 会让对数差分/比率在小分母上爆炸。1000 户对 A 股上市公司是极宽松
# 的下界 —— 实际最小的也有数千户, 不会误伤真值。
HOLDER_NUM_MIN = 1000


def compute_holder_factors(dates: pd.DatetimeIndex) -> dict:
    """股东户数变化因子。

    经济含义: 户数**下降** = 筹码由散户向少数账户集中, 通常先于上涨(A 股长期
    被引用的「股东户数减少」效应)。故因子取**变化的负号**:
        holder_chg   > 0 表示户数减少(集中)
    """
    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        if not db.table_exists(conn, "holder_num"):
            conn.close()
            log.warning("holder_num 表不存在, 跳过股东户数因子")
            return {}
        h = pd.read_sql("SELECT ts_code, end_date, ann_date, holder_num FROM holder_num", conn)
    if h.empty:
        log.warning("holder_num 数据为空, 跳过股东户数因子")
        return {}

    h = h.dropna(subset=["holder_num", "ann_date"])
    h = h[h["ann_date"].astype(str).str.len() == 8]
    # ⚠️ 上游脏值清洗(2026-09-11 实测): 全表 190,622 行里有 **11,052 行 holder_num < 100**,
    # 最小值是 **1**("只有 1 个股东的上市公司"), 另有 12,580 行 < 1000。这些是占位/补录值
    # (ann_date 也异常, 如 001248.SZ 的 2022 年报直到 2025-03 才"披露")。
    # 不清掉它们, 比率会在小分母上爆炸: 1 → 500 户 = +49,900%, 直接把因子均值拉到 -48。
    # 1000 户对 A 股上市公司是极宽松的下界(实际最小的也有数千户), 不会误伤真值。
    h = h[h["holder_num"] >= HOLDER_NUM_MIN]
    if h.empty:
        return {}
    # 同一 (ts_code, end_date) 有更正披露时保留 ann_date 最新的一条
    h = (h.sort_values("ann_date")
           .drop_duplicates(["ts_code", "end_date"], keep="last")
           .sort_values(["ts_code", "end_date"]))

    # 变化在 **end_date 序列**上算(季度环比) —— 必须先去重到报告期, 否则同一报告期
    # 的多次披露会把 shift(1) 算成 0 变化。
    # 用**对数差分**而非比率: 比率对「户数翻倍」与「户数减半」不对称(+100% vs -50%),
    # 且对剩余的小分母仍然敏感; log 差分对称、极值压缩, 是户数这类右偏计数的标准做法。
    g = h.groupby("ts_code", sort=False)["holder_num"]
    h = h.assign(
        chg_1q=g.transform(lambda s: np.log(s) - np.log(s.shift(1))),
        chg_4q=g.transform(lambda s: np.log(s) - np.log(s.shift(4))),
    )

    from app.factors.factor_engine import shift_to_next_trading_day
    h["visible"] = shift_to_next_trading_day(h["ann_date"].astype(str), dates.strftime("%Y%m%d"))
    h = h.dropna(subset=["visible"])
    if h.empty:
        return {}
    # 同日多条(不同报告期)取报告期最新的一条, 避免 pivot 默认 mean 混期
    h = h.sort_values("end_date").drop_duplicates(["ts_code", "visible"], keep="last")
    h["visible"] = pd.to_datetime(h["visible"])

    full_idx = pd.Index(sorted(set(h["visible"]) | set(dates)))
    out = {}
    for src, name in (("chg_1q", "holder_chg_1q"), ("chg_4q", "holder_chg_4q")):
        pv = h.pivot_table(index="visible", columns="ts_code", values=src)
        pv = pv.reindex(full_idx).ffill().reindex(index=dates)
        pv.index = pd.to_datetime(pv.index)
        # 取负号: 户数减少(集中) → 因子为正
        out[name] = (-pv).astype(np.float32)
    log.info("股东户数因子: 2 个 (visible 对齐 %d 个披露日)", h["visible"].nunique())
    return out


def sw_industry_map_pit(dates: pd.DatetimeIndex, level: str = "L1") -> pd.DataFrame:
    """PIT 申万行业映射: 返回 index=dates, columns=ts_code, values=行业名 的 DataFrame。

    取每个截面日 d 上, 满足 `in_date <= d` 且(`out_date` 为空 或 `out_date >= d`)的
    成分记录。同股同日命中多个行业(数据更正/并行分类)时**保留 in_date 最晚的一条**
    (即最近一次归属变更)。

    无申万归属的股票(次新股、退市、北交)返回 NaN —— 调用方应把 NaN 行**排除出中性化**
    而不是填成"未知": 把无归属股票当成一个行业, 就是把一组无关股票当成同业做减法。
    """
    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        if not (db.table_exists(conn, "sw_member") and db.table_exists(conn, "sw_classify")):
            conn.close()
            log.warning("sw_member/sw_classify 表不存在, 无法构建 PIT 行业映射")
            return pd.DataFrame(index=dates)
        mem = pd.read_sql(
            "SELECT m.con_code, m.in_date, m.out_date, c.industry_name "
            "FROM sw_member m JOIN sw_classify c ON m.index_code = c.index_code "
            "WHERE c.level = ?", conn, params=(level,))
    if mem.empty:
        log.warning("申万成分表为空, 无法构建 PIT 行业映射")
        return pd.DataFrame(index=dates)

    mem["in_date"] = mem["in_date"].astype(str).str.strip()
    mem["out_date"] = mem["out_date"].fillna("").astype(str).str.strip()
    mem = mem[mem["in_date"].str.len() == 8]
    # out_date 空串/NaN = 至今仍在
    mem["_out"] = mem["out_date"].where(mem["out_date"].str.len() == 8, "99999999")

    cols = {}
    for d in dates:
        ds = pd.Timestamp(d).strftime("%Y%m%d")
        sel = mem[(mem["in_date"] <= ds) & (mem["_out"] >= ds)]
        if sel.empty:
            continue
        sel = sel.sort_values("in_date").drop_duplicates("con_code", keep="last")
        cols[pd.Timestamp(d)] = sel.set_index("con_code")["industry_name"]
    if not cols:
        return pd.DataFrame(index=dates)
    out = pd.DataFrame(cols).T
    out.index = pd.to_datetime(out.index)
    return out


def industry_map_pit_last(conn=None, level: str = "L1") -> pd.Series:
    """便捷封装: 取**最近一个截面**的 PIT 行业映射 (ts_code -> industry_name)。

    供 `factor_engine.compute_all` 在尚未改造为逐期 PIT 中性化之前, 至少**修掉
    "当前快照"与"NULL 填未知"两个缺陷** —— 与旧 `industry_map` 的区别是:
      · 来自申万成分的历史归属, 不是 stock_basic 的当前快照
      · 无归属的股票保持 NaN(调用方跳过), 不再 fillna("未知") 把无关股票当同业
    """
    conn_own = conn is None
    if conn_own:
        conn = db.get_conn()
    try:
        if not (db.table_exists(conn, "sw_member") and db.table_exists(conn, "sw_classify")):
            return pd.Series(dtype=object)
        mem = pd.read_sql(
            "SELECT m.con_code, m.in_date, m.out_date, c.industry_name "
            "FROM sw_member m JOIN sw_classify c ON m.index_code = c.index_code "
            "WHERE c.level = ?", conn, params=(level,))
    finally:
        if conn_own:
            conn.close()
    if mem.empty:
        return pd.Series(dtype=object)
    mem["in_date"] = mem["in_date"].astype(str).str.strip()
    mem["out_date"] = mem["out_date"].fillna("").astype(str).str.strip()
    mem["_out"] = mem["out_date"].where(mem["out_date"].str.len() == 8, "99999999")
    today = pd.Timestamp.today().strftime("%Y%m%d")
    sel = mem[(mem["in_date"] <= today) & (mem["_out"] >= today)]
    sel = sel.sort_values("in_date").drop_duplicates("con_code", keep="last")
    return sel.set_index("con_code")["industry_name"]


def compute_holder_factors_safe(dates: pd.DatetimeIndex) -> dict:
    """异常不冒泡的包装(因子层单个家族失败不应炸断整条 compute_all)"""
    try:
        return compute_holder_factors(dates)
    except Exception:
        log.exception("股东户数因子计算失败, 跳过")
        return {}
