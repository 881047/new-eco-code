# -*- coding: utf-8 -*-
"""月度选股规则「长期期望是否为正」回测 —— 只读（预注册 `plans/monthly-rule-expectancy-prereg-20260923.md` 的执行体）。

冻结判据 → 本实现一一对应（改任何一条 = 换判据，必须先改预注册文件）：
  §二 数据   : model_score 116 锚点(20170126~20260831) / daily open(建仓·换仓)+close(估值) /
               adj_factor 统一复权尺 / trade_cal(is_open=1) / 主基准 000905.SH、对照 000300.SH
  §三 规则   : 按 score 降序取前 k(k∈{5,10,20,30,50}) 等权；锚点后第一个开市日开盘建仓；
               持有到下一锚点后第一个开市日开盘全卖全买；无开盘价/停牌/上市不足 60 交易日的票
               剔除后顺位补足 —— 可交易域直接用生产 `app/pit/engine.py::PITEngine.tradable_mask`
               (六层掩码/时点名称/首现日锚定，与生产选股域同口径)；
               **不建模涨跌停无法成交**(§七.2 冻结的乐观偏差，各臂同偏差)
  §四 成本   : 直接 import 生产 `app/portfolio/fees.py::trade_cost`(佣金 max(万2.5,5元)+
               卖出印花税按成交日+过户费)，费率参数走 config `fees` 段单一真源；
               资金两档：A 档 11,898 元(现行口径冻结数)/ B 档 2,000×k；资金逐期复利滚动
  §五 判据   : t = m/(s/√n)；配对差 d_t = r_t − r^BM_t；判决格：m>0 且 t≥2 ⇒「存在正期望」/
               t<2 ⇒「不可辨」/ m≤0 ⇒「未见正期望」(仅 m<0 且 t≤−2 才判「存在负期望」)；
               **不挑最优 k**(PBO=0.434 的教训)，k 之间只报差异与单调性
  §六 稳健性 : 分两段 2017-01~2021-12 / 2022-01~2026-08，各报中位/胜率/年化(×12 与几何)/MDD

产出: experiments/monthly_rule_expectancy.json（逐期序列 + 各 k 统计量 + 两档资金 + 两段稳健性 + 判决）
只读声明: 生产表只 SELECT(model_score/daily/daily_basic/adj_factor/trade_cal/index_daily/
stock_basic/namechange)；不写任何生产表、不改成交口径、不换 pin。行情面板经
`load_market` 读取(可能按内容指纹刷新 data/cache/market 的 parquet 缓存 —— 缓存非生产表)。
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from app.factors.factor_engine import load_market  # noqa: E402
from app.pit.engine import PITEngine               # noqa: E402
from app.portfolio.fees import fee_params, trade_cost  # noqa: E402
from app import config as cfg_mod                  # noqa: E402

DB = BASE / "data" / "neweco.db"
OUT = BASE / "experiments" / "monthly_rule_expectancy.json"
PREREG = "plans/monthly-rule-expectancy-prereg-20260923.md"

# ---- §三/§四/§五 冻结参数（与预注册逐字一致） ----
KS = [5, 10, 20, 30, 50]
CAP_A = 11898.0            # A 档 现行口径：股票部分总资金(冻结)
CAP_B_PER_STOCK = 2000.0   # B 档 = 2,000 × k
BM_MAIN = "000905.SH"      # 主基准 中证500
BM_ALT = "000300.SH"       # 稳健性对照 沪深300（不参与判决）
T_CRIT = 2.0               # 判决门槛(冻结，非 1.96)
SPLIT1_END = "20211231"    # 分段：≤20211231 为 2017-01~2021-12 段
N_ANCHORS_EXPECT = 116
FIRST_ANCHOR, LAST_ANCHOR = "20170126", "20260831"


def judge(m: float, t: float) -> str:
    """§五判决格（只认 m/t，不许引申）"""
    if m > 0 and t >= T_CRIT:
        return "存在正期望"
    if m > 0:
        return "不可辨"
    if m < 0 and t <= -T_CRIT:
        return "存在负期望"
    return "未见正期望"


def _buy_net_amount(gross: float, p: dict) -> float:
    """等权买入的净成交额 A：解 A + trade_cost(A,'buy') = gross（把买入费用吃进预算）。
    trade_cost(buy) = max(rate·A, min) + tfr·A，两段解析解。"""
    rate, tfr, mn = p["commission_rate"], p["transfer_fee"], p["min_commission"]
    if (rate + tfr) * gross >= mn:
        return gross / (1.0 + rate + tfr)
    return (gross - mn) / (1.0 + tfr)


def stats(rs) -> dict:
    r = np.asarray(rs, dtype=float)
    n = len(r)
    m = float(r.mean())
    s = float(r.std(ddof=1))
    t = m / (s / math.sqrt(n)) if s > 0 else float("nan")
    nav = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(nav)
    return {
        "n": n,
        "mean_month": round(m, 6),
        "t": round(t, 4),
        "win_rate": round(float((r > 0).mean()), 4),
        "median_month": round(float(np.median(r)), 6),
        "ann_x12": round(m * 12, 6),                      # ×12 口径
        "ann_geo": round(float(nav[-1] ** (12.0 / n) - 1.0), 6),  # 几何口径
        "mdd": round(float((nav / peak - 1.0).min()), 4),
        "cum": round(float(nav[-1] - 1.0), 6),
    }


def stats_diff(ds) -> dict:
    """配对差序列只报统计量 —— d_t 不是收益, cumprod/年化/MDD 对它无意义(禁止计算)。"""
    r = np.asarray(ds, dtype=float)
    n = len(r)
    m = float(r.mean())
    s = float(r.std(ddof=1))
    t = m / (s / math.sqrt(n)) if s > 0 else float("nan")
    return {"n": n, "mean_month": round(m, 6), "t": round(t, 4),
            "win_rate": round(float((r > 0).mean()), 4),
            "median_month": round(float(np.median(r)), 6)}


def main() -> None:
    t0 = time.time()
    cfg = cfg_mod.load_config()
    fee_p = fee_params(cfg)
    con = sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)

    # ---------- §二 锚点与建仓日 ----------
    anchors = [r[0] for r in con.execute(
        "SELECT DISTINCT trade_date FROM model_score ORDER BY trade_date")]
    assert len(anchors) == N_ANCHORS_EXPECT, f"锚点数 {len(anchors)} ≠ 冻结的 {N_ANCHORS_EXPECT}"
    assert anchors[0] == FIRST_ANCHOR and anchors[-1] == LAST_ANCHOR, \
        f"锚点端点变了: {anchors[0]}~{anchors[-1]} ≠ 冻结 {FIRST_ANCHOR}~{LAST_ANCHOR}"

    cal = [r[0] for r in con.execute(
        "SELECT cal_date FROM trade_cal WHERE is_open=1 ORDER BY cal_date")]
    cal_arr = np.array(cal)
    build_dates = []
    for a in anchors:
        nxt = cal_arr[cal_arr > a]
        assert len(nxt), f"锚点 {a} 之后无开市日"
        build_dates.append(str(nxt[0]))
    assert len(set(build_dates)) == len(build_dates), "建仓日重复(锚点过密?)"
    print(f"锚点 {len(anchors)} 个, 建仓日 {build_dates[0]}~{build_dates[-1]}, 共 {len(set(build_dates))} 个")

    # ---------- 行情面板（生产同一份实现；缓存命中则秒级） ----------
    panel = load_market(since=None)
    pit = PITEngine(m=panel, cfg=cfg)
    dates_idx = pit.dates
    open_df, close_df, adj_df = panel["open"], panel["close"], panel["adj"]
    cols_set = set(map(str, open_df.columns))
    for b in build_dates:
        assert pd.Timestamp(b) in dates_idx, f"建仓日 {b} 不在行情面板内"

    # ---------- §二 基准（主/对照） ----------
    bm = {}
    for code in (BM_MAIN, BM_ALT):
        df = pd.read_sql(
            "SELECT trade_date, open, close FROM index_daily WHERE ts_code=?",
            con, params=(code,))
        s_open = df.set_index("trade_date")["open"]
        s_close = df.set_index("trade_date")["close"]
        bm[code] = (s_open, s_close)

    # ---------- 打分面板 ----------
    sc = pd.read_sql("SELECT trade_date, ts_code, score FROM model_score", con)
    con.close()
    score_by_anchor = {a: g.set_index("ts_code")["score"] for a, g in sc.groupby("trade_date")}
    del sc

    # ---------- §三 逐锚点：生产域 → 顺位补足 → 前 50 只合格名单 ----------
    TOPN = max(KS)
    elig_top = []          # 每期前 TOPN 合格代码
    audit_rows = []        # 每期域审计
    for i, a in enumerate(anchors):
        b = pd.Timestamp(build_dates[i])
        ranked = score_by_anchor[a].dropna().sort_values(ascending=False)
        uni = pit.tradable_mask(b)
        uni_r = uni.reindex(ranked.index).fillna(False)
        n_uni = int(uni_r.sum())
        o_row = open_df.loc[b]
        a_row = adj_df.loc[b]
        elig, n_no_open, n_no_adj = [], 0, 0
        for code, ok in uni_r.items():
            if not ok:
                continue
            o = o_row.get(code)
            adj = a_row.get(code) if code in cols_set else np.nan
            if not (o is not None and o == o and o > 0):
                n_no_open += 1
                continue
            if not (adj == adj):
                n_no_adj += 1
                continue
            elig.append(str(code))
            if len(elig) >= TOPN:
                break
        assert len(elig) >= TOPN, f"{build_dates[i]} 合格候选仅 {len(elig)} < {TOPN}"
        elig_top.append(elig)
        audit_rows.append({
            "anchor": a, "build": build_dates[i], "n_scored": int(len(ranked)),
            "n_in_universe": n_uni, "n_no_open_walked": n_no_open, "n_no_adj_walked": n_no_adj,
        })
        if (i + 1) % 20 == 0:
            print(f"  域扫描 {i + 1}/{len(anchors)} ({time.time() - t0:.0f}s)")

    # ---------- 逐期公共数据（所有臂共享） ----------
    n_periods = len(anchors) - 1
    assert n_periods == 115
    period_meta = [{"anchor": anchors[i], "build": build_dates[i],
                    "next_build": build_dates[i + 1]} for i in range(n_periods)]
    per_period = []
    n_suspended_swap = 0
    for i in range(n_periods):
        b0, b1 = pd.Timestamp(build_dates[i]), pd.Timestamp(build_dates[i + 1])
        codes = elig_top[i]
        o0, a0 = open_df.loc[b0], adj_df.loc[b0]
        o1, a1 = open_df.loc[b1], adj_df.loc[b1]
        g_adj, carry = [], []
        for code in codes:
            x0, f0 = float(o0[code]), float(a0[code])
            x1, f1 = float(o1.get(code, np.nan)), float(a1.get(code, np.nan))
            if x1 == x1 and x1 > 0 and f1 == f1:
                g_adj.append(x1 * f1 / (x0 * f0))
                carry.append(None)
            else:  # 换仓日停牌/无价：按最近可得 close×adj 结转(无卖出成本, 冻结简化)
                n_suspended_swap += 1
                if code in cols_set:
                    s = (close_df[code].loc[:b1] * adj_df[code].loc[:b1]).dropna()
                else:
                    s = pd.Series(dtype=float)
                v = float(s.iloc[-1]) if len(s) else float("nan")
                assert v == v, f"{code} 在 {b1.date()} 无任何可得价"
                g_adj.append(v / (x0 * f0))
                carry.append(True)
        per_period.append({"codes": codes, "g_adj": g_adj, "carry": carry})

    # ---------- 基准逐期收益（同窗 open→open，缺 open 回退 close 并计数） ----------
    bm_ret, bm_fallback = {}, 0
    for code in (BM_MAIN, BM_ALT):
        s_open, s_close = bm[code]
        rs = []
        for i in range(n_periods):
            b0, b1 = build_dates[i], build_dates[i + 1]
            try:
                p0, p1 = float(s_open[b0]), float(s_open[b1])
                if p0 > 0 and p1 > 0:
                    rs.append(p1 / p0 - 1.0)
                    continue
            except KeyError:
                pass
            bm_fallback += 1
            try:
                rs.append(float(s_close[b1]) / float(s_close[b0]) - 1.0)
            except KeyError as e:
                raise AssertionError(f"基准 {code} 在 {e} 无 close 价, 无法配对") from e
        bm_ret[code] = rs

    # ---------- §四/§五 逐臂回测 ----------
    arms = {}
    for k in KS:
        for tier, cap in (("A", CAP_A), ("B", CAP_B_PER_STOCK * k)):
            W = float(cap)
            rs, d_main, d_alt = [], [], []
            cost_total, amt_total, short_fills, zero_allocs = 0.0, 0.0, 0, 0
            for i in range(n_periods):
                codes = per_period[i]["codes"][:k]
                n_sel = len(codes)
                if n_sel < k:
                    short_fills += 1
                G = W / n_sel
                A = _buy_net_amount(G, fee_p)
                if A < fee_p["min_commission"]:
                    # 可执行性守卫(不引入新参数, 用冻结费率自带的 5 元下限): 每腿毛预算
                    # 连最低佣金都付不起 ⇒ 真实账户成交不了这种腿(实为现金/空仓期), r=0。
                    # 没有它, 资金塌缩尾段会出现"负净值"幻影(A<5 买入 → 卖出所得为负)。
                    zero_allocs += 1
                    rs.append(0.0)
                    d_main.append(0.0 - bm_ret[BM_MAIN][i])
                    d_alt.append(0.0 - bm_ret[BM_ALT][i])
                    continue
                buy_cost = trade_cost(A, "buy", build_dates[i], fee_p)
                V_end = 0.0
                for j, code in enumerate(codes):
                    g = per_period[i]["g_adj"][j]
                    amt = A * g
                    if per_period[i]["carry"][j] or amt < fee_p["min_commission"]:
                        # 换仓日停牌 / 卖出额连最低佣金都付不起(真实卖不掉): 无成本结转
                        V_end += amt
                        continue
                    # 卖出成交额 = A × g_adj（复权保值口径: 送转股保真 —— 股数随 f 比例扩张,
                    # 真实卖出额 = shares×raw_price = A×g_adj; 现金红利使其高估~红利比例, 费用影响 1e-5 量级。
                    # 用裸价比 A×x1/x0 会把送转股的卖出额砍半 ⇒ 5 元最低佣金被幻影放大, 已证伪。）
                    amt = A * g
                    sc = trade_cost(amt, "sell", build_dates[i + 1], fee_p) / amt
                    V_end += A * g * (1.0 - sc)
                    amt_total += amt
                    cost_total += sc * amt
                cost_total += buy_cost
                amt_total += A * n_sel
                r = V_end / W - 1.0
                rs.append(r)
                d_main.append(r - bm_ret[BM_MAIN][i])
                d_alt.append(r - bm_ret[BM_ALT][i])
                W = V_end
            st = stats(rs)
            stm = stats_diff(d_main)
            arm = {
                "k": k, "tier": tier, "cap_initial": cap,
                "full": st,
                "paired_vs_905": {**stm, "judgement": judge(stm["mean_month"], stm["t"])},
                "paired_vs_300_report_only": stats_diff(d_alt),
                "judgement": judge(st["mean_month"], st["t"]),
                "split1_2017_2021": stats([r for r, meta in zip(rs, period_meta)
                                           if meta["anchor"] <= SPLIT1_END]),
                "split2_2022_2026": stats([r for r, meta in zip(rs, period_meta)
                                           if meta["anchor"] > SPLIT1_END]),
                "extremes": {"worst_month": round(min(rs), 4), "best_month": round(max(rs), 4)},
                "cost": {
                    "total_cost_yuan": round(cost_total, 2),
                    "avg_one_way_cost_rate": round(cost_total / (2 * amt_total), 6) if amt_total else 0.0,
                    "avg_per_stock_amount_yuan": round(amt_total / (2 * n_periods * k), 2),
                    "short_fill_periods": short_fills,
                    "zero_alloc_periods": zero_allocs,
                },
                "series": {"r": [round(x, 8) for x in rs],
                           "d_905": [round(x, 8) for x in d_main],
                           "d_300": [round(x, 8) for x in d_alt]},
            }
            arms[f"k{k}_cap{tier}"] = arm
            print(f"k={k:>2} {tier}档: m={st['mean_month'] * 100:+.3f}pp t={st['t']:+.2f} "
                  f"[{arm['judgement']}] | vs905 d={stm['mean_month'] * 100:+.3f}pp t={stm['t']:+.2f} "
                  f"[{arm['paired_vs_905']['judgement']}] | MDD={st['mdd'] * 100:.1f}% "
                  f"最差月={arm['extremes']['worst_month'] * 100:.1f}%")

    # ---------- 判决速览（现行口径 k=5 A 档） ----------
    head = arms["k5_capA"]
    verdict = {
        "rule": "model_score top-k 等权月度换仓（k=5 现行, A 档 11,898 元）",
        "absolute": {"mean_month": head["full"]["mean_month"], "t": head["full"]["t"],
                     "judgement": head["judgement"]},
        "paired_vs_905": {"mean_month": head["paired_vs_905"]["mean_month"],
                          "t": head["paired_vs_905"]["t"],
                          "judgement": head["paired_vs_905"]["judgement"]},
        "note": "k∈{10,20,30,50} 只报差异与单调性（不挑最优 k, §五）",
    }

    out = {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "script": "scripts/oneoff/monthly_rule_expectancy_backtest.py",
            "prereg": PREREG,
            "read_only": True,
            "pin_ref": "config.yaml backtest_run（本脚本不读不改）",
            "data_max_date": str(pd.Timestamp(panel["close"].index[-1]).strftime("%Y%m%d")),
            "fee_params": {k: float(v) for k, v in fee_p.items()},
            "domain_flags": {"star_enabled": bool(cfg.get("star_enabled", False)),
                             "bse_enabled": bool(cfg.get("bse_enabled", False)),
                             "stock_size_pctl": cfg.get("stock_size_pctl", 0),
                             "stock_min_amt20": cfg.get("stock_min_amt20", 0)},
            "runtime_sec": round(time.time() - t0, 1),
        },
        "frozen": {"KS": KS, "CAP_A": CAP_A, "CAP_B_PER_STOCK": CAP_B_PER_STOCK,
                   "BM_MAIN": BM_MAIN, "BM_ALT": BM_ALT, "T_CRIT": T_CRIT,
                   "SPLIT1_END": SPLIT1_END, "n_anchors": len(anchors),
                   "n_periods": n_periods},
        "periods": period_meta,
        "domain_audit": {
            "per_period": audit_rows,
            "suspended_at_swap_total": n_suspended_swap,
            "bm_open_fallback_to_close": bm_fallback,
            "avg_universe": round(float(np.mean([r["n_in_universe"] for r in audit_rows])), 1),
        },
        "benchmark_stats": {c: stats(v) for c, v in bm_ret.items()},
        "arms": arms,
        "verdict": verdict,
        "limitations": [
            "§七.1 前视: 只用当期锚点可得 score（T+1 开盘建仓，与生产执行同尺）",
            "§七.2 乐观偏差: 未建模涨跌停无法成交（各臂同偏差）；换仓日停牌按最近可得 close×adj "
            "无成本结转（真实会顺延/作废，双向简化，共 %d 次）" % n_suspended_swap,
            "§七.3 不含现金收益（闲置资金零利息）；红利经 adj_factor 体现",
            "可执行性守卫（不引入新参数，用冻结费率自带的 5 元下限）：单腿净买入额 < 最低佣金 ⇒ "
            "该期按现金/空仓处理（%d 期级别计数见各臂 cost.zero_alloc_periods）；卖出额 < 最低佣金 ⇒ "
            "无成本结转 —— 真实账户无法成交的腿；没有它，资金塌缩尾段会产生「负净值」幻影",
            "未建模整手/最小交易单位：按净额等权分数股计（真实 2 万账户受整手约束更差）；"
            "最低佣金 5 元的非线性已按生产逐笔费率计入。卖出成交额按复权保值口径 A×g_adj"
            "（送转股保真；现金红利使其高估~红利比例, 费用影响 1e-5 量级）——"
            "裸价比口径会把送转股卖出额砍半、被最低佣金幻影放大（首跑已证伪并修正）",
            "配对差 d_t 只报统计量（均值/t/胜率/中位）：d 不是收益, cumprod/年化/MDD 对它无意义",
            "资金逐期复利滚动：最低佣金对小资金的惩罚在早期最重、随复利稀释（与真实账户一致）",
            "样本 115 期、单规则 ⇒ 结论只覆盖这条规则，不外推到别的打分或别的市场",
        ],
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n产物: {OUT}  ({time.time() - t0:.0f}s 总耗时)")
    print(f"判决(现行 k=5 A档): 绝对 {verdict['absolute']} | 配对vs中证500 {verdict['paired_vs_905']}")


if __name__ == "__main__":
    main()
