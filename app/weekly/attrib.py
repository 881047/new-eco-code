"""周频袖套的**损益归因**（生产版，2026-09-13）—— 规划 §6.1-5「归因覆盖 100% 损益来源」

## 它回答什么

    这一周的收益里：多少来自**风格暴露**、多少来自**行业配置**、多少来自**选股**，
    又有多大一块被**现金拖累**和**交易成本**吃掉？

没有这个，终端就分不清「模型会选股」和「模型正好押在小盘/换手上」——而这两件事在
2024 年以后的含义完全不同（F24 实测：近端毛超额的 78~84% 是风格）。

## 与 F24（研究版：491 期回测）的关系

同一套方法（逐期横截面 Fama-MacBeth + **逐期守恒**的残差），但输入换成**自己的账本**：
  · 持仓 = 净值点上的**真实股数快照** ⇒ 真实市值权重（不是等权近似）；
  · 成本 = 账本**真实成交费用**（不是费率假设）；
  · 窗口 = 袖套自己的历史（不需要重跑 491 期回测，也天然无前视）。
风格因子定义与 `experiments/05_label/F24_attribution.py` **逐字同定义**，
并由 `tests/test_weekly_attrib.py::test_style_panels_match_research` 钉住（防两边漂移 ——
"研究≠生产"这个坑本项目已经踩过三次）。

## 方法（对每对相邻净值点 [t, t+1]）

    r_i − mean_pool(r) = Σ_k x̃_ik·λ_k + Σ_g d̃_ig·λ_g + ε_i        （全部在 t 日截面）

  · `r_i` = 复权价 `open_{t+1}/open_t − 1`（与 F24 的可交易窗口同口径）；
  · 池子 = t 日 **PIT 可交易域**（ST/停牌/新股/流动性四层，见 `pit.engine`）；
  · `x̃` = 风格截面 z 标准化（±3 截尾）+ 缺失指示控制项；`d̃` = 申万一级 PIT 行业去均值哑变量；
  · 组合贡献 = 持仓权重 × 暴露（相对池子）× λ；**残差（选股）= 持仓超额 − 全部列贡献**。

再拆实施缺口（恒等式逐期闭合）：

    净值超额 = 持仓超额（风格+行业+选股）+ 现金拖累 + 其余（费用/滑点/整手/时点）
    现金拖累 = −(1 − 投入率_t) × 持仓收益_t      （基准是满仓池子 ⇒ 闲置现金必然扣分）

## 样本守则与边界

  · 净值点 < `MIN_WEEKS`(8) 只报累计与逐周明细，**不报年化**（与 `ledger.snapshot` 同一条守则）；
  · 旧格式净值点（无 `positions`）跳过该期并在结果里**计数上报**，不静默；
  · λ 是估计量：本模块是**会计分解**，不是显著性检验（不给 t 值下结论）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from app.factors import factor_engine as fe
from app.factors.holder_factors import sw_industry_map_pit
from app.pit.engine import PITEngine
from app.weekly import blocks as wk_blocks
from app.weekly.ledger import Ledger, week_anchors

log = logging.getLogger(__name__)

# 与 F24 逐字同定义（改这里 = 改研究口径, 必须同步 F24 并重跑）
CORE_STYLES = ("size", "mom_20", "mom_60", "vol_20", "turn_20")
FILL_STYLES = ("ep", "bp", "dv", "rev_5")
STYLES = CORE_STYLES + FILL_STYLES
MIN_NAMES = 200
CLIP = 3.0
MIN_WEEKS = 8
# 2026-09-20 第一步量化实测（产物 experiments/b_stage_20260920/per_year_52_sensitivity.json）：
# 周频实际每年 50~53 个锚点（完整年均值 51.182；F66 逐期序列算出 51.2394）⇒ 本常数把几何年化
# 定高约 1.6%：全史池口径超额 17.785/17.469 换成实际期数 = 17.491/17.180（动 0.29pp）。
# 逐条比「被挪动的 pp」与「它在线上的余量」：**无任何在册结论跨线**（最紧的 seedB 留出集余量
# 1.832pp ≫ 挪动 0.038pp；seedA 留出集本来就在线下方 0.424pp）⇒ **维持 52 不改**：改它要同步
# F24/F25/F28/F29/F34 + mirror_pool_reference 五个冻结脚本与全部在册读数，收益只是把第 2 位
# 有效数字挪对。今后引用周频 pp/年 时可注明「本尺偏高 ~1.6%」（14 号三补三十·续）。
PER_YEAR = 52.0
#: 生产默认落盘位置；**实际路径按 `db.DATA_DIR` 现算**（见 `_cache_path`）。
CACHE = Path(__file__).resolve().parent.parent.parent / "data" / "weekly" / "attrib.json"


def _cache_path(cache_path=None) -> Path:
    """缓存路径**现算**，不认模块导入时的常量。

    2026-09-22（D 案接线，同类缺陷一并修）：任何端到端跑 `_weekly_health_slot` 的测试都会
    走到 `refresh()`；写死模块级常量 ⇒ **测试库**算出的空读数会盖掉**生产**缓存
    （块级 IC 那一版就是这么被盖掉的，见 `health._block_cache_path`）。
    `tests/_isolation.py` 已把 `db.DATA_DIR` 指到临时目录 ⇒ 跟着它走，隔离由**路径**保证。
    """
    if cache_path:
        return Path(cache_path)
    try:
        from app import db as _db
        base = Path(getattr(_db, "DATA_DIR", None) or CACHE.parent.parent)
    except Exception:                                     # 拿不到就回退生产默认
        return CACHE
    return base / "weekly" / CACHE.name


def style_panels(m: dict, dates: pd.DatetimeIndex, cols) -> dict:
    """9 个风格暴露（只用 ≤ 当期数据，无前视）。与 F24 同定义。"""
    px = (m["close"] * m["adj"]).astype(np.float64)
    r1 = px.pct_change()
    raw = {
        "size": np.log(m["total_mv"].replace(0, np.nan)),
        "ep": 1.0 / m["pe_ttm"].where(m["pe_ttm"] > 0),
        "bp": 1.0 / m["pb"].where(m["pb"] > 0),
        "dv": m["dv_ttm"],
        "mom_20": px / px.shift(20) - 1.0,
        "mom_60": px / px.shift(60) - 1.0,
        "rev_5": -(px / px.shift(5) - 1.0),
        "vol_20": r1.rolling(20).std(),
        "turn_20": m["turnover"].rolling(20).mean(),
    }
    return {k: v.reindex(index=dates, columns=cols).astype(np.float64) for k, v in raw.items()}


def _design(k: int, styles_np: dict, ind_np: np.ndarray, reg_ok: np.ndarray):
    """设计矩阵：z 标准化风格 + 缺失指示 + 去均值行业哑变量（与 F24 同一套）。"""
    names, cols = [], []
    for name in STYLES:
        v = styles_np[name][k].copy()
        miss = ~np.isfinite(v)
        med = float(np.nanmedian(v[reg_ok]))
        if np.isfinite(med):
            v[miss] = med
        x = v[reg_ok]
        sd = float(np.nanstd(x))
        if not np.isfinite(sd) or sd <= 1e-12:
            continue
        names.append(name)
        cols.append(np.clip((v - float(np.nanmean(x))) / sd, -CLIP, CLIP))
        if miss[reg_ok].any():
            d = miss.astype(np.float64)
            names.append(f"MISS::{name}")
            cols.append(d - d[reg_ok].mean())
    if len(names) < 5:
        return None
    bucket = pd.Series(np.where(pd.isna(ind_np[k]), "未归属", ind_np[k]))
    for g, cnt in bucket[reg_ok].value_counts().items():
        if cnt < 5:
            continue
        d = (bucket.to_numpy() == g).astype(np.float64)
        names.append(f"UIND::{g}" if g == "未归属" else f"IND::{g}")
        cols.append(d - d[reg_ok].mean())
    return names, np.column_stack(cols)


def attribute_periods(points: list, m: dict, pit: PITEngine) -> dict:
    """逐期归因。points = [{date, nav, invested, positions:{code: shares}}] 时间升序。"""
    dates = pd.to_datetime(m["close"].index)
    cols = list(m["close"].columns)
    cp = {c: j for j, c in enumerate(cols)}
    pos_of = {d: i for i, d in enumerate(dates)}
    anchors = pd.to_datetime([p["date"] for p in points])
    mask = pit.tradable_matrix(anchors, columns=cols).fillna(False).to_numpy()
    styles_np = {k: v.to_numpy(dtype=np.float64)
                 for k, v in style_panels(m, anchors, cols).items()}
    ind_np = sw_industry_map_pit(anchors, "L1").reindex(index=anchors, columns=cols).to_numpy(object)
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()

    recs, skipped = [], 0
    for k in range(len(points) - 1):
        a, b = points[k], points[k + 1]
        hold = a.get("positions") or {}
        if not hold:
            skipped += 1                                  # 旧格式净值点(无快照) ⇒ 跳过并计数
            continue
        e0, e1 = pos_of.get(pd.Timestamp(a["date"])), pos_of.get(pd.Timestamp(b["date"]))
        if e0 is None or e1 is None or e1 <= e0:
            skipped += 1
            continue
        r = op_adj[e1] / op_adj[e0] - 1.0
        ret_ok = np.isfinite(r) & np.isfinite(op_adj[e0]) & (op_adj[e0] > 0)
        pool = mask[k] & ret_ok
        if pool.sum() < MIN_NAMES:
            skipped += 1
            continue
        bench = float(np.mean(r[pool]))
        y = r - bench
        # 真实市值权重(用 t 日收盘×股数; 复权不影响权重之间的比例)
        px0 = m["close"].reindex(index=dates, columns=cols).to_numpy(np.float64)[e0]
        codes = [c for c in hold if c in cp]
        if len(codes) < 20:
            skipped += 1
            continue
        jdx = np.array([cp[c] for c in codes], dtype=int)
        shares = np.array([max(float(hold[c]), 0.0) for c in codes], dtype=np.float64)
        pxv = np.where(np.isfinite(px0[jdx]), px0[jdx], 0.0)
        val = shares * pxv
        okv = np.isfinite(val) & (val > 0) & ret_ok[jdx]
        if okv.sum() < 20:
            skipped += 1
            continue
        hidx = jdx[okv]
        wv = val[okv] / val[okv].sum()
        wfull = np.zeros(len(cp), dtype=np.float64)
        wfull[hidx] = wv
        hold_ok = np.zeros(len(cp), dtype=bool)
        hold_ok[hidx] = True
        port_ret = float(np.sum(wv * r[hidx]))
        reg_ok = pool.copy()
        for n in CORE_STYLES:
            reg_ok &= np.isfinite(styles_np[n][k])
        if reg_ok.sum() < MIN_NAMES:
            skipped += 1
            continue
        d = _design(k, styles_np, ind_np, reg_ok)
        if d is None:
            skipped += 1
            continue
        names, A = d
        Aok, yok = A[reg_ok], y[reg_ok]
        if not (np.isfinite(Aok).all() and np.isfinite(yok).all()):
            skipped += 1
            continue
        beta, *_ = np.linalg.lstsq(Aok, yok, rcond=None)
        h_ok = hold_ok & reg_ok
        wreg = wfull[h_ok]
        if wreg.sum() <= 0:
            skipped += 1
            continue
        wreg = wreg / wreg.sum()
        expo = (A[h_ok] * wreg[:, None]).sum(axis=0)      # 组合**加权暴露**(未乘 λ)
        contrib = expo * beta                             # 组合暴露 × λ
        style = {n: float(contrib[j]) for j, n in enumerate(names)
                 if not n.startswith(("IND::", "UIND::", "MISS::"))}
        ind = {n: float(contrib[j]) for j, n in enumerate(names) if n.startswith(("IND::", "UIND::"))}
        miss = {n: float(contrib[j]) for j, n in enumerate(names) if n.startswith("MISS::")}
        invested_ratio = (float(a["invested"]) / float(a["nav"])) if float(a.get("nav") or 0) else 0.0
        nav_ret = float(b["nav"]) / float(a["nav"]) - 1.0 if float(a.get("nav") or 0) else np.nan
        rec = {"date": str(b["date"]), "n_hold": int(hold_ok.sum()),
               "pool": int(pool.sum()), "hold_cov": round(float(h_ok.sum() / len(hidx)), 4),
               "bench_ret": bench, "port_ret": port_ret,
               "port_excess": port_ret - bench, "nav_excess": nav_ret - bench,
               "invested": round(invested_ratio, 4),
               "cash_drag": -(1.0 - invested_ratio) * port_ret,
               "style": style, "industry": ind, "missing": miss}
        rec["style_total"] = float(sum(style.values()))
        rec["industry_total"] = float(sum(ind.values()))
        rec["missing_total"] = float(sum(miss.values()))
        rec["specific"] = rec["port_excess"] - contrib.sum()        # 逐期守恒
        rec["impl_gap"] = rec["nav_excess"] - rec["port_excess"]
        rec["other"] = rec["impl_gap"] - rec["cash_drag"]
        rec["fees"] = float(b.get("fees") or 0.0)
        # ---- 块级分解（D 案 ③-2）：把**风格**那部分按六块归并 ----
        # 口径：块贡献 = 块内风格列的持仓暴露加权贡献之和（`STYLE_BLOCK` 见 blocks.py）。
        # ⚠️ 如实声明：B6（龙虎榜事件）在 Fama-MacBeth 风格集里**没有对应列** ⇒ 本口径下恒为 0，
        # 这不是"它没有贡献"，而是"这套回归看不见它"。行业/缺失指示/选股残差同样不属于任何块，
        # 单列成 `未归类` —— **藏进残差就是自欺**，所以恒等式要求 7 项之和 = 持仓超额。
        blk = {k: 0.0 for k in wk_blocks.BLOCK_KEYS}
        for n, v in style.items():
            b = wk_blocks.STYLE_BLOCK.get(n, "")
            if b:
                blk[b] += float(v)
        blk[wk_blocks.UNATTRIBUTED] = rec["port_excess"] - float(sum(blk.values()))
        rec["blocks"] = blk
        rec["block_total"] = float(sum(blk.values()))       # 恒等于 port_excess（构造保证）
        # B1 规模暴露（供 `protocol` 的暴露守卫用）：size 列的**加权暴露**，单位 = 池内 σ
        _j = names.index("size") if "size" in names else None
        rec["size_z"] = None if _j is None else float(expo[_j])
        recs.append(rec)
    return {"periods": recs, "skipped": skipped,
            "n_points": len(points), "window": [str(points[0]["date"]), str(points[-1]["date"])]}


def summarize(res: dict) -> dict:
    """把逐期记录压成一页读数（pp/年 = 平均每期 × 52；同时给累计口径）。"""
    recs = res.get("periods") or []
    if not recs:
        return {"n": 0}

    def mean_pp(key):
        s = pd.Series([r.get(key, np.nan) for r in recs], dtype=float).dropna()
        return None if s.empty else round(100 * PER_YEAR * float(s.mean()), 3)

    styles = {}
    for name in STYLES:
        s = pd.Series([r["style"].get(name, np.nan) for r in recs], dtype=float).dropna()
        if not s.empty:
            styles[name] = round(100 * PER_YEAR * float(s.mean()), 3)
    # 累计口径用**比值**而不是把逐期超额连乘(后者在高波动下会失真):
    #   净值指数 = Π(1+nav_ret), 基准指数 = Π(1+bench_ret), 累计超额 = 净值/基准 − 1
    nav_idx = float(pd.Series([1 + r["nav_excess"] + r["bench_ret"]
                               for r in recs], dtype=float).prod())
    bench_idx = float(pd.Series([1 + r["bench_ret"] for r in recs], dtype=float).prod())
    st, it, mt = mean_pp("style_total"), mean_pp("industry_total"), mean_pp("missing_total")
    sp, pe = mean_pp("specific"), mean_pp("port_excess")
    identity = None if None in (st, it, mt, sp, pe) else round(st + it + mt + sp - pe, 3)
    # ---- 块级汇总（D 案 ③-2）----
    blocks = {}
    for k in tuple(wk_blocks.BLOCK_KEYS) + (wk_blocks.UNATTRIBUTED,):
        s = pd.Series([(r.get("blocks") or {}).get(k, np.nan) for r in recs],
                      dtype=float).dropna()
        if not s.empty:
            blocks[k] = round(100 * PER_YEAR * float(s.mean()), 3)
    # 恒等式实测：逐期 |Σ块贡献(含未归类) − 持仓超额| 的最大值（构造上应 ~1e-18，
    # 报出来是为了让"哪里被藏起来了"这件事无处可藏；阈值 1e-6 由单测钉住）
    devs = [abs(float(r["block_total"]) - float(r["port_excess"])) for r in recs
            if np.isfinite(r.get("block_total", np.nan)) and np.isfinite(r["port_excess"])]
    named = {k: v for k, v in blocks.items() if k in wk_blocks.BLOCK_KEYS}
    top = max(named.items(), key=lambda kv: kv[1]) if named else None
    worst = min(named.items(), key=lambda kv: kv[1]) if named else None
    sz = pd.Series([r.get("size_z") for r in recs], dtype=float).dropna()
    out = {"n": len(recs), "skipped": res.get("skipped", 0), "window": res.get("window"),
           "port_excess_pp": mean_pp("port_excess"),
           "nav_excess_pp": mean_pp("nav_excess"),
           "style": styles, "style_total_pp": mean_pp("style_total"),
           "industry_total_pp": mean_pp("industry_total"),
           "missing_total_pp": mean_pp("missing_total"),
           "specific_pp": mean_pp("specific"),
           "cash_drag_pp": mean_pp("cash_drag"), "other_pp": mean_pp("other"),
           "impl_gap_pp": mean_pp("impl_gap"),
           "fees_pp": mean_pp("fees"), "invested": round(
               float(pd.Series([r["invested"] for r in recs]).mean()), 4),
           "cum_nav_excess": round(nav_idx / bench_idx - 1, 4),
           "identity_check_pp": identity,
           "blocks": blocks,
           "block_identity_max_dev": round(max(devs), 12) if devs else None,
           "top_block": {"block": top[0], "pp": top[1],
                         "role": wk_blocks.BLOCK_ROLE.get(top[0], "")} if top else None,
           "worst_block": {"block": worst[0], "pp": worst[1],
                           "role": wk_blocks.BLOCK_ROLE.get(worst[0], "")} if worst else None,
           "unattributed_pp": blocks.get(wk_blocks.UNATTRIBUTED),
           "size_z_mean": None if sz.empty else round(float(sz.mean()), 4),
           "size_z_last": None if sz.empty else round(float(sz.iloc[-1]), 4)}
    return out


def invested_ratio_note(weight) -> str:
    """看板文案：投入率与**当前**权益权重对比的说明（纯函数，便于钉住）。

    2026-09-13：原实现把 0.873 写死在字符串里（旧敞口/研究构型的字样），而现役
    `weekly.equity_weight` 已是 1.0 ⇒ 看板说明与实盘配置不符。**权重必须由调用方传入**。
    """
    return (f"投入率 < 权益权重({weight}) 的部分 = 整手粒度/买不起"
            " ⇒ F25 实测这是一条**敞口刻度**")


def current(window: int = 52, ledger_path=None, since: str = None) -> dict:
    """账本 → 归因读数。任何异常都返回 `available=False`（监控/看板不能被它带崩）。"""
    try:
        led = Ledger.load(0.0, ledger_path)
        pts = (led.state.get("nav_history") or [])[-(window + 1):]
        if len(pts) < 2:
            return {"available": False, "n_points": len(pts),
                    "note": "净值点不足两期, 归因要有两期才能算(先跑几次 exec)"}
        fees = {}
        for t in led.state.get("trades") or []:
            fees[t.get("date")] = fees.get(t.get("date"), 0.0) + float(t.get("fee") or 0.0)
        pts = [dict(p, fees=fees.get(p.get("date"), 0.0)) for p in pts]
        first = str(pts[0]["date"])
        start = since or (pd.Timestamp(first) - pd.Timedelta(days=420)).strftime("%Y%m%d")
        m = fe.load_market(since=start)
        pit = PITEngine(m=m)
        res = attribute_periods(pts, m, pit)
        s = summarize(res)
        s.update({"available": True, "first": first, "last": str(pts[-1]["date"])})
        # 2026-09-13 修正（同类缺陷扫描）：原来这里写死 "权益权重(0.873)" —— 那是研究构型/旧敞口的字样，
        # 而现役 weekly.equity_weight 已是 1.0 ⇒ 看板上的说明与实盘配置不符。改为**读实时配置**。
        from app import config as cfg_mod
        from app.weekly import plan as wk_plan
        try:
            _w = float(wk_plan.equity_weight(cfg_mod.load_config()))
        except Exception:
            _w = None
        s["invested_ratio_note"] = invested_ratio_note(_w if _w is not None else "?")
        # 2026-09-21 修正: `s["n"]` 是归因**期**数, 而 pts 来自账本净值点(每天盯市一个)
        # ⇒ 它是**日点**数, 不是周数; 8 个日点(≈1.6 周)就会越过"≥8 期才报年化"。
        # 改用真·自然周(唯一口径, 见 `ledger.week_anchors`)。判据数值未放宽。
        n_anchor = week_anchors([p.get("date") for p in pts if p.get("date")])
        s["n_week_anchors"] = n_anchor
        if n_anchor < MIN_WEEKS:
            s["note"] = (f"样本 {s['n']} 个归因期只覆盖 **{n_anchor} 个自然周**"
                         f"(守则: ≥{MIN_WEEKS} 周才报年化; 日点≈周数×5, 不可混用);"
                         f" 当前只有累计口径可读")
        return s
    except Exception as e:                     # 归因是"读数", 绝不能把链子带崩
        log.exception("周频归因异常")
        return {"available": False, "error": str(e)}


def lines(s: dict) -> list:
    """人读若干行（CLI / 邮件 / 看板共用）。"""
    if not s.get("available"):
        return [f"归因不可用: {s.get('note') or s.get('error') or '样本不足'}"]
    out = [f"窗口 {s['window'][0]}~{s['window'][1]} | {s['n']} 期 | 投入率 {s['invested']:.3f}"
           f" | 跳过期数 {s['skipped']}"]
    if s.get("port_excess_pp") is not None:
        out.append(f"持仓超额 {s['port_excess_pp']:+.2f}pp/年"
                   f" = 风格 {s['style_total_pp']:+.2f}(size {s['style'].get('size', 0):+.2f})"
                   f" + 行业 {s['industry_total_pp']:+.2f} + 选股 {s['specific_pp']:+.2f}"
                   f" + 缺失项 {s['missing_total_pp']:+.2f}")
        out.append(f"净值超额 {s['nav_excess_pp']:+.2f}pp/年 = 持仓超额 + 现金拖累 "
                   f"{s['cash_drag_pp']:+.2f} + 其余 {s['other_pp']:+.2f}(含费用 {s['fees_pp']:+.2f})"
                   f" | 累计净值超额 {100 * s['cum_nav_excess']:+.2f}%")
    if s.get("blocks"):
        bl = s["blocks"]
        out.append("块贡献(pp/年): " + " ".join(
            f"{k} {bl[k]:+.2f}" for k in wk_blocks.BLOCK_KEYS if k in bl)
            + f" | {wk_blocks.UNATTRIBUTED} {bl.get(wk_blocks.UNATTRIBUTED, 0):+.2f}"
            + "（B6 在本口径无对应风格列 ⇒ 恒 0，不代表它没有贡献）")
        if s.get("top_block"):
            t, w = s["top_block"], s["worst_block"]
            out.append(f"  最大块贡献 {t['block']}({t['role']}) {t['pp']:+.2f}"
                       f" | 最大块拖累 {w['block']}({w['role']}) {w['pp']:+.2f}")
    if s.get("note"):
        out.append(f"注: {s['note']}")
    return out


def refresh(cache_path=None) -> dict:
    """算一次归因并**落缓存**。

    为什么要有缓存: `current()` 要拉约两年行情 + 跑横截面回归（实测 ~60s），
    挂在 HTTP 端点上会让看板卡死；所以由 19:45 档算一次、看板/API 只读这份文件。
    """
    res = current()
    p = _cache_path(cache_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    except OSError:
        log.warning("归因缓存写入失败: %s", p)
    return res


def cached(cache_path=None) -> dict:
    """读归因缓存（看板/API 用）。没有缓存时给出可执行的下一步，不是空白。"""
    p = _cache_path(cache_path)
    if not p.exists():
        return {"available": False,
                "note": "还没有归因缓存 —— 19:45 档会自动算；也可手动 "
                        "`scripts/run_weekly.py attrib --refresh`"}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                      # 缓存坏了也不能把看板带崩
        log.exception("归因缓存读取失败")
        return {"available": False, "error": str(e)}
