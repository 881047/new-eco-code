"""Point-in-Time Engine —— 「评分/执行日 d 当天, 到底什么可买、它当时叫什么、当时属于谁」

对应框架图的 `A股数据湖 → **Point-in-Time Engine**`。它被提为一等模块的理由不是"更优雅",
而是**本项目最贵的一次错误恰好是这里**: 回测买过生产买不到的 ST/退市/停牌股,
旧基线的年化被高估了 **28.78pp**(见 `config.py` 已证伪登记处与项目记忆)。
⇒ 时点口径必须**只有一个实现**, 且必须**能被审计**(每一层剔掉了谁、剔了多少)。

## 口径的权威定义

本模块的 `tradable_mask` 与生产训练用的 `trainer.tradable_mask`
(`app/models/trainer.py` 内嵌闭包)是**同一套口径**, 六层过滤:

| # | 层 | 规则 | 为什么 |
|---|---|---|---|
| 1 | 已上市 | `first_trade <= d - 60 交易日` | 次新无历史/易被爆炒; 且对齐训练的 60 日观察窗 |
| 2 | 未停牌 | `vol(d) > 0` | 停牌买不到 |
| 3 | 非 ST/退 | **时点名称**(`NameLookup.name_at`) 不含 `ST`/`退` | ⭐ **必须用 d 当天的名字** —— 用当前名称 = 前视 + 幸存者偏差 |
| 4 | 板块 | 科创(`688`)按 config `star_enabled`; 北交按 `allow_bse` | 「训练域 = 选股域」(用户 2026-09-08 拍板) |
| 5 | 市值 | `total_mv >= 截面 size_pctl 分位` | 防御头部押注超小盘尾部 |
| 6 | 流动性 | 近 20 日均成交额 `>= min_amt20` | 同上 |

**首现日锚定上市日**(不是"数据里第一次出现"): 对北交所, 精选层时期的新三板交易数据会让
股票在北交所开市前就"出现" ⇒ 前视。取 `max(数据首现, list_date)`。

## 使用

    pit = PITEngine()                      # 构造时一次性载入行情/名称/上市日
    ok  = pit.tradable_mask(pd.Timestamp("2023-06-30"))
    idx = pit.universe("2023-06-30")       # = tradable_mask 为 True 的代码
    pit.audit("2023-06-30")                # 每一层剔了多少只 ⇒ 过滤可解释

    # 模块级函数(无缓存, 每次自建): 一次性脚本/测试用
    ok = tradable_mask(m, dates, pd.Timestamp("2023-06-30"))

## 已知边界(不假装覆盖)

- **行业口径仍是当前快照**(`stock_basic.industry`), 不是 PIT。任务 #23 已实测:
  修成 PIT 的代价 ≈ 0(成对检验 36 个统计量只有 1 个 |t|≥2, 正是多重比较预期值),
  且 PIT 覆盖率本身逐年剧变(2023 丢 10.3%/2024 丢 0.0%)= 「含义静默改变」陷阱。
  `industry_at()` 因此**默认走快照并如实标注**, 留了 PIT 钩子但不假装它有数据。
- **停牌用 `vol>0` 近似**(与生产一致), 不读 `suspend_d`(该表不存在)。
- `hk_hold` 式的"覆盖度断崖"特征**一律不得进生产**, 见方案 §3 不变量 #6。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from app import db
from app.models.names import NameLookup
from app.portfolio.board import board_of

log = logging.getLogger(__name__)

LISTED_MIN_DAYS = 60          # 上市未满 60 交易日 → 不可交易(与生产一致)


class PITEngine:
    """时点引擎。构造一次, 之后按日问询。

    参数
    ----
    m : dict         行情面板(`factor_engine.load_market` 的返回); None = 自己加载
    since : str      自建面板时的起点(YYYYMMDD); None = 全历史
    cfg : dict       config(缺省读 `config.load_config()`); 只用到
                     `star_enabled` / `stock_size_pctl` / `stock_min_amt20`
    """

    def __init__(self, m: dict = None, since: str = None, cfg: dict = None,
                 conn=None):
        if m is None:
            from app.factors.factor_engine import load_market
            m = load_market(since=since)
        self.m = m
        self.dates = pd.to_datetime(m["close"].index)
        if cfg is None:
            from app import config as cfg_mod
            cfg = cfg_mod.load_config()
        self.cfg = cfg or {}
        self.size_pctl = float(self.cfg.get("stock_size_pctl", 0) or 0)
        self.min_amt20 = float(self.cfg.get("stock_min_amt20", 0) or 0)
        self.star_enabled = bool(self.cfg.get("star_enabled", False))

        cols = m["close"].columns
        self._nl = NameLookup(conn)
        self._is_star = pd.Series([board_of(str(c)) == "star" for c in cols], index=cols)
        self._is_bse = pd.Series([board_of(str(c)) == "bse" for c in cols], index=cols)
        self._first_trade = self._build_first_trade(conn)
        # 名称按时点缓存: 同一 (code, 日) 在一次运行内不变; 构造实例的寿命 = 一次执行
        self._name_cache: dict = {}

    # ------------------------------------------------------------------
    # 构造期: 首现日 + 上市日
    # ------------------------------------------------------------------
    def _build_first_trade(self, conn) -> pd.Series:
        first = self.m["close"].notna().idxmax()
        own = conn is None
        conn = conn or db.get_conn()
        try:
            if db.table_exists(conn, "stock_basic"):
                lm = pd.read_sql("SELECT ts_code, list_date FROM stock_basic",
                                 conn).set_index("ts_code")["list_date"]
            else:
                lm = pd.Series(dtype=str)
        finally:
            if own:
                conn.close()
        list_dt = pd.to_datetime(lm.reindex(self.m["close"].columns), errors="coerce")
        # 上市日晚于末个交易日(未来上市) → clip 到末尾, 视为不可交易
        pos = self.dates.searchsorted(list_dt.fillna(self.dates[0]).to_numpy())
        first_list = pd.Series(self.dates[pos.clip(max=len(self.dates) - 1)],
                               index=self.m["close"].columns)
        return pd.concat([first, first_list], axis=1).max(axis=1)

    # ------------------------------------------------------------------
    # 时点问询
    # ------------------------------------------------------------------
    def name_at(self, code: str, dt) -> str:
        d = self._dstr(dt)
        k = (code, d)
        if k not in self._name_cache:
            self._name_cache[k] = self._nl.name_at(str(code), d)
        return self._name_cache[k]

    # 未接线(2026-09-12 独立审计 #13): 全库无调用方 —— 保留给框架阶段
    # (framework.enabled=false), 不要当成公开 API 使用;
    # 生产过滤一律走 tradable_mask / tradable_matrix。
    # ⚠️ 契约边界(2026-09-16 数据线审查 DATA-10): `_build_first_trade` 用「面板首现」
    # 锚定上市日 ⇒ **窗口化面板(load_market(since=X))在面板起点附近的 60 交易日判定
    # 不可信**(面板首日≠上市日, 次新会被放行)。生产 trainer(全历史面板)与
    # alpha/factory(warmup=400 自然日, 刻意覆盖)恰好避开 —— 任何新增的短窗口调用方
    # 必须自行保证面板起点早于待评分截面 60 个交易日以上。
    def is_st(self, dt) -> pd.Series:
        """d 当天的 ST/退 判定(**时点名称**, 不是当前名称)"""
        names = pd.Series([self.name_at(c, dt) for c in self.m["vol"].columns],
                          index=self.m["vol"].columns)
        return names.str.contains("ST|退", na=False)

    # 未接线(2026-09-12 独立审计 #13): 全库无调用方 —— 保留给框架阶段
    # (framework.enabled=false), 不要当成公开 API 使用;
    # 生产过滤一律走 tradable_mask / tradable_matrix。
    def board(self, dt) -> pd.Series:
        return pd.Series([board_of(str(c)) for c in self.m["vol"].columns],
                         index=self.m["vol"].columns)

    # 未接线(2026-09-12 独立审计 #13): 全库无调用方 —— 保留给框架阶段
    # (framework.enabled=false), 不要当成公开 API 使用;
    # 生产过滤一律走 tradable_mask / tradable_matrix。
    def industry_at(self, dt, pit: bool = False) -> pd.Series:
        """行业归属。**默认当前快照**(已知缺陷, 见模块 docstring); `pit=True` 时尝试
        读 PIT 表, 表不存在则如实回退快照并告警 —— 不静默假装。"""
        if pit:
            own = None
            try:
                own = db.get_conn()
                # 2026-09-16 数据线审查 DATA-05: 表名原写 `sw_industry_member`, 而 DDL 与
                # 同步写入的实际叫 **sw_member**(db.py:371) ⇒ 本钩子永远走"表不存在"分支
                # 形同虚设; 顺带补 level='L1' 过滤(同股多行业归属时 set_index 会重复索引)
                # 与去重。当前生产路径未接线(industry_at 默认快照), 修复不改变任何现网读数。
                if db.table_exists(own, "sw_member"):
                    q = ("SELECT ts_code, l1_name FROM sw_member "
                         "WHERE level='L1' AND in_date <= ? AND (out_date IS NULL OR out_date > ?)")
                    df = pd.read_sql(q, own, params=(self._dstr(dt), self._dstr(dt)))
                    if len(df):
                        return df.drop_duplicates("ts_code").set_index("ts_code")["l1_name"]
                    log.warning("sw_member 在 %s 无命中, 回退当前快照", dt)
                else:
                    log.warning("PIT 行业表 sw_member 不存在, 回退当前快照"
                                "(已知缺陷, 任务 #23 实测代价≈0)")
            except Exception:
                log.exception("PIT 行业查询失败, 回退当前快照")
            finally:
                if own is not None:
                    own.close()
        own = db.get_conn()
        try:
            if db.table_exists(own, "stock_basic"):
                return pd.read_sql("SELECT ts_code, industry FROM stock_basic",
                                   own).set_index("ts_code")["industry"] \
                    .reindex(self.m["close"].columns)
        finally:
            own.close()
        return pd.Series(index=self.m["close"].columns, dtype=object)

    def tradable_mask(self, dt, allow_bse: bool = False,
                      star_enabled: bool = None) -> pd.Series:
        """d 当天可交易掩码(bool Series, index = 全部代码)。

        与生产 `trainer.tradable_mask` 同口径(见模块 docstring 的六层表)。
        """
        dt = pd.Timestamp(dt)
        pos = self.dates.get_loc(dt)
        cutoff = self.dates[max(0, pos - LISTED_MIN_DAYS)]
        names = pd.Series([self.name_at(c, dt) for c in self.m["vol"].columns],
                          index=self.m["vol"].columns)
        is_st = names.str.contains("ST|退", na=False)
        star = self.star_enabled if star_enabled is None else bool(star_enabled)
        base = (self._first_trade <= cutoff) & (self.m["vol"].loc[dt] > 0) & (~is_st) \
            & (allow_bse | ~self._is_bse) & (star | ~self._is_star)
        # ⚠️ 这两层**无条件执行**(即使阈值是 0), 与生产逐字一致: `mv.quantile(0)` = 截面
        # 最小值 ⇒ 市值 NaN 的股票仍被剔(0 阈值不是"关掉这一层"); amt20 NaN 同理
        # (`NaN >= 0` 为 False)。写成"阈值为 0 就跳过"会**静默放宽**生产口径。
        mv = self.m["total_mv"].loc[dt]
        size_ok = mv >= mv.quantile(self.size_pctl)
        amt20 = self.m["amount"].iloc[max(0, pos - 19): pos + 1].mean()
        liq_ok = amt20 >= self.min_amt20
        return (base & size_ok & liq_ok).fillna(False)

    # 未接线(2026-09-12 独立审计 #13): 全库无调用方 —— 保留给框架阶段
    # (framework.enabled=false), 不要当成公开 API 使用;
    # 生产过滤一律走 tradable_mask / tradable_matrix。
    def universe(self, dt, **kw) -> pd.Index:
        """d 当天可交易代码集合(`tradable_mask` 为 True 的那些)"""
        return self.tradable_mask(dt, **kw).pipe(lambda s: s.index[s.fillna(False)])

    def tradable_matrix(self, dates, columns=None, **kw) -> pd.DataFrame:
        """`dates × columns` 的 bool 面板 —— 每行是**那天**的可交易域。

        2026-09-12 审查 P0-05 新增: 可交易域只取决于 (日, 股), **与因子无关**, 所以
        在同一个评估批次里必须只算一次、所有候选复用。逐个候选调 `filter_panel`
        会把 O(候选数 × 截面数 × 股票数) 的掩码计算重复一遍(GP 会上千个个体)。

        不在交易日历的日期 → 整行 False(该截面整体不可交易, 与 `filter_panel` 一致)。
        """
        cols = pd.Index(self.m["close"].columns) if columns is None else pd.Index(columns)
        rows = {}
        unknown = []
        for dt in pd.to_datetime(pd.Index(dates)):
            if dt not in self.dates:
                unknown.append(dt)
                rows[dt] = np.zeros(len(cols), dtype=bool)
                continue
            ok = self.tradable_mask(dt, **kw).reindex(cols).fillna(False)
            rows[dt] = ok.to_numpy(dtype=bool)
        if unknown:
            log.warning("tradable_matrix: %d 个日期不在交易日历, 整行按不可交易处理: %s",
                        len(unknown), [str(d.date()) for d in unknown[:5]])
        # dict 插入序 = dates 序 ⇒ .T 之后 index 就是 dates(显式重贴一次, 不靠隐含假设)
        mat = pd.DataFrame(rows).T
        mat.index = pd.to_datetime(pd.Index(dates))
        mat.columns = cols
        return mat

    def filter_panel(self, wide: pd.DataFrame, k: int = 60, **kw) -> pd.DataFrame:
        """把一张 `index=日期, columns=代码` 的面板按**每行自己那天的**可交易域置 NaN。

        给 Alpha Factory 用: 因子的覆盖域必须先被 PIT 域裁一遍, 否则实验会在
        生产买不到的股票上打分(旧基线 28.78pp 那个错误的同类)。
        """
        mat = self.tradable_matrix(wide.index, wide.columns, **kw)
        return wide.where(mat)

    # ------------------------------------------------------------------
    # 审计: 每一层剔掉了谁
    # ------------------------------------------------------------------
    # 未接线(2026-09-12 独立审计 #13): 全库无调用方 —— 保留给框架阶段
    # (framework.enabled=false), 不要当成公开 API 使用;
    # 生产过滤一律走 tradable_mask / tradable_matrix。
    def audit(self, dt, allow_bse: bool = False) -> pd.DataFrame:
        """逐层过滤的剔除统计 —— 「PIT Engine 做了什么」必须可解释。

        返回 DataFrame(index=层名, columns=[剩余, 本层剔除])。
        ⚠️ 各层是**交集**关系, 本表按顺序累乘, 与 `tradable_mask` 的最终结果一致。
        """
        dt = pd.Timestamp(dt)
        pos = self.dates.get_loc(dt)
        cutoff = self.dates[max(0, pos - LISTED_MIN_DAYS)]
        names = pd.Series([self.name_at(c, dt) for c in self.m["vol"].columns],
                          index=self.m["vol"].columns)
        layers = [
            ("全部代码", pd.Series(True, index=self.m["vol"].columns)),
            ("①已上市满60日", self._first_trade <= cutoff),
            ("②未停牌(vol>0)", self.m["vol"].loc[dt] > 0),
            ("③非ST/退(时点名)", ~names.str.contains("ST|退", na=False)),
            ("④板块(科创/北交)", (allow_bse | ~self._is_bse)
             & (self.star_enabled | ~self._is_star)),
        ]
        mv = self.m["total_mv"].loc[dt]
        layers.append((f"⑤市值下界(pctl={self.size_pctl:g})",
                       mv >= mv.quantile(self.size_pctl)))
        amt20 = self.m["amount"].iloc[max(0, pos - 19): pos + 1].mean()
        layers.append((f"⑥近20日均额下限({self.min_amt20:g})", amt20 >= self.min_amt20))
        rows, acc, prev = [], pd.Series(True, index=self.m["vol"].columns), None
        for nm, layer in layers:
            layer = layer.reindex(self.m["vol"].columns).fillna(False)
            acc = acc & layer
            rows.append({"层": nm, "剩余": int(acc.sum()),
                         "本层剔除": int((prev & ~layer).sum()) if prev is not None else 0})
            prev = acc
        return pd.DataFrame(rows).set_index("层")

    def _dstr(self, dt) -> str:
        return pd.Timestamp(dt).strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# 模块级便捷函数(无缓存, 每次自建引擎) —— 一次性脚本/测试用
# ---------------------------------------------------------------------------
def tradable_mask(m: dict, dates, dt, cfg: dict = None, **kw) -> pd.Series:
    """模块级便捷包装。⚠️ `dates` 为**死参数**(2026-09-28 审查注记): 索引取自 `m`,
    传入不一致的 dates 无任何效果 —— 保留占位仅为兼容既有调用方签名, 勿再依赖。"""
    return PITEngine(m=m, cfg=cfg).tradable_mask(dt, **kw)


def universe_at(m: dict, dates, dt, cfg: dict = None, **kw) -> pd.Index:
    """同上：`dates` 为死参数占位（索引取自 `m`）。"""
    return PITEngine(m=m, cfg=cfg).universe(dt, **kw)


__all__ = ["PITEngine", "tradable_mask", "universe_at", "LISTED_MIN_DAYS"]
