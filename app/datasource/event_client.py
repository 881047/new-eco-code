"""事件类数据源（业绩预告，2026-09-13 新增）

为什么单独一件: 现有数据湖里全是**面板型**（逐日行情/资金/两融）与**报告期型**
（财报/股东户数），而"业绩预告"是**事件流**——同一报告期在一两个月里陆续披露，
每条自带 `ann_date`。它的价值在于「业绩变动幅度」是一个**时点可知**的超预期代理
（PEAD 的经典输入），而且覆盖面只有部分股票（强制预告偏坏消息）⇒ 必须按事件处理。

设计要点（与 flow_client 同一套约定）:
  · best-effort: 任何一期失败只记日志, 不中断; 可中断续跑(逐期 upsert, PK 幂等);
  · 只存**原始事实**(类型/幅度/数值/公告日), 不在拉取层做任何标准化 —— 因子的
    裁剪/衰减权重属于研究层, 改了要重跑实验, 不能悄悄污染数据;
  · 不把未预告的股票填 0: "没预告"是**业绩选择后的缺失**, 填 0 等于把坏消息股
    当成中性股, 是 hk_hold 那类"含义静默改变"的翻版。
"""
from __future__ import annotations

import logging
import time

import numpy as np
import pandas as pd

from app import db
from app import timeutil

log = logging.getLogger(__name__)

FORECAST_START = "20130101"
_METRIC_KEEP = ("归属于上市公司股东的净利润", "扣除非经常性损益后的净利润")
_SUFFIX = {"6": ".SH", "9": ".SH", "0": ".SZ", "3": ".SZ", "2": ".SZ", "4": ".BJ", "8": ".BJ"}


def _quarter_ends(start: str, end: str) -> list:
    """start~end 之间的所有报告期(0331/0630/0930/1231)。"""
    a, b = pd.Timestamp(start), pd.Timestamp(end)
    out = []
    for y in range(a.year, b.year + 1):
        for md in ("0331", "0630", "0930", "1231"):
            d = f"{y}{md}"
            if start <= d <= end:
                out.append(d)
    return out


def _code_map(conn) -> dict:
    df = pd.read_sql("SELECT ts_code FROM stock_basic", conn)
    return {c.split(".")[0]: c for c in df["ts_code"]}


def _to_ts_code(code, mapping: dict):
    c = str(code).strip().zfill(6)
    if c in mapping:
        return mapping[c]
    return c + _SUFFIX.get(c[0], ".SH")          # 退市/未登记代码按首位推断


def _to_num(s):
    return pd.to_numeric(s, errors="coerce")


def _s(series) -> pd.Series:
    """PK 里的字符串列: 缺失一律填 `未标注`。

    ⚠️ 不能只靠 `.astype(str)`: 新 pandas 的 str dtype 下缺失是 `pd.NA`, 传给 sqlite 会变
    NULL, 而 PK 列 NOT NULL ⇒ 整个 chunk 报 IntegrityError(2026-09-13 实测踩到)。
    """
    return series.astype(object).where(series.notna(), "未标注").astype(str).str.strip()


def normalize_forecast(raw: pd.DataFrame, end_date: str, mapping: dict) -> pd.DataFrame:
    """东财原始表 → 入库形状（纯函数, 便于单测）。"""
    if raw is None or raw.empty:
        return pd.DataFrame()
    df = pd.DataFrame({
        "ts_code": raw["股票代码"].map(lambda c: _to_ts_code(c, mapping)),
        "end_date": end_date,
        "ann_date": raw["公告日期"].astype(str).str.replace("-", "", regex=False).str[:8],
        "metric": _s(raw["预测指标"]),
        "fc_type": _s(raw["预告类型"]),
        "chg_mid": _to_num(raw.get("业绩变动幅度")),
        "profit_mid": _to_num(raw.get("预测数值")),
    })
    df = df[df["metric"].isin(_METRIC_KEEP)]
    df = df[df["ann_date"].str.len() == 8]
    # 2026-09-13（关键路径补测）: 只查长度会放过 `20241301` / `20240230` 这类脏值 ——
    # 与 07 号事故台账 #19（`_est_ann_date` 的脏月份）是同一类：脏日期被当成有效数据。
    # 生产库实测（2026-09-13）forecast 121,048 行**非法日历日 = 0** ⇒ 本闸不改任何现有数据,
    # 只挡未来的坏行；事件因子的可见日全靠这一列, 宁可缺失（下游 mask 会剔除）也不许错位。
    df = df[pd.to_datetime(df["ann_date"], format="%Y%m%d", errors="coerce").notna()]
    df = df.drop_duplicates(subset=["ts_code", "end_date", "ann_date", "metric"], keep="last")
    return df.reset_index(drop=True)


def sync_forecast(start: str = FORECAST_START, end: str = None) -> int:
    """逐报告期拉业绩预告并 upsert。返回写入行数。可中断续跑（幂等）。"""
    import akshare as ak

    conn = db.get_conn()
    try:
        mapping = _code_map(conn)
    finally:
        conn.close()
    end = end or timeutil.date8()
    periods = _quarter_ends(start, end)
    log.info("业绩预告: %d 个报告期 (%s ~ %s)", len(periods), periods[0], periods[-1])
    total, failed = 0, []
    for p in periods:
        try:
            raw = ak.stock_yjyg_em(date=p)
        except Exception as e:                     # 限流/接口变更: 记下来继续
            log.warning("报告期 %s 拉取失败: %s", p, str(e)[:120])
            failed.append(p)
            time.sleep(1.0)
            continue
        try:
            # 2026-09-16 数据线审查 DATA-11: normalize 在 try 外时, 上游改列名的 KeyError
            # 会冒泡出本循环 ⇒ 剩余全部报告期被外层静默跳过。移入逐期 try = "该期失败"
            # 与 flow/lhb 客户端同口径。
            df = normalize_forecast(raw, p, mapping)
        except Exception as e:
            log.warning("报告期 %s 归一化失败(疑似上游字段变更): %s", p, str(e)[:120])
            failed.append(p)
            continue
        if df.empty:
            continue
        conn = db.get_conn()
        try:
            db.upsert_df(conn, df, "forecast")
        finally:
            conn.close()
        total += len(df)
        log.info("  报告期 %s: %d 行 (累计 %d)", p, len(df), total)
        time.sleep(0.3)
    if failed:
        log.warning("失败报告期 %d 个: %s", len(failed), failed)
    return total


# ---------------------------------------------------------------- 限售解禁
def normalize_restricted(raw: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    """东财解禁明细 → 入库形状。**只保留日程与股数**, 不存市值/比例/未来涨跌幅。"""
    if raw is None or raw.empty:
        return pd.DataFrame()
    df = pd.DataFrame({
        "ts_code": raw["股票代码"].map(lambda c: _to_ts_code(c, mapping)),
        "release_date": pd.to_datetime(raw["解禁时间"], errors="coerce").dt.strftime("%Y%m%d"),
        "share_type": _s(raw["限售股类型"]),
        "release_shares": pd.to_numeric(raw["解禁数量"], errors="coerce"),
    })
    df = df[df["release_date"].notna() & df["release_shares"].notna()]
    df = df[df["release_shares"] > 0]
    return df.drop_duplicates(subset=["ts_code", "release_date", "share_type"],
                              keep="last").reset_index(drop=True)


# STATUS: frozen (2026-09-20 审查 D-01) —— 全仓零引用、零测试覆盖：采集能力保留但**未接线**。
# 接线前必须先补一条 mock akshare 的冒烟测试，勿直接启用（无守护代码不得进生产路径）。
def sync_restricted_release(start: str = "20130101", end: str = None) -> int:
    """按月拉限售解禁明细并 upsert（逐月幂等, 可中断续跑）。"""
    import akshare as ak

    conn = db.get_conn()
    try:
        mapping = _code_map(conn)
    finally:
        conn.close()
    end = end or timeutil.date8()
    months = pd.date_range(pd.Timestamp(start).replace(day=1), pd.Timestamp(end), freq="MS")
    total = 0
    for m in months:
        m0 = m.strftime("%Y%m%d")
        m1 = (m + pd.offsets.MonthEnd(0)).strftime("%Y%m%d")
        try:
            raw = ak.stock_restricted_release_detail_em(start_date=m0, end_date=m1)
        except Exception as e:
            log.warning("解禁 %s 拉取失败: %s", m0[:6], str(e)[:120])
            time.sleep(1.0)
            continue
        df = normalize_restricted(raw, mapping)
        if df.empty:
            continue
        conn = db.get_conn()
        try:
            db.upsert_df(conn, df, "restricted_release")
        finally:
            conn.close()
        total += len(df)
        if len(df):
            log.info("  解禁 %s: %d 行 (累计 %d)", m0[:6], len(df), total)
        time.sleep(0.2)
    return total


# ---------------------------------------------------------------- 董监高增减持
def normalize_insider(raw: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    """东财董监高持股变动 → 入库形状。股数保留**带符号**（负=减持）。"""
    if raw is None or raw.empty:
        return pd.DataFrame()
    return _insider_frame(raw["代码"], raw["日期"],
                          raw["董监高人员姓名"].where(raw["董监高人员姓名"].notna(),
                                                      raw.get("变动人")),
                          raw["变动原因"], raw["变动股数"], raw["变动比例"],
                          raw["成交均价"], raw["变动金额"], mapping)


def _insider_frame(code, date_, person, reason, shares_raw, ratio_raw, price, amount,
                   mapping: dict) -> pd.DataFrame:
    """董监高变动的**唯一**归一化实现（akshare 表 / 原始 API JSON 两条路都走这里）。"""
    # 传进来的可能是 Series(akshare 表)也可能是 list(原始 API JSON) ⇒ 统一成 Series
    shares = pd.to_numeric(pd.Series(shares_raw), errors="coerce")
    ratio = pd.to_numeric(pd.Series(ratio_raw), errors="coerce")
    # 方向: 优先用股数自身的符号; 若整列都非负(东财有时只给绝对值), 才用变动比例的符号。
    # ⚠️ 不能写成 shares * sign(shares) —— 那会把减持的负号乘成正的(实测踩到)。
    # ⚠️ 已知未证实处(2026-09-20 第一步量化实测，产物 experiments/b_stage_20260920/
    #   audit_b_stage_first_step.json)：`ratio` 为 0/缺失时这里兜底给 **+1(增持)** ⇒ 全表
    #   171,344 行里有 **1,426 行(0.83%)** 的方向是被**假定**的。有没有被消费：`alpha_bank` 里
    #   insider 相关 2 行(`kg_insider_net`/`kg_insider_sell_60d`)**状态全是 CANDIDATE**
    #   ⇒ 从未过闸、从未进生产打分(`daily_score.ALL_FACTORS` 无 insider 列) ⇒ 本轮判：保持默认 +1。
    #   真要改这个默认值 = 改事件口径 ⇒ 须预注册 + 重跑 F27；现状被 tests/test_event_client.py 钉住。
    if (shares.dropna() < 0).any():
        delta = shares
    else:
        delta = shares * np.sign(ratio.where(ratio != 0, 1.0))
    df = pd.DataFrame({
        "ts_code": pd.Series(code).map(lambda c: _to_ts_code(c, mapping)),
        "change_date": pd.to_datetime(pd.Series(date_), errors="coerce").dt.strftime("%Y%m%d"),
        "person": _s(pd.Series(person)),
        "reason": _s(pd.Series(reason)),
        "delta_shares": pd.Series(delta).to_numpy(),
        "price": pd.to_numeric(pd.Series(price), errors="coerce").to_numpy(),
        "amount": pd.to_numeric(pd.Series(amount), errors="coerce").to_numpy(),
        "ratio": pd.Series(ratio).to_numpy(),
    })
    df = df[df["change_date"].notna() & df["delta_shares"].notna() & (df["delta_shares"] != 0)]
    return df.drop_duplicates(subset=["ts_code", "change_date", "person", "reason",
                                      "delta_shares"], keep="last").reset_index(drop=True)


# akshare 的 stock_hold_management_detail_em 是"内部翻完 344 页再返回", 中途网络抖动
# 就要从头再来(实测 21% 处断过一次)。这里按它的 API 规格自己翻页:**逐页落库 + 断点续传**。
_EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_EM_INSIDER_PARAMS = {
    "reportName": "RPT_EXECUTIVE_HOLD_DETAILS", "columns": "ALL", "quoteColumns": "",
    "filter": "", "pageSize": "5000", "sortTypes": "-1,1,1",
    "sortColumns": "CHANGE_DATE,SECURITY_CODE,PERSON_NAME", "source": "WEB", "client": "WEB",
}
_INSIDER_PAGE_KEY = "event_client.insider_last_page"


# STATUS: frozen (2026-09-20 审查 D-01) —— 全仓零引用、零测试覆盖：采集能力保留但**未接线**。
# 接线前必须先补一条 mock 的冒烟测试（该接口内部翻 344 页 ≈ 13 分钟，务必先测断点续传）。
def sync_insider_paged(max_pages: int = None, pause: float = 0.15) -> int:
    """分页拉董监高变动（断点续传：进度记在 model_meta.insider_last_page）。"""
    import requests

    conn = db.get_conn()
    try:
        mapping = _code_map(conn)
        start_page = int(db.get_meta(conn, _INSIDER_PAGE_KEY) or 1)
    finally:
        conn.close()
    total_rows = 0
    page = start_page
    while True:
        params = dict(_EM_INSIDER_PARAMS, pageNumber=str(page), p=str(page),
                      pageNo=str(page), pageNum=str(page))
        data, pages = None, None
        for attempt in range(4):                      # 逐页重试, 单页抖动不毁全场
            try:
                r = requests.get(_EM_URL, params=params, timeout=30)
                js = r.json()
                data = (js.get("result") or {}).get("data") or []
                pages = int((js.get("result") or {}).get("pages") or 0)
                break
            except Exception as e:
                log.warning("insider 第 %d 页失败(%d/4): %s", page, attempt + 1, str(e)[:100])
                time.sleep(2.0 * (attempt + 1))
        if data is None:
            log.error("insider 第 %d 页 4 次全失败, 停在断点(重跑本函数即续)", page)
            break
        if not data:
            break
        df = _insider_frame([d.get("SECURITY_CODE") for d in data],
                            [d.get("CHANGE_DATE") for d in data],
                            [d.get("DSE_PERSON_NAME") or d.get("PERSON_NAME") for d in data],
                            [d.get("CHANGE_REASON") for d in data],
                            [d.get("CHANGE_SHARES") for d in data],
                            [d.get("CHANGE_RATIO") for d in data],
                            [d.get("AVERAGE_PRICE") for d in data],
                            [d.get("CHANGE_AMOUNT") for d in data], mapping)
        conn = db.get_conn()
        try:
            if len(df):
                db.upsert_df(conn, df, "insider_trade")
            db.set_meta(conn, _INSIDER_PAGE_KEY, str(page + 1))
            conn.commit()
        finally:
            conn.close()
        total_rows += len(df)
        if page % 20 == 0 or page == 1:
            log.info("  insider 第 %d/%s 页: +%d 行 (累计 %d)", page, pages, len(df), total_rows)
        if pages and page >= pages:
            conn = db.get_conn()
            try:
                db.set_meta(conn, _INSIDER_PAGE_KEY, "1")   # 拉完复位, 方便下次全量重扫
                conn.commit()
            finally:
                conn.close()
            break
        if max_pages and page - start_page + 1 >= max_pages:
            break
        page += 1
        time.sleep(pause)
    return total_rows


# STATUS: frozen (2026-09-20 审查 D-01) —— 全仓零引用、零测试覆盖：采集能力保留但**未接线**。
# 与 sync_insider_paged 二选一即可（本函数一次拉全史，paged 版支持断点续传）。
def sync_insider() -> int:
    """拉全部董监高持股变动（接口一次返回全史, 内部翻 344 页 ≈ 13 分钟）。"""
    import akshare as ak

    raw = ak.stock_hold_management_detail_em()
    conn = db.get_conn()
    try:
        mapping = _code_map(conn)
    finally:
        conn.close()
    df = normalize_insider(raw, mapping)
    if df.empty:
        return 0
    conn = db.get_conn()
    try:
        return db.upsert_df(conn, df, "insider_trade")
    finally:
        conn.close()


# ---------------------------------------------------------------- 回购 / 增发（第 9、10 族）
def normalize_repurchase(raw: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    """东财回购清单 → 入库形状。**只留计划侧字段**（已回购金额是当前快照累计值 ⇒ 前视）。
    2026-09-16 数据线审查 DATA-08: `progress`(实施进度)同性质 —— 终态(如"完成实施")是
    锚点日之后才可知的快照, 库内 4917/5508 行已是终态 ⇒ 按 start_date 读即前视。尚无
    因子消费该表 ⇒ 直接移除, 不留埋雷。"""
    if raw is None or raw.empty:
        return pd.DataFrame()
    df = pd.DataFrame({
        "ts_code": raw["股票代码"].map(lambda c: _to_ts_code(c, mapping)),
        "start_date": pd.to_datetime(raw["回购起始时间"], errors="coerce").dt.strftime("%Y%m%d"),
        "amount_floor": pd.to_numeric(raw.get("计划回购金额区间-下限"), errors="coerce"),
        "amount_cap": pd.to_numeric(raw.get("计划回购金额区间-上限"), errors="coerce"),
    })
    df = df[df["start_date"].notna()]
    return df.drop_duplicates(subset=["ts_code", "start_date"], keep="last").reset_index(drop=True)


# STATUS: frozen (2026-09-20 审查 D-01) —— 全仓零引用、零测试覆盖：采集能力保留但**未接线**。
# 接线前必须先补 mock 冒烟测试；注意本函数写 repurchase 表，需确认与 updater 编排不冲突。
def sync_repurchase() -> int:
    """拉全部股票回购方案（一次调用 ≈50s）。"""
    import akshare as ak
    conn = db.get_conn()
    try:
        mapping = _code_map(conn)
    finally:
        conn.close()
    df = normalize_repurchase(ak.stock_repurchase_em(), mapping)
    conn = db.get_conn()
    try:
        return db.upsert_df(conn, df, "repurchase")
    finally:
        conn.close()


def normalize_seo(raw: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    """东财增发清单 → 入库形状。"""
    if raw is None or raw.empty:
        return pd.DataFrame()
    df = pd.DataFrame({
        "ts_code": raw["股票代码"].map(lambda c: _to_ts_code(c, mapping)),
        "issue_date": pd.to_datetime(raw["发行日期"], errors="coerce").dt.strftime("%Y%m%d"),
        "method": _s(raw["发行方式"]),
        "shares": pd.to_numeric(raw["发行总数"], errors="coerce"),
        "price": pd.to_numeric(raw["发行价格"], errors="coerce"),
        "list_date": pd.to_datetime(raw.get("增发上市日期"), errors="coerce").dt.strftime("%Y%m%d"),
    })
    df = df[df["issue_date"].notna()]
    return df.drop_duplicates(subset=["ts_code", "issue_date", "method"],
                              keep="last").reset_index(drop=True)


# STATUS: frozen (2026-09-20 审查 D-01) —— 全仓零引用、零测试覆盖：采集能力保留但**未接线**。
# 接线前必须先补 mock 冒烟测试；注意本函数写 seo_issue 表，需确认与 updater 编排不冲突。
def sync_seo_issue() -> int:
    """拉全部增发记录（一次调用 ≈50s）。"""
    import akshare as ak
    conn = db.get_conn()
    try:
        mapping = _code_map(conn)
    finally:
        conn.close()
    df = normalize_seo(ak.stock_qbzf_em(), mapping)
    conn = db.get_conn()
    try:
        return db.upsert_df(conn, df, "seo_issue")
    finally:
        conn.close()


if __name__ == "__main__":                         # 手动: python -m app.datasource.event_client
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    print("写入", sync_forecast(), "行")
