"""REST API"""
import json
import logging
import math
import re

import pandas as pd
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from app import config as cfg_mod
from app import db
from app.datasource import quote_client
from app.web import llm as llm_mod

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")
# LLM 网关（2026-09-16 前端重构）：/api/llm/status|ask|factor_review，配置住 .env
router.include_router(llm_mod.router)


def _first(df, col):
    if df.empty or pd.isna(df[col].iloc[0]):
        return None
    return df[col].iloc[0]


def _clean_df(df):
    """NaN → None, 避免 FastAPI JSON 序列化(allow_nan=False)返回 500"""
    obj = df.astype(object)
    return obj.where(obj.notna(), None)


def _merge_live(positions, quotes):
    """盘中实时覆盖(仅展示, 不落库): 就地更新持仓 pnl/pnl_pct 并附加 live_price/live_pct_chg。
    返回 (market_value, quoted_at)。
    口径说明(2026-09-04 统一): 盘中无当日复权因子, 全部持仓统一按未复权现价口径 ——
    有实时行情的用实时价, 无实时行情(停牌/瞬时失败缓存)的用最近收盘价原价;
    避免除权日同表出现「实时未复权 vs EOD后复权」两套口径来回跳变。
    EOD 18:00 管线会用官方收盘价+adj 整体覆盖纠正, 此处只管盘中展示。
    ⚠️ 与 /weekly 内联的持仓实时价合并是"同机制、非同实现"(2026-10-06 审查登记,
    有意保留两份): 边界语义已各自演化 —— ①回退价字段 本函数=close vs weekly=last_px;
    ②成本键 avg_cost vs cost; ③行情价 falsy 时 本函数仍用之(mv=0/显示0) vs weekly
    回退 last_px; ④pct_chg 缺失时 本函数写 None 键 vs weekly 不写键; ⑤本函数独有
    quoted_at 汇总。改任何一处前必须对照另一处逐项核对(响应逐字节等价是硬要求)。
    """
    mv = 0.0
    quoted_at = None
    for p in positions:
        q = quotes.get(p["ts_code"])
        price = q["price"] if q else p["close"]
        if q:
            p["live_price"] = round(price, 3)
            p["live_pct_chg"] = round(q["pct_chg"], 2) if q.get("pct_chg") is not None else None
            t = q.get("time")
            if t and (quoted_at is None or t > quoted_at):
                quoted_at = t
        if price:
            mv += p["shares"] * price
            if p["avg_cost"]:
                p["pnl"] = round(p["shares"] * (price - p["avg_cost"]), 2)
                p["pnl_pct"] = round((price / p["avg_cost"] - 1) * 100, 2)
    if quoted_at and len(quoted_at) >= 14:
        quoted_at = f"{quoted_at[8:10]}:{quoted_at[10:12]}:{quoted_at[12:14]}"
    else:
        quoted_at = None
    return round(mv, 2), quoted_at


@router.get("/overview")
def overview():
    # 2026-09-05 第二轮复查 N1: 生产基线固定 —— 看板不得自动取最新实验 run,
    # config.backtest_run 指定(缺省 "latest" 时退回最新)
    cfg = cfg_mod.load_config()
    pin = cfg.get("backtest_run") or "latest"
    conn = db.get_conn()
    try:
        st = pd.read_sql("SELECT * FROM regime ORDER BY trade_date DESC LIMIT 1", conn)
        plan_d = _first(pd.read_sql("SELECT MAX(plan_date) AS d FROM plan", conn), "d")
        score_d = _first(pd.read_sql("SELECT MAX(trade_date) AS d FROM daily_score", conn), "d")
        if score_d is None:                          # 无日频推分时退回月度表
            score_d = _first(pd.read_sql("SELECT MAX(trade_date) AS d FROM model_score", conn), "d")
        data_d = _first(pd.read_sql("SELECT MAX(trade_date) AS d FROM daily", conn), "d")
        # 第七轮复查 N7-3: pin 指定 run 缺失时静默回退最新 run —— 返回标记让前端提示
        # 2026-09-20 三补二十七 §④ 根治: **配了 pin 却查不到 ⇒ 不再退回"最新一行"**。
        # 库里现在同时躺着生产 pin 行与几十行实验臂(`20260919_b1tr_*`/`*_b1real_*`/`abuw_*`),
        # 而 `ORDER BY run_date DESC LIMIT 1` 按字典序会把**实验臂**当成基线显示 —— 一次手滑
        # 改 config 或误删 pin 行, 看板就换成一个从没判决过的臂, 且没有任何地方报警。
        # 宁可空块 + pin_missed=True(宁红不假绿), 也不猜一个臂。
        pin_missed = False
        bt = pd.DataFrame(columns=["metrics"])
        if pin == "latest":
            bt = pd.read_sql("SELECT metrics FROM backtest_summary "
                             "ORDER BY run_date DESC LIMIT 1", conn)
        else:
            bt = pd.read_sql("SELECT metrics FROM backtest_summary WHERE run_date=?",
                             conn, params=(pin,))
            if bt.empty:
                pin_missed = True
    finally:
        conn.close()
    profile = cfg.get("risk_profile", "balanced")
    state = st["state"].iloc[0] if not st.empty else "neutral"
    eq_w = cfg.get("regime_equity_weight", {}).get(profile, {}).get(state)
    metrics = json.loads(bt["metrics"].iloc[0]) if not bt.empty else {}
    return {
        "state": state,
        # 2026-09-28 审查: 与相邻 drawdown 同款 NaN 守卫 —— regime.score 为 NULL 时
        # float(nan) 会让 allow_nan=False 的 JSON 序列化 500, 而看门狗探的正是本端点
        "state_score": float(st["score"].iloc[0]) if not st.empty and pd.notna(st["score"].iloc[0]) else None,
        "state_drawdown": float(st["drawdown"].iloc[0]) if not st.empty and pd.notna(st["drawdown"].iloc[0]) else None,
        "equity_weight": eq_w,
        "plan_date": plan_d,
        "score_date": score_d,
        "data_date": data_d,
        "backtest": metrics,
        "backtest_pin_missed": pin_missed,
    }


@router.get("/scores")
def scores(limit: int = 30, date: str = None, ascending: bool = False):
    # 2026-09-05 审查修复: 无校验的 limit(负数=无限行/超大值 OverflowError 500)
    limit = max(1, min(int(limit or 30), 500))
    conn = db.get_conn()
    try:
        if date is None:
            date = _first(pd.read_sql("SELECT MAX(trade_date) AS d FROM daily_score", conn), "d")
            if date is None:                         # 无日频推分时退回月度表(日期与查询表保持一致)
                date = _first(pd.read_sql("SELECT MAX(trade_date) AS d FROM model_score", conn), "d")
                src = "model_score"
            else:
                src = "daily_score"
        else:
            # 2026-09-28 审查: 显式传 date 时原硬编码 daily_score —— 拿着 /overview
            # 的 score_date(dynamic 关闭时回退 model_score 日期)查询会得到空表。
            # 与 monitor 同款: 先探日频, 无行回退月度表。
            has_ds = _first(pd.read_sql(
                "SELECT COUNT(*) AS n FROM daily_score WHERE trade_date=?", conn,
                params=(date,)), "n")
            src = "daily_score" if has_ds else "model_score"
        order = "ASC" if ascending else "DESC"
        # 2026-09-06 第十轮 N10-16: 先查一次最大行情日再作常量 JOIN(原常量子查询在
        # 千万行 daily 表上逐行求 MAX 的风险)
        max_d = _first(pd.read_sql("SELECT MAX(trade_date) AS d FROM daily", conn), "d")
        df = pd.read_sql(f"""
            SELECT s.ts_code, ROUND(s.score,1) AS score, s.pred_ret, b.name, b.industry,
                   d.close, d.pct_chg
            FROM {src} s
            LEFT JOIN stock_basic b ON s.ts_code=b.ts_code
            LEFT JOIN daily d ON d.ts_code=s.ts_code AND d.trade_date=?
            WHERE s.trade_date=? ORDER BY s.score {order} LIMIT ?
        """, conn, params=(max_d, date, limit))
    finally:
        conn.close()
    return {"date": date, "rows": _clean_df(df).to_dict("records")}  # 停牌股 LEFT JOIN 出 NULL → NaN 会 500


@router.get("/plan")
def plan(date: str = None):
    conn = db.get_conn()
    try:
        if date is None:
            date = _first(pd.read_sql("SELECT MAX(plan_date) AS d FROM plan", conn), "d")
        df = pd.read_sql(
            "SELECT * FROM plan WHERE plan_date=? ORDER BY CASE plan_type "
            "WHEN 'stock' THEN 0 WHEN 'etf' THEN 1 WHEN 'otc' THEN 2 ELSE 3 END",
            conn, params=(date,))
    finally:
        conn.close()
    return {"date": date, "rows": _clean_df(df).to_dict("records")}


@router.get("/nav")
def nav():
    # 与 /overview 同口径: 生产基线 run 固定(第二轮复查 N1)
    cfg = cfg_mod.load_config()
    pin = cfg.get("backtest_run") or "latest"
    conn = db.get_conn()
    try:
        # 与 /overview 同一根治(2026-09-20): 配了 pin 就**只**认那一行, 缺失 ⇒ run=None + 标记,
        # 不再 `MAX(run_date)` 兜底 —— 那个兜底会把最新实验臂画成生产基线曲线。
        pin_missed = False
        run = None
        if pin == "latest":
            run = _first(pd.read_sql("SELECT MAX(run_date) AS d FROM backtest_nav", conn), "d")
        else:
            run = _first(pd.read_sql("SELECT MAX(run_date) AS d FROM backtest_nav WHERE run_date=?",
                                     conn, params=(pin,)), "d")
            if not run:
                pin_missed = True
        df = pd.read_sql(
            "SELECT trade_date, nav, sys_nav, bench_nav FROM backtest_nav "
            "WHERE run_date=? ORDER BY trade_date", conn, params=(run,)) if run else \
            pd.DataFrame(columns=["trade_date", "nav", "sys_nav", "bench_nav"])
    finally:
        conn.close()
    return {"run_date": run, "pin_missed": pin_missed,
            "rows": _clean_df(df).to_dict("records")}  # 早段 bench_nav 可能为 NULL


@router.get("/style")
def style(lookback: int = 250):
    """风格读数: 池子(等权全A) vs 中证800。**描述性**, 不是信号 —— 见 app/models/style.py"""
    from app.models import style as style_mod
    # 2026-09-05 同类修复: 无校验的 lookback 会拖全表 → 夹到合理区间
    lookback = max(60, min(int(lookback or 250), 1250))
    return style_mod.load_style(lookback)


@router.get("/importance")
def importance():
    conn = db.get_conn()
    try:
        td = _first(pd.read_sql("SELECT MAX(train_date) AS d FROM model_importance", conn), "d")
        df = pd.read_sql(
            "SELECT factor, importance FROM model_importance WHERE train_date=? "
            "ORDER BY importance DESC", conn, params=(td,))
    finally:
        conn.close()
    return {"train_date": td, "rows": df.to_dict("records")}


@router.get("/lhb")
def lhb():
    """龙虎榜展示: 优先同花顺口径(人气排名/机构/游资), 无数据时回退 AkShare 口径; 附带持仓上榜标记"""
    conn = db.get_conn()
    try:
        date = _first(pd.read_sql("SELECT MAX(trade_date) AS d FROM lhb_ths", conn), "d")
        src = "lhb_ths"
        if date is None:
            date = _first(pd.read_sql("SELECT MAX(trade_date) AS d FROM lhb", conn), "d")
            src = "lhb"
        if date is None:
            return {"date": None, "source": None, "rows": [], "hot_money": []}
        plan_d = _first(pd.read_sql("SELECT MAX(plan_date) AS d FROM plan", conn), "d")
        if src == "lhb_ths":
            df = pd.read_sql("""
                SELECT t.thscode AS ts_code, t.name, t.change, t.net_value, t.hot_rank,
                       t.org_net_value, t.limit_reason, t.concepts, t.range_days,
                       CASE WHEN p.ts_code IS NOT NULL THEN 1 ELSE 0 END AS in_plan
                FROM lhb_ths t
                LEFT JOIN plan p ON p.ts_code=t.thscode AND p.plan_date=? AND p.plan_type='stock'
                WHERE t.trade_date=? AND (t.range_days=1 OR NOT EXISTS (
                    SELECT 1 FROM lhb_ths t2
                    WHERE t2.trade_date=t.trade_date AND t2.thscode=t.thscode AND t2.range_days=1))
                ORDER BY t.hot_rank ASC NULLS LAST, t.net_value DESC
            """, conn, params=(plan_d, date))
            hm = pd.read_sql("""
                SELECT hm_name, name, thscode, net_value, buying FROM lhb_ths_hm
                WHERE trade_date=? ORDER BY buying DESC
            """, conn, params=(date,))
        else:
            df = pd.read_sql("""
                SELECT l.ts_code, l.name, l.pct_chg/100 AS change, l.net_buy AS net_value,
                       NULL AS hot_rank, o.org_net_value, l.reason AS limit_reason,
                       NULL AS concepts, 1 AS range_days,
                       CASE WHEN p.ts_code IS NOT NULL THEN 1 ELSE 0 END AS in_plan
                FROM lhb l
                LEFT JOIN lhb_org o ON o.trade_date=l.trade_date AND o.ts_code=l.ts_code
                LEFT JOIN plan p ON p.ts_code=l.ts_code AND p.plan_date=? AND p.plan_type='stock'
                WHERE l.trade_date=? ORDER BY l.net_buy DESC
            """, conn, params=(plan_d, date))
            hm = pd.DataFrame()
    finally:
        conn.close()
    return {"date": date, "source": src,
            "rows": _clean_df(df).to_dict("records"),
            "hot_money": _clean_df(hm).to_dict("records")}


def _plan_payload(p: dict, exp_now: dict) -> dict:
    """方案 JSON → 看板 payload（纯函数，便于单测）。

    **预期以实时口径为准**：`expectation_at(当前配置)` 才是"现在这个敞口该有的预期"，
    而方案文件里那份是**写方案当时**的快照 ⇒ 后者挪到 `expectation_at_plan_time` 作溯源。
    为什么必须这样（2026-09-13 自查评审抓到）：方案写完后再改 `weekly.equity_weight`，
    旧实现会让看板一直挂着旧敞口的预期（配置漂移）；而且"谁测的"这类标注会跟着旧代码走
    （实测里 `source` 一直显示 F25，而数值早已换成 F52）。
    """
    out = {k: p.get(k) for k in
           ("anchor", "capital", "top_n", "band", "n_target", "n_held",
            "invested", "cash_left", "invested_ratio", "dropped")}
    out["expectation_at_plan_time"] = p.get("expectation")
    out["expectation"] = exp_now
    rows = p.get("rows") or []
    out["n_rows_total"] = len(rows)
    out["rows"] = rows[:20]
    return out


@router.get("/weekly")
def weekly():
    """周频袖套卡片（2026-09-13）：最新方案 + 独立账本 KPI。**只读，不触发任何交易**。

    健康度单独一个端点(`/weekly/health`)：它要算 12 个锚点的滚动 IC（若干次 SQL，
    实测 ~2s），不适合跟着卡片的分钟级轮询一起跑。

    表格条数是**截断展示**（方案 20 行 / 持仓 50 / 成交 30），所以同时回传总量
    (`plan.n_rows_total` 等)，让前端口径写得出「显示前 N / 共 M」，而不是让人
    以为方案只有 20 只。
    """
    from app.weekly import plan as wk_plan
    from app.weekly.ledger import Ledger
    cfg = cfg_mod.load_config()
    out = {"enabled": bool((cfg.get("weekly") or {}).get("enabled", False))}
    try:
        plans = sorted(wk_plan.PLAN_DIR.glob("plan_*.json"))
        if plans:
            p = json.loads(plans[-1].read_text(encoding="utf-8"))
            exp_now = wk_plan.expectation_at(cfg_mod.load_config())
            out["plan"] = _plan_payload(p, exp_now)
            out["plan"]["file"] = plans[-1].name
            # F28: 样本外那一档是**模块级事实**(一次性开封的判据), 不是某份方案文件的属性 ——
            # 老方案文件里没有它 ⇒ 补上, 否则看板只显示全史那一行 = 误导。
            # 2026-09-13: 改为按**当前敞口**取（expectation_at）, 且样本外那段自带
            # "只在 w=0.873 测过"的标注 —— 免得满仓运行时被读成"满仓的样本外读数"。
            out["plan"]["expectation_oos"] = exp_now.get("oos_24m")
            # F38 同理: 多重比较打折是**模块级事实**(构型从 ~25 个臂里选出), 老方案文件里没有
            out["plan"]["expectation_multiplicity"] = exp_now.get("multiplicity")
            # 敞口/口径也一并给看板(卡片要能显示"这个预期是在哪个 w 上来的")
            out["plan"]["expectation_source"] = exp_now.get("source")
            out["plan"]["expectation_weight"] = exp_now.get("equity_weight")
            # 月度口径(用户的目标函数就是"月收益") —— 老方案文件没有它 ⇒ 也从模块补
            out["plan"]["expectation_monthly"] = exp_now.get("monthly")
    except Exception as e:
        out["plan_error"] = str(e)
    # 运维状态(2026-09-13): 看板一眼能看出"下个执行档什么时候、今天该不该管" ——
    # 与 `run_weekly.py doctor` 同源(exec 看门狗 + 调度口径的下个执行日), 只读、不写盘。
    try:
        # 2026-09-28 收编 timeutil(东八区): 裸 date.today() 换机时 next_exec 判定漂一天
        from app import timeutil
        from app.weekly import doctor as wk_doctor
        _today = timeutil.date8()
        conn = db.get_conn()
        try:
            ex_day, ex_anchor = wk_doctor.next_exec(conn, _today)   # 调度口径的下个执行日
        finally:
            conn.close()
        wd = wk_doctor.check_exec_ran(cfg, _today)
        out["ops"] = {"today": _today, "next_exec": ex_day, "next_anchor": ex_anchor,
                      "exec_ok_today": wd["ok"], "exec_detail": wd["detail"]}
    except Exception as e:
        out["ops_error"] = str(e)
    try:
        # 首次执行前账本文件还不存在 ⇒ 按方案里的 capital 建临时视图(别报 0)
        led = Ledger.load(float((out.get("plan") or {}).get("capital")
                                or wk_plan.sleeve_capital(cfg)))
        out["kpi"] = led.snapshot(benchmark=True)
        out["nav"] = led.state.get("nav_history", [])[-120:]
        pos = led.state.get("positions", {})
        trades = led.state.get("trades", [])
        out["n_positions_total"] = len(pos)
        out["n_trades_total"] = len(trades)
        # 2026-09-17 用户反馈: 持仓**全量返回**（原截 50 —— 98 只小字典 ≈10KB, 截断
        # 毫无载荷收益, 反而让看板看不到全部股票）。n_positions_total 保留作兼容。
        out["positions"] = [{"ts_code": c, "shares": v.get("shares"),
                             "cost": v.get("cost"), "last_px": v.get("last_px")}
                            for c, v in list(pos.items())]
        # 2026-09-17 用户反馈: 袖套持仓表缺名称与盈亏 —— 联查名称、盘中合并实时价
        # (与月频 _merge_live 同机制: 时段门+TTL 缓存, 时段外/失败回退 last_px)、
        # 计算逐只与合计盈亏。账本 shares 会被除权缩放, 盈亏% 按价比口径展示。
        # ⚠️ 与月频 _merge_live 是"同机制、非同实现"(2026-10-06 审查登记, 有意保留
        # 两份): 差异清单见 _merge_live docstring —— 回退价/成本键/行情价 falsy 语义/
        # pct 缺失键语义/quoted_at 五处, 强行参数化合并需要 4 个开关且无法保证
        # 响应逐字节等价; 改任何一处前必须对照另一处。
        _pos = out["positions"]
        if _pos:
            _codes = [p["ts_code"] for p in _pos]
            with db.conn_ctx() as conn_n:
                _names = dict(conn_n.execute(
                    f"SELECT ts_code, name FROM stock_basic WHERE ts_code IN "
                    f"({','.join('?' * len(_codes))})", _codes).fetchall())
            _live = None
            try:
                from app.datasource.quote_client import get_quotes
                _live = get_quotes(_codes)
            except Exception:
                _live = None
            _mv = _pnl = 0.0
            for p in _pos:
                p["name"] = _names.get(p["ts_code"], "")
                q = (_live or {}).get(p["ts_code"])
                if q and q.get("price"):
                    p["live_price"] = round(q["price"], 3)
                    if q.get("pct_chg") is not None:
                        p["live_pct_chg"] = round(q["pct_chg"], 2)
                px = p.get("live_price") or p.get("last_px")
                if px and p.get("cost"):
                    p["pnl"] = round(p["shares"] * (px - p["cost"]), 2)
                    p["pnl_pct"] = round((px / p["cost"] - 1) * 100, 2)
                    _mv += p["shares"] * px
                    _pnl += p["pnl"]
            out["positions_summary"] = {
                "market_value": round(_mv, 2), "pnl": round(_pnl, 2),
                "n": len(_pos)}
        out["trades"] = trades[-30:]
    except Exception as e:
        out["kpi_error"] = str(e)
    return out


@router.get("/kg/positions_story")
def kg_positions_story(days: int = Query(90, ge=1, le=3660)):
    """两账户持仓的事件流摘要（知识图谱）。每只持仓股: 事件数/最新事件/拥挤分。只读。

    2026-09-18 审查 Q-02：`days` 必须服务端钳住 —— 这个端点是**双出口**（看板启动链 +
    智能体工具），只钳前端等于没钳；超大值会让 `kg_story.py:39` 的 `pd.Timedelta(days=)`
    抛 OverflowError，逐股 `except: continue` 把 120 行**全**吞掉 ⇒ 200 + 空列表 ⇒
    前端 `crowdRisk` 算出 hotCount=0，拥挤度卡显示「无拥挤风险」。那是假象不是结论。
    """
    from app.kg_story import get_stock_story
    codes = set()
    out = []
    conn = db.get_conn()
    try:
        codes |= {r[0] for r in conn.execute("SELECT ts_code FROM paper_position").fetchall()}
        # 2026-09-18 审查 P1-02: 原为相对路径 `Path("data/weekly/ledger.json")` —— 依赖 cwd。
        # 非项目根启动(服务/CLI/测试)时 `.exists()` 恒假 ⇒ 袖套持仓在看板里**静默消失**
        # 且无任何报错(空 dict 与"确实没有持仓"不可区分)。改用 ledger 的绝对常量。
        from app.weekly.ledger import DEFAULT_PATH as WEEKLY_LEDGER_PATH
        wk = (json.loads(WEEKLY_LEDGER_PATH.read_text(encoding="utf-8"))
              if WEEKLY_LEDGER_PATH.exists() else {})
        codes |= set((wk.get("positions") or {}).keys())
    finally:
        conn.close()
    ordered = sorted(codes)
    n_failed = 0
    for code in ordered[:120]:
        try:
            s = get_stock_story(code, days=days)
            out.append({"ts_code": code, "industry": s["industry"],
                        "n_events": s["n_events"],
                        "latest": s["events"][0] if s["events"] else None,
                        "crowd": s.get("crowd"),
                        "events": s["events"],      # 2026-09-18: 完整事件流（行级展开用）
                        "summary": s["summary"]})
        except Exception as e:
            # 失败必须留痕：静默 continue 会让"取数失败"与"真的没有事件"同形
            n_failed += 1
            log.warning("positions_story 单股取数失败 %s (days=%s): %s", code, days, e)
    # 2026-09-18 审查修：截断必须说出来（原先 [:120] 后的丢弃是静默的）
    # 同日：失败也必须说出来（原先只有截断透明，整接口失败会塌缩成空结果）
    return {"positions": out, "days": days,
            "n_total": len(ordered), "truncated": len(ordered) > 120,
            "n_failed": n_failed}


@router.get("/weekly/health")
def weekly_health(window: int = Query(12, ge=1, le=52)):
    """周频袖套健康度（滚动 IC / 换手 / 投入率 → 四档）。只读，只报警不动作。"""
    from app.weekly import health as wk_health
    return wk_health.current(window=int(window))


@router.get("/weekly/attrib")
def weekly_attrib():
    """周频袖套**损益归因**（规划 §6.1-5）。只读缓存 —— 端点本身**不跑回归**：
    归因要拉约两年行情 + 逐期横截面回归（实测 ~60s），挂在 HTTP 上会把看板卡死；
    19:45 档算一次写 `data/weekly/attrib.json`，这里只读它。
    """
    from app.weekly import attrib as wk_attrib
    res = wk_attrib.cached()
    res = dict(res)
    res["lines"] = wk_attrib.lines(res)      # 展示文案与 CLI/邮件同源
    return res


@router.get("/paper")
def paper():
    conn = db.get_conn()
    try:
        acc = pd.read_sql("SELECT * FROM paper_account WHERE id=1", conn)
        # 2026-09-06 第十轮 N10-16: 最大行情日先查一次再作常量 JOIN
        max_d = _first(pd.read_sql("SELECT MAX(trade_date) AS d FROM daily", conn), "d")
        pos = pd.read_sql("""
            SELECT p.ts_code, p.name, p.shares, p.avg_cost, p.entry_adj,
                   d.close, a.adj_factor
            FROM paper_position p
            LEFT JOIN daily d ON d.ts_code=p.ts_code AND d.trade_date=?
            LEFT JOIN adj_factor a ON a.ts_code=p.ts_code AND a.trade_date=?
        """, conn, params=(max_d, max_d))
        trades = pd.read_sql(
            "SELECT * FROM paper_trade ORDER BY exec_date DESC, ts_code LIMIT 50", conn)
        nav = pd.read_sql(
            "SELECT * FROM paper_nav ORDER BY trade_date DESC LIMIT 500", conn)
        orders = pd.read_sql(
            "SELECT ts_code, name, side, shares, reason, created_at, first_attempt "
            "FROM paper_order WHERE status='pending' ORDER BY side DESC, id LIMIT 50", conn)
        interest = _first(pd.read_sql("SELECT SUM(amount) AS s FROM paper_interest", conn), "s")
        last_date = _first(pd.read_sql("SELECT MAX(trade_date) AS d FROM daily", conn), "d")
    finally:
        conn.close()
    cash = float(acc["cash"].iloc[0]) if not acc.empty else 0.0
    initial = (float(acc["initial_capital"].iloc[0])
               if not acc.empty and pd.notna(acc["initial_capital"].iloc[0]) else None)
    interest = float(interest or 0.0)
    positions = []
    mv = 0.0
    for _, r in pos.iterrows():
        close = r["close"] if pd.notna(r["close"]) else None
        # 总收益口径估值: 当日收盘×(当日adj/开仓adj) (P0-3)
        tr = None
        if close and pd.notna(r["avg_cost"]):
            ea = r["entry_adj"] if pd.notna(r["entry_adj"]) and r["entry_adj"] else None
            if ea and pd.notna(r["adj_factor"]) and r["adj_factor"]:
                tr = close * (r["adj_factor"] / ea)
            else:
                tr = close
        if tr:
            mv += r["shares"] * tr
        positions.append({
            "ts_code": r["ts_code"], "name": r["name"], "shares": int(r["shares"]),
            "avg_cost": round(float(r["avg_cost"]), 3) if pd.notna(r["avg_cost"]) else None,
            "close": close,
            "pnl": round(r["shares"] * (tr - r["avg_cost"]), 2) if tr else None,
            "pnl_pct": round((tr / r["avg_cost"] - 1) * 100, 2) if tr and r["avg_cost"] else None,
        })
    # 盘中实时估值(仅展示): 交易时段拉腾讯批量行情, 时段外/失败自动回退收盘口径
    quotes = None
    try:
        quotes = quote_client.get_quotes(pos["ts_code"].tolist())
    except Exception:
        quotes = None  # 兜底: 行情模块异常不影响整个接口
    is_live = False
    quoted_at = None
    if quotes:
        mv, quoted_at = _merge_live(positions, quotes)
        is_live = True
    total = round(cash + mv + interest, 2)
    nav_rows = nav.iloc[::-1].to_dict("records") if not nav.empty else []
    bench0 = None
    for r in nav_rows:
        # 2026-09-05 审查修复: NaN 是真值, 原 `if r["bench_nav"]` 会把 NULL 行的 NaN
        # 当有效基准 → 全序列 bench_norm=NaN → allow_nan=False 的 JSON 序列化抛错 500
        bv = r["bench_nav"]
        if bv is not None and bv == bv and bench0 is None:
            bench0 = bv
    for r in nav_rows:
        bv = r["bench_nav"]
        ok_b = bv is not None and bv == bv and bench0 is not None and bench0 > 0
        r["bench_norm"] = round(bv / bench0, 4) if ok_b else None
        nv0 = nav_rows[0]["nav"] if nav_rows else None
        r["nav_norm"] = round(r["nav"] / nv0, 4) if (nav_rows and nv0) else None
    # trades/nav 含 None/NaN 时统一清洗, 防 JSON 序列化 500(与其余数据接口同口径)
    return {
        "created_at": acc["created_at"].iloc[0] if not acc.empty else None,
        "initial_capital": round(initial, 2) if initial is not None else None,
        "interest": round(interest, 2),
        "cash": round(cash, 2), "market_value": round(mv, 2), "total": total,
        "data_date": last_date, "is_live": is_live, "quoted_at": quoted_at,
        # 2026-09-09: 交易总闸状态(paper.paused) —— 前端据此显示"已暂停交易"提示条
        "paused": bool(cfg_mod.load_config().get("paper", {}).get("paused", False)),
        "positions": positions,
        "trades": _clean_df(pd.DataFrame(trades.to_dict("records"))).to_dict("records"),
        "nav": _clean_df(pd.DataFrame(nav_rows)).to_dict("records"),
        "orders": _clean_df(pd.DataFrame(orders.to_dict("records"))).to_dict("records"),
    }


@router.get("/signal")
def signal():
    conn = db.get_conn()
    try:
        # 2026-09-06 第十三轮 R13-7: 只取最近 60 个信号日(原全表, 数年后内存放大)
        recent = pd.read_sql(
            "SELECT DISTINCT signal_date FROM signal ORDER BY signal_date DESC LIMIT 60",
            conn)["signal_date"]
        if recent.empty:
            df = pd.DataFrame()
        else:
            q = ",".join("?" * len(recent))
            df = pd.read_sql(
                f"SELECT * FROM signal WHERE signal_date IN ({q}) "
                "ORDER BY signal_date DESC, ts_code", conn, params=tuple(recent))
    finally:
        conn.close()
    if df.empty:
        return {"date": None, "rows": [], "history": []}
    latest = df["signal_date"].iloc[0]
    rows = _clean_df(df[df["signal_date"] == latest]).to_dict("records")
    history = _clean_df(df[df["signal_date"] != latest].head(200)).to_dict("records")
    return {"date": latest, "rows": rows, "history": history}


@router.get("/status")
def status():
    # 2026-09-14：`data_status.rows` 这一列**按表不同**：走 `sync_daily_bulk` 的表（日线族）
    # 存的是"本次同步了**几天**"（`updater.py` 的 `done += 1`），其余表存"本次写入**行数**"。
    # 前端原来一律写"行"，于是 `daily: 2` 会被读成"只写了 2 行"。这里把单位显式带出去。
    _DAY_UNIT_TABLES = {"daily", "adj_factor", "daily_basic", "fund_daily", "fund_adj",
                        "margin", "moneyflow", "hk_hold", "block_trade"}
    conn = db.get_conn()
    try:
        df = pd.read_sql("SELECT * FROM data_status ORDER BY name", conn)
        # 2026-09-16 审查 COR-02 根治：给前端**交易日历读数**——daily 表的最新数据日之后
        # 到今天为止有多少个开市日。长假（春节/国庆）该值为 0 ⇒ 行动中枢按"落后开市日数"
        # 报警就不会在长假误报（此前用日历日 ≥7 天判 P1，年年长假狼来了）。
        # 2026-09-18 邮件优化：这段口径提到 `db.open_days_since` 单一真源 —— 晨间待办邮件
        # 要用同一个数，两处各写一遍 SQL 迟早漂移。顺带把原来的 naive `date.today()`
        # 换成 timeutil 的东八区日（审查 Q-12 列过的位点之一）。
        open_days_since_daily = db.open_days_since(conn, "daily")
    finally:
        conn.close()
    rows = _clean_df(df).to_dict("records")
    for r in rows:
        r["unit"] = "day" if r.get("name") in _DAY_UNIT_TABLES else "row"
    return {"rows": rows, "open_days_since_daily": open_days_since_daily}


@router.get("/notify")
def notify_log():
    """邮件发送记录(2026-09-04 P1-11: 失败不再只有日志, Web 可见)"""
    conn = db.get_conn()
    try:
        df = pd.read_sql(
            "SELECT created_at, tag, ok, error FROM notify_log ORDER BY id DESC LIMIT 20", conn)
    finally:
        conn.close()
    return {"rows": _clean_df(df).to_dict("records")}


@router.get("/all_accounts")
def all_accounts():
    """三模型 7 纸面账户聚合(2026-10-05): fterm 双轨 + ECO-W2 三账户 + 本盘月频/周频。

    只读聚合, 数据层在 app/all_accounts.py(自 D:\\account-board 迁入);
    本盘两个账户同样走账本直读以对齐统一 schema, 实时口径仍看 /api/paper 与 /api/weekly。
    """
    from app import all_accounts as aa
    return aa.build_all()


@router.get("/model_picks")
def model_picks():
    """三模型选股榜 Top100 + 交集(2026-10-08, 用户需求: fterm/ECO-W2 对标 NEW ECO
    功能 + 三模型选股前一百名交集)。

    数据层 app/model_picks.py(纯只读): NEW ECO=daily_score 最新日 Top100,
    fterm=dlv2 EMA 平滑秩 Top100(D:\\Final state 库 kv), ECO-W2=B0 规则全序 Top100
    (sidecar 由 ECO-W2 仓规范代码离线产出并自校验)。单模型失败不影响其余。
    """
    from app import model_picks as mp
    return mp.build_picks()


@router.get("/stock")
def stock_detail(code: str = Query(..., min_length=6, max_length=10)):
    """个股详情(2026-10-05): 基础/行情/打分/龙虎榜/信号/月频交易 + 七账户持仓聚合。

    查询参数用 code= 而非路径参数: api 契约测试按 getJSON 抽 URL 与 OpenAPI 精确比对,
    /api/stock 固定路径 + query 才能两端一致(前端 api.js 同款写法)。
    数据层 app/stock_detail.py(纯只读; verdict 为纯规则读数)。
    """
    from app import stock_detail as sd
    return sd.build_stock_detail(code)


@router.get("/settings")
def get_settings():
    cfg = cfg_mod.load_config()
    # 2026-09-05 审查修复: email 被手改成非 dict(字符串/null)时 dict() 抛错 500 → 400
    raw_email = cfg.get("email", {})
    if not isinstance(raw_email, dict):
        raw_email = {}
    email = dict(raw_email)
    if email.get("auth_code"):
        email["auth_code"] = "******"
    cfg["email"] = email
    return cfg


@router.post("/settings")
def post_settings(body: dict):
    # inject_env=False: 保存时不得把 .env 注入的授权码回写 config.yaml(第二轮复查 N4)
    cfg = cfg_mod.load_config(inject_env=False)
    if "email" in body and not isinstance(body["email"], dict):
        raise HTTPException(400, "email 必须为对象")
    # 2026-09-06 第十轮 N10-30: risk_profile 枚举校验(原任意字符串静默回退 neutral 权重)
    rp = body.get("risk_profile")
    if rp is not None and rp not in ("aggressive", "balanced", "conservative"):
        raise HTTPException(400, "risk_profile 必须为 aggressive/balanced/conservative 之一")
    for k in ("capital", "risk_profile", "max_stocks", "industry_max_weight", "single_side_cost"):
        if k in body:
            cfg[k] = body[k]
    # 资金运算关键标量校验: 防误清/负数/越界导致管线崩溃或反向现金流 (审查发现)
    try:
        cap = float(cfg.get("capital"))
    except (TypeError, ValueError):
        raise HTTPException(400, "capital 必须为数字")
    if not (cap > 0 and math.isfinite(cap)):
        raise HTTPException(400, "capital 必须为正的有限数字")
    try:
        ms = int(cfg.get("max_stocks"))
    except (TypeError, ValueError):
        raise HTTPException(400, "max_stocks 必须为整数")
    if not (1 <= ms <= 50):
        raise HTTPException(400, "max_stocks 超出范围(1-50)")
    try:
        imw = float(cfg.get("industry_max_weight"))
        ssc = float(cfg.get("single_side_cost"))
    except (TypeError, ValueError):
        raise HTTPException(400, "industry_max_weight/single_side_cost 必须为数字")
    if not (0.05 <= imw <= 1.0) or not math.isfinite(imw):
        raise HTTPException(400, "industry_max_weight 超出范围(0.05-1)")
    if not (0 <= ssc <= 0.05) or not math.isfinite(ssc):
        raise HTTPException(400, "single_side_cost 超出范围(0-0.05)")
    # 2026-09-12 审查 P0-06 **补洞**(由 tests/test_review_fixes_0905.py 里被改写成"真注入
    # 哨兵值"的用例发现): 只要文件里还残留明文授权码, **任何一次保存**都得把它搬进 .env。
    # 原实现把这段清洗写在下面 `if "email" in body` 的**里面**, 于是"进了设置页、但没动
    # 邮箱那一段"这条最常见的路径上, 明文码原地留在受 git 跟踪的 config.yaml 里 —— 而这条
    # 路径恰恰是暴露窗口最长的一条(文件里已经有明文码, 用户还以为点一下保存就干净了)。
    # 只搬值, **不做字段校验**: 校验仍只在用户真的提交了 email 段时跑 —— 免得一份年代久远
    # 的配置里某个无关字段(比如老的 smtp_server 写法)让这次保存直接 400。
    _cur = cfg.get("email")
    _legacy = dict(_cur) if isinstance(_cur, dict) else {}
    if _legacy.get("auth_code"):
        cfg_mod.set_env_secret("EMAIL_AUTH_CODE", _legacy["auth_code"])
        _legacy["auth_code"] = ""
        cfg["email"] = _legacy
    if "email" in body:
        e = dict(cfg.get("email", {}))
        for k in ("enabled", "smtp_server", "smtp_port", "sender", "recipients"):
            if k in body["email"]:
                e[k] = body["email"][k]
        # 2026-09-12 审查 P0-06: 授权码**不再写 config.yaml** —— 该文件受 git 跟踪,
        # 明文存进去等于把邮箱授权码提交进仓库(设置页是最容易踩的一次: 界面上填一次就落盘)。
        # 新码写 .env 的 EMAIL_AUTH_CODE(.gitignore 覆盖); `******` 是界面回显的打码占位,
        # 原样提交表示"没改", 不当成新码。
        # 文件里残留旧码的搬迁**已经不在这个分支里**了(见上面那一块, 单一搬迁点)。若哪天有人
        # 把它挪回来, `config.save_config` 的硬拒会当场 400 —— 不会静默落盘。
        ac = body["email"].get("auth_code")
        if ac and ac != "******":
            cfg_mod.set_env_secret("EMAIL_AUTH_CODE", ac)
            e["auth_code"] = ""
        # 字段校验: 防止被篡改为任意 SMTP 主机(授权码外泄链)
        try:
            e["smtp_port"] = int(e["smtp_port"])
        except (TypeError, ValueError):
            raise HTTPException(400, "smtp_port 必须为整数")
        if not (1 <= e["smtp_port"] <= 65535):
            raise HTTPException(400, "smtp_port 超出范围")
        if e.get("smtp_server") and not re.fullmatch(r"[A-Za-z0-9.\-]{3,80}", str(e["smtp_server"])):
            raise HTTPException(400, "smtp_server 格式非法")
        rc = e.get("recipients")
        if isinstance(rc, str):
            rc = [rc]
        if not isinstance(rc, list) or not all(isinstance(x, str) and "@" in x for x in rc):
            raise HTTPException(400, "recipients 格式非法")
        e["recipients"] = rc
        cfg["email"] = e
    try:
        cfg_mod.save_config(cfg)          # 内部先 validate_config(P2-16)
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    return {"ok": True}


@router.post("/run/daily")
def run_daily(background_tasks: BackgroundTasks):
    from app.scheduler import trigger_daily
    background_tasks.add_task(trigger_daily)
    return {"ok": True, "msg": "每日更新已提交(若已有任务在运行, 本次自动跳过)"}


@router.post("/run/monthly")
def run_monthly(background_tasks: BackgroundTasks):
    from app.scheduler import trigger_monthly
    background_tasks.add_task(trigger_monthly)
    return {"ok": True, "msg": "月度全流程已提交(约30-60分钟, 若已有任务在运行, 本次自动跳过)"}
