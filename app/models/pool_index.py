"""池子指数(2026-09-10): 主板+创业板等权全A 日频指数 —— 策略真正捕鱼的"池子"

为什么要有一条自己的指数:
  1. **风格基准(诊断)**: 策略对中证800 的日频 R² 只有 0.19, 对池子 0.40 —— 中证800
     (大中盘)根本不是它持仓的参照系。9.5 年逐年超额是 2017-2020 全负/2021-2026 全正,
     典型风格切换; 拿池子比才能分清"选股 alpha"与"小盘风格 beta"。
  2. **择时输入(regime 风格防守)**: 现行 regime 的均线/回撤/波动全算在中证800 上,
     而持仓是小盘 —— 2023-11-20→2024-02-07(系统线历史谷底)池子跌 30.4%、中证800
     只跌 7.4%, -20% 回撤线没触发, 防守信号对真正的风险是瞎的。

口径(与生产选股域一致, 无前视):
- 主板+创业板: 剔科创(688 **与 689 CDR**)、北交(4/8/9 前缀)。
  2026-09-13: 原 SQL 只剔 '688%', 漏了 689 —— 而"科创板"在 `board.board_of` 里是
  `("688","689")` 两个前缀(D06/D07)。池子是指标口径(风格读数用), 与选股域必须同口径,
  否则"池子 vs 中证800"的风格读数会被一只 CDR 扰动。此处的排除是**无条件**的
  (池子定义恒为"主板+创业板等权"), 与 star_enabled/bse_enabled 两个开关无关 ——
  旧注释写成"由开关决定"是误导, 一并改正。
  - 当日有成交(amount>0); |pct_chg|<=21 通用脏数据护栏(920xxx.BJ 的 pre_close=0.01
    占位值会把 pct_chg 顶到 15 万%; 该批已在板块前缀上排除, 护栏仍保留以防同类)
  - **月内权重随价格漂移、月初再平衡**的等权组合(标准等权指数口径), 首日基准 1000。
    不用「当日横截面均值逐日复利」——那是**每日再平衡**, 自带再平衡红利(实测
    2023-11-20→2024-02-07 差 0.76pp, 长期约 +5pp/年量级), 会把漂移顶高、把回撤抹平,
    作为防守信号系统性偏松。月内漂移版 = 逐股月内复利路径的横截面均值, 与
    scripts/oneoff/diag_style_bench.py 的月度口径同源(池子 +7.17%/年)
  - **不做 PIT 名称过滤**(含 ST/新股/退市整理股): 作为风格指数这是二阶影响, 换来
    无需重算历史名称链; 退市整理期的深跌日因 |pct_chg|<=21 被挡, 对指数是轻微上偏
  - ⚠️ 同一条护栏还有**另外半边**（2026-09-20 登记，此前 docstring 只承认了深跌那一半）:
    过滤发生在**月内累计之前**(`WHERE ... ABS(pct_chg)<=21` 包住窗口函数), 所以**退市股终点
    那一笔 −100% 也进不了 `AVG(lvl)`** ⇒ 池等权线系统性**偏乐观**(该丢的跌幅没丢)。
    量级与"哪些格子受影响"属**研究口径**, 动它 = 换尺 ⇒ 未动, 见 14 号三补二十五 §③-3
    与三补二十九 Q7~Q9 的量化安排。

全量重算: 一条窗口函数 SQL(SQLite ≥3.25)。月内路径每月重置, 天然可增量, 但全量
一次即可自愈(晚到的数据修正会在下次重算生效), 故不做增量分支。
"""
import logging

import pandas as pd

from app import db

log = logging.getLogger(__name__)

BASE_LEVEL = 1000.0

# 逐股按月累计(月初重置为 1) → 当日横截面均值 = 月内权重漂移的等权组合水平
_SQL = """
WITH path AS (
    SELECT trade_date,
           exp(SUM(LN(1 + pct_chg/100.0)) OVER (
               PARTITION BY ts_code, substr(trade_date,1,6) ORDER BY trade_date)) AS lvl
    FROM daily
    WHERE amount > 0 AND pct_chg IS NOT NULL AND ABS(pct_chg) <= 21
      AND ts_code NOT LIKE '688%' AND ts_code NOT LIKE '689%' AND ts_code NOT LIKE '8%'
      AND ts_code NOT LIKE '4%' AND ts_code NOT LIKE '9%'
)
SELECT trade_date, AVG(lvl) AS lvl, COUNT(*) AS n
FROM path GROUP BY trade_date ORDER BY trade_date
"""


def build_pool_index() -> int:
    """全量重算池子指数并落库, 返回写入行数。"""
    conn = db.get_conn()
    try:
        df = pd.read_sql(_SQL, conn)
        if df.empty:
            log.warning("池子指数: daily 无可用数据, 跳过")
            return 0
        df["lvl"] = df["lvl"].astype(float)
        # 月末水平=当月组合收益(月初重置为 1) → 逐月复利得月末累计, 再按当月起点接回日频
        ym = df["trade_date"].str[:6]
        month_end = df.groupby(ym)["lvl"].last()
        cum = month_end.cumprod()
        factor = cum / month_end          # 当月起点累计(前几个月之积)
        df["close"] = BASE_LEVEL * ym.map(factor) * df["lvl"]
        df["ret"] = df["close"].pct_change()
        out = df[["trade_date", "ret", "close", "n"]]
        n = db.upsert_df(conn, out, "pool_index")
        last = out.iloc[-1]
        db.set_status(conn, "pool_index", str(last["trade_date"]), n)
        log.info("池子指数: %d 条, 最新 %s (成分 %d 只)",
                 n, last["trade_date"], int(last["n"]))
        return n
    finally:
        conn.close()


def load_pool_close(conn=None) -> pd.Series:
    """池子指数收盘序列(index=trade_date 升序)。表为空时返回空 Series(调用方降级)。"""
    own = conn is None
    if own:
        conn = db.get_conn()
    try:
        if not db.table_exists(conn, "pool_index"):
            return pd.Series(dtype=float)
        s = pd.read_sql("SELECT trade_date, close FROM pool_index ORDER BY trade_date",
                        conn)
    finally:
        if own:
            conn.close()
    return s.set_index("trade_date")["close"].astype(float)
