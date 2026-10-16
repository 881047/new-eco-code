r"""三模型选股榜 Top100 + 交集（2026-10-08）：`/api/model_picks` 的数据层。

需求（用户 2026-10-08）：Final/W2 盘面页对标 NEW ECO 功能 + 三个盘面
选股前一百名的交集展示。本模块与 all_accounts.py 同纪律：

- **纯只读**：SQLite ``mode=ro``、JSON 只 load；不写任何外部仓。
- **单源故障隔离**：单模型读数失败只影响该模型的榜，不影响交集页其余部分。
- **口径如实**：三个模型的"选股"频率与宇宙不同（NEW ECO=月频模型的每日推分、
  Final=日频 dlv2 EMA 平滑秩、W2=周频 B0 规则全序），交集小是结构性的，
  注记如实展示，不编数。

各模型 Top100 的出处（单源，改动须三处同步：本文件 + 各仓 dump 脚本 + 前端口径文案）：
- NEW ECO：本仓 ``daily_score`` 最新 trade_date 按 score 降序 head(100)（每日 18:00 日链推分）。
- Final：``D:\\Final\\data\\state\\fterm.db`` 的 ``ema_state`` kv ``ema``（dlv2 全截面
  预测的 EMA 平滑秩，日频 20:05 档更新，asof=``ema_asof``），按值降序 head(100)。
  注意：盘面键为 ``Final``，仓库 ``D:\\Final``，代码包名仍为 ``fterm``——三者不是同一件事。
- W2：sidecar ``data/ecow2_picks_top100.json``——由 **ECO-W2 仓（D:\\ECO-W2）自己的规范代码**
  （``construct.build_frames`` + ``rules.full_order``）离线算出并自校验
  （head(6)/head(8) == 账本当期 picks），服务进程不 import w2（跨仓隔离 + 口径保真）。
"""
from __future__ import annotations

import json
import time

from app.all_accounts import (_f, _names, _norm_date, _ro, FTERM_W2_DB, NEWECO_DB)

TOP_N = 100
ECOW2_PICKS_SIDECAR = r"D:\NEW ECO\data\ecow2_picks_top100.json"

#: 模型键序与 acctCols.MODEL_ORDER / all_accounts.MODEL_SCHEDULE 一致（契约测试 ⑱ 同源）。
MODEL_KEYS = ("NEW ECO", "Final", "W2")


def _clean(x):
    v = _f(x)
    return v


# --------------------------------------------------------------- 三模型榜


def _neweco_picks():
    """daily_score 最新交易日 Top100（每日 18:00 推分；score 为截面分位量纲 0-100）。"""
    con = _ro(NEWECO_DB)
    try:
        (td,) = con.execute("SELECT MAX(trade_date) FROM daily_score").fetchone()
        if not td:
            raise RuntimeError("daily_score 为空")
        rows = con.execute(
            "SELECT ts_code, score, pred_ret FROM daily_score WHERE trade_date=?"
            " ORDER BY score DESC LIMIT ?", (td, TOP_N)).fetchall()
    finally:
        con.close()
    names = _names()
    picks = [{"rank": i + 1, "code": c, "name": names.get(c), "score": _clean(s),
              "pred_ret": _clean(p)} for i, (c, s, p) in enumerate(rows)]
    return {"ok": bool(picks), "asof": _norm_date(td), "n": len(picks),
            "source": "daily_score 每日推分（月频模型的全市场日度打分）",
            "picks": picks, "error": None if picks else "daily_score 无数据"}


def _fterm_picks():
    """fterm dlv2 EMA 平滑秩 Top100（kv ``ema`` 存于 w2 轨库、双轨共享）。"""
    con = _ro(FTERM_W2_DB)
    try:
        row = con.execute("SELECT val FROM ema_state WHERE k='ema'").fetchone()
        asof_row = con.execute("SELECT val FROM ema_state WHERE k='ema_asof'").fetchone()
    finally:
        con.close()
    if not row:
        raise RuntimeError("ema_state 无 'ema' 键（20:05 档尚未跑过决策环）")
    ema = json.loads(row[0])
    names = _names()
    ranked = sorted(((c, _f(v)) for c, v in ema.items() if _f(v) is not None),
                    key=lambda x: (-x[1], x[0]))[:TOP_N]
    picks = [{"rank": i + 1, "code": c, "name": names.get(c), "ema": v}
             for i, (c, v) in enumerate(ranked)]
    return {"ok": bool(picks), "asof": _norm_date(asof_row[0]) if asof_row else None,
            "n": len(picks), "n_universe": len(ema),
            "source": "dlv2 EMA 平滑秩（日频全截面，α=0.5；20:05 档更新）",
            "picks": picks, "error": None if picks else "EMA 状态为空"}


def _ecow2_picks():
    """ECO-W2 B0 规则全序 Top100（sidecar 由 ECO-W2 仓规范代码离线产出）。"""
    try:
        with open(ECOW2_PICKS_SIDECAR, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError) as e:
        return {"ok": False, "asof": None, "n": 0, "source": "B0 规则全序",
                "picks": [], "error": f"sidecar 不可读（等待 ECOW2_picks_top100_daily）: {e}"}
    names = _names()
    raw = d.get("picks") or []
    picks = [{"rank": p.get("rank"), "code": p.get("code"), "name": names.get(p.get("code")),
              "total_mv": _clean(p.get("total_mv")), "close": _clean(p.get("close"))}
             for p in raw[:TOP_N]]
    return {"ok": bool(picks) and bool(d.get("verified")),
            "asof": _norm_date(d.get("anchor")), "n": len(picks),
            "anchor": d.get("anchor"), "t1": d.get("t1"),
            "n_full_order": d.get("n_full_order"),
            "verified": bool(d.get("verified")),
            "generated_at": d.get("generated_at"),
            "source": "B0 规则全序（子池 close≤20 元 ∧ amt20 门槛内按 total_mv 升序，"
                      "与 k 无关；ECO-W2 仓规范代码离线算出并对照账本 picks 自校验）",
            "picks": picks,
            "error": None if (picks and d.get("verified")) else (
                "sidecar 未通过账本自校验" if picks else "sidecar picks 为空")}


# --------------------------------------------------------------- 交集


def _intersect(blocks):
    """blocks: {model: {ok, picks}} → triple/pairwise 交集（只统计 ok 的模型）。"""
    ok = {m: {p["code"]: p["rank"] for p in (b.get("picks") or [])}
          for m, b in blocks.items() if b.get("ok")}
    models = [m for m in MODEL_KEYS if m in ok]
    if len(models) < 2:
        return {"triple": [], "pairs": {}, "counts": {}, "note": "可用模型不足两个，交集无意义"}
    codesets = {m: set(ok[m]) for m in models}
    triple_codes = set.intersection(*codesets.values()) if len(models) >= 3 else set()
    names = _names()
    triple = [{"code": c, "name": names.get(c),
               "ranks": {m: ok[m][c] for m in models if c in ok[m]}}
              for c in sorted(triple_codes)]
    pairs = {}
    for i, m1 in enumerate(models):
        for m2 in models[i + 1:]:
            inter = sorted(codesets[m1] & codesets[m2])
            pairs[f"{m1} ∩ {m2}"] = {
                "n": len(inter),
                "codes": [{"code": c, "name": names.get(c),
                           "ranks": {mm: ok[mm][c] for mm in (m1, m2)}} for c in inter],
            }
    counts = {m: len(codesets[m]) for m in models}
    counts.update({f"triple": len(triple)})
    counts.update({f"pair:{k}": v["n"] for k, v in pairs.items()})
    return {"triple": triple, "pairs": pairs, "counts": counts,
            "note": ("三盘面宇宙与频率不同（NEW ECO 月频模型日推分 / Final 日频 EMA 秩 / "
                     "W2 周频 B0 小市值规则），交集小是结构性的——交集名单=三套独立"
                     "证据同时点名的股票，属强共识读数而非预期高产")}


# --------------------------------------------------------------- 聚合

_CACHE = {"ts": 0.0, "payload": None}
_CACHE_TTL = 60.0


def build_picks(force=False):
    """三模型 Top100 + 交集；60s 进程内缓存（榜单是日频数据，不必更频）。"""
    now = time.time()
    if not force and _CACHE["payload"] and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["payload"]

    builders = {"NEW ECO": _neweco_picks, "Final": _fterm_picks, "W2": _ecow2_picks}
    models = {}
    for m in MODEL_KEYS:
        try:
            models[m] = builders[m]()
        except Exception as exc:  # 单源故障隔离（与 all_accounts 同风格）
            models[m] = {"ok": False, "asof": None, "n": 0, "picks": [],
                         "source": "", "error": str(exc)[:300]}

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "top_n": TOP_N,
        "models": models,
        "intersection": _intersect(models),
    }
    _CACHE["ts"] = now
    _CACHE["payload"] = payload
    return payload
