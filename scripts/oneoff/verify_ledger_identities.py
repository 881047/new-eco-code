"""资金账本三线恒等式核查(2026-09-16, 全项目逻辑审查·资金线; 只读)

审查约定: 缺陷报告可以骗人, 恒等式不会。三本账本各自必须满足:
  ① 月频模拟盘: paper_nav 每行 nav == cash + market_value + 累计利息(至当日);
  ② 月频现金走查: cash(t) == cash(t-1) + Σ当日卖出净入 - Σ当日买入净出(含费,
     与 paper_trade 逐笔行对账);
  ③ 持仓走查: paper_position.shares == Σ买入 - Σ卖出(paper_trade 逐股累计);
  ④ 周频袖套: ledger.json 最后一行 nav == cash + interest + Σ(shares×last_px);
  ⑤ 袖套现金走查: 从 capital 起逐笔 trade 累加(卖出 +净额 / 买入 -(额+费)) == 当前 cash。

任何一条超差(容差 0.02 元, 舍入级)即打印 FAIL 并给出行号/差额。全部只读。
运行: ./.venv/Scripts/python.exe -X utf8 scripts/oneoff/verify_ledger_identities.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from app import db
from app.logger import setup_logger

setup_logger()

TOL = 0.02
LEDGER = Path(__file__).resolve().parents[2] / "data" / "weekly" / "ledger.json"

fails = []


def check(name, ok, detail):
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        fails.append((name, detail))


conn = db.get_conn()
try:
    # ---- ① nav 恒等式 ----
    nav = pd.read_sql("SELECT trade_date, nav, cash, market_value FROM paper_nav ORDER BY trade_date", conn)
    ints = pd.read_sql("SELECT trade_date, amount FROM paper_interest ORDER BY trade_date", conn)
    ints["cum"] = ints["amount"].cumsum()
    icum = dict(zip(ints["trade_date"], ints["cum"].round(2)))
    worst = 0.0
    worst_d = None
    for _, r in nav.iterrows():
        ic = icum.get(r["trade_date"], 0.0)
        diff = abs(r["nav"] - (r["cash"] + r["market_value"] + ic))
        if diff > worst:
            worst, worst_d = diff, r["trade_date"]
    check("① paper_nav.nav == cash+mv+利息累计", worst <= TOL,
          f"{len(nav)} 行, 最大偏差 {worst:.4f} 元 @ {worst_d} (容差 {TOL})")

    # ---- ② 现金走查 ----
    tr = pd.read_sql(
        "SELECT exec_date, ts_code, side, shares, price, cost, amount FROM paper_trade ORDER BY id", conn)
    w = nav[["trade_date", "cash"]].copy()
    cash_map = dict(zip(w["trade_date"], w["cash"]))
    dates = sorted(cash_map)
    prev_cash = None
    worst2, worst2_d = 0.0, None
    tr_by_date = {d: (0.0, 0.0) for d in dates}   # date -> (买入净出, 卖出净入)
    for _, t in tr.iterrows():
        buy_out, sell_in = tr_by_date.get(t["exec_date"], (0.0, 0.0))
        if t["side"] == "buy":
            buy_out += round(t["amount"] + t["cost"], 2)
        else:
            sell_in += round(t["amount"], 2)      # 卖出 amount 已是净额(=proceeds-cost)
        tr_by_date[t["exec_date"]] = (buy_out, sell_in)
    for d in dates:
        cur = cash_map[d]
        if prev_cash is not None:
            b, s = tr_by_date.get(d, (0.0, 0.0))
            expect = round(prev_cash - b + s, 2)
            diff = abs(expect - cur)
            if diff > worst2:
                worst2, worst2_d = diff, d
        prev_cash = cur
    check("② paper 现金逐日走查 == paper_trade 对账", worst2 <= TOL,
          f"最大偏差 {worst2:.4f} 元 @ {worst2_d}")

    # ---- ③ 持仓走查 ----
    pos = pd.read_sql("SELECT ts_code, shares FROM paper_position", conn)
    flows = tr.groupby(["ts_code", "side"])["shares"].sum().unstack(fill_value=0)
    bad = []
    for _, p in pos.iterrows():
        b = float(flows["buy"].get(p["ts_code"], 0.0)) if "buy" in flows.columns else 0.0
        s = float(flows["sell"].get(p["ts_code"], 0.0)) if "sell" in flows.columns else 0.0
        if abs((b - s) - float(p["shares"])) > 1e-6:
            bad.append((p["ts_code"], b - s, p["shares"]))
    # 反向: trade 里净持仓>0 但 position 表缺失的
    for ts_code, row in flows.iterrows():
        net = float(row.get("buy", 0.0) - row.get("sell", 0.0))
        in_pos = ts_code in set(pos["ts_code"])
        if net > 0 and not in_pos:
            bad.append((ts_code, net, "缺失于 paper_position"))
    check("③ paper_position.shares == 逐股成交净额", not bad,
          f"{len(pos)} 只持仓, 异常 {bad[:3] if bad else 0}")
finally:
    conn.close()

# ---- ④⑤ 袖套账本 ----
if LEDGER.exists():
    st = json.loads(LEDGER.read_text(encoding="utf-8"))
    hist = st.get("nav_history") or []
    if hist:
        last = hist[-1]
        mv = sum(float(p["shares"]) * float(p.get("last_px") or p.get("cost") or 0)
                 for p in (st.get("positions") or {}).values())
        diff = abs(float(last["nav"]) - (float(st["cash"]) + float(last.get("interest", 0)) + mv))
        check("④ 袖套最后一行 nav == cash+interest+Σ(shares×px)", diff <= TOL,
              f"偏差 {diff:.4f} 元 @ {last['date']}")
        cash_walk = float(st["capital"])
        bad_t = 0
        for t in (st.get("trades") or []):
            amt = float(t["shares"]) * float(t["price"])
            fee = float(t.get("fee", 0))
            cash_walk += (amt - fee) if t["side"] == "sell" else -(amt + fee)
        diff = abs(cash_walk - float(st["cash"]))
        check("⑤ 袖套现金走查 == capital+Σ成交净额", diff <= max(TOL, 0.5),
              f"{len(st.get('trades') or [])} 笔, 偏差 {diff:.4f} 元(费用 round(2) 累计容差 0.5)")
        # 除权缩放连续性: shares 应与 nav 点位里的 positions 一致(最新点)
        pt = (last.get("positions") or {})
        mismatch = [c for c, sh in pt.items()
                    if abs(float(sh) - float((st["positions"].get(c) or {}).get("shares", -1))) > 1e-6]
        check("⑥ 袖套最新净值点持仓快照 == 当前持仓", not mismatch,
              f"不一致 {mismatch[:3] if mismatch else 0}")
else:
    print("SKIP  ④⑤⑥ 袖套账本不存在(首次执行前)")

print()
if fails:
    print(f"共 {len(fails)} 条恒等式 FAIL —— 账本存在逻辑缺陷或数据损坏, 逐条排查")
    sys.exit(1)
print("全部恒等式 PASS —— 三本账本在真实数据上自洽")
