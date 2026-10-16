"""月度调仓方案构建: 股票(整手约束+行业上限) + 场内基金备选 + 场外基金 + 逆回购"""
import logging
import re

import pandas as pd

from app import config as cfg_mod
from app import db
from app import timeutil
from app.portfolio import funds, repo
from app.portfolio.sizing import allocate_lots

log = logging.getLogger(__name__)


def _top_candidates(plan_date: str, score_table: str = "model_score"):
    """股票候选截面(方案构建与影子实测共用, 2026-09-08 R11-4 实测抽出): 评分 join
    行情 → R11-3 市值/流动性过滤 → 停牌/涨停剔除 → PIT ST/退过滤 → star 开关 →
    状态自适应 top 候选。返回 (top, state, latest):
    top=按 ts_code 索引的候选(含 score/name/industry/close 等), latest=行情日线帧

    score_table(2026-09-28 打通): 原硬编码 `FROM model_score`, 导致组合层 A/B 的胜出
    影子表**只能驱动回测、不能驱动建仓** —— 判决通过的东西落不了地。现参数化,
    **缺省恒为生产表 ⇒ 生产路径逐位不变(不换 pin)**。表名用与
    engine.py:388 / trainer.py:426 / fusion.py:273 同一正则把守(sqlite 不支持表名
    占位符, 只能拼串 ⇒ 必须显式校验挡注入)。"""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", score_table):
        raise ValueError(f"非法表名: {score_table}")
    cfg = cfg_mod.load_config()
    max_stocks = int(cfg.get("max_stocks", 12))
    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        scores = pd.read_sql(
            "SELECT s.ts_code, s.score, s.pred_ret, b.name, b.industry "
            f"FROM {score_table} s LEFT JOIN stock_basic b ON s.ts_code=b.ts_code "
            "WHERE s.trade_date=?", conn, params=(plan_date,))
        # 2026-09-08 审查 P2: 取评分日时点状态(与回测同口径) —— 原取最新状态(构建日,
        # 可能晚于 plan_date), 状态切换月方案与回测的只数/仓位错位
        st = pd.read_sql("SELECT state FROM regime WHERE trade_date<=? "
                         "ORDER BY trade_date DESC LIMIT 1", conn, params=(plan_date,))
        last_date = pd.read_sql("SELECT MAX(trade_date) AS d FROM daily", conn)["d"].iloc[0]
        # 2026-09-06 一致性核查 D2b: 整手预算收盘价取 plan_date 时点(原 last_date=今天,
        # 补建历史方案时用今天价格定历史股数=轻微前视); plan_date 无行情时回退 last_date
        has_px = pd.read_sql("SELECT COUNT(*) AS n FROM daily WHERE trade_date=?", conn,
                             params=(plan_date,))["n"].iloc[0] > 0
        px_date = plan_date if has_px else last_date
        latest = pd.read_sql(
            "SELECT ts_code, close, pre_close, vol, pct_chg FROM daily WHERE trade_date=?",
            conn, params=(px_date,))
        # R11-3(2026-09-06): 市值/流动性硬过滤数据(与 trainer/daily_score 同口径);
        # 2026-09-06 第十二轮 R12-1: mv/amt 取 plan_date 时点(原 last_date —— 补建历史
        # 方案时会用今天的数据筛历史候选=轻微前视; 与同文件 ST 时名过滤的 plan_date 对齐);
        # 数据缺失(新装机/测试库)时降级跳过, 不阻断方案
        mv = pd.DataFrame()
        amt = pd.DataFrame()
        try:
            if db.table_exists(conn, "daily_basic"):
                mv = pd.read_sql("SELECT ts_code, total_mv FROM daily_basic WHERE trade_date=?",
                                 conn, params=(plan_date,))
            last20 = pd.read_sql(
                "SELECT DISTINCT trade_date FROM daily WHERE trade_date<=? "
                "ORDER BY trade_date DESC LIMIT 20", conn, params=(plan_date,))["trade_date"]
            amt = pd.read_sql(
                f"SELECT ts_code, AVG(amount) AS amt20 FROM daily WHERE trade_date IN "
                f"({','.join('?' * len(last20))}) GROUP BY ts_code",
                conn, params=tuple(last20))
        except Exception:
            mv, amt = pd.DataFrame(), pd.DataFrame()
        # 2026-09-28 审查修复(P1): daily_basic 表在而 plan_date 无行(或读取失败)时, 下游
        # 原**静默降级**为"无过滤域"选股 ⇒ 推分域(有过滤)与建仓域(无过滤)分叉 —— 与
        # daily_score P1-04 同型的静默口径漂移, 同口径改 fail-closed(宁可不出方案,
        # 不按另一种口径建仓)。表不存在(测试临时库/新装机)仍走降级, 不阻断最小化场景。
        if mv.empty and db.table_exists(conn, "daily_basic"):
            raise RuntimeError(
                f"{plan_date} daily_basic 无行情(市值过滤域缺失) —— 拒绝按无过滤域建仓, "
                f"请先同步数据(monthly_sync)后重试")
    if scores.empty:
        raise RuntimeError(f"{plan_date} 无模型打分, 请先运行训练脚本")

    state = st["state"].iloc[0] if not st.empty else "neutral"
    latest = latest.set_index("ts_code")

    sc = scores.join(latest, on="ts_code")
    # R11-3(2026-09-06): 市值/流动性硬过滤 —— 剔市值截面后 N 分位 + 近20日均额下限
    if not mv.empty and not amt.empty:
        mv_q = mv["total_mv"].quantile(float(cfg.get("stock_size_pctl", 0)))
        ok_mv = set(mv[mv["total_mv"] >= mv_q]["ts_code"])
        ok_amt = set(amt[amt["amt20"] >= float(cfg.get("stock_min_amt20", 0))]["ts_code"])
        sc = sc[sc["ts_code"].isin(ok_mv) & sc["ts_code"].isin(ok_amt)]
    else:
        log.warning("市值/流动性数据缺失, 跳过 R11-3 硬过滤(不阻断方案)")
    sc = sc[sc["close"] > 0]
    sc = sc[sc["vol"] > 0]                          # 剔除停牌
    # 剔除涨停(买不进): 按板块限幅用前收盘计算, 与执行引擎同公式
    # (2026-09-07 全市场扩展: 原固定 9.8% 主板口径, 现 创业/科创 20%、北交 30%;
    # ST 股随后即被时点名过滤剔除, 此处按普通限幅即可)
    from app.portfolio.board import board_of, limit_price, limit_ratio
    # 第十轮 N10-4: 候选剔除按方案锚定日的时点名称 PIT(原用 stock_basic 当前名称,
    # 补建历史方案时存在当前名偏差); 与 trainer/回测同口径。名称提前到限幅之前构造:
    # 限幅要用它取"退"(退市整理期 ±10%)与"S"(S股 ±5%)两档。
    from app.models.names import NameLookup
    nl = NameLookup()
    _nm = sc["ts_code"].map(lambda c: nl.name_at(c, plan_date))
    # 2026-09-12: 传 plan_date + 当日名称 —— 创业板 2020-08-24 前是 ±10%; 主板 ST
    # 2026-07-06 起 ±10%; "S" 股 ±5%; 退市整理期走板块常态档(name_of 供新老划断)。
    # 当天建方案时 plan_date 恒为今天, 故对生产逐位无影响; 补建历史方案才是修正。
    lim = sc["ts_code"].map(
        lambda c: limit_ratio(c, False, plan_date, nl.name_at(c, plan_date), nl.name_at)
    ).astype("float64")
    # 2026-09-28: astype 防御 —— pandas 3 下对**空截面**做 .map 会保留 str dtype,
    # 下面 `1 + lim` 变字符串加法抛 TypeError(应走到"无候选"的 RuntimeError 语义)
    # 2026-09-08 审查: 涨跌停价改交易所四舍五入(round2), 与执行/回测引擎同口径
    # 2026-10-05: 北交所取整接线(上轨 floor/下轨 ceil, 见 board.limit_price) ——
    # 逐行传板块; 非 BSE 板内部就是 round2, 与旧口径逐位相同。
    _limit_up = pd.Series(
        [limit_price(p, r, board_of(c)) for p, r, c in
         zip(sc["pre_close"], lim, sc["ts_code"])], index=sc.index, dtype="float64")
    sc = sc[~(sc["pre_close"] > 0) | ~(sc["close"] >= _limit_up - 0.001)]
    # 2026-09-04 审查 P2-2: 买入候选不再剔除跌停 —— 跌停是卖不出、不是买不进,
    # 跌停日反而是好买点; 真实成交约束由模拟盘执行引擎(开盘一字板判定)把关
    sc = sc[~_nm.str.contains("ST|退", na=False)]
    # 2026-09-08 用户拍板: 训练全市场、选股排除科创板(与回测引擎同口径, 见 config
    # star_enabled 注释)
    if not cfg.get("star_enabled", False):
        # 2026-09-13: 同上 —— 判据唯一来源 board.is_star（含 689 CDR），不再写字面前缀
        from app.portfolio.board import is_star
        sc = sc[~sc["ts_code"].map(is_star).fillna(False)]
    # 状态自适应只数(2026-09-05): 按市场状态取持仓只数(缺省回退 max_stocks)
    n_stocks = int(cfg.get("state_stocks", {}).get(state, max_stocks))
    # 第五轮复查 P5-1(2026-09-05): 候选池 = max(只数, stock_candidate_top)。补位机制
    # 保留但默认关闭(config=0, 实证补位在 1 万本金下恶化回撤), 与回测同一口径
    pool_n = max(n_stocks, int(cfg.get("stock_candidate_top", 0)))
    top = sc.nlargest(pool_n, "score")
    # 建仓追高剔除闸(2026-09-26 用户拍板采纳; 与回测引擎同一实现 app/portfolio/chase.py,
    # 三轮预注册 A/B 见 05 号台账 七补/九补)。只剔最终名单、不补位(=chase6 语义);
    # z 截面 = 当期锚点全量打分 ∩ PIT 可交易域(与 A/B 冻结口径一致, 不用本函数的过滤后 sc)。
    chase_cfg = cfg.get("chase") or {}
    if chase_cfg.get("enabled") and not top.empty:
        from app.factors.factor_engine import load_market
        from app.portfolio.chase import ChaseGate
        gate_ = ChaseGate(load_market(), cfg)
        _excl = gate_.exclusions(plan_date, scores.set_index("ts_code")["score"],
                                 theta=float(chase_cfg.get("theta", 2.0)))
        _n0 = len(top)
        # 2026-09-28 审查修复: 此刻 top 尚未 set_index, index 是 read_sql 的 RangeIndex ——
        # 原按 index.isin(剔除集) 过滤时 ts_code 字符串集合永远匹配不上 ⇒ chase 静默空操作。
        # 必须按 ts_code **列**过滤(与回测引擎 engine.py 同款写法)。
        top = top[~top["ts_code"].isin(_excl)]
        if len(top) < _n0:
            log.info("chase 剔除 %d 只追高候选(%s, 不补位)",
                     _n0 - len(top), plan_date)
    if top.empty:
        raise RuntimeError("无可交易的候选股票")
    # 与 sizing 的 M3 统一: shares key=ts_code(输入带 ts_code 列时 sizing 会重建索引)
    top = top.set_index("ts_code")
    return top, state, latest, last_date


def build_plan(plan_date: str = None, score_table: str = "model_score") -> dict:
    """score_table(2026-09-28 打通): 透传给 _top_candidates, 让影子表能驱动建仓。
    缺省恒为生产表 ⇒ 生产路径逐位不变(不换 pin)。"""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", score_table):
        raise ValueError(f"非法表名: {score_table}")
    cfg = cfg_mod.load_config()
    capital = float(cfg.get("capital", 100000))
    ind_cap = float(cfg.get("industry_max_weight", 0.25))
    cost = float(cfg.get("single_side_cost", 0.0015))
    profile = cfg.get("risk_profile", "balanced")

    if plan_date is None:
        # 只取已完结自然月的评分截面(2026-09-04 审查 P0-2: 月中训练会插入"伪月末"评分,
        # 直接 MAX 会把方案锚定到月中交易日)
        # 2026-09-29 收编 timeutil(东八区): 裸 date.today() 是 09-28 收编批次
        # (trainer/factor_engine/e2e)的漏网点 —— 换非东八区机器部署时"已完结自然月"
        # 判定会在月初漂一天, 方案锚定日跟着漂。本机逐位无影响。
        month_first = timeutil.today().strftime("%Y%m") + "01"
        with db.conn_ctx() as conn:
            # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
            plan_date = pd.read_sql(
                f"SELECT MAX(trade_date) AS d FROM {score_table} WHERE trade_date<?",
                conn, params=(month_first,))["d"].iloc[0]

    # ---------- 股票组合(候选构建与影子实测共用 _top_candidates) ----------
    top, state, latest, last_date = _top_candidates(plan_date, score_table)
    n_stocks = int(cfg.get("state_stocks", {}).get(state, int(cfg.get("max_stocks", 12))))

    eq_w = float(cfg.get("regime_equity_weight", {}).get(profile, {}).get(state, 0.8))
    equity_cash = capital * eq_w
    # 第三轮复查 M4: 行业上限生效值可能被自适应放宽(max(ind_cap, floor/只数)),
    # 配置页显示 25% 而实际 33.3% —— 生效值显式入日志/方案; 只数用状态自适应值
    floor_stocks = float(cfg.get("industry_floor_stocks", 2))
    eff_cap = max(ind_cap, floor_stocks / n_stocks) if ind_cap < 1.0 else 1.0
    if abs(eff_cap - ind_cap) > 1e-9:
        log.info("行业上限: 配置 %.0f%% → 生效 %.1f%% (自适应放宽 max(cap, %.1f/只数=%d))",
                 ind_cap * 100, eff_cap * 100, floor_stocks, n_stocks)
    # 整手/资金约束下的组合分配(与回测同一套机制), 自适应只数+权重重归一 (P0-1)
    alloc = allocate_lots(top, equity_cash, n_stocks,
                          ind_cap=ind_cap, cost_rate=cost, floor_stocks=floor_stocks)
    warning = None
    if alloc["n_eff"] == 0:
        # 资金/整手约束下无股票可买: 沿用上月股票组合并告警, 避免模拟盘误清仓与整月无方案
        # (2026-09-04 修复: 原先直接 raise, 月频管线无捕获 → 无方案/无邮件且补跑不重试)
        # 第十轮 N10-15 口径注: 沿用旧股数不重过 allocate_lots/不做当前资金可买性校验,
        # 可买性由模拟盘执行层 affordable 截断兜底
        with db.conn_ctx() as conn:
            # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
            prev = pd.read_sql(
                "SELECT ts_code, name, shares, weight, industry, reason FROM plan "
                "WHERE plan_type='stock' AND plan_date="
                "(SELECT MAX(plan_date) FROM plan WHERE plan_type='stock' AND plan_date<?)",
                conn, params=(plan_date,))
        stock_rows = []
        cost_est = 0.0
        if not prev.empty:
            prev = prev.join(latest[["close"]], on="ts_code")
            for _, r in prev.iterrows():
                stock_rows.append({
                    "plan_date": plan_date, "plan_type": "stock", "ts_code": r["ts_code"],
                    "name": r["name"], "weight": r["weight"],
                    "shares": int(r["shares"]), "industry": r["industry"],
                    "reason": r["reason"] + " (沿用)",
                })
                if pd.notna(r["close"]):
                    cost_est += r["shares"] * r["close"]
        warning = ("资金/整手约束下无股票可买, 已沿用上月股票组合(模拟盘不清仓)"
                   if stock_rows else "资金/整手约束下无股票可买且无上月组合, 本期仅现金/基金")
        log.error("方案 %s: %s", plan_date, warning)
        stock_cost = round(cost_est, 2)
        cash = round(capital - stock_cost, 2)
    else:
        stock_rows = []
        for code, shares in alloc["shares"].items():
            # code 即 ts_code(alloc 的 key 经 sizing M3 统一); set_index 默认 drop=True
            # 会移除 ts_code 列, 故 ts_code 用 key 而非 row["ts_code"]
            row = top.loc[code]
            stock_rows.append({
                "plan_date": plan_date, "plan_type": "stock", "ts_code": code,
                "name": row["name"], "weight": alloc["weights"].get(code),
                "shares": int(shares), "industry": row["industry"],
                "reason": f"模型打分 {row['score']:.1f} ({row['industry']})",
            })
        cash = capital - alloc["cost"]
        stock_cost = alloc["cost"]

    # 第七轮复查 N7-8 / 第八轮 N8-3: 现金拆分为主动降仓预留(1-eq_w)与整手/行业上限
    # 被动闲置, 便于解释"为什么没满仓"(meta 行入库 + 方案摘要, /api/plan 直接可见)
    cash_active = round(capital * (1 - eq_w), 2)
    cash_passive = round(cash - cash_active, 2)

    # ---------- 场内基金备选 ----------
    etf_rows = []
    etfs = funds.score_etfs(top_n=8, fund_type=None if state == "attack" else "债券型")
    if etfs.empty:
        etfs = funds.score_etfs(top_n=8)
    for _, r in etfs.iterrows():
        etf_rows.append({
            "plan_date": plan_date, "plan_type": "etf", "ts_code": r["ts_code"],
            "name": r["name"], "weight": None, "shares": None,
            # 类型单独存列(2026-09-04 审查 P2-9: 原前端 ETF 表"类型"列恒空白)
            "fund_type": r["fund_type"],
            "reason": f"动量分 {r['score']:.2f} / 3月 {r['mom60']:.1%} / {r['fund_type']}",
        })

    # ---------- 场外基金 ----------
    otc = funds.otc_fund_picks(plan_date=plan_date)  # N10-29: 成立年限按方案锚定日
    otc_rows = []
    for t, items in otc.items():
        for it in items:
            otc_rows.append({
                "plan_date": plan_date, "plan_type": "otc", "ts_code": str(it["code"]),
                "name": f"[{t}] {it['name']}", "weight": None, "shares": None,
                "reason": (f"近3年 {it['ret_3y']}% / 近1年 {it['ret_1y']}%"
                           if it.get("ret_3y") is not None else f"近1年 {it['ret_1y']}%"),
            })

    # ---------- 逆回购 ----------
    # 日历规则按方案数据日推算(2026-09-04 审查 P2-5: 原按系统"今天", 历史补跑时与方案日期错配)
    repo_r = repo.repo_advice(cash, today=plan_date)
    repo_rows = [{
        "plan_date": plan_date, "plan_type": "repo", "ts_code": "GC001",
        "name": "国债逆回购", "weight": None, "shares": None,
        "reason": f"{repo_r['desc']} (当前隔夜利率 {repo_r['rate']}%)",
    }]

    # ---------- 入库 ----------
    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        conn.execute("DELETE FROM plan WHERE plan_date=?", (plan_date,))
        all_rows = stock_rows + etf_rows + otc_rows + repo_rows
        # 第八轮 N8-3(2026-09-06): 现金拆分以 meta 行入库, /api/plan 直接可见 ——
        # 主动降仓预留(1-eq_w)与整手/行业上限被动闲置分开解释"为什么没满仓"
        all_rows.append({
            "plan_date": plan_date, "plan_type": "meta", "ts_code": "",
            "name": "", "weight": None, "shares": None,
            "industry": "",
            "reason": (f"现金 {cash:.2f} 元 = 主动降仓预留 {cash_active:.2f} + "
                       f"被动闲置 {cash_passive:.2f} (权益仓位 {eq_w:.0%})"),
        })
        db.upsert_df(conn, pd.DataFrame(all_rows), "plan")

    summary = {
        "plan_date": plan_date, "data_date": last_date, "state": state,
        "equity_weight": eq_w, "capital": capital,
        "stocks": stock_rows, "stock_cost": round(stock_cost, 2),
        "cash": round(cash, 2),
        "cash_active_reserve": cash_active, "cash_passive_idle": cash_passive,
        "repo": repo_r,
        "etfs": etf_rows, "otc": otc, "warning": warning,
        # 第五轮复查 P5-3/P5-5(2026-09-05): 目标/实际只数与生效行业上限进方案摘要
        "stock_target": n_stocks, "stock_actual": len(stock_rows), "eff_cap": eff_cap,
    }
    log.info("方案生成完成: %s, 状态=%s, 权益仓位=%.0f%%, 股票%d/%d只(实际/目标), "
             "行业上限%.1f%%, 现金%.2f元",
             plan_date, state, eq_w * 100, len(stock_rows), n_stocks, eff_cap * 100, cash)
    return summary
