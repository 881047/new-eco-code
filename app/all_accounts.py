r"""全账户聚合（2026-10-05）：把 fterm（D:\Final）与 ECO-W2（D:\ECO-W2）的纸面账户
只读聚合进 NEW ECO 看板，作为 `/api/all_accounts` 的数据层。

来历：这三个模型的账户原先各自有看板（fterm 8790/8791、ECO-W2 8789、本盘 8787），
2026-10-05 先建了独立工具 D:\account-board（127.0.0.1:8792）做聚合；后按用户拍板
「移植进 NEW ECO 更简单」，把该工具的适配层原样迁入本仓（sources.py → 本文件），
8792 退役。NEW ECO 自身的月频盘/周频袖套仍走 /api/paper 与 /api/weekly
（含盘中实时行情），本模块**不重复**实现这两家，只负责外部两盘的 7 个账户（2026-10-15 起 fterm 增至 4 个）。

设计约束（自 account-board 起就钉死的纪律，迁移后继续有效）：
- **纯只读**：SQLite 一律 ``file:...?mode=ro`` URI 打开，JSON 只 load 不写；
  不往三个外部项目写任何东西，也不提供写接口。
- **单源故障隔离**：build_all 对每个适配器包 try/except，单盘挂了只影响对应账户卡片。
- **防御式字段**：源账本缺字段输出 None（FastAPI 序列化 NaN 会 500，_f() 一并过滤）。
- fterm 持仓不入库现价（以权益曲线为准）、ECO-W2 CB 账本只有每期 nav_est ——
  如实展示 None，不编数。
- 每日更新链路在外部项目自己的计划任务里（fterm_daily 20:05、ECO-W2_*_paper_daily
  20:30/20:35），本模块只读落账结果，不参与计算。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from datetime import date

# ---------------------------------------------------------------- 路径注册表

NEWECO_DB = r"D:\NEW ECO\data\neweco.db"
NEWECO_WEEKLY_LEDGER = r"D:\NEW ECO\data\weekly\ledger.json"
FTERM_W2_DB = r"D:\Final\data\state\fterm.db"
FTERM_T3_DB = r"D:\Final\data\state\fterm_100w.db"
FTERM_BLEND_DB = r"D:\Final\data\state\fterm_blend.db"
FTERM_K100_DB = r"D:\Final\data\state\fterm_k100.db"
FTERM_CONFIG = r"D:\Final\config.yaml"
ECOW2_MINI_LEDGER = r"D:\ECO-W2\data\mini\ledger.json"
ECOW2_MINI_RUNS = r"D:\ECO-W2\data\mini\runs"

# 账户描述符是 id/name/model 的唯一出处；适配器从描述符取，失败兜底也用它。
SOURCES = [
    ("neweco_monthly", [{"id": "neweco-monthly", "name": "NEW ECO 月频盘", "model": "NEW ECO"}]),
    ("neweco_weekly", [{"id": "neweco-weekly", "name": "NEW ECO 周频袖套", "model": "NEW ECO"}]),
    # 2026-10-08 三盘面改名：model 键 fterm→Final、ECO-W2→W2（与前端 acctCols.MODEL_ORDER
    # 及 model_picks.MODEL_KEYS 三方同源，见 tests/test_web_static_contract.py 契约 ⑱）。
    # 账户 id 与 loader tuple 键保持原样——id 是数据查找键，改名风险大于收益。
    # name 去掉 "fterm-w2" 里的 w2 字样：它与 W2 盘面（D:\ECO-W2）同名易混，两者完全不同。
    ("fterm_w2", [{"id": "fterm-w2", "name": "Final 2万档", "model": "Final"}]),
    ("fterm_t3", [{"id": "fterm-t3", "name": "Final T3 100万验证档", "model": "Final"}]),
    # 2026-10-15 接入两个新影子/观测轨（用户拍板「把 final 四个账本同步到前端」）：
    # w2_blend = #124 采纳的两流等权 D 影子轨；k100 = B8 宽组合观测轨。
    # 二者均为只读聚合（本模块纪律不变），账本由 fterm 自己的 20:05 日任务落账。
    ("fterm_blend", [{"id": "fterm-blend", "name": "Final 两流混合影子轨", "model": "Final"}]),
    ("fterm_k100", [{"id": "fterm-k100", "name": "Final k100 观测轨", "model": "Final"}]),
    ("ecow2_stock", [
        {"id": "ecow2-k6", "name": "W2 B0-k6", "model": "W2"},
        {"id": "ecow2-k8", "name": "W2 B0-k8", "model": "W2"},
    ]),
]

# ---------------------------------------------------------------- 小工具


def _f(x):
    """转 float，None/NaN/异常一律返回 None（JSON 序列化禁 NaN）。"""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


def _r2(x):
    v = _f(x)
    return None if v is None else round(v, 2)


def _norm_date(s):
    """'20260930' / '2026-09-30' / '2026-10-05T16:09:21' -> 'YYYY-MM-DD' 或 None。"""
    if not s:
        return None
    s = str(s).split("T")[0].split(" ")[0]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _parse_date(s):
    d = _norm_date(s)
    if not d or len(d) != 10:
        return None
    try:
        return date(int(d[:4]), int(d[5:7]), int(d[8:10]))
    except ValueError:
        return None


def _ro(db_path):
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


_names_cache = {"ts": 0.0, "val": {}}
_NAMES_TTL = 600.0


def _names():
    """code → name（本仓 stock_basic，5911 行全量进内存，TTL 10 分钟）。

    2026-10-08 对标升级：fterm/ECO-W2 的持仓与选股榜此前只有代码无名称
    （name=None）；代码格式与 ts_code 一致，直接查本仓 stock_basic 补齐。
    非股票代码查不到 → None（前端显示代码本身）。
    """
    if _names_cache["val"] and time.time() - _names_cache["ts"] < _NAMES_TTL:
        return _names_cache["val"]
    out = {}
    try:
        con = _ro(NEWECO_DB)
        try:
            for c, n in con.execute("SELECT ts_code, name FROM stock_basic"):
                out[c] = n
        finally:
            con.close()
    except Exception:
        pass  # 名称属增强字段，失败不影响账户本体
    _names_cache["ts"] = time.time()
    _names_cache["val"] = out
    return out


def _mk_account(desc, *, capital, equity, cash, pos_value, points,
                positions, trades, asof, updated=None, extra=None):
    """把各家原始数据装进统一 schema；points=[(date_str, nav)] 升序。

    归一基准优先用本金 capital（建仓口径），缺失时退回曲线首点；
    否则 ECO-W2 这类首期点为成交后净值（nav_post）的账本会少算费用损耗。
    """
    points = [(d, nav) for d, nav in points if _norm_date(d)]
    base = _f(capital) or (_f(points[0][1]) if points else None)
    curve = []
    peak, dd = None, 0.0
    for d, nav in points:
        nav_f, base_f = _f(nav), _f(base)
        pct = 0.0 if not base_f else (nav_f / base_f - 1.0) * 100.0
        curve.append([_norm_date(d), round(pct, 3)])
        if nav_f is not None:
            peak = nav_f if peak is None else max(peak, nav_f)
            if peak:
                dd = max(dd, (1.0 - nav_f / peak) * 100.0)
    return {
        **desc,
        "ok": True,
        "capital": _r2(capital),
        "equity": _r2(equity),
        "cash": _r2(cash),
        "pos_value": _r2(pos_value),
        "pct": curve[-1][1] if curve else None,
        "dd_pct": round(dd, 2),
        "days": len(curve),
        "curve": curve,
        "positions": positions,
        "trades": trades,
        "extra": extra or {},
        "asof": _norm_date(asof),
        "updated": _norm_date(updated) or _norm_date(asof),
        "stale": False,
        "error": None,
    }


def _pos(code, name, shares, cost, last):
    shares, last = _f(shares), _f(last)
    value = shares * last if shares is not None and last is not None else None
    return {"code": code, "name": name, "shares": shares, "cost": _f(cost),
            "last": last, "value": _r2(value)}


def _trade(date_, code, name, side, shares, price, amount, reason):
    shares, price = _f(shares), _f(price)
    if amount is None and shares is not None and price is not None:
        amount = shares * price
    return {"date": _norm_date(date_), "code": code, "name": name, "side": side,
            "shares": shares, "price": price, "amount": _r2(amount), "reason": reason}


# ---------------------------------------------------------------- NEW ECO 月频盘

# daily 表现价查询的进程内 TTL 缓存（轮询期间少打扰 11G 老库）
_close_cache = {}
_CLOSE_TTL = 300.0


def neweco_monthly(descs):
    (desc,) = descs
    con = _ro(NEWECO_DB)
    try:
        row = con.execute(
            "select cash, initial_capital from paper_account where id=1").fetchone()
        nav_rows = con.execute(
            "select trade_date, nav, cash, market_value, bench_nav"
            " from paper_nav order by trade_date").fetchall()
        pos_rows = con.execute(
            "select ts_code, name, shares, avg_cost from paper_position order by ts_code").fetchall()
        trade_rows = con.execute(
            "select exec_date, ts_code, name, side, shares, price, amount, reason"
            " from paper_trade order by exec_date desc, id desc limit 30").fetchall()
        order_rows = con.execute(
            "select order_date, ts_code, name, side, shares, reason"
            " from paper_order where status='pending' order by id").fetchall()

        now = time.time()
        positions = []
        for ts_code, name, shares, avg_cost in pos_rows:
            hit = _close_cache.get((NEWECO_DB, ts_code))
            if not hit or now - hit[0] > _CLOSE_TTL:
                hit = (now, con.execute(
                    "select trade_date, close from daily where ts_code=?"
                    " order by trade_date desc limit 1", (ts_code,)).fetchone())
                _close_cache[(NEWECO_DB, ts_code)] = hit
            last = _f(hit[1][1]) if hit[1] else None
            positions.append(_pos(ts_code, name, shares, avg_cost, last))
    finally:
        con.close()

    if not nav_rows:
        raise RuntimeError("paper_nav 为空, 无净值可展示")
    asof, nav, cash, mv, _bench = nav_rows[-1]
    points = [(d, n) for d, n, _, _, _ in nav_rows]
    # 基准曲线：bench_nav 以自身首点归一（价格指数口径，TradingView 的模拟盘图同尺，
    # 与回测图的全收益 H00906.CSI **不同尺**——两把尺不许统一，见契约测试 §两把基准尺）。
    bench_base = _f(nav_rows[0][4])
    bench_curve = [[_norm_date(d), round((_f(b) / bench_base - 1.0) * 100.0, 3)]
                   for d, _, _, _, b in nav_rows
                   if bench_base and _f(b) is not None]
    extra = {"orders": [_trade(d, c, n, s, sh, None, None, r)
                        for d, c, n, s, sh, r in order_rows],
             "bench_curve": bench_curve,
             "bench_name": "中证800价格指数（paper_nav.bench_nav，自首点归一）"}
    return [_mk_account(desc, capital=row[1] if row else None, equity=nav, cash=cash,
                        pos_value=mv, points=points, positions=positions,
                        trades=[_trade(*t) for t in trade_rows], asof=asof, extra=extra)]


# ---------------------------------------------------------------- NEW ECO 周频袖套


def neweco_weekly(descs):
    (desc,) = descs
    with open(NEWECO_WEEKLY_LEDGER, encoding="utf-8") as fh:
        w = json.load(fh)
    hist = w.get("nav_history") or []
    if not hist:
        raise RuntimeError("nav_history 为空")
    points = [(h.get("date"), h.get("nav")) for h in hist]
    last = hist[-1]
    positions = [_pos(code, None, p.get("shares"), p.get("cost"), p.get("last_px"))
                 for code, p in (w.get("positions") or {}).items()]
    positions.sort(key=lambda x: x["code"])
    trades = [_trade(t.get("date"), t.get("ts_code"), None, t.get("side"),
                     t.get("shares"), t.get("price"), None, t.get("reason"))
              for t in (w.get("trades") or [])][-30:]
    trades.reverse()
    extra = {"note": "周频袖套独立账本(JSON), 与月频 paper_* 表互不污染；"
                     "实时明细以 /api/weekly 为准（含盘中估值）"}
    return [_mk_account(desc, capital=w.get("capital"), equity=last.get("nav"),
                        cash=w.get("cash"), pos_value=last.get("invested"),
                        points=points, positions=positions, trades=trades,
                        asof=w.get("last_mark") or last.get("date"), extra=extra)]


# ---------------------------------------------------------------- fterm 双轨

_limits_cache = {"ts": 0.0, "val": None}


def _fterm_limits():
    """从 fterm config.yaml 风控段抠标量（fterm 旧看板 /api/summary 的 limits 同源）。

    刻意不引 PyYAML：只匹配 `key: 数字` 行，抠不到的字段直接不出现。
    """
    if _limits_cache["val"] and time.time() - _limits_cache["ts"] < 600:
        return _limits_cache["val"]
    out = {}
    try:
        text = open(FTERM_CONFIG, encoding="utf-8").read()
        for key in ("max_cash_util", "risk_per_trade", "daily_stop", "total_dd_halt"):
            m = re.search(rf"^\s*{key}:\s*([0-9.]+)\s*(?:#|$)", text, re.M)
            if m:
                out[key] = float(m.group(1))
    except OSError:
        pass
    _limits_cache["ts"] = time.time()
    _limits_cache["val"] = out
    return out


def _fterm_track(desc, db_path):
    con = _ro(db_path)
    try:
        eq_rows = con.execute(
            "select date, equity, pos_value, cash from equity_curve order by date").fetchall()
        pos_rows = con.execute("select code, lots, entry_price from positions").fetchall()
        fill_rows = con.execute(
            "select ts, product, side, lots, price, fee from fills order by ts desc limit 30").fetchall()
        pending_rows = con.execute(
            "select id, code, side, lots, reason, created from pending_orders order by id").fetchall()
        alert_rows = con.execute(
            "select ts, level, code, message from alerts order by ts desc limit 20").fetchall()
        decision_rows = con.execute(
            "select date, product, direction, approved, reason from decisions"
            " order by date desc, rowid desc limit 15").fetchall()
        hb = con.execute("select ts, note from heartbeat where id=1").fetchone()
    finally:
        con.close()
    if not eq_rows:
        raise RuntimeError(f"equity_curve 为空: {db_path}")
    asof, equity, pos_value, cash = eq_rows[-1]
    points = [(d, e) for d, e, _, _ in eq_rows]
    # 持仓增强（2026-10-08 对标 NEW ECO 模型页）：现价查本仓 daily 最新收盘
    # （代码格式 ts_code 一致；单连接批量查，复用 _close_cache 进程内 TTL），
    # 名称查 stock_basic。查不到（如停牌久缺日线）→ None，如实展示。
    now = time.time()
    names = _names()
    positions = []
    if pos_rows:
        # 2026-10-13：现价属**增强字段**（与 _names() 同口径，见其 docstring）——
        # 本仓 daily 不可读时必须降级为 None，而不是让整个 fterm 账户失败。
        # 原实现 `ncon = _ro(NEWECO_DB)` 写在 try 之外 ⇒ 库不可读时抛穿整个 loader，
        # 破坏「单源故障隔离」（tests/test_all_accounts.py::TestFaultIsolation 钉死此不变量：
        # 一个源坏掉不许影响其它账户）。
        try:
            ncon = _ro(NEWECO_DB)
        except Exception:
            ncon = None
        try:
            for code, lots, entry in pos_rows:
                last = None
                if ncon is not None:
                    hit = _close_cache.get((NEWECO_DB, code))
                    if not hit or now - hit[0] > _CLOSE_TTL:
                        hit = (now, ncon.execute(
                            "select trade_date, close from daily where ts_code=?"
                            " order by trade_date desc limit 1", (code,)).fetchone())
                        _close_cache[(NEWECO_DB, code)] = hit
                    last = _f(hit[1][1]) if hit[1] else None
                positions.append(_pos(code, names.get(code), (lots or 0) * 100.0,
                                      entry, last))
        finally:
            if ncon is not None:
                ncon.close()
    trades = [_trade(ts, product, None, side, (lots or 0) * 100.0,
                     price, None, f"fee={fee}" if fee is not None else None)
              for ts, product, side, lots, price, fee in fill_rows]
    limits = _fterm_limits()
    halt_ratio = limits.get("total_dd_halt")
    halt_line = round(equity * (1.0 - halt_ratio), 2) if halt_ratio and _f(equity) else None
    extra = {"orders": [_trade(None, code, None, side, (lots or 0) * 100.0, None,
                               None, reason) for _, code, side, lots, reason, _ in pending_rows],
             "alerts": [{"ts": ts, "level": lv, "code": code, "message": msg}
                        for ts, lv, code, msg in alert_rows],
             "decisions": [{"date": _norm_date(d), "code": p, "direction": dr,
                            "approved": bool(ap) if ap is not None else None,
                            "reason": r} for d, p, dr, ap, r in decision_rows],
             "limits": limits,
             "halt_line": halt_line,
             "heartbeat": f"{hb[0]} {hb[1]}" if hb else None,
             "note": "lots 已按 ×100 股换算; 持仓现价不入库, 以权益曲线为准"}
    return _mk_account(desc, capital=points[0][1], equity=equity, cash=cash,
                       pos_value=pos_value, points=points, positions=positions,
                       trades=trades, asof=asof,
                       updated=hb[0] if hb else asof, extra=extra)


def fterm_w2(descs):
    return [_fterm_track(descs[0], FTERM_W2_DB)]


def fterm_t3(descs):
    return [_fterm_track(descs[0], FTERM_T3_DB)]


def fterm_blend(descs):
    return [_fterm_track(descs[0], FTERM_BLEND_DB)]


def fterm_k100(descs):
    return [_fterm_track(descs[0], FTERM_K100_DB)]


# ---------------------------------------------------------------- ECO-W2 股票双臂


def _ecow2_runs(run_dir, n=5):
    """runs/<anchor>.json 最近 n 条：[{anchor, t1, status, reason, ran_at, n_pos}]。"""
    import os
    out = []
    try:
        names = sorted((f for f in os.listdir(run_dir) if f.endswith(".json")),
                       reverse=True)[:n]
    except OSError:
        return out
    for name in names:
        try:
            r = json.load(open(os.path.join(run_dir, name), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"anchor": r.get("anchor") or name[:-5],
                    "t1": r.get("t1"),
                    "status": r.get("status"),
                    "reason": r.get("reason"),
                    "ran_at": r.get("ran_at") or r.get("executed_on"),
                    "n_pos": r.get("n_pos")})
    return out


def ecow2_stock(descs):
    with open(ECOW2_MINI_LEDGER, encoding="utf-8") as fh:
        d = json.load(fh)
    arms = d.get("arms") or {}
    periods_by_arm = d.get("periods") or {}
    trades_by_arm = d.get("trades") or {}
    runs = _ecow2_runs(ECOW2_MINI_RUNS)
    schedule = sorted(({"iso": iso, "anchor": anchor}
                       for iso, anchor in (d.get("weeks") or {}).items()),
                      key=lambda x: x["anchor"], reverse=True)
    out = []
    for desc in descs:
        arm_id = desc["id"].rsplit("-", 1)[-1]  # ecow2-k6 -> k6
        arm = arms.get(arm_id)
        if not arm:
            raise RuntimeError(f"账本缺少臂 {arm_id}")
        b = arm.get("broker") or {}
        nav = _f(arm.get("nav", b.get("nav")))
        cash = _f(b.get("cash"))
        periods = periods_by_arm.get(arm_id) or []
        points = [(p.get("t1") or p.get("anchor"),
                   p.get("nav_eop") if _f(p.get("nav_eop")) is not None else p.get("nav_post"))
                  for p in periods]
        if not points and nav is not None:
            points = [(d.get("last_step", {}).get(arm_id), nav)]
        positions = []
        names = _names()
        for p in (b.get("positions") or []):
            item = _pos(p.get("code"), names.get(p.get("code")), p.get("shares"),
                        p.get("entry_px"), p.get("last_px"))
            item["stuck"] = bool(p.get("stuck"))
            positions.append(item)
        trades = [_trade(t.get("date"), t.get("ts_code"), None, t.get("side"),
                         t.get("shares"), t.get("price"), t.get("amount"),
                         t.get("reason")) for t in trades_by_arm.get(arm_id, [])][-30:]
        trades.reverse()
        circuit = d.get("circuit") or {}
        extra = {"circuit": {k: circuit.get(k) for k in
                             ("halted", "derisked", "cautious", "peak_nav", "c1_at")},
                 # 逐期表（ECO-W2 旧 panel 的 periods 视图）：最新在前，取 8 期；
                 # 最新一期带 picks 名单（2026-10-08 对标：当期选股名单展示）
                 "periods": [{"anchor": p.get("anchor"), "t1": p.get("t1"),
                              "nav_bop": _r2(p.get("nav_bop")),
                              "nav_post": _r2(p.get("nav_post")),
                              "nav_eop": _r2(p.get("nav_eop")),
                              "n_pos": p.get("n_pos"),
                              "n_new": p.get("n_new_trades"),
                              "allow_buys": p.get("allow_buys"),
                              "circuit_status": p.get("circuit_status"),
                              "picks": len(p.get("picks") or []),
                              "pick_list": (list(p.get("picks") or [])
                                            if pi == len(periods) - 1 else None)}
                             for pi, p in zip(range(len(periods)), reversed(periods[-8:]))],
                 "runs": runs,
                 "schedule": schedule,
                 "note": f"账本 updated_at={_norm_date(d.get('updated_at'))}"}
        out.append(_mk_account(desc, capital=b.get("capital"), equity=nav, cash=cash,
                               pos_value=(nav - cash) if nav is not None and cash is not None else None,
                               points=points, positions=positions, trades=trades,
                               asof=d.get("last_step", {}).get(arm_id),
                               updated=d.get("updated_at"), extra=extra))
    return out


# ---------------------------------------------------------------- 模型元数据（门户卡片用）

#: 各模型训练/落账档位（与 app/scheduler.py 档位及三仓计划任务一致；两边改动需同步）。
MODEL_SCHEDULE = {
    "NEW ECO": ["每日 18:00 日链推分（daily_score）",
                "每日 19:30 / 20:30 / 21:30 打分保鲜",
                "每月 1 日 18:33 月度全流程（trainer 逐月重训）"],
    "Final": ["每工作日 20:05 信号+落账（t+1 次日开盘价撮合）",
              "训练为离线固定版，无定时重训"],
    "W2": ["每日 20:30 股票纸面（规则臂 B0）",
               "每周五 19:00 参考净值链重建",
               "LightGBM 属 M1 实验线（离线 walk-forward，无定时训练）"],
}

FTERM_META_SIDECAR = r"D:\NEW ECO\data\fterm_model_meta.json"
ECOW2_FACTORS_JSON = r"D:\ECO-W2\data\s6\factors.json"


def _neweco_model_meta(con):
    """NEW ECO：model_meta 权威键 + model_importance 最新期（逐月重训落库的因子重要性）。"""
    meta = {}
    for key in ("factor_names:model_score", "trained_at:model_score", "data_asof:model_score"):
        row = con.execute("SELECT value FROM model_meta WHERE key=?", (key,)).fetchone()
        meta[key] = row[0] if row else None
    factors = [s.strip() for s in (meta["factor_names:model_score"] or "").split(",") if s.strip()]
    td = con.execute("SELECT MAX(train_date) FROM model_importance").fetchone()[0]
    imp = (con.execute("SELECT factor, importance FROM model_importance WHERE train_date=? "
                       "ORDER BY importance DESC", (td,)).fetchall() if td else [])
    return {"factors": factors,
            "importance": [{"factor": f, "importance": v} for f, v in imp],
            "trained_at": meta["trained_at:model_score"],
            "data_asof": meta["data_asof:model_score"],
            "factor_source": "config 全池 31 − factor_exclusions 5 = 26（model_meta 权威键）"}


def _fterm_model_meta():
    """fterm：工件元数据 sidecar（离线 torch 提取——服务进程刻意不引 torch，
    提取脚本对 data/models/dlvv2_production.pt 只读一次写成 JSON）。"""
    try:
        with open(FTERM_META_SIDECAR, encoding="utf-8") as fh:
            d = json.load(fh)
        return {"factors": d.get("feature_cols") or [],
                "trained_at": d.get("trained_at"),
                "n_nets": d.get("n_nets"), "n_samples": d.get("n_samples"),
                "factor_source": f"训练工件 dlv2_production.pt（{d.get('source_mtime', '')} 提取）"}
    except (OSError, ValueError) as e:
        return {"factors": [], "factor_source": f"元数据 sidecar 不可读: {e}"}


def _ecow2_model_meta():
    """ECO-W2：入模因子 = data/s6/factors.json::kept（M1 LightGBM 实验线）；
    生产纸面轨是规则臂 B0（低价小市值排序），不用因子 —— 如实注记，不许标成"生产模型"。"""
    try:
        with open(ECOW2_FACTORS_JSON, encoding="utf-8") as fh:
            d = json.load(fh)
        return {"factors": d.get("kept") or [],
                "trained_at": (d.get("generated_at") or "")[:10],
                "factor_source": "s6/factors.json::kept（M1 LightGBM 实验线入模因子；"
                                 "生产纸面轨为规则臂 B0，不使用因子）"}
    except (OSError, ValueError) as e:
        return {"factors": [], "factor_source": f"factors.json 不可读: {e}"}


def build_model_meta():
    """三模型门户元数据：因子清单 + 训练时间点 + 档位。单模型失败不影响其他模型。"""
    out = {}
    for model, schedule in MODEL_SCHEDULE.items():
        entry = {"schedule": schedule, "factors": [], "factor_source": "", "error": None}
        try:
            if model == "NEW ECO":
                con = _ro(NEWECO_DB)
                try:
                    entry.update(_neweco_model_meta(con))
                finally:
                    con.close()
            elif model == "Final":
                entry.update(_fterm_model_meta())
            elif model == "W2":
                entry.update(_ecow2_model_meta())
        except Exception as exc:
            entry["error"] = str(exc)[:200]
        out[model] = entry
    return out


# ---------------------------------------------------------------- 聚合

_CACHE = {"ts": 0.0, "payload": None}
_CACHE_TTL = 15.0
_ADAPTERS = {
    "neweco_monthly": neweco_monthly,
    "neweco_weekly": neweco_weekly,
    "fterm_w2": fterm_w2,
    "fterm_t3": fterm_t3,
    "fterm_blend": fterm_blend,
    "fterm_k100": fterm_k100,
    "ecow2_stock": ecow2_stock,
}


def _error_account(desc, err):
    return {**desc, "ok": False, "capital": None, "equity": None, "cash": None,
            "pos_value": None, "pct": None, "dd_pct": None, "days": 0,
            "curve": [], "positions": [],
            "trades": [], "extra": {"note": None}, "asof": None, "updated": None,
            "stale": False, "error": str(err)[:300]}


def build_all(force=False):
    """聚合全部 8 个账户；15s 进程内缓存，前端轮询不至于反复砸账本文件。"""
    now = time.time()
    if not force and _CACHE["payload"] and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["payload"]

    accounts = []
    for fn_name, descs in SOURCES:
        try:
            got = _ADAPTERS[fn_name](descs)
            if len(got) != len(descs):
                raise RuntimeError(f"{fn_name} 返回 {len(got)} 个账户, 预期 {len(descs)}")
            accounts.extend(got)
        except Exception as exc:  # 单源故障隔离
            accounts.extend(_error_account(d, exc) for d in descs)

    asofs = [_parse_date(a["asof"]) for a in accounts if a["ok"] and a["asof"]]
    newest = max(asofs) if asofs else None
    for a in accounts:
        if a["ok"] and a["asof"] and newest:
            mine = _parse_date(a["asof"])
            a["stale"] = bool(mine and (newest - mine).days >= 3)

    ok_accounts = [a for a in accounts if a["ok"]]
    by_model = {}
    for a in ok_accounts:
        m = by_model.setdefault(a["model"], {"equity": 0.0, "n": 0})
        m["equity"] = round(m["equity"] + (a["equity"] or 0.0), 2)
        m["n"] += 1
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "accounts": accounts,
        "model_meta": build_model_meta(),
        "totals": {
            "equity": round(sum(a["equity"] or 0.0 for a in ok_accounts), 2),
            "n_ok": len(ok_accounts),
            "n_total": len(accounts),
            "by_model": by_model,
        },
    }
    _CACHE["ts"] = now
    _CACHE["payload"] = payload
    return payload
