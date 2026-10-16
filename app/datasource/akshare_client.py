"""AkShare 兜底数据源: 财务指标 / 逆回购利率 / 场外基金排行

全部 best-effort: 接口名与列名随 akshare 版本变化, 失败不阻塞主流程,
调用方根据返回值决定是否降级。
"""
import logging

import pandas as pd

from app import db
from app import timeutil

log = logging.getLogger(__name__)


# ---------- 财务指标(兜底, 逐只拉取) ----------
def _est_ann_date(end_date: str) -> str:
    """按法定披露截止日保守估计披露日(宁可滞后不可前视):
    年报→次年4/30, 一季报→4/30, 半年报→8/31, 三季报→10/31"""
    # 2026-09-08 审查 P2: 上游返回非法日期(空串/"nan"/长度不足)时 int() ValueError
    # 会逸出逐股 try 炸断整个 fallback —— 非法值返回空串(ann_date 为空 → 因子侧
    # 缺失而非前视, 不中断批次)
    if not isinstance(end_date, str) or len(end_date) != 8 or not end_date.isdigit():
        return ""
    y, m = int(end_date[:4]), int(end_date[4:6])
    # 2026-09-13 修（由新增测试发现）：原来只校验"8 位数字"，月=13 这类脏值会掉进最后一条
    # 分支返回 `{y}1031` —— **早于它的输入**，方向正是"前视"那一侧。本函数的承诺是
    # "非法值返回空串(缺失而非前视)"，这里把月份范围补上，承诺才成立。
    if not 1 <= m <= 12:
        return ""
    if m == 12:
        return f"{y + 1}0430"
    if m <= 3:
        return f"{y}0430"
    if m <= 6:
        return f"{y}0831"
    return f"{y}1031"
_FINA_COL_CANDIDATES = {
    "roe": ("净资产收益率(%)", "净资产收益率(加权)(%)", "净资产收益率"),
    "netprofit_yoy": ("净利润增长率(%)", "净利润同比增长率(%)"),
    "or_yoy": ("主营业务收入增长率(%)", "营业收入增长率(%)", "营业总收入同比增长率(%)"),
    "grossprofit_margin": ("销售毛利率(%)", "毛利率(%)"),
    "debt_to_assets": ("资产负债率(%)",),  # 单元素元组必须带逗号
    "eps": ("摊薄每股收益(元)", "每股收益(元)"),
}


def _pick_col(df: pd.DataFrame, candidates) -> str:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def sync_fina_fallback(universe: set) -> int:
    """用 akshare 逐只拉取财务指标(约 40 分钟, 只跑一次, 可中断续跑)"""
    import akshare as ak

    conn = db.get_conn()
    try:
        # 以关键字段非空判定"已完整入库"(历史版本曾缺失 debt_to_assets, 需重取)
        have = set(pd.read_sql(
            "SELECT DISTINCT ts_code FROM fina_indicator WHERE debt_to_assets IS NOT NULL", conn)["ts_code"])
        todo = sorted(universe - have)
        log.info("AkShare 财务兜底: 待拉取 %d 只", len(todo))
        done = 0
        for i, ts_code in enumerate(todo):
            symbol = ts_code.split(".")[0]
            try:
                raw = ak.stock_financial_analysis_indicator(symbol=symbol, start_year="2013")
            except Exception as e:
                log.warning("财务兜底 %s 失败: %s", ts_code, e)
                continue
            if raw is None or raw.empty:
                continue
            date_col = _pick_col(raw, ("日期", "报告期", "时间"))
            if date_col is None:
                continue
            out = pd.DataFrame()
            out["ts_code"] = [ts_code] * len(raw)
            out["end_date"] = raw[date_col].astype(str).str.replace("-", "").str[:8]
            # 兜底无真实披露日: 按法定披露截止日保守估计, 避免未来函数
            out["ann_date"] = out["end_date"].map(_est_ann_date)
            for field, cands in _FINA_COL_CANDIDATES.items():
                col = _pick_col(raw, cands)
                out[field] = pd.to_numeric(raw[col], errors="coerce") if col else None
            db.upsert_df(conn, out, "fina_indicator")
            done += 1
            if (i + 1) % 200 == 0:
                log.info("财务兜底: %d/%d", i + 1, len(todo))
                db.set_status(conn, "fina_indicator", "", done)
        db.set_status(conn, "fina_indicator", "", done)
        missing = len(todo) - done
        if missing:
            log.warning("AkShare 财务兜底: 仍有 %d 只无财务数据(多为退市/新股/接口异常), 请知悉", missing)
    finally:
        conn.close()
    log.info("财务兜底完成: %d 只", done)
    return done


# ---------- 国债逆回购利率 ----------
# 候选列按期限分组(2026-09-04 修复): 原 r7d 误配 R-001/R001(1天期), 且子串匹配
# "R001"∈"FR001" 会把银行间定盘利率的 FR001 错装进 7 天列(实库 overnight==r7d 100% 相同)
# 顺序: 交易所 GC/R- 优先, 银行间 FR 次之, 存款类 DR 兜底; 各期限只列本期限代码;
# 末尾中文天数列("1天"等)用于 bond_repo_zh_rates 兜底接口
_RATE_COL_ALIASES = {
    "overnight": ("GC001", "R-001", "R001", "FR001", "DR001", "1天"),
    "r7d": ("GC007", "R-007", "R007", "FR007", "DR007", "7天"),
    "r14d": ("GC014", "R-014", "R014", "FR014", "DR014", "14天"),
}

# 固定接口+显式 fallback 顺序(2026-09-04 审查 P1-5): 原用 dir(ak) 扫描所有含 repo 的函数逐个
# 发请求试探, 又慢又脆弱(akshare 升级后函数集变化会改变结果)
_REPO_FUNCS = ("repo_rate_query", "bond_repo_zh_rates")


def _pick_alias_col(df: pd.DataFrame, aliases) -> object:
    """先精确匹配列名, 再子串匹配(防 "1天"⊂"21天"、"R001"⊂"FR001" 类误配)"""
    for a in aliases:
        for c in df.columns:
            if str(c).strip().upper() == a.upper():
                return c
    for a in aliases:
        for c in df.columns:
            if a in str(c).upper():
                return c
    return None


def _extract_repo(df: pd.DataFrame) -> pd.DataFrame:
    """从任意列名中识别 (日期, 隔夜, 7天, 14天) 资金利率; 银行间FR与交易所GC走势一致可互相代理"""
    date_col = None
    for c in df.columns:
        if any(k in str(c).lower() for k in ("日期", "date")):
            date_col = c
            break
    if date_col is None:
        return None
    out = pd.DataFrame()
    out["trade_date"] = df[date_col].astype(str).str.replace("-", "").str.replace("/", "").str[:8]
    for field, aliases in _RATE_COL_ALIASES.items():
        col = _pick_alias_col(df, aliases)
        out[field] = pd.to_numeric(df[col], errors="coerce") if col else None
    return out


def sync_repo_rates(start: str) -> int:
    """拉取逆回购/资金利率历史; 优先 repo_rate_query(银行间定盘FR), 失败返回 0,
    建议层降级为固定利率+日历规则"""
    import akshare as ak

    for name in _REPO_FUNCS:
        fn = getattr(ak, name, None)
        if fn is None:
            log.debug("回购接口 %s 不存在(akshare 版本变化), 尝试下一个", name)
            continue
        try:
            raw = fn()
        except Exception as e:
            log.debug("回购接口 %s 失败: %s", name, e)
            continue
        if raw is None or raw.empty:
            continue
        df = _extract_repo(raw)
        if df is None or df.empty or df["overnight"].isna().all():
            continue
        df = df[df["trade_date"] >= start]
        with db.conn_ctx() as conn:
            # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
            n = db.upsert_df(conn, df, "repo_rate")
            db.set_status(conn, "repo_rate", str(df["trade_date"].max()), n)
        log.info("回购利率来源: %s, %d 条", name, n)
        return n
    log.warning("未找到可用的回购历史接口, 逆回购建议将使用固定利率假设+日历规则")
    return 0


# ---------- 场外基金排行(月度快照) ----------
_FUND_RANK_TYPES = ("债券型", "混合型", "股票型", "指数型", "QDII", "FOF")


def fund_rank_snapshot() -> int:
    """东财开放式基金排行快照, 存入 fund_rank 表"""
    import akshare as ak

    snap = timeutil.today().strftime("%Y%m%d")
    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        total = 0
        for ftype in _FUND_RANK_TYPES:
            try:
                raw = ak.fund_open_fund_rank_em(symbol=ftype)
            except Exception as e:
                log.warning("基金排行 %s 失败: %s", ftype, e)
                continue
            if raw is None or raw.empty:
                continue
            code_col = _pick_col(raw, ("基金代码", "代码"))
            name_col = _pick_col(raw, ("基金简称", "名称"))
            r1y = _pick_col(raw, ("近1年", "近一年"))
            r3y = _pick_col(raw, ("近3年", "近三年"))
            if not code_col:
                continue
            out = pd.DataFrame()
            out["code"] = raw[code_col].astype(str)
            out["name"] = raw[name_col] if name_col else ""
            out["fund_type"] = ftype
            out["ret_1y"] = pd.to_numeric(raw[r1y], errors="coerce") if r1y else None
            out["ret_3y"] = pd.to_numeric(raw[r3y], errors="coerce") if r3y else None
            out["snap_date"] = snap  # 最后赋标量列, 避免被 Series 赋值撑大后变成 NaN
            total += db.upsert_df(conn, out, "fund_rank")
    log.info("场外基金排行快照: %d 条", total)
    return total
