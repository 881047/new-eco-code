# -*- coding: utf-8 -*-
"""月度方案执行归因：**理论组合（锚点/决策价）vs 实际组合（成交价）** —— 只读。

判据、口径、公式**先冻结**在 `plans/monthly-exec-attribution-prereg-20260923.md`，
本文件只是它的执行体（A-F 各项与文档 §三 一一对应）：

  · 共同分母 B = Σ q·P^anchor  ⇒ 两种组合的收益可比
  · 恒等式1（零残差）: Δ = R_ac − R_th = Σ w^a·(P^a−P^f)/P^a   ← 全部差异就是加权入场价差
  · 恒等式2（Perold 三层，乘法精确）: (1+r^a→t) = (1+r^delay)(1+c^slip)(1+r^post)
    加性近似必须把交互项 ε 列出，不许吞掉
  · 权重偏离: Brinson–Fachler 单层（Allocation / Selection / Interaction），基准 中证500
  · 市场/行业/风格: r = r_M + (r_IND − r_M) + (r_i − r_IND)，行业 = 申万 L2 等权篮子(剔除自身)

只读声明: 只 SELECT daily / daily_basic / index_daily / plan / paper_trade / paper_position /
adj_factor / sw_member / sw_classify；不写任何生产表、不改成交口径、不换 pin。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
DB = BASE / "data" / "neweco.db"
OUT = BASE / "experiments" / "monthly_exec_attribution.json"
PREREG = "plans/monthly-exec-attribution-prereg-20260923.md"

PLAN_DATE = "20260831"
EXEC_DATE = "20260915"
BM = "000905.SH"          # 主基准 中证500
BM_ALT = "000300.SH"      # 稳健性对照 沪深300
MIN_IND_MEMBERS = 3


def _px(con, code, date, prefer="close"):
    """返回 <=date 的最后一个交易日收盘（或开盘）。"""
    r = con.execute(
        f"select trade_date,{prefer} from daily where ts_code=? and trade_date<=? "
        f"order by trade_date desc limit 1", (code, date)).fetchone()
    return (r[0], r[1]) if r else (None, None)


def _industry(con, code, date):
    r = con.execute(
        "select k.index_code,k.industry_name,k.level from sw_member m "
        "join sw_classify k on k.index_code=m.index_code "
        "where m.con_code=? and k.level='L2' and m.in_date<=? and (m.out_date is null or m.out_date>?) "
        "limit 1", (code, date, date)).fetchone()
    return (r[0], r[1]) if r else (None, None)


def _basket_return(con, codes, d0, d1):
    """等权篮子收益 Σ(1/n)·(P1/P0−1)；codes 已剔除标的自身/不限成员时亦然。"""
    rets, used = [], []
    for c in codes:
        d0e, p0 = _px(con, c, d0)
        d1e, p1 = _px(con, c, d1)
        if p0 and p1 and d0e and d1e:
            rets.append(p1 / p0 - 1)
            used.append(c)
    if not rets:
        return None, 0
    return sum(rets) / len(rets), len(rets)


def main() -> int:
    con = sqlite3.connect(str(DB))

    plan = con.execute(
        "select ts_code,name,weight,shares,industry from plan "
        "where plan_date=? and plan_type='stock' order by weight desc", (PLAN_DATE,)).fetchall()
    trades = {r[0]: r[1] for r in con.execute(
        "select ts_code,price from paper_trade where exec_date=? and reason=?",
        (EXEC_DATE, "月度方案买入"))}

    rows, B, F = [], 0.0, 0.0
    for code, name, w, q, ind_plan in plan:
        pf = trades.get(code)
        if pf is None:
            continue
        d_a, pa = _px(con, code, PLAN_DATE)
        _, po = _px(con, code, EXEC_DATE, prefer="open")
        d_t, pt = _px(con, code, "20991231")
        rows.append(dict(code=code, name=name, w_target=float(w or 0), q=int(q or 0),
                         industry_plan=ind_plan, p_anchor=pa, anchor_date=d_a,
                         p_arrival=po, p_fill=pf, p_now=pt, now_date=d_t))
        B += int(q) * pa
        F += int(q) * pf

    if not rows:
        print("没有可归因的持仓")
        return 1

    # 复权自检（期间有无除权除息）
    adj_clean = True
    for r in rows:
        a = con.execute("select adj_factor from adj_factor where ts_code=? and trade_date between ? and ? "
                        "order by trade_date limit 1", (r["code"], EXEC_DATE, r["now_date"])).fetchone()
        b = con.execute("select adj_factor from adj_factor where ts_code=? and trade_date between ? and ? "
                        "order by trade_date desc limit 1", (r["code"], EXEC_DATE, r["now_date"])).fetchone()
        r["adj_factor_start"] = a[0] if a else None
        r["adj_factor_end"] = b[0] if b else None
        if a and b and abs((a[0] or 0) - (b[0] or 0)) > 1e-9:
            adj_clean = False

    details, Rth, Rac = [], 0.0, 0.0
    for r in rows:
        pa, po, pf, pt, q = r["p_anchor"], r["p_arrival"], r["p_fill"], r["p_now"], r["q"]
        r_at = pt / pa - 1                    # 决策价 → 现在
        r_delay = po / pa - 1                 # 决策 → 到达(arrival=开盘)
        c_slip = pf / po - 1                  # 到达 → 成交（>0 为成本）
        r_post = pt / pf - 1                  # 成交 → 现在
        eps = r_at - (r_delay + c_slip + r_post)          # 交互项，显式列出
        mult = (1 + r_delay) * (1 + c_slip) * (1 + r_post) - 1 - r_at  # 乘法恒等式校验(应≈0)
        wa = q * pa / B                       # 按决策价的成本权重
        wf = q * pf / F                       # 按成交价的实际权重
        # 恒等式1：该项目 = 加权入场价优势（按共同分母 B）
        exec_gain_on_B = q * (pa - pf) / B
        Rth += wa * r_at
        r.update(r_anchor_to_now=r_at, r_delay=r_delay, c_slip=c_slip, r_post=r_post,
                 interaction_eps=eps, identity2_residual=mult, w_anchor=wa, w_fill=wf,
                 exec_gain_on_B=exec_gain_on_B)
        details.append(r)

    # 统一算收益（避免累积误差）
    Rth = sum(q * (pt - pa) for q, pa, pt in [(x["q"], x["p_anchor"], x["p_now"]) for x in rows]) / B
    Rac = sum(q * (pt - pf) for q, pf, pt in [(x["q"], x["p_fill"], x["p_now"]) for x in rows]) / B
    Rac_inv = sum(q * (pt - pf) for q, pf, pt in [(x["q"], x["p_fill"], x["p_now"]) for x in rows]) / F

    # 基准与市场/行业/风格
    def idx_ret(code, d0, d1):
        a = con.execute("select close from index_daily where ts_code=? and trade_date<=? "
                        "order by trade_date desc limit 1", (code, d0)).fetchone()
        b = con.execute("select close from index_daily where ts_code=? and trade_date<=? "
                        "order by trade_date desc limit 1", (code, d1)).fetchone()
        return (b[0] / a[0] - 1) if (a and b) else None

    rM_anchor = idx_ret(BM, PLAN_DATE, rows[0]["now_date"])     # 决策 → 现在
    rM_exec = idx_ret(BM, EXEC_DATE, rows[0]["now_date"])       # 成交 → 现在
    rM_exec_alt = idx_ret(BM_ALT, EXEC_DATE, rows[0]["now_date"])

    for x in rows:
        idx_code, ind_name = _industry(con, x["code"], PLAN_DATE)
        members, basket, n_used = [], None, 0
        if idx_code:
            members = [m[0] for m in con.execute(
                "select con_code from sw_member where index_code=? and in_date<=? "
                "and (out_date is null or out_date>?)", (idx_code, PLAN_DATE, PLAN_DATE))]
            members = [m for m in members if m != x["code"]]
            if len(members) >= MIN_IND_MEMBERS:
                basket, n_used = _basket_return(con, members, EXEC_DATE, x["now_date"])
        x["sw_l2"] = ind_name
        x["sw_l2_code"] = idx_code
        x["ind_peers"] = n_used
        x["ind_basket_ret_exec"] = basket
        if basket is None:
            x["mkt_term"] = x["ind_term"] = x["spec_term"] = None
        else:
            x["mkt_term"] = rM_exec
            x["ind_term"] = basket - rM_exec
            x["spec_term"] = x["r_post"] - basket

    # 规模/风格：窗口起点流通市值分位 + 小盘 vs 大盘等权篮子
    snap = con.execute("select ts_code,circ_mv from daily_basic where trade_date=(select max(trade_date) "
                       "from daily_basic where trade_date<=?) and circ_mv is not null",
                       (EXEC_DATE,)).fetchall()
    if snap:
        ordered = sorted(snap, key=lambda t: t[1])
        total = len(ordered)
        rank = {c: i / max(1, total - 1) for i, (c, _) in enumerate(ordered)}
        mv = dict(snap)
        small = [c for c, _ in ordered[: int(total * 0.3)]]
        large = [c for c, _ in ordered[-int(total * 0.3):]]
        r_small, _ = _basket_return(con, small, EXEC_DATE, rows[0]["now_date"])
        r_large, _ = _basket_return(con, large, EXEC_DATE, rows[0]["now_date"])
        style_gap = (r_small - r_large) if (r_small is not None and r_large is not None) else None
        for x in rows:
            x["circ_mv_yi"] = round(mv.get(x["code"], float("nan")) / 1e4, 2) if x["code"] in mv else None
            x["mv_pct"] = round(rank.get(x["code"], float("nan")), 4) if x["code"] in rank else None
            x["size_bucket"] = ("小盘(后30%)" if x["mv_pct"] is not None and x["mv_pct"] < 0.3
                                else "大盘(前30%)" if x["mv_pct"] is not None and x["mv_pct"] > 0.7 else "中盘(中间40%)")
    else:
        style_gap = None

    # Brinson–Fachler（成交→现在，基准 中证500）
    bf = []
    for x in rows:
        wf, wt, r = x["w_fill"], x["w_target"], x["r_post"]
        alloc = (wf - wt) * rM_exec
        selec = wt * (r - rM_exec)
        inter = (wf - wt) * (r - rM_exec)
        bf.append(dict(code=x["code"], name=x["name"], allocation=alloc, selection=selec,
                       interaction=inter))
    bf_sum = {k: sum(b[k] for b in bf) for k in ("allocation", "selection", "interaction")}

    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "prereg": PREREG,
        "window": {"plan_date": PLAN_DATE, "exec_date": EXEC_DATE, "as_of": rows[0]["now_date"]},
        "inputs": {"codes": [x["code"] for x in rows], "B": round(B, 2), "F": round(F, 2),
                   "adj_factor_unchanged": adj_clean},
        "returns": {"R_theoretical_on_B": Rth, "R_actual_on_B": Rac,
                    "R_actual_on_invested": Rac_inv, "delta": Rac - Rth,
                    "bench_from_execBM500": rM_exec, "bench_from_execBM300": rM_exec_alt,
                    "bench_from_anchorBM500": rM_anchor},
        "identity1_check": {
            "sum_weighted_entry_gap": sum(x["exec_gain_on_B"] for x in rows),
            "Rac_minus_Rth": Rac - Rth},
        "details": details,
        "brinson": {"items": bf, "sum": bf_sum,
                    "check": {"sum_minus_bench": bf_sum["allocation"] + bf_sum["selection"] +
                              bf_sum["interaction"] - (Rac_inv - rM_exec)}},
        "style": {"small_minus_large_equal_weight": style_gap},
        "limits": [
            "样本 = 1 个方案 / 5 只票 / 1 次成交 ⇒ 不得写任何'系统性'结论",
            "未实现浮盈，未扣卖出费用（佣金+印花税+滑点，粗估 0.2~0.3% 量级，只提示不入账）",
            "不估 beta：用等权篮子而非回归 ⇒ 是会计式分解，不是风险模型",
            f"行业 = 申万 L2 等权篮子(剔除自身)；成员 <{MIN_IND_MEMBERS} 只则该票标'行业样本不足'",
            "改执行时点属口径变更 = 换 pin 级，本轮不做、也不据此建议",
        ],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"产物 {OUT}")
    print(f"窗口 {PLAN_DATE} → 成交 {EXEC_DATE} → 现在 {rows[0]['now_date']} | B={B:.2f} F={F:.2f}")
    print(f"理论(共同基准) {Rth*100:+.2f}% | 实际(共同基准) {Rac*100:+.2f}% | 实际(已投本金) {Rac_inv*100:+.2f}%")
    print(f"Δ = {Rac-Rth:+.4%} | 恒等式1 校验 Σw·<｜hy_place▁holder▁no▁813｜>价差 = {sum(x['exec_gain_on_B'] for x in rows):+.6f}")
    print(f"基准(成交→现在) 中证500 {rM_exec*100:+.2f}% | 决策→现在 {rM_anchor*100:+.2f}%")
    print("Brinson: 配置 %+.2f%% / 选股 %+.2f%% / 交互 %+.2f%%（单位 %%，相对 R_ac,inv − 基准 = %+.2f%%）" %
          (bf_sum["allocation"] * 100, bf_sum["selection"] * 100, bf_sum["interaction"] * 100,
           (Rac_inv - rM_exec) * 100))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
