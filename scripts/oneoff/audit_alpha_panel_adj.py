"""alpha 工厂的面板用**未复权价**、门禁标签用**复权价**：影响面实测（2026-09-19，一次性；**只读**）

## 为什么

`app/alpha/expr.py:247-249` 的 `build_panel()` 把 `fe.load_market()` 的
`open/high/low/close/pre_close` **原样**交给表达式引擎（没乘 `adj`），而同一模块的
`expr.py:52` 口径段自己写着「复权用 `adj_factor`(后复权)」，门禁标签
`app/alpha/gate.py:69` 又是 `m["close"] * m["adj"]`（全收益）。生产价量因子
（`factor_engine.compute_price_factors`）同样一律 `close × adj`。

⇒ 工厂是在拿**一条带人工缺口的价格序列**算因子，再去和**另一条尺**上的未来收益对排序：
除权日 `Ref($close,5)/$close` 这类表达式会把"股价因分红回落"当成"股价下跌"，
`Max($high,20)/$close`、`Rank($close,20)`、RSV/RSI 一族同理。影响的是**门禁判决**
（哪些因子进 `alpha_bank` 的 POOLED 名单），不是今天的生产打分。

## 口径（跑数前写死，不做判决）

- 只测**真实引擎**：`expr.Engine(panel).eval(表达式)`，两条臂只差面板 ——
  RAW = 今天的 `build_panel`（裸价），ADJ = 同一面板的 `open/high/low/close/pre_close`
  × `adj`、`vwap` 同乘（`vwap=amount*10/vol` 是元/股，与 close 同尺，必须一起乘，
  否则 `$close/$vwap` 会先被修好又被弄坏）；其余列（vol/amount/turnover/mv/pe/pb）不动。
- 样本 = **除权最活跃**的 300 只（按 `adj_factor` 变化次数取，效应天然是这些票最大）
  × 2019-01-01~2026-08-31；面板从 `daily` + `adj_factor` 现搭，不读缓存、不碰生产表。
- 主读数是**横截面 Spearman 秩相关**（门禁与 KGD 用的都是排序信息），不是数值差：
  数值差再大，只要横截面次序不动，判决就不动；所以"多少条表达式的秩相关掉到 0.99 以下"
  才是"要不要立项统一尺"的门槛。
- 窗口子集另算一次：只取**该表达式窗口内出现过除权**的日子（效应集中处）。
- ⚠️ **不重跑门禁**（一次全 158 条 × 死区/活跃区要小时级，且那已经是阶段 B 的活）：
  本脚本只给"值不值得立项"的量级。

## 纪律

**不是判决、不改任何代码/口径。** 统一 `build_panel` 的尺 = 改研究口径 ⇒
现有 `alpha_bank` 读数不可比，要走 A/B + 重跑门禁，按 01 号纪律另立一轮。
引用本脚本任何数字必须连"300 只除权活跃样本 / 未重跑门禁 / 秩相关口径"一起引用。

运行：`D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\audit_alpha_panel_adj.py`
产物：`logs/audit_alpha_panel_adj.json`（自己的文件，不写任何生产表）。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

OUT = BASE / "logs" / "audit_alpha_panel_adj.json"
N_CODES = 300
SINCE = "20190101"
UNTIL = "20260831"
PRICE_VARS = ("close", "open", "high", "low", "pre_close", "vwap")
# 每个家族取一条代表(窗口取 20，除权落在窗内的概率够高)：11 族覆盖 158 条的全部算子形态
REPRESENTATIVE = ["KMID", "KLEN", "ROC20", "MA20", "STD20", "BETA20", "RESI20",
                  "MAX20", "MIN20", "QTLU20", "QTLD20", "RANK20", "RSV20", "CORR20"]


def _ro():
    con = sqlite3.connect(f"file:{BASE / 'data' / 'neweco.db'}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=1")
    return con


def sample_codes(con) -> list:
    """除权最活跃的 N 只：adj_factor 相邻日变化的次数。"""
    rows = con.execute(
        "SELECT ts_code, COUNT(*) AS n FROM ("
        "  SELECT ts_code, trade_date, adj_factor,"
        "         LAG(adj_factor) OVER (PARTITION BY ts_code ORDER BY trade_date) AS prev"
        "  FROM adj_factor WHERE trade_date>=? AND trade_date<=?"
        ") WHERE prev IS NOT NULL AND ABS(adj_factor/prev - 1.0) > 1e-9"
        " GROUP BY ts_code ORDER BY n DESC LIMIT ?", (SINCE, UNTIL, N_CODES)).fetchall()
    return [r[0] for r in rows]


def build_panels(con, codes: list) -> tuple:
    """→ (panel_raw, panel_adj, exdiv_mask)：两条臂只差价格列的 `adj`。"""
    q = ",".join("?" * len(codes))
    d = pd.read_sql(f"SELECT ts_code, trade_date, open, high, low, close, pre_close, vol,"
                     f" amount FROM daily WHERE ts_code IN ({q}) "
                     f"AND trade_date>=? AND trade_date<=?", con, params=(*codes, SINCE, UNTIL))
    a = pd.read_sql(f"SELECT ts_code, trade_date, adj_factor FROM adj_factor "
                    f"WHERE ts_code IN ({q}) AND trade_date>=? AND trade_date<=?",
                    con, params=(*codes, SINCE, UNTIL))
    piv = {c: d.pivot_table(index="trade_date", columns="ts_code", values=c)
           for c in ("open", "high", "low", "close", "pre_close", "vol", "amount")}
    adj = a.pivot_table(index="trade_date", columns="ts_code", values="adj_factor")
    dates = pd.to_datetime(piv["close"].index)
    for f in piv.values():
        f.index = dates
    adj.index = dates
    adj = adj.reindex(index=piv["close"].index, columns=piv["close"].columns)

    def _vwap(p):
        return (p["amount"] * 10.0) / p["vol"].replace(0, np.nan)

    raw = dict(piv)
    raw["vwap"] = _vwap(raw)
    adjp = dict(piv)
    for c in ("open", "high", "low", "close", "pre_close"):
        adjp[c] = piv[c] * adj
    adjp["vwap"] = _vwap(piv) * adj          # vwap 是"元/股"，与 close 同尺 ⇒ 必须同乘
    # 除权掩码：该股当日 adj 相对昨日变了 ⇒ 落在 [t-w, t] 的窗口就被污染
    chg = (adj / adj.shift(1) - 1.0).abs() > 1e-9
    return raw, adjp, chg.fillna(False)


def _rank_corr(x: pd.DataFrame, y: pd.DataFrame) -> tuple:
    """逐锚点横截面 Spearman：均值 + 5% 分位 + 低于 0.99 的日子占比。"""
    ics, n_cells = [], 0
    for dt in x.index:
        a, b = x.loc[dt], y.loc[dt]
        ok = a.notna() & b.notna()
        if int(ok.sum()) < 50:
            continue
        r = a[ok].rank().corr(b[ok].rank())
        if pd.notna(r):
            ics.append(float(r))
        n_cells += int(ok.sum())
    if not ics:
        return None
    s = np.asarray(ics)
    return {"n_dates": len(s), "mean": round(float(s.mean()), 5),
            "p05": round(float(np.percentile(s, 5)), 5),
            "below_0p99_share": round(float((s < 0.99).mean()), 4)}


def _value_gap(x: pd.DataFrame, y: pd.DataFrame) -> dict:
    """数值差（相对 |ADJ| 的比例）分布。⚠️ 必须先把 NaN 摊平掉再取分位数 ——
    直接对带 NaN 的 DataFrame 做 np.median/percentile 会得到一串 NaN。"""
    ok = x.notna() & y.notna() & (y.abs() > 1e-12)
    if not bool(ok.any().any()):
        return {}
    rel = ((x - y).abs() / y.abs())[ok].to_numpy().ravel()
    rel = rel[np.isfinite(rel)]
    if rel.size == 0:
        return {}
    return {"n_cells": int(rel.size),
            "median_bp": round(float(np.median(rel)) * 1e4, 1),
            "p95_bp": round(float(np.percentile(rel, 95)) * 1e4, 1),
            "max_bp": round(float(rel.max()) * 1e4, 1)}


def main() -> int:
    from app.alpha import expr as E
    from app.alpha import zoos

    cands = dict((n, e) for n, e, _m in zoos.qlib158())
    missing = [r for r in REPRESENTATIVE if r not in cands]
    if missing:
        print(f"代表表达式在 qlib158() 里找不到: {missing}")
        return 1

    # ---------- ① 静态：158 条里有多少条踩价格水平 ----------
    price_touching = {}
    for name, ex in cands.items():
        used = tuple(sorted({v for v in re.findall(r"\$([a-zA-Z_]+)", ex) if v in PRICE_VARS}))
        if used:
            price_touching[name] = used
    static = {"n_total": len(cands), "n_price_touching": len(price_touching),
              "n_scale_free": len(cands) - len(price_touching),
              "by_family": {k: list(v) for k, v in sorted(
                  {n: price_touching[n] for n in REPRESENTATIVE if n in price_touching}.items())}}

    con = _ro()
    try:
        codes = sample_codes(con)
        raw, adjp, exdiv = build_panels(con, codes)
    finally:
        con.close()
    if len(codes) < 30:
        print(f"样本只有 {len(codes)} 只，不足以读数")
        return 1

    # ---------- ② 量级：真实引擎跑两条臂 ----------
    eng_raw, eng_adj = E.Engine(raw), E.Engine(adjp)
    per_expr = {}
    for name in REPRESENTATIVE:
        ex = cands[name]
        x, y = eng_raw.eval(ex), eng_adj.eval(ex)
        sub = {"expr": ex, "corr_all_dates": _rank_corr(x, y), "value_gap": _value_gap(x, y)}
        # 只取"回看窗口里含除权"的日子(窗口按 20 计, 与 REPRESENTATIVE 的取名一致)
        hit = exdiv.rolling(20, min_periods=1).max().astype(bool).reindex(index=x.index,
                                                                          columns=x.columns)
        xs = x.where(hit & x.notna())
        ys = y.where(hit & y.notna())
        sub["corr_on_window_with_exdiv"] = _rank_corr(xs, ys)
        per_expr[name] = sub
        print(f"  {name:<8} 全期秩相关 "
              f"{(sub['corr_all_dates'] or {}).get('mean')}  "
              f"含除权窗口 {(sub['corr_on_window_with_exdiv'] or {}).get('mean')}  "
              f"数值差 p95 {sub['value_gap'].get('p95_bp')}bp")

    degraded = sorted(n for n, s in per_expr.items()
                      if (s["corr_all_dates"] or {}).get("mean", 1.0) < 0.99
                      or (s["corr_on_window_with_exdiv"] or {}).get("mean", 1.0) < 0.99)
    res = {"sample": {"n_codes": len(codes), "since": SINCE, "until": UNTIL,
                      "pick": "除权次数最多的 300 只(效应上界, 不是全市场均值)"},
           "static_price_dependency": static,
           "per_expression": per_expr,
           "n_representative_degraded_below_0p99": len(degraded),
           "degraded": degraded,
           "how_to_read": ("秩相关是门禁真正用的信息；数值差只说明「因子值本身错了多少」。"
                          "两条臂唯一差别 = 价格列是否乘 adj ⇒ 差全部来自除权日。"),
           "not_a_verdict": "阶段 A 量级实测：未重跑门禁、未改 build_panel、未动 alpha_bank。",
           "next_step": ("若要统一尺 = 改研究口径 ⇒ alpha_bank 既有读数不可比, "
                         "须预注册 + 全 158 条重跑门禁 A/B(01 号纪律)。")}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n158 条里踩价格水平的: {static['n_price_touching']} 条"
          f"（尺度无关 {static['n_scale_free']} 条）")
    print(f"代表样本里秩相关掉到 0.99 以下的: {len(degraded)}/{len(REPRESENTATIVE)} → {degraded}")
    print(f"产物: {OUT.relative_to(BASE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
