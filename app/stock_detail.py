r"""个股详情聚合（2026-10-05）：`/api/stock?code=XXXX` 的数据层。

覆盖（全部只读，来自本仓 neweco.db + app/all_accounts 的七账户聚合）：
- **基础/行情**：stock_basic（名称/行业/市场）、daily 近 120 根（K 线/收盘线）、
  daily_basic 最新（换手/PE-TTM/PB/总市值/流通市值，亏损股 PE 为 NULL 由前端容错）；
- **打分**：daily_score 近 10 期（日推分）+ 当日全市场排名（COUNT(score>)+1）、
  model_score 近 5 期（月频评分）；
- **龙虎榜**：lhb 近 10 条（有 ts_code 维度；lhb_ths 键是 thscode 不按票查）；
- **信号**：signal 近 10 条（buy_candidate/watch 等）；
- **月频账户**：paper_position（当前是否持有）+ paper_trade 近 10 笔；
- **跨账户矩阵**：七账户里谁持有该票、谁的成交史里有它（来自 all_accounts.build_all，
  与 /#/accounts 同源）。

verdict 是**纯规则读数**（确定性可复算，口径全部来自上文查询，不引入任何模型推断）——
与 intelligence 规则引擎同一文化：每句话都能指出数据出处。
"""
from __future__ import annotations

import logging

from app import db
from app.all_accounts import build_all

log = logging.getLogger(__name__)


def _yuan_wan(x):
    """龙虎榜净买额（元）→ 万元带符号读数。"""
    if x is None:
        return "--"
    return f"{x / 1e4:+,.0f} 万"


def build_stock_detail(code: str, payload: dict = None) -> dict:
    """聚合单票详情。code 必填；payload 可注入（测试用），缺省取七账户聚合。"""
    code = (code or "").strip()
    if not code:
        raise ValueError("code 必填")
    payload = payload if payload is not None else build_all()

    conn = db.get_conn()
    try:
        basic = conn.execute(
            "SELECT ts_code, name, industry, market, list_date FROM stock_basic WHERE ts_code=?",
            (code,)).fetchone()
        kline = conn.execute(
            "SELECT trade_date, open, high, low, close, pre_close, pct_chg, vol, amount"
            " FROM daily WHERE ts_code=? ORDER BY trade_date DESC LIMIT 120",
            (code,)).fetchall()
        dbasic = conn.execute(
            "SELECT turnover_rate, pe_ttm, pb, total_mv, circ_mv FROM daily_basic"
            " WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1", (code,)).fetchone()
        ds = conn.execute(
            "SELECT trade_date, score, pred_ret FROM daily_score WHERE ts_code=?"
            " ORDER BY trade_date DESC LIMIT 10", (code,)).fetchall()
        rank = n_pool = None
        if ds:
            td, sc = ds[0][0], ds[0][1]
            rank = conn.execute(
                "SELECT COUNT(*)+1 FROM daily_score WHERE trade_date=? AND score>?",
                (td, sc)).fetchone()[0]
            n_pool = conn.execute(
                "SELECT COUNT(*) FROM daily_score WHERE trade_date=?", (td,)).fetchone()[0]
        ms = conn.execute(
            "SELECT trade_date, score, pred_ret FROM model_score WHERE ts_code=?"
            " ORDER BY trade_date DESC LIMIT 5", (code,)).fetchall()
        lhb = conn.execute(
            "SELECT trade_date, name, close, pct_chg, net_buy, lhb_amount, turnover, reason"
            " FROM lhb WHERE ts_code=? ORDER BY trade_date DESC LIMIT 10", (code,)).fetchall()
        sig = conn.execute(
            "SELECT signal_date, action, reason FROM signal WHERE ts_code=?"
            " ORDER BY signal_date DESC LIMIT 10", (code,)).fetchall()
        pt = conn.execute(
            "SELECT exec_date, side, shares, price, amount, reason FROM paper_trade"
            " WHERE ts_code=? ORDER BY exec_date DESC, id DESC LIMIT 10", (code,)).fetchall()
        ppos = conn.execute(
            "SELECT name, shares, avg_cost FROM paper_position WHERE ts_code=?",
            (code,)).fetchone()
    finally:
        conn.close()

    # ---- 跨账户矩阵（七账户谁持有/谁交易过；单源失败在 build_all 里已隔离为 ok:false） ----
    holdings, trades_by_acct = [], []
    for a in payload.get("accounts") or []:
        if not a.get("ok"):
            continue
        for p in a.get("positions") or []:
            if p.get("code") == code:
                holdings.append({"account_id": a["id"], "account_name": a["name"],
                                 "model": a["model"], "shares": p.get("shares"),
                                 "cost": p.get("cost"), "last": p.get("last"),
                                 "value": p.get("value")})
        hits = [t for t in (a.get("trades") or []) if t.get("code") == code]
        if hits:
            trades_by_acct.append({"account_id": a["id"], "account_name": a["name"],
                                   "model": a["model"], "trades": hits[:10]})

    verdict, tone = _verdict(holdings, ds, rank, n_pool, lhb, sig)
    return {
        "code": code,
        "name": basic[1] if basic else None,
        "industry": basic[2] if basic else None,
        "market": basic[3] if basic else None,
        "list_date": basic[4] if basic else None,
        "known": basic is not None,
        "latest": _latest(kline),
        "basic_fields": ({"turnover_rate": dbasic[0], "pe_ttm": dbasic[1], "pb": dbasic[2],
                          "total_mv": dbasic[3], "circ_mv": dbasic[4]} if dbasic else {}),
        "kline": [{"date": k[0], "close": k[4], "pct_chg": k[6]} for k in reversed(kline)],
        "daily_scores": [{"trade_date": r[0], "score": r[1], "pred_ret": r[2]} for r in ds],
        "score_rank": rank, "score_pool": n_pool,
        "model_scores": [{"trade_date": r[0], "score": r[1], "pred_ret": r[2]} for r in ms],
        "lhb": [{"trade_date": r[0], "name": r[1], "close": r[2], "pct_chg": r[3],
                 "net_buy": r[4], "net_buy_wan": _yuan_wan(r[4]), "lhb_amount": r[5],
                 "turnover": r[6], "reason": r[7]} for r in lhb],
        "signals": [{"signal_date": r[0], "action": r[1], "reason": r[2]} for r in sig],
        "monthly": {"holding": ({"name": ppos[0], "shares": ppos[1], "avg_cost": ppos[2]}
                                if ppos else None),
                    "trades": [{"exec_date": r[0], "side": r[1], "shares": r[2],
                                "price": r[3], "amount": r[4], "reason": r[5]} for r in pt]},
        "holdings": holdings,
        "trades_by_account": trades_by_acct,
        "verdict": verdict,
        "tone": tone,
    }


def _latest(kline):
    """表头读数：最新收盘/涨跌幅（kline 传 DESC 序）。"""
    if not kline:
        return {"close": None, "pct_chg": None, "trade_date": None}
    return {"close": kline[0][4], "pct_chg": kline[0][6], "trade_date": kline[0][0]}


def _verdict(holdings, ds, rank, n_pool, lhb, sig):
    """纯规则智能读数（每句可溯源；确定性可复算）。"""
    parts = []
    if holdings:
        names = "、".join(h["account_name"] for h in holdings)
        parts.append(f"被 {len(holdings)}/7 账户持有（{names}）")
    else:
        parts.append("7 个账户均未持有")
    if ds:
        sc, pr = ds[0][1], ds[0][2]
        rk = f"，全市场第 {rank}/{n_pool} 名" if rank is not None else ""
        pr_txt = f"{pr * 100:+.2f}%" if pr is not None else "--"
        parts.append(f"最新日推分 {sc:.1f}{rk}，预期 {pr_txt}")
    else:
        parts.append("无日推分")
    if lhb:
        parts.append(f"最近上榜 {lhb[0][0]}（净买 {_yuan_wan(lhb[0][4])}）")
    else:
        parts.append("无龙虎榜记录")
    if sig:
        parts.append(f"最近信号 {sig[0][0]} {sig[0][1]}")
    # 警示口径：最近一次上榜是净卖出（资金面事实陈述，不做方向推断）
    tone = "warn" if (lhb and (lhb[0][4] or 0) < 0) else "ok"
    return "｜".join(parts), tone
