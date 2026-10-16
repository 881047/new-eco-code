# -*- coding: utf-8 -*-
"""P3 · LLM 月度复盘/异常检测闭环（天枢同款，用户 2026-10-01 拍板"尝试P3"）。

定位与纪律（与 scripts/llm_factor_review.py 同一口径）：
- **LLM 的输出不进任何决策/门禁/打分/组合链路**——它是"写复盘的眼"，不是"做买卖的脑"；
- 机械事实先行：月度事实单由纯 SQL 编译（键 = fact key），机械异常检测器（确定性规则）
  永远先于 LLM 存在，LLM 只做假设与叙事；**LLM 挂了报告降级为纯机械版照常落盘**；
- **幻觉计零**：LLM 每条 finding 必须引用 fact key，引用对不上事实单的直接丢弃
  （沿用 factor_review 的 name-match 纪律）；
- 每条输出带模型名与时间戳，可追溯、可再生（重跑即覆盖同月产物）。

用法：
    .venv\\Scripts\\python -X utf8 scripts/llm_monthly_review.py                # 复盘上个自然月
    .venv\\Scripts\\python -X utf8 scripts/llm_monthly_review.py --month 2026-09
    .venv\\Scripts\\python -X utf8 scripts/llm_monthly_review.py --status      # 打印上次产物
    .venv\\Scripts\\python -X utf8 scripts/llm_monthly_review.py --dry         # 只产机械部分，不调 LLM

产物 data/llm/monthly_review/{month}.json + .md（data/llm/ 已 gitignore）。
调度 = 计划任务 NEWECO_llm_monthly_review（每月 1 日 20:00，月链当晚复盘上月 + 新方案体检）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

OUT_DIR = BASE / "data" / "llm" / "monthly_review"
NAV_MDD_WARN = -0.08          # 月内回撤告警线（信息性，非决策）
UNDERPERF_WARN = -0.03        # 月度跑输基准告警线（pp）
STOP_REBUY_DAYS = 10          # 止损卖出后同票再买入的观察窗（自然日；光韵达事故类）
STALE_PENDING_DAYS = 5        # pending 订单陈旧线

# ---------------- 事实单（纯 SQL，键 = fact key） ----------------

def _mk(key, **kw):
    return {"key": key, **kw}


def build_facts(conn: sqlite3.Connection, month: str) -> dict:
    """month = 'YYYY-MM'。所有行带 `key` 供 LLM 引用与机械校验。"""
    ym = month.replace("-", "")
    lo, hi = f"{ym}01", f"{ym}31"
    facts: dict = {"month": month}

    plans = conn.execute(
        "SELECT plan_date, plan_type, ts_code, name, weight, shares, reason, industry "
        "FROM plan WHERE plan_date BETWEEN ? AND ? ORDER BY plan_date, ts_code",
        (lo, hi)).fetchall()
    facts["plan"] = [_mk(f"plan:{r[0]}:{r[2]}", plan_date=r[0], plan_type=r[1],
                         ts_code=r[2], name=r[3], weight=r[4], shares=r[5],
                         reason=r[6], industry=r[7]) for r in plans]
    latest = conn.execute(
        "SELECT plan_date, ts_code, name, weight, reason FROM plan "
        "ORDER BY plan_date DESC LIMIT 8").fetchall()
    facts["latest_plan"] = [_mk(f"plan:{r[0]}:{r[1]}", plan_date=r[0], ts_code=r[1],
                                name=r[2], weight=r[3], reason=r[4]) for r in latest]

    orders = conn.execute(
        "SELECT id, order_date, ts_code, name, side, shares, reason, status, first_attempt "
        "FROM paper_order WHERE order_date BETWEEN ? AND ? ORDER BY order_date, id",
        (lo, hi)).fetchall()
    facts["orders"] = [_mk(f"order:{r[0]}", order_id=r[0], order_date=r[1], ts_code=r[2],
                           name=r[3], side=r[4], shares=r[5], reason=r[6], status=r[7],
                           first_attempt=r[8]) for r in orders]

    trades = conn.execute(
        "SELECT id, exec_date, ts_code, name, side, shares, price, amount, reason "
        "FROM paper_trade WHERE exec_date BETWEEN ? AND ? ORDER BY exec_date, id",
        (lo, hi)).fetchall()
    facts["trades"] = [_mk(f"trade:{r[0]}", trade_id=r[0], exec_date=r[1], ts_code=r[2],
                           name=r[3], side=r[4], shares=r[5], price=r[6], amount=r[7],
                           reason=r[8]) for r in trades]

    sigs = conn.execute(
        "SELECT signal_date, ts_code, name, action, reason FROM signal "
        "WHERE signal_date BETWEEN ? AND ? AND action != 'watch' "
        "ORDER BY signal_date", (lo, hi)).fetchall()
    facts["signals"] = [_mk(f"signal:{r[0]}:{r[4]}:{r[1]}", signal_date=r[0], ts_code=r[1],
                            name=r[2], action=r[3], reason=r[4]) for r in sigs]
    facts["signal_summary"] = dict(conn.execute(
        "SELECT action, COUNT(*) FROM signal WHERE signal_date BETWEEN ? AND ? "
        "GROUP BY action", (lo, hi)).fetchall())

    alerts = conn.execute(
        "SELECT id, created_at, tag, ok, error FROM notify_log "
        "WHERE created_at BETWEEN ? AND ? ORDER BY id", (f"{month}-01", f"{month}-31")).fetchall()
    facts["alerts"] = [_mk(f"notify:{r[0]}", notify_id=r[0], created_at=r[1], tag=r[2],
                           ok=r[3], error=(r[4] or "")[:120]) for r in alerts]

    perr = conn.execute(
        "SELECT id, started_at, stage, run_date, status, error FROM pipeline_run "
        "WHERE status='error' AND started_at BETWEEN ? AND ? ORDER BY id",
        (f"{month}-01", f"{month}-31")).fetchall()
    facts["pipeline_errors"] = [_mk(f"pipeline:{r[0]}", run_id=r[0], started_at=r[1],
                                    stage=r[2], run_date=r[3], status=r[4],
                                    error=(r[5] or "")[:160]) for r in perr]

    nav = conn.execute(
        "SELECT trade_date, nav, bench_nav FROM paper_nav "
        "WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date", (lo, hi)).fetchall()
    if nav:
        rets = [(r[1] / nav[0][1] - 1.0) for r in nav if r[1]]
        peak, mdd = nav[0][1], 0.0
        for _d, v, _b in nav:
            if v:
                peak = max(peak, v)
                mdd = min(mdd, v / peak - 1.0)
        b_last = next((b for _d, _v, b in reversed(nav) if b), None)
        facts["nav"] = _mk(f"nav:{nav[-1][0]}", first_date=nav[0][0], last_date=nav[-1][0],
                           ret=round(rets[-1], 4) if rets else None,
                           bench_ret=round(b_last / nav[0][2] - 1.0, 4)
                           if nav[0][2] and b_last else None,
                           mdd=round(mdd, 4), days=len(nav))
    else:
        facts["nav"] = None

    pos = conn.execute(
        "SELECT ts_code, name, shares, avg_cost FROM paper_position "
        "ORDER BY ts_code").fetchall()
    facts["positions"] = [_mk(f"position:{r[0]}", ts_code=r[0], name=r[1],
                              shares=r[2], avg_cost=r[3]) for r in pos]
    acct = conn.execute(
        "SELECT cash, last_applied_plan, initial_capital FROM paper_account LIMIT 1").fetchone()
    facts["account"] = {"cash": acct[0], "last_applied_plan": acct[1],
                        "initial_capital": acct[2]} if acct else None
    return facts


# ---------------- 机械异常检测（确定性，永远先于 LLM） ----------------

def _is_stop(reason: str) -> bool:
    return bool(reason) and reason.startswith("止损")


def _is_plan_buy(reason: str) -> bool:
    return bool(reason) and reason.startswith("月度方案")


def mechanical_checks(facts: dict) -> list:
    out = []
    month = facts["month"]
    ym = month.replace("-", "")          # YYYYMMDD 族（order_date/first_attempt 口径）

    def add(check, ok, detail, cites):
        out.append({"check": check, "ok": bool(ok), "detail": detail,
                    "citations": cites})

    add("plan_built", bool(facts["plan"]),
        f"月内方案行 {len(facts['plan'])} 条（plan_date 前缀 {month}）",
        [p["key"] for p in facts["plan"][:3]])

    pending = [o for o in facts["orders"] if o["status"] == "pending"]
    add("pending_orders", not pending,
        f"pending 订单 {len(pending)} 张" + (f"：{[o['key'] for o in pending][:5]}" if pending else ""),
        [o["key"] for o in pending][:5])

    # 陈旧参照系 = 月末（复盘时点 ≈ 月末+1）；stale = 首挂早于月末-5 天仍 pending
    stale_ref = _shift_day(_month_end(ym), -STALE_PENDING_DAYS)
    stale = [o for o in pending if o.get("first_attempt") and
             o["first_attempt"][:8] <= stale_ref]
    add("stale_pending", not stale,
        f"首挂已超 {STALE_PENDING_DAYS} 天仍 pending 的订单 {len(stale)} 张",
        [o["key"] for o in stale][:5])

    stops = [t for t in facts["trades"] if t["side"] == "sell" and _is_stop(t["reason"])]
    add("stop_loss_fired", not stops,
        f"止损卖出 {len(stops)} 笔" + (f"：{[(t['name'], t['reason'][:30]) for t in stops]}" if stops else ""),
        [t["key"] for t in stops])

    # 光韵达事故类：止损卖出后同票再买入（含动态层/方案层任何买入）
    rebuy_pairs = []
    for s in stops:
        for b in facts["trades"]:
            if (b["ts_code"] == s["ts_code"] and b["side"] == "buy"
                    and b["exec_date"] >= s["exec_date"]):
                rebuy_pairs.append((s, b))
    add("stop_loss_rebuy", not rebuy_pairs,
        f"止损后同票再买 {len(rebuy_pairs)} 例"
        + (f"：{[f"{s['key']} -> {b['key']}" for s, b in rebuy_pairs[:3]]}" if rebuy_pairs else ""),
        [k for s, b in rebuy_pairs[:3] for k in (s["key"], b["key"])])

    buys = [t for t in facts["trades"] if t["side"] == "buy"]
    off_plan = [t for t in buys
                if not _is_plan_buy(t["reason"]) and "替换" not in (t["reason"] or "")
                and "止损" not in (t["reason"] or "")]
    add("off_plan_buys", not off_plan,
        f"非方案/非替换来源的买入 {len(off_plan)} 笔",
        [t["key"] for t in off_plan][:5])

    fails = [a for a in facts["alerts"] if not a["ok"]]
    add("failed_alerts", not fails,
        f"发送失败告警 {len(fails)} 条",
        [a["key"] for a in fails][:5])

    add("pipeline_errors", not facts["pipeline_errors"],
        f"pipeline error {len(facts['pipeline_errors'])} 条",
        [p["key"] for p in facts["pipeline_errors"]][:5])

    nav = facts["nav"]
    if nav and nav["mdd"] is not None and nav["mdd"] < NAV_MDD_WARN:
        add("nav_mdd", False, f"月内回撤 {nav['mdd']:.1%} 越线 {NAV_MDD_WARN:.0%}", [nav["key"]])
    else:
        add("nav_mdd", True, f"月内回撤 {nav['mdd']:.1%}" if nav and nav["mdd"] is not None else "无 NAV 数据", [nav["key"]] if nav else [])
    if nav and nav["ret"] is not None and nav["bench_ret"] is not None \
            and nav["ret"] - nav["bench_ret"] < UNDERPERF_WARN:
        add("underperformance", False,
            f"月度超额 {nav['ret'] - nav['bench_ret']:+.1%} 越线 {UNDERPERF_WARN:+.0%}", [nav["key"]])
    else:
        add("underperformance", True,
            f"月度超额 {nav['ret'] - nav['bench_ret']:+.1%}"
            if nav and nav["ret"] is not None and nav["bench_ret"] is not None else "无 NAV 数据",
            [nav["key"]] if nav else [])
    return out


def _shift_day(yyyymmdd: str, days: int) -> str:
    import datetime as _dt
    d = _dt.datetime.strptime(yyyymmdd, "%Y%m%d") + _dt.timedelta(days=days)
    return d.strftime("%Y%m%d")


def _month_end(ym: str) -> str:
    """ym='202609' → '20260930'（真月末，防 31 号越界）。"""
    import datetime as _dt
    y, m = int(ym[:4]), int(ym[4:6])
    nxt = _dt.date(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
    return (nxt - _dt.timedelta(days=1)).strftime("%Y%m%d")


# ---------------- 引用校验（幻觉计零） ----------------

def fact_keys(facts: dict) -> set:
    keys = set()
    for sec in ("plan", "latest_plan", "orders", "trades", "signals", "alerts",
                "pipeline_errors", "positions"):
        for row in facts.get(sec, []) or []:
            keys.add(row["key"])
    if facts.get("nav"):
        keys.add(facts["nav"]["key"])
    return keys


def verify_findings(facts: dict, findings: list) -> tuple:
    """每条 finding 必须至少引用一个真实 fact key；引用含幻觉键的 finding 整条丢弃。"""
    keys = fact_keys(facts)
    kept, dropped = [], []
    for f in findings or []:
        cites = [c for c in (f.get("citations") or []) if isinstance(c, str)]
        if not cites:
            dropped.append({"finding": f, "reason": "无引用"})
            continue
        bad = [c for c in cites if c not in keys]
        if bad:
            dropped.append({"finding": f, "reason": f"引用不存在: {bad[:3]}"})
            continue
        kept.append({**f, "citations": cites})
    return kept, dropped


# ---------------- LLM 层 ----------------

_PROMPT = """你是量化交易系统的月度复盘审读员。下面是模拟盘 {month} 的**事实单**
（每条记录的 key 是唯一可引用标识）。机械异常检测器已经给出结论，你只做两件事：
1. 对机械检测的每一条非 ok 项给出**机制假设**（为什么会发生，结合事实单里的相邻证据）；
2. 找出机械规则没覆盖、但事实单里可见的**值得人看的现象**。

硬性纪律：
- 每条 finding 的 citations 必须是事实单里真实存在的 key（如 "order:12"、"notify:81"、
  "plan:20260930:002245"）；**编造 key = 该条作废**；
- severity: high（资金安全/执行漂移）/ mid（异常但已解释）/ low（观察项）；
- 不给任何交易建议、不预测行情、不评价选股好坏（选股判决归模型与门禁，不归你）；
- 只输出 JSON：{{"summary": "...", "findings": [{{"severity": "...", "claim": "...",
  "citations": [...], "hypothesis": "..."}}], "watch_next": ["..."]}}

机械检测结论：
{checks}

事实单：
{facts}
"""


def run_review(month: str, dry: bool = False) -> dict:
    from app import db
    with db.conn_ctx() as conn:
        facts = build_facts(conn, month)
    checks = mechanical_checks(facts)
    out = {"month": month, "ran_at": datetime.now().isoformat(timespec="seconds"),
           "facts": facts, "mechanical_checks": checks, "llm": None, "degraded": False}
    if dry:
        out["degraded"] = True
        out["note"] = "--dry：只产机械部分"
        _write(out)
        return out
    from app.web import llm as llm_mod
    if not llm_mod._llm_cfg()["api_key"]:
        out["degraded"] = True
        out["note"] = "LLM 未配置，纯机械版"
        _write(out)
        return out
    compact = {k: v for k, v in facts.items() if k not in ("signal_summary",)}
    try:
        # 09-30 在册教训: thinking token 计入 max_tokens 预算, 56KB 事实单 + 开思考
        # ⇒ 思考吃光预算 content 恒空。复盘 = 对给定事实出假设, 关思考是正解形态。
        text = llm_mod._chat(
            [{"role": "user", "content": _PROMPT.format(
                month=month,
                checks=json.dumps(checks, ensure_ascii=False, indent=1),
                facts=json.dumps(compact, ensure_ascii=False, default=str))}],
            max_tokens=8000, thinking=False)
        parsed = json.loads(text.strip().removeprefix("```json")
                            .removesuffix("```").strip())
        kept, dropped = verify_findings(facts, parsed.get("findings") or [])
        out["llm"] = {"summary": parsed.get("summary"),
                      "findings": kept, "dropped": dropped,
                      "watch_next": parsed.get("watch_next") or [],
                      "model": llm_mod._llm_cfg()["model"]}
    except Exception as e:                       # LLM 失败不拖垮机械版落盘
        out["degraded"] = True
        raw = "" if 'text' not in dir() else (text or "")
        out["note"] = (f"LLM 调用/解析失败，纯机械版: {e!r}｜原始返回头: "
                       f"{raw[:120]!r}")[:300]
    _write(out)
    return out


def _write(out: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / f"{out['month']}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                 encoding="utf-8")
    p.with_suffix(".md").write_text(to_markdown(out), encoding="utf-8")
    print(f"产物: {p} (+.md)  degraded={out['degraded']}")


def to_markdown(out: dict) -> str:
    lines = [f"# 月度复盘 {out['month']}（{out['ran_at']}）",
             "", "## 机械检测", ""]
    for c in out["mechanical_checks"]:
        mark = "✅" if c["ok"] else "⚠️"
        lines.append(f"- {mark} **{c['check']}** — {c['detail']}"
                     + (f" （引用: {', '.join(c['citations'][:3])}）" if c["citations"] else ""))
    llm = out.get("llm")
    if llm:
        lines += ["", f"## LLM 复盘（{llm.get('model')}，仅供人读，无决策权）", ""]
        if llm.get("summary"):
            lines += [llm["summary"], ""]
        for f in llm.get("findings", []):
            lines.append(f"- [{f.get('severity')}] {f.get('claim')} "
                         f"（引用: {', '.join(f.get('citations', [])[:3])}）")
            if f.get("hypothesis"):
                lines.append(f"  - 假设：{f['hypothesis']}")
        if llm.get("dropped"):
            lines += ["", f"_丢弃未锚定 findings {len(llm['dropped'])} 条（幻觉计零）_"]
        if llm.get("watch_next"):
            lines += ["", "**下月观察项**："] + [f"- {w}" for w in llm["watch_next"]]
    elif out.get("degraded"):
        lines += ["", f"_降级：{out.get('note', '')}_"]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default=None, help="复盘月 YYYY-MM（默认上个自然月）")
    ap.add_argument("--status", action="store_true", help="打印上次产物摘要")
    ap.add_argument("--dry", action="store_true", help="只产机械部分，不调 LLM")
    args = ap.parse_args()
    if args.status:
        files = sorted(OUT_DIR.glob("*.json")) if OUT_DIR.exists() else []
        if not files:
            print("无产物")
            return 0
        latest = json.loads(files[-1].read_text(encoding="utf-8"))
        print(json.dumps({"month": latest["month"], "ran_at": latest["ran_at"],
                          "degraded": latest["degraded"],
                          "checks": [(c["check"], c["ok"], c["detail"])
                                     for c in latest["mechanical_checks"]],
                          "findings": len((latest.get("llm") or {}).get("findings", []))},
                         ensure_ascii=False, indent=1))
        return 0
    month = args.month
    if not month:
        import datetime as _dt
        today = _dt.date.today().replace(day=1) - _dt.timedelta(days=1)
        month = today.strftime("%Y-%m")
    out = run_review(month, dry=args.dry)
    bad = [c for c in out["mechanical_checks"] if not c["ok"]]
    print(f"机械检测: {len(out['mechanical_checks']) - len(bad)}/{len(out['mechanical_checks'])} ok"
          + (f"；异常项: {[c['check'] for c in bad]}" if bad else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
