"""市场状态模型: 中证800 均线系统 + (可选)估值分位 + 回撤/波动率 → attack/neutral/defense

规则透明、可解释; 权益仓位由 config 的 regime_equity_weight 映射
"""
import logging

import numpy as np
import pandas as pd

from app import db
from app.config import benchmark_code
from app.datasource.tushare_client import TushareClient
from app.models import pool_index

log = logging.getLogger(__name__)


def _rolling_pct_rank(s: pd.Series, w: int) -> pd.Series:
    """滚动分位(0~1), 滑动窗口向量化实现, 比 rolling().rank 快很多。

    2026-09-09 修复(审查项 ④): 分母原为固定窗口长度 w, 窗口内的 NaN 参与比较恒为
    False → 被当成「不在低位」而不计入分子, 分位系统性低估。PE 序列 20140509 才有
    数据、1260 日窗口到 2019 年中才填满 —— 实测 321 个交易日窗口含 NaN, 其中 192 天
    pr<0.3 被误加 +1 分(2018-03-12 ~ 2019-07-04, 恰压住 2018 熊市与回撤谷底)。
    现分母改「窗口内非 NaN 个数」; 窗口末值自身为 NaN 时返回 NaN(无法排序, 贡献 0)。
    """
    a = s.to_numpy(dtype=float)
    n = len(a)
    out = np.full(n, np.nan)
    # 2026-09-07 边界修复: n==w(恰好一个完整窗口)时也应算出最早 1 个点, 原 n>w 会漏;
    # sliding_window_view 在 n>=w 时产出 n-w+1 个窗口, 与 out[w-1:] 等长
    if n >= w:
        from numpy.lib.stride_tricks import sliding_window_view
        v = sliding_window_view(a, w)
        last = v[:, -1]
        cnt = (~np.isnan(v)).sum(axis=1)
        lt = (v < last[:, None]).sum(axis=1)    # NaN 比较为 False, 天然不计数
        eq = (v == last[:, None]).sum(axis=1)
        pr = np.full(len(cnt), np.nan)
        ok = (~np.isnan(last)) & (cnt > 0)
        pr[ok] = (lt[ok] + eq[ok] / 2) / cnt[ok]
        out[w - 1:] = pr
    return pd.Series(out, index=s.index)


def _apply_state_rules(score: pd.Series, dd: pd.Series, vol: pd.Series,
                       score_max: int) -> pd.Series:
    """三态映射纯函数(2026-09-06 第十轮 N10-8 抽出以便单测):
    attack: score>=score_max-1; defense: score<=2 / 回撤<-20% / 波动>35%; 其余 neutral"""
    state = pd.Series("neutral", index=score.index)
    state[score >= score_max - 1] = "attack"
    state[score <= 2] = "defense"
    state[dd < -0.20] = "defense"
    state[vol > 0.35] = "defense"
    return state


_STATE_RANK = {"defense": 0, "neutral": 1, "attack": 2}
_RANK_STATE = {v: k for k, v in _STATE_RANK.items()}


def _combine_states(base: pd.Series, other: pd.Series, mode: str) -> pd.Series:
    """两路状态合并(风格防守, 2026-09-10)。纯函数便于单测。

    worse  : 取更防守的一路(只可能加防守, 不会放松进攻)
    replace: 完全用 other 替代 base
    未知 mode 记警告并退回 worse(保守)。other 缺数据的日期保持 base 不变
    (池子指数未覆盖的早期日期不应凭空改变行为)。
    """
    o = other.reindex(base.index).fillna(base)
    if mode == "replace":
        return o
    if mode != "worse":
        log.warning("regime_style_guard.mode=%r 未知, 按 worse 处理", mode)
    rb = base.map(_STATE_RANK)
    ro = o.map(_STATE_RANK)
    return pd.Series(np.minimum(rb, ro), index=base.index).map(_RANK_STATE)


def _pool_state_frame(pc: pd.Series) -> pd.DataFrame:
    """池子分支(2026-09-10 抽出): 与基准分支**同一套规则**, 但纯价格无估值层 → score_max=6。

    抽出动机: 风格防守(_state_frame)与看板风格读数(app/models/style.py)都要算这一支,
    复制一份必然漂移, 故共用。pc = 池子指数收盘(等权全A, 见 app/models/pool_index.py)。

    **预热期(ma250 未满 250 日)行剔除** —— 与 _state_frame 2026-09-08 审查 P2 同类缺陷。
    预热期读数不是「清一色 defense」(ma20/60/120 更早生效, 仍能得分), 而是**系统性偏空**:
    `pc > ma250` 恒 False(6 项里固定失 1 项), `dd = pc/rolling(250).max()-1` 恒 NaN →
    -20% 回撤那条防守规则整段失效。实测池子最早的 249 个交易日输出 126 defense / 66 neutral
    / 57 attack —— 看着像正常状态, 其实缺一条腿, 比明显报错更难察觉。
    调用方 reindex 后拿到 NaN, 语义是「该日无池子读数」(_combine_states 对 NaN 保持基准
    不变; style.py 判空降级)。
    """
    ma20 = pc.rolling(20).mean()
    ma60 = pc.rolling(60).mean()
    ma120 = pc.rolling(120).mean()
    ma250 = pc.rolling(250).mean()
    score = ((pc > ma20).astype(int) + (pc > ma60).astype(int)
             + (pc > ma120).astype(int) + (pc > ma250).astype(int)
             + (ma20 > ma60).astype(int) + (ma60 > ma120).astype(int))
    dd = pc / pc.rolling(250).max() - 1
    vol = pc.pct_change().rolling(20).std() * np.sqrt(252)
    out = pd.DataFrame({"pool_score": score, "pool_drawdown": dd, "pool_vol": vol,
                        "pool_state": _apply_state_rules(score, dd, vol, 6)})
    return out[ma250.notna()]


def _state_frame(c: pd.Series, pe, guard: dict) -> pd.DataFrame:
    """纯函数(无 IO, 2026-09-10 抽出): 基准指数收盘 c + 可选 PE 序列 pe + 风格防守
    配置 → 逐日状态帧(trade_date/state/score/drawdown/vol/pool_*)。

    生产路径 compute_regime 与 A/B 实验脚本共用本函数, 保证回测口径与生产完全一致
    (A/B 直接复用而非重写一遍规则)。
    """
    ma20 = c.rolling(20).mean()
    ma60 = c.rolling(60).mean()
    ma120 = c.rolling(120).mean()
    ma250 = c.rolling(250).mean()
    score = ((c > ma20).astype(int) + (c > ma60).astype(int) + (c > ma120).astype(int)
             + (c > ma250).astype(int) + (ma20 > ma60).astype(int)
             + (ma60 > ma120).astype(int))
    if pe is not None:
        pr = _rolling_pct_rank(pe, 1260)
        score = score + (pr < 0.3).astype(int) - (pr > 0.9).astype(int)
    dd = c / c.rolling(250).max() - 1
    vol = c.pct_change().rolling(20).std() * np.sqrt(252)
    # attack 阈值随评分区间自适应(2026-09-04 审查 P2-1): 估值层使总分区间变为 -1~7,
    # 无估值层时为 0~6 —— 固定 ≥6 在无估值层分支等于要求六项全对, 几乎不可达
    score_max = 7 if pe is not None else 6
    state = _apply_state_rules(score, dd, vol, score_max)

    # 风格防守(2026-09-10, config regime_style_guard 门控, 默认关闭):
    # 上面整套规则的均线/回撤/波动全部算在**中证800**(大中盘)上, 而持仓是小盘 ——
    # 2023-11-20→2024-02-07(系统线历史谷底)池子跌 30.4%、中证800 只跌 7.4%,
    # -20% 回撤线根本没触发, 防守信号对真正的风险是瞎的(见 scripts/oneoff/
    # diag_drawdown_attrib.py / diag_style_bench.py)。池子分支用**同一套规则**
    # (纯价格, 无估值层 → score_max=6)算在等权全A 上, 再按 mode 合并:
    #   worse  = 取更防守的一路(只可能加防守, 不会放松)
    #   replace= 完全改用池子分支(等权全A 才是持仓的真实参照系)
    # 池子数据缺失/未建表时保持原状态 —— 关闭与降级行为一致。
    pool_state = pd.Series(np.nan, index=c.index, dtype=object)
    pool_score_s = pd.Series(np.nan, index=c.index)
    pool_dd_s = pd.Series(np.nan, index=c.index)
    pool_vol_s = pd.Series(np.nan, index=c.index)
    if (guard or {}).get("enabled"):
        pc = pool_index.load_pool_close().reindex(c.index)
        if pc.dropna().empty:
            log.warning("风格防守已启用但池子指数为空(pool_index 未建), 本次按现行规则")
        else:
            # reindex: _pool_state_frame 剔了预热期行, 对齐回基准索引后预热期为 NaN ——
            # _combine_states 对 NaN 保持基准状态, 池子预热期不会凭空改状态
            pf = _pool_state_frame(pc).reindex(c.index)   # pc 上方已对齐 c.index
            state = _combine_states(state, pf["pool_state"],
                                    str(guard.get("mode", "worse")))
            pool_state, pool_score_s, pool_dd_s, pool_vol_s = \
                pf["pool_state"], pf["pool_score"], pf["pool_drawdown"], pf["pool_vol"]

    out = pd.DataFrame({"trade_date": c.index, "state": state.values,
                        "score": score.values, "drawdown": dd.values, "vol": vol.values,
                        "pool_state": pool_state.values,
                        "pool_score": pool_score_s.values,
                        "pool_drawdown": pool_dd_s.values,
                        "pool_vol": pool_vol_s.values})
    # 2026-09-08 审查 P2: 原 score.notna() 恒真(布尔比较转 int 永不为 NaN) —— 预热期
    # (ma250 未满 250 日)垃圾行以 score=0/defense 入库, 历史日期读 regime 会拿到伪
    # defense。改为按 ma250 有效性过滤, 预热期行剔除
    return out[ma250.notna().values]


def compute_regime() -> dict:
    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        close = pd.read_sql(
            "SELECT trade_date, close FROM index_daily WHERE ts_code=? ORDER BY trade_date",
            conn, params=(benchmark_code(),))
    if close.empty:
        log.warning("指数数据为空, 无法计算市场状态")
        return {}
    c = close.set_index("trade_date")["close"].astype(float)

    # 估值层(可选): 沪深300 PE-TTM 5年分位。
    # 2026-09-06 第十八轮 R18-1 降级策略重做: 原 R17-2 接口不可用时 return{} 把不依赖
    # 接口的价格六层一起冻掉 —— 转熊时 regime 失明不降仓, 比翻口径更危险。现价格层
    # 永远照算照写; 估值层正常拉取并落库 valuation_pe, 失败时用库内最近一次有效 PE
    # 序列 ffill 保持 7 分制连续, 并显式告警到日志与 data_status(regime_valuation)
    from app import config as cfg_mod
    use_val = bool(cfg_mod.load_config().get("regime_use_valuation", True))
    pe = None
    pe_stored = False
    if use_val:
        ts = TushareClient()  # R19-3(2026-09-06): 关估值层时不白构造 client
        if ts.available("index_dailybasic", ts_code="000300.SH", trade_date="20240801"):
            # 2026-09-06 第十九轮 R19-1 修正: 恢复全历史拉取 —— 第十轮 N10-13 加的
            # 6 年窗口使 2013~2024 估值分位缺失(滚动分位需 5 年预热), 历史状态分布
            # 漂移(585/1154/1582 → 839/1002/1480 实证); 免费档全量实际可用, 每日全量
            # 重拉的配额浪费维持为 R18-4 的 P5 记录(未来改增量合并)。估值用沪深300/
            # 价格用中证800的标的错配已知可接受(估值仅作 ±1 微调), 扩大估值权重前必须
            # 统一指数口径
            basic = ts.call("index_dailybasic", ts_code="000300.SH",
                            fields="trade_date,pe_ttm")
            if not basic.empty:
                pe_s = basic.set_index("trade_date")["pe_ttm"].astype(float)
                with db.conn_ctx() as conn:
                    # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
                    db.upsert_df(conn, pe_s.rename("pe_ttm").reset_index(), "valuation_pe")
                pe_stored = True
        if not pe_stored:
            with db.conn_ctx() as conn:
                # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
                stored = (pd.read_sql("SELECT trade_date, pe_ttm FROM valuation_pe "
                                      "ORDER BY trade_date", conn)
                          if db.table_exists(conn, "valuation_pe") else pd.DataFrame())
            if not stored.empty:
                # 降级: 最近一次有效 PE 序列 ffill 外延(估值分位近似不变), 7 分制连续
                pe = stored.set_index("trade_date")["pe_ttm"].astype(float).reindex(c.index).ffill()
                log.error("估值接口不可用/返回空, 降级使用库内最近一次有效 PE 序列"
                          "(ffill 外延), 价格层照常更新 —— 见 data_status[regime_valuation]")
            else:
                log.error("估值接口不可用且无历史 PE 序列, 本期按纯价格体系(score_max=6)计算")
        else:
            pe = pe_s.reindex(c.index)
        if pe is not None:
            log.info("估值层启用(沪深300 PE分位)")
    else:
        log.info("regime_use_valuation=false, 估值层跳过(纯价格体系)")

    out = _state_frame(c, pe, cfg_mod.load_config().get("regime_style_guard") or {})
    if out.empty:
        # 2026-09-08 审查 P2: 基准指数史不足 250 日(新装/部分回填)原 set_status/iloc
        # IndexError 崩溃且反复重试 —— 空结果不写库直接返回
        log.warning("基准指数历史不足 250 日, 跳过市场状态计算(下次续跑)")
        return {}
    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        n = db.upsert_df(conn, out, "regime")
        db.set_status(conn, "regime", str(out["trade_date"].iloc[-1]), n)
        # R18-1(2026-09-06): 估值层健康度入状态页(正常=数据日/行数; degraded=降级ffill)
        if use_val:
            if pe_stored:
                # R19-2(2026-09-06): 显式排序取最新日(防上游降序返回)
                # 2026-09-08 审查 P2: pe_ttm 全 NaN 时原 index[-1] IndexError —— 空判降级
                pe_ok = pe_s.dropna().sort_index()
                if not pe_ok.empty:
                    db.set_status(conn, "regime_valuation", str(pe_ok.index[-1]), len(pe_s))
                else:
                    db.set_status(conn, "regime_valuation", "degraded", 0)
            elif pe is not None:
                db.set_status(conn, "regime_valuation", "degraded", 0)
            else:
                db.set_status(conn, "regime_valuation", "degraded_nohistory", 0)

    last = out.iloc[-1]
    log.info("市场状态计算完成: %d 条, 最新状态 %s (score=%.1f)", n, last["state"], last["score"])
    return {"last_state": str(last["state"]), "score": float(last["score"]),
            "drawdown": float(last["drawdown"]) if pd.notna(last["drawdown"]) else None,
            "vol": float(last["vol"]) if pd.notna(last["vol"]) else None}
