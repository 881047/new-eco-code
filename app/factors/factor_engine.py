"""因子引擎: 日频计算量价/估值因子, 月度截面存储(PIT 财务合并 + 行业中性化)

因子在月度截面(月末交易日)存储, 全部经过: 截面去极值(2%/98%) → 行业中性化 → z-score
"""
import datetime
import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path

import numpy as np
import pandas as pd

from app import db
from app import timeutil
from app.config import DATA_DIR, benchmark_code, benchmark_nav_code

log = logging.getLogger(__name__)

BENCH = "000906.SH"  # 兼容常量; 实际取值见 config.benchmark_code() (2026-09-06 R18-2)。
# ⚠️ 面板里有**两条**基准腿(2026-09-20 B1): `m["bench"]`=价格指数(喂 beta_250 等因子, 动它=改
# 生产特征), `m["bench_tr"]`=全收益(只给回测报告腿/袖套 KPI)。取码分别见 benchmark_code()
# 与 benchmark_nav_code(), 两键互不回落。守卫: tests/test_bench_leg_wiring.py。
FINA_FIELDS = ["roe", "netprofit_yoy", "or_yoy", "grossprofit_margin", "debt_to_assets"]

# ---------------------------------------------------------------- 行情面板缓存（2026-09-13）
# 为什么：`load_market` 每次要读 ~1700 万行再透视，实测 **322~465 秒**（F50/F52/F55 各付一次）。
# 研究迭代与 19:30 打分保鲜档都被它卡住。缓存键不是文件 mtime —— 模拟盘/账本写的是**同一个库**，
# mtime 一变就作废等于没有缓存；改用**内容指纹**：各行情表的 MAX(trade_date)（价格面板只由日链更新）。
MARKET_CACHE_DIR = DATA_DIR / "cache" / "market"
# 只缓存"大面板"：全历史 / 近 10+ 年（周频 SINCE=20140101）要 300+ 秒，值得缓存；
# 而 `plan._tradable_at` 每周用**不同**的 250 天窗口（几秒级、体积小），若也落缓存会
# 每周挤掉那份大缓存（只保留最近 2 份）⇒ 下次打分又得重建 326 秒。窗口起点晚于这个截点就跳过缓存。
_CACHE_SINCE_CUTOFF = "20150101"
_CACHE_FRAMES = ("close", "pre_close", "open", "high", "low", "vol", "amount", "adj", "pct",
                 "turnover", "pe_ttm", "pb", "dv_ttm", "total_mv", "circ_mv")


def _cache_worth(since: str = None) -> bool:
    """这个 since 的面板大不大（值不值得缓存）。None=全历史 ⇒ 是。"""
    return since is None or str(since) <= _CACHE_SINCE_CUTOFF


def market_fingerprint(conn, since: str = None) -> str:
    """行情面板的内容指纹（便宜、只读）。任一行情表缺失/查询失败 ⇒ 返回空串（禁用缓存）。"""
    parts = [f"since={since}", f"bench={benchmark_code()}", f"bench_tr={benchmark_nav_code()}"]
    for t in ("daily", "daily_basic", "adj_factor", "index_daily"):
        try:
            row = conn.execute(f"SELECT MAX(trade_date) FROM {t}").fetchone()
        except Exception:
            return ""
        parts.append(f"{t}={row[0] if row else '-'}")
    # 2026-09-13 评审发现（自查）：只比 `MAX(trade_date)` 会漏掉"**原地重写已有日期**"——
    # 日链对疑似残缺批会次日重拉并覆盖同一天的行情（见 updater 的"残缺批次日重拉会自愈"），
    # 此时最大日期不变、数据已变，缓存却不会失效 ⇒ 会继续供应旧价。
    # `data_status.updated_at` 每次同步都会刷新 ⇒ 把它一起纳入指纹。
    # （表缺失/查询失败**不**禁用缓存：它是可选信号，四个价格表才是硬条件。）
    try:
        rows = conn.execute(
            "SELECT name, last_date, updated_at FROM data_status "
            "WHERE name IN ('daily','daily_basic','adj_factor','index_daily') "
            "ORDER BY name").fetchall()
    except Exception:
        rows = []
    parts.append("status=" + ";".join("/".join(str(x) for x in r) for r in rows))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _is_prod_db(conn) -> bool:
    """连接指向的是不是生产库？⇒ **只有生产库才允许落缓存**。

    为什么需要它（2026-09-13）：测试/一次性脚本常用临时 sqlite（同样的表名、不同的数据），
    若不区分，它们会往 `data/cache/market/` 写**假面板**，还可能按 mtime 把真缓存挤掉
    （只保留最近 2 份）。读缓存不受限（键=指纹，天然区分）。
    """
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        return bool(row) and Path(row[2] or "").resolve() == Path(db.DB_PATH).resolve()
    except Exception:
        return False


def _cache_load(key: str, t0: float) -> dict:
    """命中返回面板；缺文件/损坏/形状异常一律返回 None（走重建路径，绝不返回半截数据）。"""
    d = MARKET_CACHE_DIR / key
    if not (d / "_meta.json").exists():
        return None
    try:
        info = json.loads((d / "_meta.json").read_text(encoding="utf-8"))
        if info.get("key") != key:
            return None
        out = {}
        for name in _CACHE_FRAMES:
            df = pd.read_parquet(d / f"{name}.parquet")
            df.index = pd.to_datetime(df.index)
            if df.shape != tuple(info["shapes"][name]):
                log.warning("行情缓存 %s 的 %s 形状不符（%s ≠ %s），重建",
                            key, name, df.shape, info["shapes"][name])
                return None
            out[name] = df
        bench = pd.read_parquet(d / "bench.parquet").iloc[:, 0]
        bench.index = pd.to_datetime(bench.index)
        out["bench"] = bench
        # bench_tr 缺文件会抛 → 外层 except 走重建（不自作聪明补 NaN：半截基准腿比没有更坏）
        bench_tr = pd.read_parquet(d / "bench_tr.parquet").iloc[:, 0]
        bench_tr.index = pd.to_datetime(bench_tr.index)
        out["bench_tr"] = bench_tr
        log.info("行情面板命中缓存 %s（%s，载入 %.1fs；原始查询约 5~7 分钟）",
                 key, out["close"].shape, time.time() - t0)
        return out
    except Exception as e:
        log.warning("行情缓存读取失败（重建）: %s", e)
        return None


def _cache_status_path() -> Path:
    """「上一次缓存写入成没成」的证据文件路径（`doctor` 体检项读它）。

    ⚠️ 路径**现算**，不写成模块常量：`tests/test_market_cache.py` 会
    `mock.patch.object(fe, "MARKET_CACHE_DIR", tmp)`，写成常量会让状态文件落到
    **生产**目录。同型事故已有一次（2026-09-22：测试库算出的"0 个锚点"被写进生产
    `data/weekly/block_ic.json`），见 `health._block_cache_path` 的注记。
    """
    return MARKET_CACHE_DIR / "_last_write.json"


def read_cache_status() -> dict:
    """读缓存写入状态（`doctor` 与测试共用）。文件不在/损坏 ⇒ 空 dict（**不抛**）。"""
    try:
        return json.loads(_cache_status_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_cache_status(**fields) -> dict:
    """原子更新状态文件（读-改-写，保留未在本次传入的键，如 `alerted_day`）。

    失败只 `log.warning` —— 它是**观测**，绝不能把主流程带崩（同 `_cache_save` 的纪律）。
    """
    st = read_cache_status()
    st.update(fields)
    st["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    p = _cache_status_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:
        log.warning("行情缓存状态写入失败（不影响本次结果）: %s", e)
    return st


def _alert_cache_write_failed(key: str, err: Exception) -> None:
    """缓存写入失败 → 运维告警（**同一自然日只发一封**）。

    为什么必须加这一步（2026-09-24 审查）：该失败原来只有一行 `log.warning`，
    **不进邮件、不进 doctor、不进体检** ⇒ 2026-09-24 19:34 写下的那行
    `No type extension with name arrow.py_extension_type found` **沉睡了 2 小时**
    才被人工审查发现。这是本项目"静默失败"主题的一个变体：**日志有痕、告警无路**。
    （同族：07 号 #46 只修了"有买无卖 ⇒ 换手 NaN"，没管失败的可观测性。）

    去重为什么不用 `scheduler._ops_alert_once`：那会让因子引擎**反向依赖调度器**
    （调度器是驱动方），且它的 `_ALERTED` 只活在进程内。这里用状态文件的
    `alerted_day` 做同日去重 —— 自包含、可测、且服务重启后仍生效。
    """
    # 2026-09-28 收编 timeutil(东八区): 去重日判定随机器时区漂移会造成多发/漏发
    today = timeutil.today().strftime("%Y%m%d")
    st = _write_cache_status(ok=False, key=key, error=str(err))
    if st.get("alerted_day") == today:
        return
    sent = False
    try:
        from app import notify                      # 惰性导入：避免模块级依赖倒挂
        sent = notify.send_ops_alert(
            "行情缓存写入失败",
            "行情面板缓存**写不进磁盘**。本次结果不受影响，但**同日后续的大面板加载**"
            "会失去缓存加速（每次多付约 4~8 分钟原始查询）。\n\n"
            f"缓存 key：{key}\n"
            f"错误：{err}\n\n"
            "处置：重启服务可重置进程内 pyarrow 扩展类型状态（最低成本）；"
            "若复发，查 logs/app.log 的「行情缓存写入失败」。")
    except Exception as e:                          # 告警发不出去不该影响跑数
        log.warning("行情缓存失败告警发送异常: %s", e)
    if sent:
        _write_cache_status(alerted_day=today)


def _cache_save(key: str, panel: dict) -> None:
    """原子落盘（临时目录 → os.replace），失败只告警；只保留最近 2 份。

    2026-09-24：成功/失败都往 `_last_write.json` 落一份证据，供 `doctor` 体检项读取；
    失败还会发一封同日去重的运维告警（原来只有一行 WARNING，没人看得见）。
    """
    tmp = MARKET_CACHE_DIR / f".tmp_{key}_{os.getpid()}"
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        shapes = {}
        for name in _CACHE_FRAMES:
            panel[name].to_parquet(tmp / f"{name}.parquet", compression="snappy")
            shapes[name] = list(panel[name].shape)
        panel["bench"].to_frame("bench").to_parquet(tmp / "bench.parquet", compression="snappy")
        panel["bench_tr"].to_frame("bench_tr").to_parquet(tmp / "bench_tr.parquet",
                                                          compression="snappy")
        (tmp / "_meta.json").write_text(json.dumps(
            {"key": key, "built_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "shapes": shapes}, ensure_ascii=False), encoding="utf-8")
        final = MARKET_CACHE_DIR / key
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)
        keep = sorted([p for p in MARKET_CACHE_DIR.glob("*")
                       if p.is_dir() and not p.name.startswith(".")],
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for old in keep[2:]:
            shutil.rmtree(old, ignore_errors=True)
        log.info("行情面板已缓存 → %s（%s）", final, panel["close"].shape)
        _write_cache_status(ok=True, key=key, error=None)
    except Exception as e:
        log.warning("行情缓存写入失败（不影响本次结果）: %s", e)
        shutil.rmtree(tmp, ignore_errors=True)
        _alert_cache_write_failed(key, e)


# ---------- 数据加载 ----------
def _pivot_multi(conn, table, cols, since: str = None) -> dict:
    """一张表一条 SQL 取全列后内存透视(2026-09-04 审查 P1-2): 原 _pivot 每列一次 SELECT,
    同一张千万行表被全表扫描 5-8 遍; since 给定时只取该日及以后(日频窗口用)"""
    where, params = (" WHERE trade_date>=?", (since,)) if since else ("", None)
    q = ", ".join(["trade_date", "ts_code"] + cols)
    df = pd.read_sql(f"SELECT {q} FROM {table}{where}", conn, params=params)
    out = {}
    for c in cols:
        out[c] = (df.pivot(index="trade_date", columns="ts_code", values=c)
                  .sort_index().astype(np.float32))
    return out


def load_market(since: str = None, use_cache: bool = True) -> dict:
    """加载核心行情矩阵; since 只取该日及以后(日频链路窗口化, 原每次全历史约 1~1.5GB)。

    `use_cache=True`（默认）时按内容指纹命中 `data/cache/market/<key>/`：同一份行情数据
    第二次起从 parquet 读（秒级），日链写入新交易日 ⇒ 指纹变化 ⇒ 自动重建。
    设 `NEWECO_NO_MARKET_CACHE=1` 或传 `use_cache=False` 可强制走原始查询。
    """
    t0 = time.time()
    key, allow_write = "", False
    if use_cache and _cache_worth(since) and os.getenv("NEWECO_NO_MARKET_CACHE") != "1":
        with db.conn_ctx() as conn:
            key = market_fingerprint(conn, since)
            allow_write = _is_prod_db(conn)      # 临时库/测试库只读缓存，绝不写
        if key:
            hit = _cache_load(key, t0)
            if hit is not None:
                return hit
    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        d = _pivot_multi(conn, "daily",
                         ["close", "pre_close", "open", "high", "low", "vol", "amount", "pct_chg"],
                         since)
        # circ_mv 于 2026-09-11 加入(芯片式架构 阶段 0b): 融资余额/融券余额的归一化分母 ——
        # 用流通市值而非总市值, 因为两融标的的杠杆资金只作用于可交易的那部分股本
        b = _pivot_multi(conn, "daily_basic",
                         ["turnover_rate", "pe_ttm", "pb", "dv_ttm", "total_mv", "circ_mv"], since)
        adj = _pivot_multi(conn, "adj_factor", ["adj_factor"], since)["adj_factor"]
        # 两条基准腿一次取回(2026-09-20 B1): 价格指数腿喂因子(beta_250 等, 尺子不许动),
        # 全收益腿只给回测报告/袖套 KPI。带 ts_code 列, 空结果也保留列名 ⇒ 不会 KeyError。
        bench = pd.read_sql(
            "SELECT ts_code, trade_date, close FROM index_daily WHERE ts_code IN (?,?)",
            conn, params=(benchmark_code(), benchmark_nav_code()))

    dates = pd.to_datetime(d["close"].index)
    d["close"].index = dates  # 统一 datetime 索引, 下游标签对齐/执行日定位都依赖它

    def _al(m):
        m.index = pd.to_datetime(m.index)  # 先转自身索引再对齐
        return m.reindex(index=dates)

    def _bench_leg(code: str) -> pd.Series:
        bb = bench[bench["ts_code"] == code].set_index("trade_date")["close"]
        bb.index = pd.to_datetime(bb.index)
        return bb.reindex(dates).astype(np.float32)

    bench_s = _bench_leg(benchmark_code())
    bench_tr_s = _bench_leg(benchmark_nav_code())
    # 只报警不 ffill：补一个价会把"基准缺一天"变成"基准那天不涨不跌"，历史读数就此被改
    # （而重钉的端点恒等式按构造要求不补）。缺腿的真实后果是超额出 NaN —— 显式、可查。
    _nan_tr = int(bench_tr_s.isna().sum())
    if _nan_tr:
        log.warning("基准净值腿 bench_tr=%s 在面板 %d/%d 个交易日缺值 ⇒ 超额/基准年化将为 NaN",
                    benchmark_nav_code(), _nan_tr, len(bench_tr_s))
    panel = {
        "close": d["close"], "pre_close": _al(d["pre_close"]), "open": _al(d["open"]),
        "high": _al(d["high"]), "low": _al(d["low"]),
        "vol": _al(d["vol"]), "amount": _al(d["amount"]), "adj": _al(adj),
        "pct": _al(d["pct_chg"]),
        "turnover": _al(b["turnover_rate"]), "pe_ttm": _al(b["pe_ttm"]),
        "pb": _al(b["pb"]),
        "dv_ttm": _al(b["dv_ttm"]), "total_mv": _al(b["total_mv"]),
        "circ_mv": _al(b["circ_mv"]), "bench": bench_s, "bench_tr": bench_tr_s,
    }
    log.info("行情面板原始查询完成: %s（%.0fs）", panel["close"].shape, time.time() - t0)
    if key and allow_write:
        _cache_save(key, panel)
    return panel


def industry_map(conn) -> pd.Series:
    df = pd.read_sql("SELECT ts_code, industry FROM stock_basic", conn)
    return df.set_index("ts_code")["industry"].fillna("未知")


def month_end_dates(dates: pd.DatetimeIndex, today=None) -> pd.DatetimeIndex:
    """每个自然月的最大交易日。today 传入时只保留已完结自然月(截面日 < today 所在月第一天):
    每月1日晚跑月度流程时当月1日行情已入库, 若不过滤会把 9/1 误当"9月月度截面",
    方案锚定日漂移到月中(2026-09-04 审查 P0-2)。today=None 保持原始行为(纯日历取月末)。
    """
    df = pd.DataFrame({"d": dates}, index=dates)
    out = pd.DatetimeIndex(df.groupby([dates.year, dates.month])["d"].max().values)
    if today is not None:
        cutoff = pd.Timestamp(today).replace(day=1)
        out = out[out < cutoff]
    return out


# ---------- 因子计算 ----------
def compute_price_factors(m: dict) -> dict:
    adj = m["adj"]
    ac = (m["close"] * adj).astype(np.float32)   # 复权价
    ah = (m["high"] * adj).astype(np.float32)
    al = (m["low"] * adj).astype(np.float32)
    ret = ac.pct_change()
    bench_ret = m["bench"].pct_change()
    bench_var = bench_ret.rolling(250).var()

    f = {}
    # 2026-09-06 第十轮 N10-22 意图锁: rev_5 用近端窗口 [T-5,T], 动量系列统一从 T-5
    # 起算 —— 反转窗与动量窗正交不重叠是**有意设计**(同一段收益不重复进两个因子),
    # 不是写错; 切勿"修正"成 shift(0), 否则引入窗口重叠
    f["rev_5"] = -(ac / ac.shift(5) - 1)
    f["mom_20"] = ac.shift(5) / ac.shift(20) - 1
    f["mom_60"] = ac.shift(5) / ac.shift(60) - 1
    f["mom_120"] = ac.shift(5) / ac.shift(120) - 1
    f["mom_250"] = ac.shift(5) / ac.shift(250) - 1
    f["vol_20"] = ret.rolling(20).std()
    f["vol_60"] = ret.rolling(60).std()
    f["skew_20"] = ret.rolling(20).skew()
    f["ret_max20"] = ret.rolling(20).max()
    f["turnover_20"] = m["turnover"].rolling(20).mean()
    f["turnover_chg"] = m["turnover"].rolling(20).mean() / m["turnover"].rolling(60).mean() - 1
    f["amount_log"] = np.log(m["amount"].rolling(20).mean() + 1)
    f["illiq"] = (ret.abs() / (m["amount"] + 1)).rolling(20).mean()
    f["bias_20"] = ac / ac.rolling(20).mean() - 1
    f["bias_60"] = ac / ac.rolling(60).mean() - 1
    f["high_52w"] = ac / ah.rolling(252, min_periods=120).max() - 1
    f["low_52w"] = ac / al.rolling(252, min_periods=120).min() - 1
    # pandas 3.0: DataFrame/Series 运算默认按列对齐, 必须显式 axis=0(按行=按日期)
    f["beta_250"] = ret.rolling(250).cov(bench_ret).div(bench_var, axis=0)
    f["size"] = np.log(m["total_mv"].replace(0, np.nan))
    f["ep"] = 1.0 / m["pe_ttm"].replace(0, np.nan)
    f["bp"] = 1.0 / m["pb"].replace(0, np.nan)
    f["dv_ttm"] = m["dv_ttm"]
    return f


def shift_to_next_trading_day(ann_dates: pd.Series, calendar) -> pd.Series:
    """ann_date → 其后第一个交易日(严格大于); 无后续交易日(未来披露/日历截断)返回 None。

    2026-09-09 修复(审查项 ①): 财报披露日 PIT 对齐用。A 股财报多在收盘后披露, 当日
    截面就使用它等于 1 个交易日的前视(实测 42,348 行披露日恰落在评分/因子日)。规则:
    - ann_date 是交易日 → 次一交易日可见(保守: 覆盖早间公告的情形)
    - ann_date 是非交易日(周末/节假日) → 其次一交易日可见(信息此时已公开, 不算保守)
    """
    cal = pd.Index(sorted(set(pd.Series(list(calendar)).astype(str))))
    if len(cal) == 0:
        return pd.Series([None] * len(ann_dates), index=ann_dates.index)
    # 2026-09-12 审查 P1-01: 空/非法 ann_date 必须丢弃 —— "" 在 searchsorted 里排在
    # 所有 YYYYMMDD 之前 ⇒ pos=0 ⇒ 这条财报从**日历首个交易日(2013)**起就全程可见,
    # 是最严重形态的前视, 且静默无任何告警。updater.py 与 akshare_client 的
    # _est_ann_date 在日期非法时正是返回 ""(实库当前 0 行, 属 latent)。
    # astype("string").fillna("") 而不是 astype(str): pandas 3.0 下后者会把 NaN 留成
    # float(nan) —— 下面 len() 直接 TypeError, 且 "nan" 也过不了 len==8 这道闸。
    raw = pd.Series(list(ann_dates)).astype("string").fillna("").to_numpy(dtype=object)
    valid = np.fromiter((len(s) == 8 and s.isdigit() for s in raw),
                        dtype=bool, count=len(raw))
    pos = np.zeros(len(raw), dtype=np.int64)
    pos[valid] = cal.searchsorted(raw[valid], side="right")
    vals = cal.to_numpy()
    out = np.empty(len(raw), dtype=object)
    ok = valid & (pos < len(cal))
    out[ok] = vals[pos[ok]]
    out[~ok] = None
    # dtype=object 显式指定: 混着 None 和字符串时 pandas 会推断成 StringDtype 并把
    # None 变成 NaN, 「返回 None」这句文档就名不副实了(两个调用方都 dropna, 只影响契约)
    return pd.Series(out, index=ann_dates.index, dtype=object)


def compute_fina_factors(month_strs: list, calendar=None) -> dict:
    """PIT 财务因子: 以披露日(ann_date)对齐到月度截面, 避免未来函数

    calendar: 交易日历(可迭代, 形如 YYYYMMDD); None 时从 daily 表取。"""
    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        fina = pd.read_sql(
            "SELECT ts_code, ann_date, end_date, roe, netprofit_yoy, or_yoy, "
            "grossprofit_margin, debt_to_assets, eps FROM fina_indicator", conn)
        if calendar is None:
            calendar = pd.read_sql("SELECT DISTINCT trade_date FROM daily "
                                   "ORDER BY trade_date", conn)["trade_date"]
    if fina.empty:
        log.warning("财务数据为空, 跳过财务因子")
        return {}
    fina["ann_date"] = fina["ann_date"].astype(str)
    # 2026-09-09 修复(审查项 ①): 披露日顺延到下一交易日再参与 ffill —— 原直接按
    # ann_date 对齐, 收盘后披露的报告当日截面即生效(1 日前视)
    fina["ann_date"] = shift_to_next_trading_day(fina["ann_date"], calendar)
    fina = fina.dropna(subset=["ann_date"])
    # 2026-09-05 审查修复: 同日披露多期报告(年报+一季报在 A 股很常见)时 pivot 默认 mean
    # 会算出两期算术平均的混合值并 ffill 进整月截面 —— 每 (ts_code, ann_date) 只保留
    # 报告期末最新的一行(① 顺延后不同披露日可能落到同一交易日, 同规则去重)
    fina = fina.sort_values("end_date").drop_duplicates(["ts_code", "ann_date"], keep="last")

    # eps 同比: 与上年同期(end_date 精确减 12 个月)对齐 —— 2026-09-05 审查修复:
    # 原 shift(4) 按位置对齐, 任一季报缺失/延迟即整体错位一个季度且错误静默进入因子
    eps = fina[["ts_code", "end_date", "eps"]].drop_duplicates(["ts_code", "end_date"]).copy()
    eps["end_dt"] = pd.to_datetime(eps["end_date"])
    eps["lag_dt"] = eps["end_dt"] - pd.DateOffset(months=12)
    lag = eps[["ts_code", "end_dt", "eps"]].rename(columns={"end_dt": "lag_dt", "eps": "eps_lag4"})
    eps = eps.merge(lag, on=["ts_code", "lag_dt"], how="left")
    eps["eps_yoy"] = eps["eps"] / eps["eps_lag4"] - 1
    fina = fina.merge(eps[["ts_code", "end_date", "eps_yoy"]], on=["ts_code", "end_date"], how="left")

    full_idx = sorted(set(fina["ann_date"]) | set(month_strs))
    out = {}
    for field in FINA_FIELDS + ["eps_yoy"]:
        pv = fina.pivot_table(index="ann_date", columns="ts_code", values=field).sort_index()
        pv = pv.reindex(full_idx).ffill().loc[month_strs]
        out[field] = pv
    return out


def compute_lhb_factors(dates: pd.DatetimeIndex) -> dict:
    """龙虎榜因子: 近20交易日「净买入占比」滚动和 / 「机构净买入占比」滚动和 / 上榜次数
    未上榜股票为 NaN(LightGBM 原生处理缺失); 稀疏矩阵, 仅上榜股票有值"""
    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关); 块内不手动 close ——
        # 离开块时它必定关(双 close 无害但属"谁在下面加语句谁就炸"的脆弱写法, updater.py 同款教训)
        if not db.table_exists(conn, "lhb"):
            log.warning("lhb 表不存在, 跳过龙虎榜因子")
            return {}
        net = _pivot_multi(conn, "lhb", ["net_ratio"])["net_ratio"]
        org = _pivot_multi(conn, "lhb_org", ["org_net_ratio"])["org_net_ratio"]
    if net.empty:
        log.warning("龙虎榜数据为空, 跳过龙虎榜因子")
        return {}
    net = net.astype(np.float32)
    org = org.astype(np.float32)
    net.index = pd.to_datetime(net.index)
    org.index = pd.to_datetime(org.index)
    cols = net.columns.union(org.columns)
    net = net.reindex(index=dates, columns=cols)
    org = org.reindex(index=dates, columns=cols)
    cnt = net.notna().astype(np.float32)
    # min_periods=1: 稀疏数据按「窗口内出现的值求和」(默认 min_periods=20 会让上榜不足20次的股票永远 NaN)
    return {
        "lhb_net20": net.rolling(20, min_periods=1).sum(),
        "lhb_org_net20": org.rolling(20, min_periods=1).sum(),
        "lhb_cnt20": cnt.rolling(20, min_periods=1).sum(),
    }


# ---------- 稀疏面板守卫（D 案 ③-4，2026-09-22） ----------
#: F35 实测界线：非零占比低于它时，`neutralize` 的 2%/98% winsorize 会把**整行归零**
#: （98 分位正好是 0）⇒ z-score 全 NaN ⇒ 下游 IC **静默消失**。
SPARSE_NONZERO_RATIO = 0.02


def sparse_ratio(df: pd.DataFrame) -> float:
    """非零单元格占**非缺失**单元格的比例（F35 判据的口径）。

    分母取非缺失而不是全部格子：winsorize 的分位数是**跳过 NaN** 算的，
    "缺失"不会被 clip 归零，只有**显式的 0** 才会。全空面板返回 0.0（= 最坏情况）。
    """
    nn = df.notna().to_numpy(dtype=bool)
    n = int(nn.sum())
    if n == 0:
        return 0.0
    nz = int((df.to_numpy(dtype=np.float64, na_value=np.nan)[nn] != 0).sum())
    return nz / n


def is_sparse_panel(df: pd.DataFrame, threshold: float = SPARSE_NONZERO_RATIO) -> bool:
    return bool(sparse_ratio(df) < threshold)


def guard_sparse(name: str, df: pd.DataFrame, threshold: float = SPARSE_NONZERO_RATIO,
                 strict: bool = False) -> dict:
    """B6 类稀疏因子进入**任何新计算路径**前的默认门。

    F35 的教训是"IC 静默消失"而不是"报错" —— 557 条增发事件一条 IC 都算不出，
    表面上一片安静。所以这个门宁可吵：`strict=True` 时直接抛（新路径应当这样接），
    `strict=False` 时打 error 日志并把判定结果回给调用方自己决定。

    ⚠️ 本函数**不改变**任何因子值（那会换 pin）。要"走 sparse 口径"由调用方显式改调
    `neutralize_sparse`，且必须走 A/B + 预注册。
    """
    r = sparse_ratio(df)
    out = {"factor": str(name), "nonzero_ratio": round(float(r), 6), "threshold": threshold,
           "sparse": bool(r < threshold), "non_null_cells": int(df.notna().to_numpy().sum()),
           "advice": ""}
    if out["sparse"]:
        out["advice"] = ("非零占比 %.4f%% < %.0f%% ⇒ `neutralize` 会把整行归零(F35)；"
                         "请改走 `factor_engine.neutralize_sparse`（不 winsorize）"
                         % (100 * r, 100 * threshold))
        if strict:
            raise ValueError(out["advice"])
        log.error("稀疏面板守卫: 因子 %s %s", name, out["advice"])
    return out


def neutralize_sparse(df: pd.DataFrame, industry: pd.Series) -> pd.DataFrame:
    """**稀疏面板版**中性化：行业去均值 + z-score，**不做 winsorize**。

    与 `experiments/05_label/F35_event3_weekly_ic.py::neutralize_sparse` 同口径
    （生产不 import experiments，故在此保留一份同义实现；改任一边都要同步另一边）。
    它**不替换** `neutralize`：现有生产因子值由 `neutralize` 产出，换成这里会改存量
    因子值 ⇒ 换 pin，必须 A/B + 预注册后单独做。
    """
    df = df.replace([np.inf, -np.inf], np.nan)
    inds = industry.reindex(df.columns)
    df = df - df.T.groupby(inds, sort=False).transform("mean").T
    df = df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1), axis=0)
    return df.replace([np.inf, -np.inf], np.nan)


# ---------- 中性化 ----------
def neutralize(df: pd.DataFrame, industry: pd.Series, q: float = 0.02) -> pd.DataFrame:
    """截面(行=日期, 列=股票): 去极值 → 行业中性化 → z-score

    ⚠️ **稀疏面板警告（2026-09-13 实测）**：本函数先按行做 `2%/98%` 分位 winsorize ——
    对**非零占比 < ~2% 的事件面板**（事件哑变量、稀疏比率）该行的 98% 分位正好是 0
    ⇒ `clip` 把**整行归零** ⇒ z-score 变全 NaN ⇒ 下游 IC **静默消失**（F35 首跑踩到：
    2024+ 有 557 条增发事件却一条 IC 都算不出）。**稀疏事件面板请用专用实现**
    （见 `experiments/05_label/F35_event3_weekly_ic.py::neutralize_sparse`：行业去均值 + z-score，
    不 winsorize），不要直接拿本函数处理。

    ⚠️ **"float32 下对面板宽度不变"是假的**(2026-09-11 实测): 输入 float32 时,
    **多出若干列**(哪怕那些列**全场 NaN**)会改变
    `df.T.groupby(inds).transform("mean")` 这条累加路径的分块方式 ⇒ **每个截面平移
    1~2 个 ulp**(实测 12027/241772 格)。
    后果: **换一个面板窗口重算同一因子, 与原值不逐位相同**, 且与数据无关 ——
    排查时会误以为是口径或数据变了。
    规避: 在调本函数**之前**把列集对齐到参照那次跑的列集, 重叠月份即逐位相同
    (完整诊断见 `scripts/oneoff/exp_stage_b_flow_die.py` docstring)。本函数行为未改。

    ⚠️⚠️ **"行数增减无影响"同样是假的**(2026-09-12 审查 P2-21 时实测, 此处更正旧结论):
    保持列集完全不变, 只把行数从 1 行改成 6 行, 同一个截面就会平移约 1e-8:
      · 3000 列纯随机 float32 面板 → **6/6 行**都不同
      · 面板里只要有**一个 NaN 格**或**一个 inf 格** → 5/6 行不同
      · 6 列小面板 → 0/6(旧结论"600 行 vs 300 行 → 0 不同"就是这么来的: 面板太窄/太干净)
    机制是**精度漂移**: `clip` 对多行帧会上转 float64、对单行帧保持 float32,
    两条路径此后的中间运算精度不同 ⇒ 末位不同。
    生产面板(301 行 × 5626 列, 25 个量价/龙虎榜因子)当前两法**确实**逐位相同 ——
    所以 `factor` 表里的存量值不用担心; 但那是数据形状碰巧, **不是不变量**。
    实践含义: 想靠"整块算"省掉逐日调用(如 daily_engine 的 30×37 次)是**不安全**的;
    要逐位一致就只能逐日调, 或先显式把输入统一到同一 dtype 再比较。
    """
    df = df.replace([np.inf, -np.inf], np.nan)
    # 2026-09-22（D 案 ③-4）：F35 的教训做成**默认门** —— 稀疏面板一旦进来就先喊出来。
    # ⚠️ **只记录，不改行为**：改成分走 sparse 口径会改变存量因子值 ⇒ 那是换 pin 级动作,
    # 必须 A/B + 预注册, 不许在监控接线里顺手做。新路径请显式调 `guard_sparse(strict=True)`。
    try:
        _sr = sparse_ratio(df)
        if _sr < SPARSE_NONZERO_RATIO:
            log.error("稀疏面板守卫: 非零占比 %.4f%% < %.0f%% ⇒ 本次 winsorize 会把整行归零(F35 "
                      "教训: 事件面板 IC 静默消失); 面板 %s, 请改走 neutralize_sparse",
                      100 * _sr, 100 * SPARSE_NONZERO_RATIO, getattr(df, "shape", "?"))
    except Exception:                                    # 守卫不许把生产链带崩
        log.debug("稀疏面板守卫跳过", exc_info=True)
    lo = df.quantile(q, axis=1)
    hi = df.quantile(1 - q, axis=1)
    df = df.clip(lower=lo, upper=hi, axis=0)
    inds = industry.reindex(df.columns)
    # pandas 3.0 移除了 groupby(axis=1), 转置后按行业分组再转回
    df = df - df.T.groupby(inds, sort=False).transform("mean").T
    df = df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1), axis=0)
    df = df.replace([np.inf, -np.inf], np.nan)  # 截面常数时 z-score 除零保护
    return df


# ---------- 存储 ----------
def neutralize_pit(df: pd.DataFrame, ind_pit: pd.DataFrame, q: float = 0.02) -> pd.DataFrame:
    """**时点(PIT)行业**版中性化: 去极值(2%) → **逐期**行业去均值 → z-score。

    与 `neutralize` 的唯一差别: 行业标签按日期取(`ind_pit.loc[d]`), 而不是一个固定 Series。
    三步的顺序/分位/除零保护全部与 `neutralize` 逐步一致 —— 否则就不是"唯一变量"的实验了。

    为什么需要它(2026-09-13 实测, F21/F22):

      · 原口径 `industry_map()` 读 `stock_basic.industry` = **当前快照** ⇒ 2021 年改过行业的股票,
        全历史都拿 2026 年的行业去做减法(前视);
      · 换成**申万一级 PIT 时点行业**(`sw_member` 的 in_date/out_date)后, 作为独立 die 接入周频袖套:
        与 W5 按 **2:1** 融合, 同窗口 491 期、两套独立种子分别读到
        **超额 +14.91 / +14.30pp**(旧现役 W5+M 是 +12.02 / +11.52pp)、
        MDD −32.3% / −32.2%(持平)、打分 IC +0.0773(旧 +0.0706) ⇒ **+2.8~2.9pp, 两套种子一致**。

    ⚠️ 无申万归属的股票(次新/退市/北交)在 `ind_pit` 里是 NaN: `groupby` 会丢掉 NaN 键 ⇒
    这些格子**整格变 NaN**(= 从该因子里剔除, 由 LightGBM 原生处理缺失), **不是**保留原值。
    这是有意选择: 把无归属股票填成"未知"再去均值, 等于拿一组互不相干的股票当同业做减法。
    本函数的 NaN 行为与实验用的那份实现(`scripts/oneoff/exp_die_pit.py::neutralize_pit`)逐步一致,
    所以 `model_score_die_pit` 的读数可以与本函数直接对照。
    """
    df = df.replace([np.inf, -np.inf], np.nan)
    lo = df.quantile(q, axis=1)
    hi = df.quantile(1 - q, axis=1)
    df = df.clip(lower=lo, upper=hi, axis=0)
    out = df.copy()
    for d in df.index:
        if d not in ind_pit.index:
            continue
        g = ind_pit.loc[d].reindex(df.columns)
        row = df.loc[d]
        out.loc[d] = row - row.groupby(g, sort=False).transform("mean")
    out = out.sub(out.mean(axis=1), axis=0).div(out.std(axis=1), axis=0)
    return out.replace([np.inf, -np.inf], np.nan)


def store_factors(factors: dict, month_dates: pd.DatetimeIndex) -> int:
    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        n = 0
        for name, df in factors.items():
            sub = df.loc[month_dates]
            if sub.empty:
                continue
            long = sub.stack().rename("value").reset_index()
            long.columns = ["trade_date", "ts_code", "value"]
            long["trade_date"] = long["trade_date"].dt.strftime("%Y%m%d")
            long["factor"] = name
            long = long[["ts_code", "trade_date", "factor", "value"]].dropna(subset=["value"])
            n += db.upsert_df(conn, long, "factor")
        db.set_status(conn, "factor", month_dates[-1].strftime("%Y%m%d"), n)
    return n


# ---------- 入口 ----------
def compute_all(only: list = None, fina_only: bool = False,
                include_new: bool = False) -> int:
    """only: 只重算指定因子(增量修复用); fina_only: 只重算财务因子; 默认全量

    include_new: 是否并入 2026-09-11 新增的资金类/股东类因子(阶段 0b)。
    **默认 False —— 这是不变量而非疏漏**: 新因子一旦写进 `factor` 表, 增量重训
    (`trainer._load_factors_wide` 是整表加载)会读到它们并**静默改变生产分数**,
    而方案 §3 不变量 #7 要求阶段 0~3 完全不碰实盘链路。故月度日链照旧调用
    `compute_all()`, 行为一字不变; 阶段 2 建 die 时显式传 True。
    ⚠️ 另: 该开关不涉及行业映射 —— `industry_map` 仍读 `stock_basic.industry`
    当前快照(已知缺陷, 见 holder_factors 模块 docstring), 换成 PIT 会改变**全部
    存量因子值**, 那是需要配套全量重训与新基线的独立动作, 不在本开关内。
    """
    if fina_only:
        only = list(FINA_FIELDS) + ["eps_yoy"]
    log.info("== 因子计算开始 (%s%s) ==", ",".join(only) if only else "全量",
             ", +新因子" if include_new else "")
    m = load_market()
    dates = pd.to_datetime(m["close"].index)
    if len(dates) == 0:
        # 2026-09-05 审查修复: 空表时 md[0] IndexError 崩断月度链(daily_engine 已有同款守卫)
        log.warning("行情数据为空, 跳过因子计算(先初始化数据)")
        return 0
    # 2026-09-28 收编 timeutil(东八区): 裸 date.today() 换机部署时"最新已完结月末"
    # 判定会差一个月; 本机逐位无影响
    md = month_end_dates(dates, today=timeutil.today())
    md = md[md >= pd.Timestamp("2014-01-01")]
    log.info("月度截面: %d 个 (%s ~ %s)", len(md), md[0].date(), md[-1].date())

    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        ind = industry_map(conn)

    price_f = compute_price_factors(m)
    for name in price_f:
        price_f[name].index = dates
    fina_f = compute_fina_factors(md.strftime("%Y%m%d").tolist(),
                                  calendar=dates.strftime("%Y%m%d"))
    for name in fina_f:
        fina_f[name].index = pd.to_datetime(fina_f[name].index)

    lhb_f = compute_lhb_factors(dates)

    factors = {**price_f, **fina_f, **lhb_f}
    if include_new:
        from app.factors.flow_factors import compute_flow_factors
        from app.factors.holder_factors import compute_holder_factors_safe
        # include_research=False: hk_* 是研究专用(2024-08 覆盖度断崖), 不进生产因子表
        factors.update(compute_flow_factors(m, dates, include_research=False))
        factors.update(compute_holder_factors_safe(dates))
    if only:
        factors = {k: v for k, v in factors.items() if k in only}
    for name, df in factors.items():
        factors[name] = neutralize(df, ind)
    log.info("因子列表(%d): %s", len(factors), sorted(factors))

    n = store_factors(factors, md)
    log.info("== 因子计算完成: %d 行 ==", n)
    return n
