"""龙虎榜数据源
- AkShare(东方财富): 2015 年起历史 + 每日增量, 用于因子(回测/实盘同口径)
- 同花顺官方 API (fuyao.aicubes.cn): 仅近一年窗口, 每日增量逐日积累, 用于监控展示
两者均 best-effort, 失败不阻塞主流程; 列名随上游版本变化, 用候选列名兜底。
"""
import json
import logging
import time

import pandas as pd
import requests

from app import db
from app import timeutil
from app.config import HITHINK_FINANCE_API_KEY

log = logging.getLogger(__name__)

LHB_START = "20150101"  # 东财龙虎榜最早约 2015-01
THS_BASE = "https://fuyao.aicubes.cn"
THS_TIMEOUT = 15
THS_MAX_RETRY = 3

# ---------- AkShare 部分 ----------
# 东财接口列名随时期变化(如 净买额占总成交比 / 净买额占总成交额比), 候选列名兜底
_DETAIL_COLS = {
    "name": ("名称",),
    "close": ("收盘价",),
    "pct_chg": ("涨跌幅",),
    "net_buy": ("龙虎榜净买额",),
    "buy_value": ("龙虎榜买入额",),
    "sell_value": ("龙虎榜卖出额",),
    "lhb_amount": ("龙虎榜成交额",),
    "market_amount": ("市场总成交额",),
    "net_ratio": ("净买额占总成交比", "净买额占总成交额比"),
    "amount_ratio": ("成交额占总成交比", "成交额占总成交额比"),
    "turnover": ("换手率",),
    "float_mv": ("流通市值",),
    "reason": ("上榜原因",),
}
_ORG_COLS = {
    "name": ("名称",),
    "org_buy_num": ("买方机构数",),
    "org_sell_num": ("卖方机构数",),
    "org_buy_value": ("机构买入总额",),
    "org_sell_value": ("机构卖出总额",),
    "org_net_value": ("机构买入净额", "机构净买入额"),
    "org_net_ratio": ("机构净买额占总成交比", "机构净买额占总成交额比"),
    "reason": ("上榜原因",),
}
# 东财含「上榜后1日~10日」未来数据列, 一律不落库(防误入因子造成未来函数)


_MULTI_DAY_PREFIXES = ("连续", "严重异常")


def _dedupe_stock(df):
    """同日同股可有「当日榜+3日榜」两条记录(净买占比口径不同):
    优先保留当日榜, 即上榜原因以「连续」「严重异常」开头的多日榜记录在后"""
    if df is None or len(df) < 2:
        return df
    is_multi = df["reason"].astype(str).str.startswith(_MULTI_DAY_PREFIXES)
    df = df.assign(_multi=is_multi).sort_values("_multi", kind="stable")
    return df.drop_duplicates(subset=["ts_code"], keep="first").drop(columns=["_multi"])


def _pick_col(df, candidates):
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _norm_ts_code(code: str) -> str:
    # 2026-09-16 数据线审查 DATA-09: 与 event_client._SUFFIX 统一口径 —— 沪 B 股是
    # 900 段(.SH), 不是北交所; 北交所是 4/8 开头与 920(新代码段)。
    # 2026-09-28 审查修复: 原顺序 920xxx 先命中 "9"→.SH, 920→.BJ 分支**永不可达**,
    # 北交所新段在数据层被错配后于股票池交集整行静默丢弃 —— 920 判定必须前置。
    code = str(code).strip().zfill(6)
    if code.startswith("920"):
        return f"{code}.BJ"
    if code.startswith("6") or code.startswith("9"):
        return f"{code}.SH"
    if code.startswith(("0", "3", "2")):
        return f"{code}.SZ"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.UN"


def _parse_ak(df_raw, col_map, trade_date, out_cols):
    if df_raw is None or df_raw.empty or "代码" not in df_raw.columns:
        return None
    out = {}
    for field, cands in col_map.items():
        col = _pick_col(df_raw, cands)
        out[field] = df_raw[col] if col else None
    out = pd.DataFrame(out)
    out["trade_date"] = trade_date
    out["ts_code"] = df_raw["代码"].map(_norm_ts_code)
    # 只留沪深北(6/0/3/4/8/9 开头), 排除未知代码(统一过滤后再过股票池)
    # 2026-09-08 全市场扩展: 原排除北交所(4/8/9)是主板池时代的设计, 现纳入
    out = out[out["ts_code"].str[:1].isin(["6", "0", "3", "4", "8", "9"])]
    out = out[out_cols]
    return out


def _parse_ak_detail(raw, trade_date):
    df = _parse_ak(raw, _DETAIL_COLS, trade_date,
                   ["trade_date", "ts_code", "name", "close", "pct_chg", "net_buy",
                    "buy_value", "sell_value", "lhb_amount", "market_amount",
                    "net_ratio", "amount_ratio", "turnover", "float_mv", "reason"])
    if df is None:
        return None
    for c in ("close", "pct_chg", "net_buy", "buy_value", "sell_value", "lhb_amount",
              "market_amount", "net_ratio", "amount_ratio", "turnover", "float_mv"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return _dedupe_stock(df)


def _parse_ak_org(raw, trade_date):
    df = _parse_ak(raw, _ORG_COLS, trade_date,
                   ["trade_date", "ts_code", "name", "org_buy_num", "org_sell_num",
                    "org_buy_value", "org_sell_value", "org_net_value",
                    "org_net_ratio", "reason"])
    if df is None:
        return None
    for c in ("org_buy_num", "org_sell_num", "org_buy_value", "org_sell_value",
              "org_net_value", "org_net_ratio"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return _dedupe_stock(df)


def sync_lhb_ak(end_date=None) -> int:
    """AkShare 龙虎榜+机构买卖统计: 按交易日增量补齐(可中断续跑), 返回新增天数"""
    import akshare as ak

    today = timeutil.today().strftime("%Y%m%d")
    end_date = end_date or today
    conn = db.get_conn()
    cal = pd.read_sql(
        "SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date>=? AND cal_date<=? ORDER BY cal_date",
        conn, params=(LHB_START, end_date))["cal_date"].tolist()
    uni = set(pd.read_sql("SELECT ts_code FROM stock_basic", conn)["ts_code"])
    have = set()
    if db.table_exists(conn, "lhb"):
        have = set(pd.read_sql("SELECT DISTINCT trade_date AS d FROM lhb", conn)["d"])
    # 机构腿独立跟踪(2026-09-04 修复): 原先 missing 只由 lhb 表推导且明细腿成功即推进,
    # 机构腿失败/晚发布的日子永不重拉, lhb_org_net20 因子在残缺机构历史上滚动
    st_org = conn.execute("SELECT last_date FROM data_status WHERE name='lhb_org'").fetchone()
    last_org = st_org[0] if st_org else None
    # 2026-09-09 修复(审查项 ⑦ 机构腿 >3 日延迟): 水位只记录"处理过", 而机构榜为空时
    # 也照常推进(无法区分"当日无机构上榜"与"上游尚未发布") —— 发布延迟超过下方 3 日
    # 回补窗口的日子会被水位跳过, 永久缺机构数据。改为每轮从数据本身重算「疑似未发布日」:
    # 明细腿已有行、机构腿无行、且落在近 20 个交易日内的日子一律重拉(实测 2841 个上榜日
    # 全部有机构数据, 故"明细非空而机构为空"几乎必然是晚发布/部分发布)。无状态、自愈、
    # 只在真有洞时产生调用。
    susp_org = []
    if cal and db.table_exists(conn, "lhb") and db.table_exists(conn, "lhb_org"):
        # 2026-09-16 数据线审查 DATA-02: 20 日窗在停跑>20 个交易日时窗外洞永久滞留 ⇒ 60 日
        # （对账是纯 SQL，只有真洞才发 API 调用，成本不变；60 日覆盖停跑一个季度）
        recent = cal[-60:]
        q = ",".join("?" * len(recent))
        susp_org = [r[0] for r in conn.execute(
            f"SELECT DISTINCT d.trade_date FROM lhb d WHERE d.trade_date IN ({q}) "
            f"AND NOT EXISTS (SELECT 1 FROM lhb_org o WHERE o.trade_date=d.trade_date) "
            f"ORDER BY d.trade_date", tuple(recent)).fetchall()]
    conn.close()
    missing = [d for d in cal if d not in have]
    # 盘后(17点后)强制重拉最后两个真实交易日(2026-09-05 第七轮 N7-1): 原 append(today)
    # 自然日 —— 周六补跑时 today 非交易日, 每个榜单发必空请求; 与 sync_daily_bulk 同规则
    if end_date >= today and timeutil.now().hour >= 17:
        for d in cal[-2:]:
            if d and d not in missing:
                missing.append(d)
    missing = sorted(missing)
    missing_org = [d for d in cal if last_org is None or d > last_org]
    if end_date >= today and timeutil.now().hour >= 17:
        # 机构榜发布可能晚于明细榜(19点后), 与同花顺路径同规则重拉近3个交易日
        for d in cal[-3:]:
            if d not in missing_org:
                missing_org.append(d)
    # ⑦ 疑似未发布日并入(见上方 susp_org 说明): 不受 17 点/当日判定限制 —— 历史洞
    # 任何时候补跑都应回填
    if susp_org:
        added = [d for d in susp_org if d not in missing_org]
        if added:
            log.info("机构腿疑似晚发布回补 %d 日: %s", len(added), added[:10])
            missing_org.extend(added)
    org_set = set(missing_org)
    log.info("龙虎榜(AkShare): 缺 %d 个交易日(机构 %d)", len(missing), len(missing_org))
    done = 0
    fail_streak = 0
    conn = db.get_conn()
    detail_set = set(missing)
    all_days = sorted(detail_set | org_set)  # 两腿独立跟踪: 明细完整时机构缺口日期仍会处理
    try:
        for i, d in enumerate(all_days):
            if fail_streak >= 10:
                log.error("龙虎榜连续 10 天失败, 终止本轮(下次续跑)")
                break
            if d in detail_set:
                day_failed = False
                try:
                    raw = ak.stock_lhb_detail_em(start_date=d, end_date=d)
                    df = _parse_ak_detail(raw, d)
                    if df is not None:
                        df = df[df["ts_code"].isin(uni)]
                        n = db.upsert_df(conn, df, "lhb")
                        db.set_status(conn, "lhb", d, n)
                    else:
                        day_failed = True
                except Exception as e:
                    log.warning("龙虎榜 %s 失败: %s, 下次续跑", d, e)
                    day_failed = True
                if day_failed:
                    fail_streak += 1
                    continue
                fail_streak = 0
                done += 1
            if d in org_set:
                # 机构买卖统计独立拉取: 失败不推进状态(下次续跑重拉);
                # 无机构上榜为空属正常, 照常推进避免空榜日期被反复重拉
                try:
                    raw2 = ak.stock_lhb_jgmmtj_em(start_date=d, end_date=d)
                    df2 = _parse_ak_org(raw2, d)
                    n2 = 0
                    if df2 is not None:
                        df2 = df2[df2["ts_code"].isin(uni)]
                        n2 = db.upsert_df(conn, df2, "lhb_org")
                    db.set_status(conn, "lhb_org", d, n2)
                except Exception as e:
                    log.warning("机构买卖统计 %s 失败: %s, 状态不推进下次续跑", d, e)
            if (i + 1) % 100 == 0:
                log.info("龙虎榜: %d/%d (%s)", i + 1, len(all_days), d)
    finally:
        conn.close()
    log.info("龙虎榜(AkShare)同步结束, 本次 %d 天", done)
    return done


# ---------- 同花顺部分 ----------
def _ths_get(path: str, params: dict):
    """GET 同花顺 API; code=0 返回 data; 4001 限流退避重试; 认证/业务错误返回 None;
    1003(日期超出近一年窗口)返回 "WINDOW" 哨兵 —— 调用方按无数据推进水位而非判失败"""
    url = THS_BASE + path
    for attempt in range(THS_MAX_RETRY):
        try:
            resp = requests.get(url, params=params,
                                headers={"X-api-key": HITHINK_FINANCE_API_KEY},
                                timeout=THS_TIMEOUT)
            payload = resp.json()
        except Exception as e:
            log.warning("同花顺请求异常 %s: %s (第%d次)", path, e, attempt + 1)
            time.sleep(2 ** attempt)
            continue
        code = payload.get("code")
        if code == 0:
            return payload.get("data")
        if code in (2001, 2003):
            log.error("同花顺 API Key 无效(code=%s): %s", code, payload.get("message"))
            return None
        if code == 4001:
            log.warning("同花顺限流, 退避重试(第%d次)", attempt + 1)
            time.sleep(2 ** attempt)
            continue
        if code == 1003:
            log.info("同花顺 %s: 日期超出窗口, 跳过 (%s)", path, payload.get("message"))
            return "WINDOW"
        log.warning("同花顺业务错误 code=%s: %s", code, payload.get("message"))
        return None
    return None


def _parse_ths_stock_items(items, trade_date):
    if not items:
        return None
    rows = []
    for it in items:
        concepts = [c.get("name") for c in it.get("concept_list") or [] if c.get("name")]
        rows.append({
            "trade_date": trade_date,
            "thscode": it.get("thscode"),
            "name": it.get("name"),
            "change": it.get("change"),
            "net_value": it.get("net_value"),
            "buy_value": it.get("buy_value"),
            "sell_value": it.get("sell_value"),
            "hot_rank": it.get("hot_rank"),
            "org_net_value": it.get("org_net_value"),
            "org_net_rate": it.get("org_net_rate"),
            "org_buy_num": it.get("org_buy_num"),
            "org_sell_num": it.get("org_sell_num"),
            "limit_reason": it.get("limit_reason"),
            "concepts": json.dumps(concepts, ensure_ascii=False) if concepts else None,
            "range_days": it.get("range_days"),
        })
    df = pd.DataFrame(rows)
    # 2026-09-05 审查修复: lhb_ths 是 WITHOUT ROWID 主键(trade_date,thscode,range_days),
    # PK 列隐式 NOT NULL, 上游字段缺位(接口形状漂移)会 IntegrityError 且水位不推进、
    # 该同步永久卡死。缺主键字段的记录直接丢弃并告警。
    n_bad = df["thscode"].isna().sum() + df["range_days"].isna().sum()
    if n_bad:
        log.warning("同花顺龙虎榜 %s: %d 条缺 thscode/range_days(主键字段), 已丢弃", trade_date, n_bad)
        df = df[df["thscode"].notna() & df["range_days"].notna()]
    return df if not df.empty else None


def _parse_ths_hm_items(hm_items, trade_date):
    rows = []
    for hm in hm_items or []:
        for it in hm.get("rows") or []:
            rows.append({
                "trade_date": trade_date,
                "hm_name": hm.get("name"),
                "thscode": it.get("thscode"),
                "name": it.get("name"),
                "net_value": it.get("hot_money_item_net_value")
                if it.get("hot_money_item_net_value") is not None else it.get("net_value"),
                "buying": hm.get("buying"),
            })
    df = pd.DataFrame(rows) if rows else None
    if df is not None:
        # 同上: (trade_date, hm_name, thscode) 主键字段缺失会卡死同步
        n_bad = df["hm_name"].isna().sum() + df["thscode"].isna().sum()
        if n_bad:
            log.warning("同花顺游资榜 %s: %d 条缺 hm_name/thscode, 已丢弃", trade_date, n_bad)
            df = df[df["hm_name"].notna() & df["thscode"].notna()]
        return df if not df.empty else None
    return None


def sync_lhb_ths(end_date=None) -> int:
    """同花顺龙虎榜日增量(近一年窗口, 逐日积累), 返回新增天数; 未配 Key 时跳过。
    游资榜(lhb_ths_hm)独立水位(2026-09-07 全面检查): 原 all-board 成功即推进 lhb_ths
    状态, 游资榜当日失败(限流/接口异常)永不再拉 —— 与 AkShare 机构腿同型问题,
    独立跟踪后失败日下次续跑重拉。"""
    if not HITHINK_FINANCE_API_KEY:
        log.warning("未配置 HITHINK_FINANCE_API_KEY, 跳过同花顺龙虎榜同步")
        return 0
    today = timeutil.today().strftime("%Y%m%d")
    end_date = end_date or today
    # 2026-09-18 审查 P0-01: 原 `conn = get_conn() ... conn.close()` 把 close 放在水位
    # 读取之后, 但 2026-09-16 加的「水位内 40 日缺口对账」(:364/:373) 在 close **之后**
    # 又用同一连接查询 ⇒ 每天必抛 `sqlite3.ProgrammingError: Cannot operate on a closed
    # database`, 被 updater 的 except 吞掉 ⇒ lhb_ths 水位永不推进、数据静默停更。
    # 整段改用 conn_ctx(): 对账 SQL 在连接存活期内执行, 且异常路径也必定 close。
    with db.conn_ctx() as conn:
        cal = sorted(pd.read_sql(
            "SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date<=? ORDER BY cal_date DESC LIMIT 250",
            conn, params=(end_date,))["cal_date"].tolist())
        st = conn.execute("SELECT last_date FROM data_status WHERE name='lhb_ths'").fetchone()
        last = st[0] if st else None
        st_hm = conn.execute("SELECT last_date FROM data_status WHERE name='lhb_ths_hm'").fetchone()
        last_hm = st_hm[0] if st_hm else None
        if last_hm is None and db.table_exists(conn, "lhb_ths_hm"):
            # 无状态行但表有数据(历史库): 以实际最大日期播种水位, 避免全窗口重爬
            last_hm = conn.execute("SELECT MAX(trade_date) FROM lhb_ths_hm").fetchone()[0]
        if last is None and db.table_exists(conn, "lhb_ths"):
            # 2026-09-08 审查 P2: 主腿与 hm 腿同款播种(原缺) —— 状态行丢失时重爬整窗
            # 250 天纯浪费配额
            last = conn.execute("SELECT MAX(trade_date) FROM lhb_ths").fetchone()[0]
        missing = [d for d in cal if last is None or d > last]
        missing_hm = [d for d in cal if last_hm is None or d > last_hm]
        # 2026-09-16 数据线审查 DATA-01: 纯水位推导存在「单日失败+次日成功 ⇒ 水位越过失败日」
        # 的化石洞（生产库实证 lhb_ths 9 个/lhb_ths_hm 11 个，全在 2025 国庆窗）。改为
        # **水位内近 40 个交易日对账**：cal 有而表内无行的日子也纳入重探；有数据则补行
        #（**不动 set_status**——盲写会让水位倒退），上游确实无数据的日子逐日重探直到
        # 滑出 40 日窗（自限，每天最多 ~2×洞数 次空探）。
        if last is not None and len(cal) > 40:
            lo = cal[-40]
            have_ths = {r[0] for r in conn.execute(
                "SELECT DISTINCT trade_date FROM lhb_ths WHERE trade_date>=? AND trade_date<=?",
                (lo, last)).fetchall()}
            holes = [d for d in cal if lo <= d <= last and d not in have_ths]
            if holes:
                log.info("龙虎榜(同花顺): 水位内对账发现 %d 个缺口日, 重探: %s", len(holes), holes[:6])
                missing = sorted(set(missing) | set(holes))
        if last_hm is not None and len(cal) > 40:
            lo = cal[-40]
            have_hm = {r[0] for r in conn.execute(
                "SELECT DISTINCT trade_date FROM lhb_ths_hm WHERE trade_date>=? AND trade_date<=?",
                (lo, last_hm)).fetchall()}
            holes_hm = [d for d in cal if lo <= d <= last_hm and d not in have_hm]
            if holes_hm:
                missing_hm = sorted(set(missing_hm) | set(holes_hm))
    if end_date >= today and timeutil.now().hour >= 17:
        # 盘后强制重拉最近3个交易日: 龙虎榜晚间发布可能晚于定时任务, data_status 推进后缺失日不会自愈
        for d in cal[-3:]:
            if d not in missing:
                missing.append(d)
            if d not in missing_hm:
                missing_hm.append(d)
    missing = sorted(missing)
    missing_hm = sorted(missing_hm)
    hm_set = set(missing_hm)
    all_days = sorted(set(missing) | hm_set)  # 两腿独立: all-board 完整时游资榜缺口日仍会处理
    log.info("龙虎榜(同花顺): 缺 %d 个交易日(游资榜 %d)", len(missing), len(missing_hm))
    done = 0
    conn = db.get_conn()
    none_streak = 0
    try:
        for d in all_days:
            date_str = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            windowed = False  # all-board 是否已判"超出近一年窗口"(游资榜同窗口, 一并推进)
            if d in set(missing):
                data = _ths_get("/api/a-share/special-data/dragon-tiger-list",
                                {"board_type": "all", "date": date_str})
                if data == "WINDOW":
                    # 2026-09-05 审查修复: 1003(超出近一年窗口)按"无数据"推进水位,
                    # 原计入失败 streak → 新装机/长缺口时最老的 5 天全是 1003 → 中止在
                    # 窗口内日期之前, 水位永不推进, lhb_ths 永久停摆
                    db.set_status(conn, "lhb_ths", d, 0)
                    done += 1
                    none_streak = 0
                    windowed = True
                elif data is None:
                    # 2026-09-04 修复: 连续失败尽快中止, 防 Key 失效/接口故障时每次运行
                    # 都在任务锁内从 last 起重爬整个 ~250 交易日窗口
                    none_streak += 1
                    if none_streak >= 5:
                        log.error("同花顺连续 5 个交易日无数据, 终止本轮(疑似 Key 失效/接口故障, 下次续跑)")
                        break
                    continue
                else:
                    none_streak = 0
                    df = _parse_ths_stock_items(data.get("stock_items"), d)
                    n = db.upsert_df(conn, df, "lhb_ths") if df is not None else 0
                    # 无论有无数据都记录推进, 避免空榜日期被反复重拉
                    db.set_status(conn, "lhb_ths", d, n)
                    done += 1
            if d in hm_set:
                if windowed:
                    db.set_status(conn, "lhb_ths_hm", d, 0)
                    continue
                hm = _ths_get("/api/a-share/special-data/dragon-tiger-list",
                              {"board_type": "hot_money", "date": date_str})
                if hm == "WINDOW":
                    db.set_status(conn, "lhb_ths_hm", d, 0)
                elif isinstance(hm, dict):
                    df_hm = _parse_ths_hm_items(hm.get("hot_money_items"), d)
                    n_hm = db.upsert_df(conn, df_hm, "lhb_ths_hm") if df_hm is not None else 0
                    db.set_status(conn, "lhb_ths_hm", d, n_hm)
                # hm None(失败): 状态不推进, 下次续跑重拉
    finally:
        conn.close()
    log.info("龙虎榜(同花顺)同步结束, 本次 %d 天", done)
    return done
