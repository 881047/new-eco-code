"""数据更新: 全量初始化 + 每日增量 + 开机补跑(同一套缺失日期逻辑, 可中断续跑)"""
import datetime
import logging
from typing import Optional

import pandas as pd

from app import db
from app import timeutil
from app.datasource.tushare_client import TushareClient

log = logging.getLogger(__name__)

START_DATE = "20130101"

# trade_cal 保鲜窗(P2-9 语义不变, 2026-10-01 收紧 30→7): 日历已覆盖未来且最近 N 天内
# 刷新过才跳过重拉。收紧理由 = **临时休市**(悼念日等交易所公告后才在 tushare trade_cal
# 翻牌)的传播延迟从最坏 30 天压到 7 天; 代价 ≈ 每月多 3 次 trade_cal 调用(单次全量拉取,
# 1 点/次, 且有限流耗尽跳过保护不炸断日链)。历史修订同样走这条(公告改休/调休)。
CALENDAR_REFRESH_DAYS = 7

STOCK_DAILY_COLS = ["ts_code", "trade_date", "open", "high", "low", "close",
                    "pre_close", "change", "pct_chg", "vol", "amount"]
BASIC_COLS = ["ts_code", "trade_date", "turnover_rate", "turnover_rate_f", "volume_ratio",
              "pe", "pe_ttm", "pb", "dv_ratio", "dv_ttm", "total_mv", "circ_mv"]
FUND_DAILY_COLS = ["ts_code", "trade_date", "open", "high", "low", "close",
                   "pre_close", "pct_chg", "vol", "amount"]
FINA_COLS = "ts_code,ann_date,end_date,roe,netprofit_yoy,or_yoy,grossprofit_margin,debt_to_assets,eps"
INDEX_CODES = ["000906.SH", "000300.SH", "000905.SH", "000001.SH", "399001.SZ",
               # 2026-09-19 审查 B1: 中证800 **全收益指数**(税前股息再投资)。回测基准腿原先取
               # `000906.SH` 价格指数, 而持仓腿取 `close×adj_factor`(全收益) ⇒ 两条腿不同尺,
               # `excess_ann` 被抬高 ~2.2pp/年(三补二十七)。接进来是为了让两腿同尺的 A/B 有真值可用
               # —— **本轮只接入, 不改基准腿**(改基准 = 换 pin, 待用户拍板)。
               # N00906.CSI(净收益, 扣红利税)刻意不接: 与 adj_factor 的税前口径不同尺。
               "H00906.CSI"]
NAME_CHANGE_BULK_DAYS = 60      # 每日增量用**批量**接口回看的自然日窗口（见 init_namechange）


class DataUpdater:
    def __init__(self):
        self.ts = TushareClient()
        self._uni: Optional[set] = None

    # ---------- 股票池 ----------
    def universe_codes(self) -> set:
        if self._uni is None:
            with db.conn_ctx() as conn:
                # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
                self._uni = set(pd.read_sql("SELECT ts_code FROM stock_basic", conn)["ts_code"])
        return self._uni

    def init_trade_cal(self) -> int:
        # 日历已覆盖未来且最近30天内刷新过才跳过: 兼顾省接口与节假日修订生效 (P2-9)
        today = timeutil.today().strftime("%Y%m%d")
        with db.conn_ctx() as conn:
            # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
            if db.table_exists(conn, "trade_cal"):
                mx = conn.execute("SELECT MAX(cal_date) FROM trade_cal").fetchone()[0]
                upd = conn.execute(
                    "SELECT updated_at FROM data_status WHERE name='trade_cal'").fetchone()
                if mx and mx > today and upd and upd[0]:
                    try:
                        refreshed = datetime.datetime.strptime(upd[0], "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        refreshed = None
                    if refreshed and (timeutil.now() - refreshed).days < CALENDAR_REFRESH_DAYS:
                        # 2026-09-19 审查: 原先这里再 close() 一次 —— 连接是外层
                        # `with db.conn_ctx()` 绑的, 离开块时它必定关。重复关本身无害,
                        # 但留着等于"谁在这行下面加语句谁就炸"(抛 ProgrammingError)。
                        log.info("交易日历已是最新(%s, %s刷新), 跳过重拉", mx, upd[0])
                        return 0
        try:
            df = self.ts.call("trade_cal", exchange="SSE", start_date=START_DATE, end_date="20991231")
        except RuntimeError as e:
            # 2026-09-08 审查 P1: 限流重试耗尽 raise RuntimeError —— 与并发脚本抢配额时
            # 原炸断整条 18:00 链(regime/推分/模拟盘全跳过), 改跳过本次(水位不推进, 下次续跑)
            log.error("交易日历拉取失败(限流?): %s, 跳过本次(下次续跑)", e)
            return 0
        # 2026-09-05 审查修复: call() 对异常/权限/空表返回空 DataFrame, 直接取列会 KeyError
        if df is None or df.empty:
            log.error("交易日历返回空表(接口异常/权限不足), 跳过本次(下次续跑)")
            return 0
        with db.conn_ctx() as conn:
            # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
            n = db.upsert_df(conn, df[["cal_date", "is_open"]], "trade_cal")
            db.set_status(conn, "trade_cal", str(df["cal_date"].max()), n)
        log.info("交易日历: %d 条", n)
        return n

    def init_stock_basic(self) -> int:
        parts = []
        try:
            for status in ("L", "D", "P"):
                df = self.ts.call(
                    "stock_basic", list_status=status,
                    fields="ts_code,symbol,name,area,industry,market,list_date,delist_date",
                )
                if df is not None and not df.empty:
                    parts.append(df)
        except RuntimeError as e:
            # 2026-09-08 审查 P1: 同 init_trade_cal —— 限流耗尽不炸断整条日链,
            # 已有部分状态的批次照常入库(缺的状态下次续跑)
            log.error("stock_basic 拉取失败(限流?): %s, 已得 %d 批", e, len(parts))
            if not parts:
                return 0
        # 2026-09-05 审查修复: 三个状态全空时 pd.concat([]) 抛 ValueError 中断链路
        if not parts:
            log.error("stock_basic 返回空表(接口异常/权限不足), 跳过本次(下次续跑)")
            return 0
        all_df = pd.concat(parts, ignore_index=True).drop_duplicates("ts_code")
        # 2026-09-07 全市场扩展(用户拍板): 原仅沪主板 60xxxx+深主板 00xxxx,
        # 现纳入创业板(30)/科创板(688)/北交所(4/8/9), 退市股(L/D/P 全状态)照旧保留
        # —— PIT 口径不变: 退市股历史在退市前正常参与训练/回测
        with db.conn_ctx() as conn:
            # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
            n = db.upsert_df(conn, all_df, "stock_basic")
            db.set_status(conn, "stock_basic", "", n)
        self._uni = set(all_df["ts_code"])
        log.info("全市场股票池(含已退市/暂停, 含北交所): %d 只", n)
        return n

    # ---------- 通用逐日同步 ----------
    def sync_daily_bulk(self, table: str, api_name: str, date_col: str = "trade_date",
                        cols=None, uni_filter: Optional[set] = None,
                        extra_params: Optional[dict] = None,
                        end_date: Optional[str] = None,
                        start_date: Optional[str] = None,
                        transform=None) -> int:
        """按交易日逐日同步, 只拉缺失日期 → 增量与补跑走同一条路

        start_date: 日历下界, 默认 START_DATE。用于「先拉一段窗口拿结论、日后再补全历史」
                    (2026-09-11 阶段 0a 死区体检需要 2020-09 起的窗口, 而非 2013 起全量)。
        transform:  可选 df -> df 的清洗钩子, 在 uni_filter/cols 之前施加。
                    ⚠️ 与 cols 的先后: transform 先跑, cols 再从**其输出**上裁列 ——
                    所以 aggregation 类接口(如 block_trade 一笔一行 → 按股聚合)应让
                    transform 产出最终列, cols 传 None。
        """
        # 今天取自 timeutil.now() 而非裸 date.today(): 与下方"17点后"判定同一时钟(且可注入假钟)(也便于测试注入假时钟)
        today = timeutil.now().strftime("%Y%m%d")
        end_date = end_date or today
        with db.conn_ctx() as conn:
            # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
            cal = pd.read_sql(
                "SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date>=? AND cal_date<=? ORDER BY cal_date",
                conn, params=(start_date or START_DATE, end_date),
            )["cal_date"].tolist()
            have = set()
            if db.table_exists(conn, table):
                have = set(pd.read_sql(f"SELECT DISTINCT {date_col} AS d FROM {table}", conn)["d"])
        missing = [d for d in cal if d not in have]
        # 盘后(17点后)强制重拉最后两个交易日: 18:00 上游可能仍在发布(当日残缺批
        # 会被标 done), 次日重拉前一交易日即可补全残缺行, 防残缺EOD永久滞留 (2026-09-04)。
        # 2026-09-05 审查修复: 原用 cal[-2]+today —— 工作日节假日(调度器照常运行)时
        # today 不在 cal 中, 重拉窗口错位跳过最后一个真实交易日, 还向非交易日发出
        # 必空请求污染空结果计数。改为取日历末尾两个真实交易日。
        if end_date >= today and timeutil.now().hour >= 17:
            # 2026-09-16 数据线审查 DATA-04①: 重拉窗 2→4 个交易日 —— 「D 日残缺半批
            #(部分行已入表 ⇒ 不在 missing 里)+ D+1 停跑」时, D 从 D+2 起永久滞留;
            # 4 日窗覆盖之, 成本 = 每表每天 +2 次调用。
            for d in cal[-4:]:
                if d and d not in missing:
                    missing.append(d)
        # 追加后必须重排序: 前一日追加在末尾会逆序处理, data_status 水位退回前一天
        missing = sorted(missing)
        # 2026-09-17 每日拉取审计: 缺失日分两类 —— 水位后的正常缺失必拉; 水位内但
        # 早于近 40 个开市日的**历史洞**只重探近窗(上游确认无数据的日子——如港股假期——
        # 每天重探纯属烧配额, 且会让下方 fail_streak 在散洞表上永久熔断)。更老的洞如需
        # 回填, 调用方显式传 start_date 即可(手动回填路径不受此窗限制)。
        if start_date is None and len(cal) >= 40:
            cal40 = cal[-40]
            _old_holes = [d for d in missing if d < cal40]
            if _old_holes:
                log.warning("%s: %d 个历史缺失日早于近 40 开市日窗口, 本轮不重探"
                            "(如需回填请显式传 start_date): 最早 %s", table, len(_old_holes), _old_holes[0])
                missing = [d for d in missing if d >= cal40]
        log.info("%s: 缺 %d 个交易日", table, len(missing))
        # 连接复用(2026-09-04 审查 P1-5): 原先每完成一天开关一次连接+写一次状态,
        # 全量初始化近万次连接开关; 现在单连接跑完, 状态只在结束时写一次(水位=最后成功日)
        conn = db.get_conn()
        done = 0
        last_d = None
        fail_streak = 0
        cal_pos = {d: i for i, d in enumerate(cal)}
        prev_missing = None
        try:
            for i, d in enumerate(missing):
                try:
                    df = self.ts.call(api_name, **{date_col: d, **(extra_params or {})})
                    status = self.ts.last_status
                except Exception as e:
                    log.error("%s %s 失败: %s, 跳过(下次续跑)", table, d, e)
                    status = "error"
                    df = None
                # P1-10: 异常/空表/权限三类分开 —— 只有"确定为空"才计入 streak,
                # 网络抖动与"该日期确实无数据"不再被同等对待(原连续 10 次即终止整段)
                if status == "perm":
                    log.error("%s 接口权限不足, 终止本轮同步(数据停留在 %s)", table, d)
                    break
                prev_missing = d if df is None else None
                if status == "error" or df is None:
                    continue  # 瞬时异常: 不计 streak, 下次续跑重试
                if df.empty:
                    # 2026-09-17 每日拉取审查: streak 只数**日历相邻**的连续空日 ——
                    # 原实现对「只遍历缺失日」的循环累计隔日洞, 散洞表(hk_hold 71 个
                    # 港股假期洞)会在第 10 个洞处永久熔断, 永远走不到当前日期。
                    _adjacent = (prev_missing is not None
                                 and cal_pos.get(d, -10) - cal_pos.get(prev_missing, -10) == 1)
                    fail_streak = fail_streak + 1 if _adjacent else 1
                    prev_missing = d
                    if fail_streak >= 10:
                        log.error("%s 连续 10 天确定无数据, 终止本轮(下次续跑)", table)
                        break
                    continue
                fail_streak = 0
                # transform 先于 uni_filter/cols: 聚合类接口在此把「一笔一行」收敛成
                # 「一股一行」(2026-09-11 阶段 0a)。抛异常 = 该日数据有问题, 按瞬时错误
                # 处理(不计 streak, 次日强制重拉窗自愈), 不炸断整段同步。
                if transform is not None:
                    try:
                        df = transform(df)
                    except Exception as e:
                        log.error("%s %s transform 失败: %s, 跳过该日(次日重拉)", table, d, e)
                        continue
                    if df is None or df.empty:
                        continue
                if uni_filter is not None:
                    df = df[df["ts_code"].isin(uni_filter)]
                # 2026-09-08 审查 P2: 上游字段漂移(裁列)时 df[cols] KeyError 会炸断日链
                # —— 缺列跳过该日(次日强制重拉窗自愈), 不中断其余日期
                if cols:
                    miss = [c for c in cols if c not in df.columns]
                    if miss:
                        log.warning("%s %s 缺列 %s(上游字段变更?), 跳过该日(次日重拉)",
                                    table, d, miss)
                        continue
                n = db.upsert_df(conn, df[cols] if cols else df, table)
                # 数据质量校验(2026-09-04 审查 P3-8): 全市场日线正常 5000+ 行(2013 年
                # 约 1800), 阈值 1000 仅拦截严重残缺批, 上游残缺批静默入库时提前暴露
                if table in ("daily", "daily_basic", "adj_factor") and n < 1000:
                    log.warning("%s %s 仅 %d 行(正常 5000+), 疑似残缺批, 次日强制重拉会自愈", table, d, n)
                last_d = d
                done += 1
                if (i + 1) % 100 == 0:
                    log.info("%s: %d/%d (%s)", table, i + 1, len(missing), d)
            if last_d is not None:
                db.set_status(conn, table, last_d, done)
        finally:
            conn.close()
        log.info("%s 同步结束, 本次 %d 天", table, done)
        return done

    def sync_stock_market(self, end_date: Optional[str] = None) -> None:
        uni = self.universe_codes()
        self.sync_daily_bulk("daily", "daily", cols=STOCK_DAILY_COLS, uni_filter=uni, end_date=end_date)
        self.sync_daily_bulk("adj_factor", "adj_factor",
                             cols=["ts_code", "trade_date", "adj_factor"], uni_filter=uni, end_date=end_date)
        self.sync_daily_bulk("daily_basic", "daily_basic", cols=BASIC_COLS, uni_filter=uni, end_date=end_date)
        # stk_limit 涨跌停价(2026-10-05 接线): 此前只有 oneoff/pull_stk_limit.py 在
        # 2026-09-12 一次性回填过(水位停在 0911), 日链不含它 ⇒ 表静默断更 13 个交易日,
        # 下游(回测引擎 D06/D07 / fterm 面板撮合)无感降级。口径与 oneoff 一致:
        # 不裁列、不按股票池过滤(全市场), 权限错误由 sync_daily_bulk 终止本轮并留日志。
        self.sync_daily_bulk("stk_limit", "stk_limit", end_date=end_date)

    # ---------- 指数 ----------
    def init_index(self, end_date: Optional[str] = None) -> None:
        # 增量拉取: 缺的天数少按日拉, 缺太多整段拉(与 sync_daily_bulk 的增量语义对齐)
        # 缺口按 (指数, 日期) 粒度跟踪(2026-09-04 修复): 原先 have 是跨全部指数的日期并集,
        # 单只指数某日瞬时失败后该 (code, date) 永不再补
        # 2026-09-08 审查 P2: end 缺省原为 20991231 —— 把日历里全部未来交易日算作缺失,
        # 无参调用(init_data.py --daily)每次对 5 只指数整段重拉全历史; 缺省改今天
        end = end_date or timeutil.now().strftime("%Y%m%d")
        today = timeutil.now().strftime("%Y%m%d")  # 与 now() 同时钟(见 sync_daily_bulk)
        with db.conn_ctx() as conn:
            # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
            cal = pd.read_sql(
                "SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date>=? AND cal_date<=? ORDER BY cal_date",
                conn, params=(START_DATE, end))["cal_date"].tolist()
            have = {}
            if db.table_exists(conn, "index_daily"):
                df = pd.read_sql("SELECT ts_code, trade_date FROM index_daily", conn)
                for code, g in df.groupby("ts_code"):
                    have[code] = set(g["trade_date"])
            per_code_missing = {}
            for code in INDEX_CODES:
                ms = [d for d in cal if d not in have.get(code, set())]
                # 盘后(17点后)强制重拉最后两个真实交易日(2026-09-05 第七轮 N7-1):
                # 原 append(today) 自然日 —— 周六补跑时 today 非交易日, 对每个指数发必空
                # 请求; 与 sync_daily_bulk 同规则取日历末尾真实交易日
                if end >= today and timeutil.now().hour >= 17:
                    for d in cal[-2:]:
                        if d and d not in ms:
                            ms.append(d)
                ms = sorted(ms)
                per_code_missing[code] = ms
            n = 0
            cols = ["ts_code", "trade_date", "open", "high", "low", "close",
                    "pre_close", "pct_chg", "vol", "amount"]
            if len(set().union(*per_code_missing.values())) > 10:
                for code in INDEX_CODES:
                    # 2026-09-06 第十轮 N10-7: 与逐日分支对齐 —— 单指数异常不再中断整段拉取
                    try:
                        df = self.ts.call("index_daily", ts_code=code, start_date=START_DATE, end_date=end)
                    except Exception as e:
                        log.error("指数 %s 整段拉取失败: %s, 跳过(下次续跑)", code, e)
                        continue
                    # 2026-09-05 审查修复: 空表(瞬时异常/权限)取列 KeyError 会中断每日链
                    if df is None or df.empty:
                        log.warning("指数 %s 整段返回空表, 跳过(下次续跑)", code)
                        continue
                    # 2026-09-16 数据线审查 DATA-06: 上游字段漂移(缺列)原来 KeyError 直接
                    # 炸出 init_index ⇒ 当日整链中断; 与 sync_daily_bulk 的缺列检查同款。
                    miss = [c for c in cols if c not in df.columns]
                    if miss:
                        log.error("指数 %s 返回缺列 %s, 跳过(下次续跑)", code, miss)
                        continue
                    n += db.upsert_df(conn, df[cols], "index_daily")
            else:
                for code in INDEX_CODES:
                    for d in per_code_missing[code]:
                        try:
                            df = self.ts.call("index_daily", ts_code=code, trade_date=d)
                        except Exception as e:
                            log.error("指数 %s %s 失败: %s, 跳过(下次续跑)", code, d, e)
                            continue
                        if df is None or df.empty:
                            log.warning("指数 %s %s 返回空表, 跳过(下次续跑)", code, d)
                            continue
                        miss = [c for c in cols if c not in df.columns]   # DATA-06 同款防护
                        if miss:
                            log.error("指数 %s %s 返回缺列 %s, 跳过(下次续跑)", code, d, miss)
                            continue
                        n += db.upsert_df(conn, df[cols], "index_daily")
            # 2026-09-06 第十七轮 R17-4: 登记实际最大日期(原恒空串, 排障页误判指数缺失)
            mx = conn.execute("SELECT MAX(trade_date) FROM index_daily").fetchone()[0]
            db.set_status(conn, "index_daily", mx or "", n)
        log.info("指数行情: 缺 %d 天(按指数粒度), 本次 %d 条",
                 len(set().union(*per_code_missing.values())), n)

    # ---------- 基金 ----------
    def init_fund_basic(self) -> None:
        parts = []
        for mkt in ("E", "O"):
            df = self.ts.call(
                "fund_basic", market=mkt,
                fields="ts_code,name,management,fund_type,market,invest_type,list_date,delist_date,benchmark,m_fee,c_fee",
            )
            if df is not None and not df.empty:
                parts.append(df)
        # 2026-09-05 审查修复: 两市场全空时 pd.concat([]) ValueError 崩断 init_all
        if not parts:
            log.error("fund_basic 返回空表(接口异常/权限不足), 跳过本次(下次续跑)")
            return
        all_df = pd.concat(parts, ignore_index=True)
        with db.conn_ctx() as conn:
            # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
            n = db.upsert_df(conn, all_df, "fund_basic")
            db.set_status(conn, "fund_basic", "", n)
        log.info("基金列表(场内+场外): %d 只", n)

    def sync_fund_market(self, end_date: Optional[str] = None) -> None:
        self.sync_daily_bulk("fund_daily", "fund_daily", cols=FUND_DAILY_COLS, end_date=end_date)
        self.sync_daily_bulk("fund_adj", "fund_adj",
                             cols=["ts_code", "trade_date", "adj_factor"], end_date=end_date)

    # ---------- 财务 ----------
    def init_fina(self, recent_only: bool = False, refetch_recent: bool = False) -> None:
        """财务指标同步 —— 按 ts_code 逐只拉取(2026-09-05 第三轮复查 D4):
        免费档权限不支持 period 全市场拉取("必填参数 ts_code"), 而按股查询可用且一次
        返回该股全部报告期。recent_only=True(每日增量)只拉"缺最新报告期"的股票 +
        新上市股票, 全量初始化(init_all)拉全部股票池(2026-09-07 起含创业/科创/北交)。
        refetch_recent=True(2026-09-16 数据线审查 DATA-03): **更正公告重拉模式** ——
        近 60 天有披露(ann_date)的股票无条件重拉(tushare 按 ts_code 返回全部期次的
        最新值, 更正公告覆盖旧值; 现行增量集对 MAX(end_date)==latest_ok 的股票永不
        重拉 ⇒ 更正数据永久丢失)。全市场 ~5700 只 ≈ 30-40 分钟配额, **只作低频手动
        /报告季收尾使用, 绝不进日更**; 回填前向生效(已存 model_score 不动, pin 无虞)。"""
        # 2026-09-07 全面检查: available() 探测走 call(), 限流重试耗尽会 raise RuntimeError
        # 直接炸断每日链 —— 探测失败是瞬时性, 不应触发 40 分钟 AkShare 兜底, 按可用继续
        # (逐只拉取有失败计数保护)
        try:
            fina_ok = self.ts.available("fina_indicator", ts_code="000001.SZ", fields=FINA_COLS)
        except Exception as e:
            log.warning("fina_indicator 可用性探测异常(瞬时): %s, 按可用继续", e)
            fina_ok = True
        if not fina_ok and getattr(self.ts, "last_status", "") != "perm":
            # 2026-09-08 审查 P1: 探测遇瞬时网络错误时 call() 返回空表且 last_status=
            # "error"(不抛异常), available() 误判不可用 → 错走 40 分钟 AkShare 兜底且
            # ann_date 永久变估计值(增量口径不再重拉)。只有权限类("perm")才降级,
            # 其余按可用继续(逐只拉取有失败计数保护)
            log.warning("fina_indicator 探测未确认权限问题(last_status=%s), 按可用继续",
                        getattr(self.ts, "last_status", ""))
            fina_ok = True
        if not fina_ok:
            log.warning("fina_indicator 不可用(积分不足), 改用 AkShare 逐只拉取财务指标(约40分钟)")
            self.init_fina_fallback()
            with db.conn_ctx() as conn:
                # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
                n_actual = conn.execute("SELECT COUNT(*) FROM fina_indicator").fetchone()[0]
                db.set_status(conn, "fina_indicator", "", n_actual)
            return
        uni = self.universe_codes()
        with db.conn_ctx() as conn:
            # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
            if recent_only:
                # 增量: 缺最新已披露报告期的股票 + 最近上市的新股
                latest_ok = conn.execute(
                    "SELECT MAX(end_date) FROM fina_indicator").fetchone()[0]
                if not latest_ok:
                    recent_only = False  # 空表: 走全量
                # 第七轮复查 N7-2: 原 else 分支在此处预算 codes 随即被下方增量口径覆盖重算
                # (死代码), 已删除 —— 增量集合统一在 recent_only=True 时按"500 日地板内
                # 落后股票 + 库内无记录新股"计算
        if not recent_only:
            codes = set(uni)
        else:
            # 增量口径: 缺最近报告期的股票 + 新上市(库内无记录)
            with db.conn_ctx() as conn2:
                # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
                have = pd.read_sql(
                    "SELECT ts_code, MAX(end_date) AS mx FROM fina_indicator GROUP BY ts_code",
                    conn2).set_index("ts_code")["mx"]
            floor = (pd.Timestamp(latest_ok) - pd.Timedelta(days=500)).strftime("%Y%m%d")
            stale = set(have[(have < latest_ok) & (have >= floor)].index)
            # 2026-09-09 修复(审查项 ⑥ 复牌股): 500 日地板只看「最新报告期」会自锁 ——
            # 长期停牌后复牌/恢复披露的股票, 其最新报告期落后 >500 日, 被排除在增量集合
            # 之外 → 新报告永不被拉取 → 永远停在落后状态。补「仍在交易」判据: 地板之外
            # 但近 20 个交易日仍有成交的股票仍纳入增量(退市股无成交, 保持排除, 不浪费配额;
            # 实测当前 157 只落后 >500 日的股票近 3 个月成交数为 0, 故本轮零额外调用)。
            try:
                conn3 = db.get_conn()
                try:
                    recent_days = pd.read_sql(
                        "SELECT DISTINCT trade_date FROM daily ORDER BY trade_date DESC LIMIT 20",
                        conn3)["trade_date"].tolist()
                    if recent_days:
                        q = ",".join("?" * len(recent_days))
                        alive = set(pd.read_sql(
                            f"SELECT DISTINCT ts_code FROM daily WHERE trade_date IN ({q})",
                            conn3, params=tuple(recent_days))["ts_code"])
                        resumed = set(have[(have < floor) & have.index.isin(alive)].index)
                        if resumed:
                            log.info("财务增量: 500 日地板外仍在交易的复牌股 %d 只, 纳入本轮",
                                     len(resumed))
                        stale |= resumed
                finally:
                    conn3.close()
            except Exception:
                # 活跃性判据不可用(新装机 daily 空表/表缺失) → 退回原口径, 不误伤主流程
                log.warning("财务增量: 活跃性判据不可用, 500 日地板外的复牌股本轮跳过")
            codes = stale | (set(uni) - set(have.index))
            if refetch_recent:
                # conn_ctx 块在上方已退出 ⇒ 必须新开连接(2026-09-16 修正: 原 ctx 闭包连接已关)
                with db.conn_ctx() as conn_r:
                    refetch = {r[0] for r in conn_r.execute(
                        "SELECT DISTINCT ts_code FROM fina_indicator WHERE ann_date >= ?",
                        ((pd.Timestamp(timeutil.today()) - pd.Timedelta(days=60)).strftime("%Y%m%d"),)).fetchall()}
                log.info("财务增量: refetch_recent 模式并入近 60 天有披露的股票 %d 只(更正公告重拉)", len(refetch))
                codes = codes | refetch
        total = 0
        ok_n = 0
        fail_n = 0
        with db.conn_ctx() as conn:
            # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
            from app.datasource.akshare_client import _est_ann_date
            for i, code in enumerate(sorted(codes)):
                try:
                    df = self.ts.call("fina_indicator", ts_code=code, fields=FINA_COLS)
                    st = self.ts.last_status
                except Exception as e:
                    # 2026-09-07 全面检查: call() 限流重试耗尽 raise RuntimeError, 单只异常
                    # 不应炸断整条每日链(后续 namechange/回购/龙虎榜全被跳过) —— 按失败计数
                    log.warning("财务 %s 调用异常: %s", code, e)
                    df, st = None, "error"
                if st == "ok" and df is not None and not df.empty:
                    fail_n = 0
                    df = df[df["ts_code"] == code]
                    # 2026-09-07 污染检查: 上游偶发 ann_date 早于报告期末(实测 603400.SH 半年报
                    # ann_date 写成年报披露日) —— 前视污染, 按法定披露截止日保守钳制
                    # (与 AkShare 兜底 _est_ann_date 同口径, 防未来函数方向安全)
                    bad = df["ann_date"].astype(str) < df["end_date"].astype(str)
                    if bool(bad.any()):
                        log.warning("财务 %s %d 行 ann_date 早于报告期末, 按法定截止日钳制",
                                    code, int(bad.sum()))
                        df.loc[bad, "ann_date"] = df.loc[bad, "end_date"].map(_est_ann_date)
                    total += db.upsert_df(conn, df, "fina_indicator")
                    ok_n += 1
                    if ok_n % 500 == 0:
                        log.info("财务按股拉取: %d/%d (%s)", ok_n, len(codes), code)
                elif st == "empty":
                    fail_n = 0  # 确定无数据(次新股): 正常, 不中断
                else:
                    fail_n += 1
                    if fail_n >= 20:
                        # 连续失败尽早中止(权限降级/网络故障), 水位不推进, 下次续跑
                        log.error("财务按股拉取连续 %d 只失败, 终止本轮(下次续跑)", fail_n)
                        break
            db.set_status(conn, "fina_indicator", "", total)
        log.info("财务指标: 本次 %d 只 / %d 条 (%s)", ok_n, total,
                 "增量" if recent_only else "全量")

    def init_fina_fallback(self) -> None:
        from app.datasource import akshare_client
        akshare_client.sync_fina_fallback(self.universe_codes())

    # ---------- 历史名称 ----------
    def init_namechange(self, recent_only: bool = False) -> int:
        """历史曾用名(2026-09-05 第三轮复查 D1 根治): 按股拉取改名记录, 供 trainer
        做时点 ST/退过滤(评分日用当时名称, 而非当前名称)。全量约 5900 次调用(2026-09-07
        全市场扩展);
        增量(recent_only)只拉库内无记录的新上市股票。

        ⚠️ 2026-09-13 补**批量增量**(先跑): 上面那条"只拉库内无记录的代码"有一个死角 ——
        **已有历史改名记录的老股再次改名/被 ST，逐股增量永远补不到**（它不在 `uni - have` 里）。
        实测 20260901~20260911: 批量接口返回 18 行, 而库里缺 **4 行**
        （300351.SZ 永贵智能 / 300562.SZ 乐心股份 / 301560.SZ 众捷股份 / 301339.SZ 苏交数智）
        —— 若其中任何一只变成 ST, PIT 名称链会仍按旧名判成"非 ST" ⇒ 可能买进 ST 股。
        批量接口一次调用覆盖全市场（成本可忽略），失败只告警、不影响后面的逐股增量。
        `namechange` 有 PRIMARY KEY(ts_code,start_date) ⇒ 重复写是幂等的。
        """
        uni = self.universe_codes()
        if recent_only:
            end = timeutil.today().strftime("%Y%m%d")
            # 2026-09-16 数据线审查 DATA-04②: 固定 60 日窗在停跑>60 天后窗外的改名/戴帽
            # 漏拉 ⇒ PIT 名称链按旧名判"非 ST"。窗口改为 max(60, 上次成功更新以来的天数)。
            with db.conn_ctx() as conn:
                _row = conn.execute(
                    "SELECT updated_at FROM data_status WHERE name='namechange'").fetchone()
            _bulk_days = NAME_CHANGE_BULK_DAYS
            if _row and _row[0]:
                try:
                    _last = datetime.datetime.strptime(_row[0][:10], "%Y-%m-%d")
                    _bulk_days = max(_bulk_days,
                                     (timeutil.today() - _last.date()).days + 2)
                except ValueError:
                    pass
            start = (timeutil.today()
                     - datetime.timedelta(days=_bulk_days)).strftime("%Y%m%d")
            try:
                dfb = self.ts.call("namechange", start_date=start, end_date=end,
                                   fields="ts_code,name,start_date,end_date,ann_date,change_reason")
                if dfb is not None and not dfb.empty:
                    with db.conn_ctx() as conn:
                        n = db.upsert_df(conn, dfb, "namechange")
                    log.info("namechange 批量增量 %s~%s: %d 行(全市场)", start, end, n)
            except Exception as e:      # 批量失败不许挡住逐股增量
                log.warning("namechange 批量增量失败(不影响逐股增量): %s", str(e)[:120])
        with db.conn_ctx() as conn:
            # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
            have = set(pd.read_sql("SELECT DISTINCT ts_code FROM namechange", conn)["ts_code"]) \
                if db.table_exists(conn, "namechange") else set()
        codes = sorted(uni - have) if recent_only else sorted(uni)
        total = 0
        ok_n = 0
        fail_n = 0
        with db.conn_ctx() as conn:
            # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
            for i, code in enumerate(codes):
                try:
                    df = self.ts.call("namechange", ts_code=code,
                                      fields="ts_code,name,start_date,end_date,ann_date,change_reason")
                    st = self.ts.last_status
                except Exception as e:
                    # 同 init_fina: 单只调用异常按失败计数, 不炸断每日链 (2026-09-07 全面检查)
                    log.warning("namechange %s 调用异常: %s", code, e)
                    df, st = None, "error"
                if st == "ok" and df is not None and not df.empty:
                    fail_n = 0
                    total += db.upsert_df(conn, df, "namechange")
                    ok_n += 1
                    if (i + 1) % 500 == 0:
                        log.info("namechange: %d/%d (%s)", i + 1, len(codes), code)
                elif st == "empty":
                    fail_n = 0  # 无改名记录(绝大多数): 正常, 不中断
                else:
                    fail_n += 1
                    if fail_n >= 20:
                        log.error("namechange 连续 %d 只失败, 终止本轮(下次续跑)", fail_n)
                        break
            db.set_status(conn, "namechange", "", total)
        log.info("历史名称: %d 条 (本次 %d 只)", total, ok_n)
        return total

    # ---------- 逆回购 ----------
    def init_repo(self) -> None:
        from app.datasource import akshare_client
        akshare_client.sync_repo_rates(START_DATE)

    # ---------- 龙虎榜 ----------
    def sync_lhb(self, end_date: Optional[str] = None) -> None:
        """龙虎榜增量: AkShare(因子口径) + 同花顺(展示口径), 失败不阻塞主流程"""
        from app.datasource import lhb_client
        try:
            lhb_client.sync_lhb_ak(end_date)
        except Exception:
            log.exception("龙虎榜(AkShare)同步异常, 不影响主流程")
        try:
            lhb_client.sync_lhb_ths(end_date)
        except Exception:
            log.exception("龙虎榜(同花顺)同步异常, 不影响主流程")

    # ---------- 新信息源 (2026-09-11 芯片式架构 阶段 0a) ----------
    def sync_new_sources(self, end_date: Optional[str] = None) -> None:
        """融资融券/资金流/大宗/北向/股东户数/指数成分/申万行业。

        与量价类不同, 这批是**与现有 31 因子正交的信息源** —— 多个 die 要真正去相关,
        靠的是口粮不同而非算法不同(见 plans/abstract-sleeping-rainbow.md §0)。
        同 sync_lhb 的纪律: 单个源失败不阻塞主流程。
        ⚠️ 首次运行会补齐 START_DATE 起的全部历史(约 70 分钟), 之后日更只拉缺失日。
        """
        from app.datasource import flow_client
        try:
            flow_client.sync_new_sources(self, end_date=end_date)
        except Exception:
            log.exception("新信息源同步异常, 不影响主流程")

    # ---------- 入口 ----------
    def init_all(self, fast: bool = False) -> None:
        log.info("===== 全量初始化开始 =====")
        self.init_trade_cal()
        self.init_stock_basic()
        self.init_index()
        self.sync_stock_market()
        if fast:
            log.info("fast 模式: 跳过基金/财务/回购")
            return
        self.init_fund_basic()
        self.sync_fund_market()
        self.init_fina()
        self.init_namechange()
        self.init_repo()
        self.sync_lhb()
        self.sync_new_sources()
        log.info("===== 全量初始化完成 =====")

    def run_daily(self, full: bool = False, end_date: Optional[str] = None) -> None:
        """每日盘后增量; 错过再跑时自动补齐; 首次运行自动补基础列表
        end_date: 行情同步截止日(默认今天); 盘中补跑请传上一个已收盘交易日, 避免拉入当天不完整行情"""
        self.init_trade_cal()
        with db.conn_ctx() as conn:
            # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
            conn.execute("SELECT COUNT(*) FROM fund_basic").fetchone()   # 触发建表/校验(计数本身不用)
        self.init_stock_basic()  # 每日刷新, 纳入新上市股票
        self.sync_stock_market(end_date=end_date)
        self.init_index(end_date)
        if full:
            self.sync_fund_market(end_date=end_date)
            # 2026-09-05 审查修复: 原 full 路径固定 recent_only=True, 新装机/换库的自动补齐
            # 只会拉到最近 8 期财务, 36 个月训练窗口的历史财务因子永久缺失且无提示。
            # 历史期数不足时自动全量补齐一次, 之后日更仍走 recent_only 省配额。
            with db.conn_ctx() as conn:
                # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
                n_periods = conn.execute(
                    "SELECT COUNT(DISTINCT end_date) FROM fina_indicator").fetchone()[0] \
                    if db.table_exists(conn, "fina_indicator") else 0
            if n_periods < 40:  # 2013 起季度报告约 56 期
                log.info("财务历史仅 %d 期, 自动全量补齐(首次初始化)", n_periods)
                self.init_fina(recent_only=False)
            else:
                self.init_fina(recent_only=True)
            self.init_namechange(recent_only=True)  # 新股改名记录增量(第三轮复查 D1)
            self.init_repo()
        self.sync_lhb(end_date)
        self.sync_new_sources(end_date)
        log.info("每日增量完成")
