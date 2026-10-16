"""周频袖套的**成交核对**（2026-09-13）—— 方案意图 vs 账本实际，只读。

## 为什么需要

一笔交易在四个地方各有一份"事实"：**方案**（想买多少）、**账本**（实际记了多少）、
**行情**（成交价该是多少）、**净值**（钱对不对得上）。终端无人值守，出问题时日志里往往只有
一行 warning —— 而"想买的没买到""成交价离谱""现金对不上"这三类问题必须**一眼看得见**。

## 三项核对

1. `diff_positions` —— 目标名单逐只对比实际持仓：已建仓 / 未成交 / 部分成交 / 留存；
2. `price_deviation` —— 每笔成交价 vs **当日开盘价**（生产成交口径），报偏离 bps
   （回测假设滑点 0.1% = 10bps，实测偏离若长期大于它就说明撮合假设偏乐观）；
3. `cash_identity` —— `现金 + Σ 股数×最新价 ≈ 净值`（差 1 元以上就报出来）。

**只读**：不写账本、不改方案、不下单。任何一项取不到数据都只降级成"无法核对"，不抛异常。
"""
from __future__ import annotations

import logging

import pandas as pd

from app import db
# 假设滑点的**唯一真源**（config `paper.slip`）—— 原先 reconcile 把 10.0 硬编码在返回值与文案里,
# 那是第二份真相: 改了 config 而这里没跟着动 ⇒ 残差会凭空非零。ledger.py 早已跨模块用
# `paper.engine` 的私有函数（`:40`），这里同构。
from app.paper.engine import _slip as _assumed_slip
from app.weekly import plan as wk_plan
from app.weekly.ledger import Ledger

log = logging.getLogger(__name__)
MAX_TRADES = 200          # 滑点估计只看最近这些笔(避免按全史成交拼出超长 IN 列表)


def _bps(x: float, nd: int = 1) -> float:
    """四舍五入到 `nd` 位并**消掉负零**。

    浮点残差经常是 `-1.8e-15` 这种"实际是零、符号是负"的值, `round()` 之后得到 `-0.0`,
    打印出来就是 `-0.0 bps` / `+10.0 → -0.0` —— 运维看到会以为方向反了(回放账本的
    `mean_bps` 实测就长期显示成 `-0.0`)。`x + 0.0` 把 `-0.0` 归一回 `0.0`, 其余值不变。
    """
    return round(float(x), nd) + 0.0


def diff_positions(plan: dict, positions: dict) -> dict:
    """方案目标 vs 实际持仓（逐只）。"""
    want = {r["ts_code"]: int(r.get("shares") or 0) for r in (plan.get("rows") or [])}
    rows, n_ok, n_part, n_zero = [], 0, 0, 0
    for code, tgt in want.items():
        have = int(float((positions.get(code) or {}).get("shares") or 0))
        if have >= tgt > 0:
            state = "已建仓"
            n_ok += 1
        elif have > 0:
            state = "部分成交"
            n_part += 1
        else:
            state = "未成交"
            n_zero += 1
        if state != "已建仓":
            rows.append({"ts_code": code, "target": tgt, "actual": have, "state": state})
    extra = [c for c in positions if c not in want]           # 方案外还留着的(该卖没卖掉)
    return {"n_target": len(want), "n_ok": n_ok, "n_partial": n_part, "n_missing": n_zero,
            "n_extra": len(extra), "deviations": rows[:50], "extra": extra[:50]}


def price_deviation(trades: list, conn=None) -> dict:
    """成交价 vs 当日**开盘价**（生产两档都是开盘价口径）。

    2026-09-13 修正: 原实现把 `daily` **全表**（1200+ 万行）读进内存 —— 周一之后这个核对每天都要跑,
    那样既慢又占内存。改成按成交的 (日期, 代码) 过滤; 只取最近 `MAX_TRADES` 笔(滑点估计看近期)。

    2026-09-19 审查 B3: 新增**残差**族读数(`resid_mean_bps` / `resid_p95_bps`)。原来只有
    `mean_bps = mean(dev)`, 而买单的 dev 本应 ≈ +10bps、卖单本应 ≈ −10bps ⇒ 混合账本里两者
    **精确对消**, `abs(mean_bps)` 恒 ≈ 0, 于是 `protocol` 的放行闸 G2「|mean_bps| ≤ 20bps」
    是一条**永远不会红**的判据(实测: 425 笔的混合回放簿 mean=−0.0 而单笔最差偏离 11.1bps)。
    残差 = **逆风偏离** − 该方向本应有的滑点：先把方向统一成"逆风为正"(`adv = sign×dev`,
    买贵了/卖便宜了都是正数)，再扣掉假设滑点 ⇒ 两侧的劣化**相加**而不是相消。
    ⚠️ 不能写成 `dev − sign×滑点`: 那样买 +50 与卖 −50 又会抵消成 0(本仓预注册第一版就写成了
    这个错形式, 在取任何读数之前被合成用例抓住 —— 见 `plans/g2-residual-criterion-20260919.md` §2)。
    ⚠️ 判据的切换在 `protocol.py`(阈值 20bps 不变), 预注册见上述文档;
    `mean_bps`/`median_bps` 两个**字段与取值照旧保留**(前端与既有台账在核对它们)。
    """
    if not trades:
        return {"n": 0, "note": "还没有成交"}
    recent = trades[-MAX_TRADES:]
    dates = sorted({str(t["date"]) for t in recent})
    codes = sorted({str(t["ts_code"]) for t in recent})
    own = conn is None
    conn = conn or db.get_conn()
    try:
        qd = ",".join("?" * len(dates))
        qc = ",".join("?" * len(codes))
        df = pd.read_sql(f"SELECT ts_code, trade_date, open FROM daily "
                         f"WHERE trade_date IN ({qd}) AND ts_code IN ({qc})",
                         conn, params=(*dates, *codes))
    finally:
        if own:
            conn.close()
    px = df.set_index(["ts_code", "trade_date"])["open"]
    assumed_bps = 1e4 * _assumed_slip()          # 唯一真源: config `paper.slip`
    devs, resids = [], []
    for t in recent:
        try:
            ref = float(px.loc[(t["ts_code"], str(t["date"]))])
        except (KeyError, TypeError, ValueError):
            continue
        if ref > 0 and t.get("price"):
            dev = 10000.0 * (float(t["price"]) / ref - 1.0)     # bps
            devs.append(dev)
            # `side` 是账本既有字段(ledger.py 买入/卖出两条路径都写), 无需新数据。
            # 先翻成"逆风为正"(买贵/卖贱都 >0)再扣假设滑点 —— 顺序反过来就会重新引入抵消。
            sign = -1.0 if str(t.get("side", "buy")) == "sell" else 1.0
            resids.append(sign * dev - assumed_bps)
    if not devs:
        return {"n": 0, "note": "成交日行情尚未入库(晚间档或次日才有)"}
    s, rs = pd.Series(devs), pd.Series(resids)
    return {"n": int(len(s)), "mean_bps": _bps(s.mean()),
            "median_bps": _bps(s.median()),
            "max_abs_bps": _bps(s.abs().max()),
            "resid_mean_bps": _bps(rs.mean(), 2),
            "resid_p95_bps": _bps(rs.abs().quantile(0.95), 2),
            "assumed_slip_bps": _bps(assumed_bps, 2),
            "note": "判据看 resid_mean_bps(残差=逆风偏离−假设滑点, 逆风=买贵/卖贱为正); "
                    "mean/median 族会被买卖抵消 ⇒ 混合账本上恒 ≈0, 只作展示"}


def cash_identity(led) -> dict:
    """`现金 + 累计利息 + Σ 股数×最新价 − 净值`（净值点齐备时才有意义）。

    2026-09-14（07 号 #49）：原式**漏了利息项** —— 而 `Ledger.mark` 记的净值是
    `现金 + 利息 + 市值`（`ledger.py:175`）⇒ 原式恒等于 `−累计利息`：从第 2 个净值点
    起 `ok` 必为 False（每个交易日假 ✗），`protocol` 的放行闸 G5「记账完整」因此
    **永远过不了**（1 个点时 `len(nav) >= 2` 不成立、≥2 个点时 `ok` 为 False，两条件互斥）。
    同一份恒等式在 `scripts/audit_engineering.py::check_ledger` 里**是带利息的** ⇒ 现两处对齐。

    `n` 一并返回：`scheduler.py` 用 `idt.get("n")` 判"有没有净值点可核对"，而这个键原先
    **根本没被生产过** ⇒ 那句"资金恒等式不成立"的告警**从来不可能触发**（哑警报）。
    """
    hist = led.state.get("nav_history") or []
    if not hist:
        return {"n": 0, "note": "还没有净值点"}      # n=0 + no "ok" ⇒ 调度器判据保持静默
    last = hist[-1]
    mv = sum(float(p.get("shares") or 0) * float(p.get("last_px") or p.get("cost") or 0)
             for p in (led.state.get("positions") or {}).values())
    # 利息取**该净值点记下的值**（老格式点没有 interest 键 ⇒ 退回当前累计值，向后兼容）
    interest = float(last.get("interest", led.state.get("interest") or 0.0) or 0.0)
    gap = float(led.state["cash"]) + interest + mv - float(last["nav"])
    return {"n": len(hist), "date": last["date"], "cash": round(float(led.state["cash"]), 2),
            "interest": round(interest, 2),
            "market_value": round(mv, 2), "nav": last["nav"], "gap": round(gap, 2),
            "ok": abs(gap) <= 1.0}


def report(plan_path=None, ledger_path=None) -> dict:
    """一次跑三项核对（只读；任何异常都降级成 note）。"""
    out = {}
    try:
        plans = sorted(wk_plan.PLAN_DIR.glob("plan_*.json"))
        if plan_path:
            plans = [plan_path]
        if not plans:
            return {"available": False, "note": "还没有方案文件"}
        import json
        plan = json.loads(plans[-1].read_text(encoding="utf-8"))
        led = Ledger.load(0.0, ledger_path)
        out["plan"] = plans[-1].name
        out["anchor"] = plan.get("anchor")
        out["positions"] = diff_positions(plan, led.state.get("positions") or {})
        out["prices"] = price_deviation(led.state.get("trades") or [])
        out["identity"] = cash_identity(led)
        out["last_plan"] = led.state.get("last_plan")
        out["available"] = True
    except Exception as e:                       # 核对工具绝不能把链子带崩
        log.exception("成交核对异常")
        out = {"available": False, "error": str(e)}
    return out


def lines(res: dict) -> list:
    if not res.get("available"):
        return [f"成交核对不可用: {res.get('note') or res.get('error')}"]
    p = res["positions"]
    out = [f"方案 {res['plan']}（锚点 {res['anchor']}）| 目标 {p['n_target']} 只："
           f"已建仓 {p['n_ok']} / 部分 {p['n_partial']} / 未成交 {p['n_missing']} "
           f"| 方案外残留 {p['n_extra']} 只"]
    for d in p["deviations"][:10]:
        out.append(f"  · {d['ts_code']} 目标 {d['target']} 实际 {d['actual']}（{d['state']}）")
    if p["extra"]:
        out.append(f"  · 方案外残留: {', '.join(p['extra'][:10])}")
    pr = res["prices"]
    if not pr.get("n"):
        out.append(f"成交价核对: {pr.get('note') or ''}")
    elif pr.get("resid_mean_bps") is None:
        # 没有残差族读数(旧版核对结果/外部塞进来的 payload) ⇒ 退回旧文案，**绝不抛 KeyError**：
        # 本函数被 scheduler 的告警分支在同一 try 里调用，它一炸整条运维告警就被静默吞掉
        # (2026-09-19 加残差时 tests/test_weekly_reconcile_slot.py 就是这么抓出来的)。
        out.append(f"成交价 vs 当日开盘: {pr['n']} 笔 | 均值 {pr['mean_bps']:+.1f}bps | "
                   f"中位 {pr['median_bps']:+.1f}bps | 最大偏离 {pr['max_abs_bps']:.1f}bps"
                   f"（回测假设滑点 {pr['assumed_slip_bps']:.0f}bps）")
    else:
        out.append(f"成交价 vs 当日开盘: {pr['n']} 笔 | **执行残差均值 "
                   f"{pr['resid_mean_bps']:+.2f}bps**（判据，阈值 20）"
                   f"| 残差 p95 {pr['resid_p95_bps']:.2f}bps "
                   f"| 最大偏离 {pr['max_abs_bps']:.1f}bps"
                   f"（回测假设滑点 {pr['assumed_slip_bps']:.0f}bps）"
                   f"| 旧口径均值 {pr['mean_bps']:+.1f}bps（买卖会抵消，只作展示）")
    idt = res["identity"]
    if idt.get("n", 0) == 0 and "note" in idt:
        out.append(f"资金恒等式: {idt['note']}")
    else:
        # 2026-09-14: 算式必须与真式**逐项一致**（恒等式含利息项），否则打印的算术
        # 与打印的差值对不上（原本只印"现金+市值"，而 gap 里还有利息）
        out.append(f"资金恒等式: 现金 {idt['cash']:,.0f} + 利息 {idt.get('interest', 0.0):,.2f} "
                   f"+ 市值 {idt['market_value']:,.0f} "
                   f"= {idt['cash'] + idt.get('interest', 0.0) + idt['market_value']:,.0f} "
                   f"vs 净值 {idt['nav']:,.0f} "
                   f"（差 {idt['gap']:+.2f}）{'✓' if idt['ok'] else '✗'}")
    if res.get("last_plan"):
        lp = res["last_plan"]
        out.append(f"本次用的是 {lp.get('anchor')} 的方案（{lp.get('n_rows')} 行 / "
                   f"{lp.get('n_trades')} 笔），输入指纹 "
                   f"{(lp.get('meta') or {}).get('scores_sha16')}")
    return out
