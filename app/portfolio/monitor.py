"""动态监控: 每日盘后体检持仓, 生成今日信号(阈值触发+冷却/确认, 不无脑日频换仓)

动作类型:
  sell         止损触发(较实际买入成本回撤超限)
  watch        评分跌出前 N%, 但未到替换线/确认期未满
  sell_weak    弱势+有候选+连续确认 → 卖出(配合 buy_candidate 替换)
  buy_candidate 打分进入前 N 的非持仓股
  reduce       市场状态降级 → 减仓名单
  add          市场状态升级 → 加仓名单
  warn         停牌/ST/退市风险预警

2026-09-04 审查修复(P2-3/P2-4):
- 持仓/止损基准改为模拟盘真实持仓(paper_position 买入成本+entry_adj), 不再是 plan 方案行
  (涨停没买进的票不再"被体检", 止损基准=实际买入成本而非方案日收盘价)
- 新买入 min_hold_days 个交易日内不参与弱势替换(月度模型 21 日持有期, 日频推分噪音大,
  曾出现买入次日即被 sell_weak 换掉的案例)
- 弱势卖出需连续 weak_confirm_days 日确认(第 1 天只发 watch)
- 同股同动作 cooldown_days 个交易日内冷却(停牌/ST 不再天天刷告警)
- 候选「买回锁」(2026-09-30): 冷却期内有过卖出成交(止损/弱势/减仓/方案调出)的股票
  不进 buy_candidate/add 名单 —— 否则任一层刚做的卖出决策会被自己的候选路径买回
  (光韵达 2026-09 止损次日被方案买回事故的同族变体)
"""
import logging

import pandas as pd

from app import config as cfg_mod
from app import db

log = logging.getLogger(__name__)

#: 「真动作」集合 —— 同时决定两件事：① `has_action`(要不要现在给人发邮件)；
#: ② 即时邮件里列哪些行，以及晚间总结怎么分类计数。
#: 提到模块级是为了**只有一个真源**：`app/notify.py` 从这里 import，不在那边再抄一遍
#: （2026-09-18 邮件优化：两边各写一份时，"邮件说今天有动作、正文却一行不列"迟早出现）。
#: 观察类 `watch` / 候选类 `buy_candidate` 不在其中 —— 它们不需要今天动手。
ACTION_NOW = ("sell", "sell_weak", "reduce", "add", "warn")


def _dyn_cfg(cfg: dict) -> dict:
    d = cfg.get("dynamic", {}) or {}
    return {
        "enabled": d.get("enabled", False),  # 2026-09-06 R17-1: 缺省关闭(fail-safe)
        "stop_loss": float(d.get("stop_loss", -0.15)),
        "weak_pct": float(d.get("weak_pct", 40)),
        "swap_drawdown": float(d.get("swap_drawdown", -0.08)),
        "candidate_top": int(d.get("candidate_top", 20)),
        # 2026-09-04 新增: 弱势替换保护与信号冷却
        "min_hold_days": int(d.get("min_hold_days", 3)),        # 买入后 N 交易日不参与弱势替换
        "weak_confirm_days": int(d.get("weak_confirm_days", 2)),  # 弱势连续 N 日确认
        "cooldown_days": int(d.get("cooldown_days", 5)),        # 同股同动作 N 交易日冷却
    }


def _prev_open_day(conn, trade_date):
    row = conn.execute(
        "SELECT MAX(cal_date) FROM trade_cal WHERE is_open=1 AND cal_date<?", (trade_date,)
    ).fetchone()
    return row[0] if row else None


def _open_days_between(conn, d0, d1) -> int:
    if not d0 or not d1 or d0 >= d1:
        return 0
    return conn.execute(
        "SELECT COUNT(*) FROM trade_cal WHERE is_open=1 AND cal_date>? AND cal_date<=?",
        (d0, d1)).fetchone()[0]


def _sold_recent_codes(conn, trade_date, cooldown_days) -> set:
    """冷却期内有过**卖出成交**的代码集合(2026-09-30 候选「买回锁」)。

    口径=卖出**成交**(paper_trade), 不看信号行: 成交才是"仓位真的被清掉了"的事实;
    卖出未成交期间股票仍在持仓, 候选名单本来就因 holds 排除而到不了它。
    覆盖全部卖出路径(止损/弱势/减仓/月度方案调出) —— 任何一层刚做出的卖出决策,
    cooldown_days 个交易日内不得被动态层的候选/加仓路径买回(窗口与同动作冷却同源,
    paper_trade 体量小, 全量取回按交易日历过滤即可)。
    """
    sold = set()
    for code, exec_date in conn.execute(
            "SELECT DISTINCT ts_code, exec_date FROM paper_trade WHERE side='sell'"):
        if _open_days_between(conn, exec_date, trade_date) <= cooldown_days:
            sold.add(code)
    return sold


def _limit_up_blocked(daily: pd.DataFrame, trade_date: str, nl) -> set:
    """**涨停买不进**的代码集合（下单候选要踢掉它们）。

    2026-09-13：从 `run_monitor` 里抽出来 —— 原来这段写成 `daily.loc[..., "ts_code"]`，
    而 `daily` 在上面已 `set_index("ts_code")` ⇒ 必然 `KeyError`（同类 bug 本文件 2026-09-08
    修过另一处，只修了一处）。抽成纯函数后**可以直接测**，不必为了一行而搭整条监控链路。
    """
    from app.portfolio.board import board_of, limit_price, limit_ratio
    lim = daily.index.map(
        lambda c: limit_ratio(c, False, trade_date, nl.name_at(c, trade_date), nl.name_at))
    # 2026-10-05: 北交所取整接线(上轨 floor/下轨 ceil, 见 board.limit_price)
    limit_up = pd.Series(
        [limit_price(p, r, board_of(c)) for p, r, c in
         zip(daily["pre_close"], lim, daily.index)], index=daily.index,
        dtype="float64") - 0.001
    return set(daily.index[(daily["pre_close"] > 0) & (daily["close"] >= limit_up)])


def run_monitor(trade_date: str = None) -> dict:
    cfg = cfg_mod.load_config()
    dyn = _dyn_cfg(cfg)
    if not dyn["enabled"]:
        return {"enabled": False}
    profile = cfg.get("risk_profile", "balanced")

    conn = db.get_conn()
    try:
        if trade_date is None:
            trade_date = conn.execute("SELECT MAX(trade_date) FROM daily_score").fetchone()[0]
            if trade_date is None:                       # 无日频推分时退回月度表(日期与查询表保持一致)
                trade_date = conn.execute("SELECT MAX(trade_date) FROM model_score").fetchone()[0]
                score_src = "model_score"
            else:
                score_src = "daily_score"
        else:
            # 2026-09-28 审查修复: 显式传日期(历史回放/补跑)时原硬编码 daily_score,
            # 该日只有 model_score 行(如月末锚点)会以"打分或持仓为空"**静默无产出** ——
            # 与自动路径对称: 先探日频, 无行则回退月度表
            has_ds = conn.execute("SELECT COUNT(*) FROM daily_score WHERE trade_date=?",
                                  (trade_date,)).fetchone()[0]
            score_src = "daily_score" if has_ds else "model_score"
        # 当日打分(全市场, 来自日频推分)
        scores = pd.read_sql(
            f"SELECT s.ts_code, s.score, b.name, b.industry "
            f"FROM {score_src} s LEFT JOIN stock_basic b ON s.ts_code=b.ts_code "
            f"WHERE s.trade_date=?", conn, params=(trade_date,))
        # 当日行情
        daily = pd.read_sql(
            "SELECT ts_code, close, pre_close, vol, pct_chg FROM daily WHERE trade_date=?", conn,
            params=(trade_date,))
        cur_adj = pd.read_sql(
            "SELECT ts_code, adj_factor FROM adj_factor WHERE trade_date=?", conn, params=(trade_date,))
        # 持仓: 模拟盘真实持仓(买入成本+entry_adj), 不再用 plan 方案行 (P2-4)
        holds = pd.read_sql("SELECT ts_code, name, shares, avg_cost, entry_adj FROM paper_position", conn)
        # 每只持仓的最近买入成交日(持有期保护用) —— 2026-09-05 审查修复: 原用 MIN(exec_date)
        # 按首次买入起算, 清仓后重新买入的股会绕过 3 日持有期保护
        entry = {}
        if not holds.empty:
            rows = conn.execute(
                "SELECT ts_code, MAX(exec_date) FROM paper_trade WHERE side='buy' GROUP BY ts_code"
            ).fetchall()
            entry = {r[0]: r[1] for r in rows}
        # 历史信号: 确认(连续弱势)与冷却判定; 回溯窗口 = 冷却天数+2 个交易日。
        # 2026-09-05 审查修复: 查询排除当日行 —— 原同日第二次 run_monitor 会把刚写入的
        # 信号当冷却历史, 无条件 DELETE 重写后 sell/sell_weak 不被重插, 止损单在消费前消失
        prev_day = _prev_open_day(conn, trade_date)
        sig_cut = trade_date
        for _ in range(dyn["cooldown_days"] + 2):
            nxt = _prev_open_day(conn, sig_cut)
            if not nxt:
                break
            sig_cut = nxt
        hist = pd.read_sql(
            "SELECT signal_date, ts_code, action FROM signal WHERE signal_date>=? AND signal_date<?",
            conn, params=(sig_cut or "00000000", trade_date))
        prev_pd = hist[hist["signal_date"] == prev_day]
        prev_sigs = set(zip(prev_pd["ts_code"], prev_pd["action"])) if not prev_pd.empty else set()
        # 方案日与市场状态(regime 切换判定仍以最近方案日为基准)
        plan_date = conn.execute("SELECT MAX(plan_date) FROM plan").fetchone()[0]
        st_plan = conn.execute("SELECT state FROM regime WHERE trade_date<=? ORDER BY trade_date DESC LIMIT 1",
                               (plan_date,)).fetchone() if plan_date else None
        st_now = conn.execute("SELECT state FROM regime WHERE trade_date<=? ORDER BY trade_date DESC LIMIT 1",
                              (trade_date,)).fetchone()

        if scores.empty or holds.empty:
            log.warning("打分或持仓为空, 跳过监控")
            return {"trade_date": trade_date, "has_action": False, "items": []}

        scores = scores.set_index("ts_code")
        daily = daily.set_index("ts_code")
        cur_adj = cur_adj.set_index("ts_code")
        holds = holds.set_index("ts_code")

        def _cooled(code, action) -> bool:
            """同股同动作 N 交易日冷却: 冷却期内已有同动作信号 → True"""
            if hist.empty:
                return False
            for _, r in hist[hist["ts_code"] == code].iterrows():
                if r["action"] != action:
                    continue
                if _open_days_between(conn, r["signal_date"], trade_date) <= dyn["cooldown_days"]:
                    return True
            return False

        items = []
        # 第十轮 N10-4: ST 预警按时点名称 PIT(与 trainer/回测/paper 同口径)
        from app.models.names import NameLookup
        nl = NameLookup()

        # ---------- 1) 持仓体检 ----------
        for code, h in holds.iterrows():
            if code not in daily.index:
                # 2026-09-08 审查 P2: 退市摘牌后 daily 无行, 原静默 continue —— 补风险
                # 预警(冷却期内不重复), 估值侧按最后收盘价挂账与预警叠加才有用户感知
                if not _cooled(code, "warn"):
                    items.append({"ts_code": code, "name": h["name"], "action": "warn",
                                  "reason": "持仓无当日行情(退市摘牌/长期停牌), 请人工核对"})
                continue
            pct = scores.loc[code, "score"] if code in scores.index else None
            pos = daily.loc[code]
            # 收益口径: 较实际买入成本(avg_cost), 复权因子齐备时用总收益口径 (P0-3/P2-4)
            cur = pos["close"]
            ea = h["entry_adj"] if pd.notna(h["entry_adj"]) and h["entry_adj"] else None
            ca = cur_adj.loc[code, "adj_factor"] if code in cur_adj.index else None
            ca_ok = pd.notna(ca) and ca
            base_ok = pd.notna(h["avg_cost"]) and h["avg_cost"]
            if base_ok and ea and ca_ok:
                # 第七轮复查 N7-7: 分批加仓后的持仓, avg_cost(股数加权)与 entry_adj(调和
                # 平均)加权方式不同, 此式为总收益口径近似(非精确现金流 IRR); -15% 止损
                # 阈值足够宽, 近似不影响触发稳健性。单次买入持仓时精确
                ret = (cur * ca) / (h["avg_cost"] * ea) - 1
            else:
                ret = (cur / h["avg_cost"] - 1) if base_ok else 0.0
            held_days = _open_days_between(conn, entry.get(code), trade_date)
            # 停牌/ST 预警(冷却期内不重复)
            if pos["vol"] == 0:
                if not _cooled(code, "warn"):
                    items.append({"ts_code": code, "name": h["name"], "action": "warn",
                                  "reason": "停牌中, 无法交易"})
                continue
            # 第十轮 N10-4: ST 预警按时点名称 PIT(原用建仓冻结名 —— 持仓中途戴帽漏发、
            # 摘帽后仍报警); 与 trainer/回测/paper 同口径
            nm_now = nl.name_at(code, trade_date) if nl else str(h["name"])
            if "ST" in nm_now or "退" in nm_now:
                if not _cooled(code, "warn"):
                    items.append({"ts_code": code, "name": h["name"], "action": "warn",
                                  "reason": "名称带ST/退, 注意风险"})
                continue
            if base_ok and not (ea and ca_ok):
                # 2026-09-04 修复: 复权因子任一侧缺失时, 未复权口径在除权日会产生假止损/假弱势卖出
                # (daily 与 adj_factor 独立同步, 各自容错跳过, 缺口现实存在)。降级为告警, 不触发动作。
                log.warning("监控 %s %s: 复权因子缺失(持仓/当日), 止损信号暂停", trade_date, code)
                if not _cooled(code, "warn"):
                    items.append({"ts_code": code, "name": h["name"], "action": "warn",
                                  "reason": "复权因子缺失, 止损/替换信号暂停(数据恢复后自动继续)"})
                continue
            if base_ok and ret <= dyn["stop_loss"]:
                if not _cooled(code, "sell"):
                    items.append({"ts_code": code, "name": h["name"], "action": "sell",
                                  "reason": f"止损: 较买入成本 {ret:.1%} (阈值 {dyn['stop_loss']:.0%})"})
                continue
            if pct is not None and pct < dyn["weak_pct"]:
                if held_days < dyn["min_hold_days"]:
                    continue  # 买入后 N 交易日内不参与弱势替换(给月度模型留验证期)
                if ret <= dyn["swap_drawdown"]:
                    # 连续确认: 前一日有弱势类信号(watch/sell_weak)才升级为卖出
                    confirmed = (code, "sell_weak") in prev_sigs or (code, "watch") in prev_sigs
                    if dyn["weak_confirm_days"] <= 1 or confirmed:
                        if not _cooled(code, "sell_weak"):
                            items.append({"ts_code": code, "name": h["name"], "action": "sell_weak",
                                          "reason": f"弱势: 分位 {pct:.0f} < {dyn['weak_pct']:.0f} "
                                                    f"且回撤 {ret:.1%} (连续确认)"})
                    else:
                        items.append({"ts_code": code, "name": h["name"], "action": "watch",
                                      "reason": f"弱势观察(确认中): 分位 {pct:.0f}, 回撤 {ret:.1%}"})
                else:
                    items.append({"ts_code": code, "name": h["name"], "action": "watch",
                                  "reason": f"弱势观察: 分位 {pct:.0f} (回撤 {ret:.1%} 未达替换线)"})

        # ---------- 2) 候选名单 ----------
        cand = scores.nlargest(dyn["candidate_top"], "score")
        cand = cand[~cand.index.isin(holds.index)]
        # 买回锁(2026-09-30): 冷却期内卖出成交过的股票不得作为候选/加仓买回
        #(止损/弱势/减仓/方案调出刚执行的票, 不许被自己的候选路径推翻)
        sold_recent = _sold_recent_codes(conn, trade_date, dyn["cooldown_days"])
        if sold_recent:
            cand = cand[~cand.index.isin(sold_recent)]
        # 2026-09-04 审查 P2-2: 买侧剔除的是涨停(竞价买不进), 不是跌停(卖不出≠买不进)
        # 2026-09-07 全市场扩展: 涨停判定按板块限幅用前收盘计算(创业/科创 20%、北交 30%),
        # 与执行引擎同公式
        if "pre_close" in daily.columns and daily["pre_close"].notna().any():
            # 2026-09-08 审查: 原 daily["ts_code"] KeyError(动态层一开即崩) —— 只修了一处;
            # 2026-09-13 独立审查 #3 抓到漏网的同一类 bug, 现已抽成 _limit_up_blocked() 并加测试。
            # 涨跌停价用交易所四舍五入(round2, 与执行引擎同口径); 判定传 trade_date + 当日名称
            # (创业板 2020-08-24 前 ±10%; 主板 ST 2026-07-06 起 ±10%; S股 ±5%; 退市整理期走
            # 板块常态档 —— 实盘恒为今天, 对生产逐位无影响, 历史回放才是修正)。
            blocked = _limit_up_blocked(daily, trade_date, nl)
            cand = cand[~cand.index.isin(blocked)]
        for code, r in cand.iterrows():
            items.append({"ts_code": code, "name": r["name"], "action": "buy_candidate",
                          "reason": f"新进前{dyn['candidate_top']}: 分位 {r['score']:.1f} ({r['industry']})"})

        # ---------- 3) 市场状态切换 ----------
        s0 = st_plan[0] if st_plan else "neutral"
        s1 = st_now[0] if st_now else "neutral"
        wmap = cfg.get("regime_equity_weight", {}).get(profile, {})
        w0 = wmap.get(s0, 0.8)
        w1 = wmap.get(s1, 0.8)
        # 2026-09-10 恒定权益仓位(0.873)下 w0==w1 恒成立 —— 原条件只看 s1!=s0, 会走进 else
        # 分支算出 k=max(1, round(0))=1, 每次状态切换凭空发一条「权益仓位 87%→87%, 可加仓」
        # 的假信号(且 has_action=True 触发邮件)。去掉 regime 择时后状态切换本身不再意味着
        # 调仓, 故权重无差异时整段不发信号。
        if s1 != s0 and abs(w1 - w0) > 1e-9:
            if w1 < w0:
                # 降级: 减持仓中打分最低的几只
                k = max(1, round((w0 - w1) * len(holds)))
                weak = scores.reindex(holds.index).nsmallest(k, "score")
                for code, r in weak.iterrows():
                    if not _cooled(code, "reduce"):
                        items.append({"ts_code": code, "name": holds.loc[code, "name"], "action": "reduce",
                                      "reason": f"状态 {s0}→{s1}, 权益仓位 {w0:.0%}→{w1:.0%}, 减仓评分最低的{k}只之一"})
            else:
                # 升级: 加仓候选
                k = max(1, round((w1 - w0) * len(holds)))
                for code, r in cand.head(k).iterrows():
                    if not _cooled(code, "add"):
                        items.append({"ts_code": code, "name": r["name"], "action": "add",
                                      "reason": f"状态 {s0}→{s1}, 权益仓位 {w0:.0%}→{w1:.0%}, 可加仓"})

        # ---------- 入库 ----------
        # 候选/观察不触发邮件, 只有需要动手的动作(卖出/替换/减加仓/风险)才算
        has_action = any(i["action"] in ACTION_NOW for i in items)
        conn.execute("DELETE FROM signal WHERE signal_date=?", (trade_date,))
        if items:
            df = pd.DataFrame([{**i, "signal_date": trade_date} for i in items])
            db.upsert_df(conn, df[["signal_date", "ts_code", "name", "action", "reason"]], "signal")
        conn.commit()
    finally:
        conn.close()
    log.info("动态监控完成: %s, %d 条信号, 有动作=%s", trade_date, len(items), has_action)
    return {"trade_date": trade_date, "state": s1, "has_action": has_action, "items": items}
