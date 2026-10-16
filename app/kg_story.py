"""股票事件流叙事查询（story）——知识图谱的统一只读出口（2026-09-18）

get_stock_story(code, as_of) 返回该股截至 as_of 的"新闻档案"：
  · 事件流：按时间倒序的事件边（金股/评级/股东/分红/催化剂/情绪），各带原文引用
  · PIT 行业：锚点日生效的申万行业
  · 行业热度：同行业近 30 天事件计数（传导语境）
  · 拥挤度：LLM 打分（若有）

双用途：① 智能体问答工具（"XX 最近发生了什么"→ 真实事件流）；② 看板持仓行展开。
全部只读。
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd  # noqa: E402

from app import db  # noqa: E402
from app.logger import setup_logger  # noqa: E402

setup_logger()

EDGE_CN = {"broker_rec": "券商金股", "rating_up": "评级上调", "rating_neu": "研报覆盖",
           "holder_add": "股东增持", "holder_dec": "股东减持", "dividend": "分红送转",
           "catalyst": "研报催化剂", "sentiment": "标题情绪"}


def get_stock_story(code: str, as_of: str = None, days: int = 90) -> dict:
    """该股截至 as_of（缺省=最新数据日）的 events 叙事 JSON。全部只读。"""
    conn = db.get_conn()
    try:
        conn.execute("PRAGMA query_only=1")
        if not as_of:
            as_of = conn.execute("SELECT MAX(trade_date) FROM daily").fetchone()[0]
        at = pd.Timestamp(as_of)
        lo = (at - pd.Timedelta(days=days)).strftime("%Y%m%d")
        ev = pd.DataFrame(conn.execute(
            "SELECT edge_type, date8, entity, direction, strength, quote FROM kg_edge_event "
            "WHERE ts_code=? AND date8>? AND date8<=? ORDER BY date8 DESC",
            (code, lo, as_of)).fetchall(),
            columns=["edge_type", "date8", "entity", "direction", "strength", "quote"])
        # PIT 行业（锚点日生效）: sw_member 的时点窗(in/out) + sw_classify 行业名
        ind = conn.execute(
            "SELECT c.industry_name FROM sw_member m "
            "JOIN sw_classify c ON c.index_code = m.index_code "
            "WHERE m.con_code=? AND c.level='L1' "
            "AND m.in_date<=? AND (m.out_date IS NULL OR m.out_date='') "
            "ORDER BY m.in_date DESC LIMIT 1",
            (code, as_of)).fetchone()
        industry = ind[0] if ind else None
        # 同行业近 30 天事件热度（传导语境）
        heat = 0
        if industry:
            mates = [r[0] for r in conn.execute(
                "SELECT r.code_a FROM kg_edge_relate r JOIN sw_classify c ON c.index_code=r.node "
                "WHERE r.relate_type='industry_pit' AND r.node=? AND r.date8<=?",
                (industry, as_of)).fetchall()]
            if mates:
                ph = ",".join("?" * len(mates))
                heat = conn.execute(
                    f"SELECT COUNT(*) FROM kg_edge_event WHERE ts_code IN ({ph}) "
                    "AND date8>? AND date8<=?",
                    (*mates, (at - pd.Timedelta(days=30)).strftime("%Y%m%d"), as_of)).fetchone()[0]
    finally:
        conn.close()
    # 拥挤度从第三轮打分文件读（非库表）
    crowd = None
    sp = BASE / "data" / "kg" / "excluder_scores.jsonl"
    if sp.exists():
        anchors, by_anchor = _crowd_index(sp)
        best_anchor = max((a for a in anchors if a <= as_of), default=None)
        if best_anchor:
            r = (by_anchor.get(best_anchor) or {}).get(code)
            if r:
                crowd = {"anchor": best_anchor, "score": r["crowd"],
                         "refs": r.get("refs", [])}

    events = []
    for _, r in ev.iterrows():
        events.append({
            "date": r["date8"], "type": EDGE_CN.get(r["edge_type"], r["edge_type"]),
            "entity": r["entity"], "direction": float(r["direction"] or 0),
            "quote": (r["quote"] or "")[:120],
        })
    return {
        "ts_code": code, "as_of": as_of, "window_days": days,
        "industry": industry, "industry_heat_30d": heat,
        "n_events": len(events),
        "events": events[:40],
        "crowd": crowd,          # LLM 拥挤分（无打分则 None）
        "summary": (f"近 {days} 天 {len(events)} 条事件"
                    + (f"，行业 {industry}（同期行业事件 {heat} 条）" if industry else "")),
    }


_CROWD_CACHE = {"mtime": None, "anchors": [], "by_anchor": {}}


def _crowd_index(sp: Path) -> tuple:
    """拥挤分文件的**锚点索引**（模块级缓存，按文件 mtime 失效）。

    2026-09-18 审查修：原先每只持仓都整文件读两遍（103 只 ≈ 31 万次 json.loads，
    `/api/kg/positions_story` 实测 1.08 s、而它挂在看板启动链上）。
    返回 (anchors 升序, {anchor: {ts_code: row}})；文件不存在/读不动 ⇒ 空索引（不抛）。
    """
    try:
        mtime = sp.stat().st_mtime
    except OSError:
        return [], {}
    if _CROWD_CACHE["mtime"] != mtime:
        by_anchor = {}
        for l in sp.open(encoding="utf-8"):
            if not l.strip():
                continue
            r = json.loads(l)
            by_anchor.setdefault(r["anchor"], {})[r["ts_code"]] = r
        _CROWD_CACHE.update(mtime=mtime, anchors=sorted(by_anchor), by_anchor=by_anchor)
    return _CROWD_CACHE["anchors"], _CROWD_CACHE["by_anchor"]
