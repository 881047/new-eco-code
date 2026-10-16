"""F2 · 周频组合构造实验：把「IC 层真实存在的信号」转成「成本后可活的账户」

## 背景（F1/F1b 的实测结论）

- 5 日持有期上信号是**真实**的：`rev_5` IC=+0.0446(t=9.83)、`illiq` +0.0407、`bp` +0.0459
  （F1 全史 555 周锚点）。
- 但「31 因子等权秩融合 + top6」在 v2 修正口径下：毛超额 −5pp、净超额 −22pp
  （冒烟窗）⇒ 问题出在**组合构造**与**成本**，不是"没有信号"。
- 成本墙（Claude 独立复算）：2 万账户年摩擦 = 52×T×0.873×往返费率；
  T=30% ⇒ 8.11pp，T=50% ⇒ 13.52pp；要净赚 10pp ⇒ 毛需 18.11pp。

## 本实验要回答

    Q1  把信号换成「**只用过去信息**挑出的强因子子集」，毛超额能否转正？
    Q2  加**可交易性下限**（市值/成交额分位地板）后，能否剔除拖累收益的毒素尾部？
    Q3  加**换手控制**（滞回带）后，净超额能否 ≥ +10pp？
    Q4  账户放大（2/5/20 万）后成本墙下降多少？（同一策略、只改 capital）
    Q5  **杠杆臂**（满仓权重 1.0 / 1.5 / 2.0；融资成本按 5.5%/年计借入部分）
        能否把毛超额放大到 10pp 以上？—— 注意它同时放大回撤。

## 预注册判据（跑数前冻结）

构造臂按「由简到繁」排列，**每一步只允许加一个机制**，全部与基线同口径对比：
    A0 baseline : 31 因子等权秩融合 + top6（= F1b 的做法）
    A1 A0 + 滚动选因子（过去 156 周 IC 绝对值前 K=5，方向取过去 IC 符号）
    A2 A1 + 可交易性地板（size ≥ 30 分位 且 amount20 ≥ 30 分位）
    A3 A2 + 滞回带（持仓名享受 20% 截面标准差的加成，抑制无谓换手）
    A4 A3 + 账户放大（20 万；同策略只改 capital）
    A5 A3 + 杠杆（权重 1.5× / 2.0×，借入部分按 5.5%/年计息）
通过判据：**净**超额 ≥ +10.0pp 且活跃区(2024-01~)净超额 > 0 且 IC 覆盖充分。
若 A1/A2 相对 A0 恶化 ⇒ 记录"构造无改善"，不得继续往 A3 以上加机制。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F2_weekly_construction.py
"""
import argparse
import hashlib
import importlib.util
import json
import logging
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent
sys.path.insert(0, str(BASE))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


f1 = _load("f1", HERE / "F1_weekly_ic_decay.py")
f1b = _load("f1b", HERE / "F1b_weekly_open_overlap.py")

from app import config as cfg_mod          # noqa: E402
from app.factors import factor_engine as fe  # noqa: E402
from app.pit.engine import PITEngine       # noqa: E402
from app.portfolio import board as board_mod  # noqa: E402
from app.portfolio.fees import fee_params, trade_cost  # noqa: E402

log = logging.getLogger("f2")

OUT = HERE / "F2_weekly_construction_result.json"
ZONES = f1.ZONES
TOP_N = 6
IC_LOOKBACK = 156        # 滚动选因子: 过去 156 个周锚点(3 年)
IC_MIN = 52              # 至少这么多已实现 IC 才允许被选中
TOPK = 5                 # 选前 K 个强因子
HYST = 0.20              # 滞回带: 截面标准差的 20%
FLOOR_PCTL = 0.30        # 市值/成交额分位地板
MARGIN_RATE = 0.055      # 融资年化成本
CAPITALS = (20000, 50000, 200000)


def build_context(m, cfg, since_label):
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    pit = PITEngine(m=m, cfg=cfg)
    cols = m["close"].columns
    mask = f1.tradable_frame(pit, wk, cols)
    factors = f1.build_factors(m, wk)
    lab = f1b.forward_excess_open(m, mask, 5)
    Yr = f1._rank(lab)
    ok_lab = lab.notna().to_numpy()
    ic = {}
    for n, panel in factors.items():
        Xr = f1._rank(panel.reindex(index=wk))
        ic[n] = pd.Series(f1.row_ic(Xr, Yr, ok_lab & np.isfinite(Xr)), index=wk)
    X = {n: f1._rank(factors[n].reindex(index=wk)).astype(np.float64) for n in factors}

    # 可交易性指标(时点对齐到周锚点)
    amt20 = m["amount"].rolling(20).mean()
    amt20 = amt20.reindex(index=wk, columns=cols).to_numpy()
    mv_panel = m["total_mv"].reindex(index=wk, columns=cols).to_numpy()
    return dict(dates=dates, wk=wk, pit=pit, cols=list(cols), mask=mask, X=X, ic=ic,
                amt20=amt20, mv=mv_panel, n_anchors=len(wk))


def portfolio_arm(ctx, m, cfg, *, rolling_select: bool, floor: bool, hyst: bool,
                  capital: float, leverage: float = 1.0, gross: bool = False,
                  top_n: int = TOP_N, fixed: list = None, since_label: str = ""):
    """周频 top-N, 留存不动; 逐笔费用; 涨跌停闸门; 停牌按最后价; adj 缩放股数。"""
    wk, cols, pit = ctx["wk"], ctx["cols"], ctx["pit"]
    X, IC, mask = ctx["X"], ctx["ic"], ctx["mask"]
    names = sorted(X)
    idx = list(wk)
    cp = {c: j for j, c in enumerate(cols)}
    dates = ctx["dates"]
    pos_of = {d: i for i, d in enumerate(dates)}
    op_raw = m["open"].reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pc_raw = m["pre_close"].reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    adj = m["adj"].reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    mask_np = mask.to_numpy()
    slip = 0.0 if gross else float(cfg.get("paper", {}).get("slip", 0.001))
    fee_p = None if gross else fee_params(cfg)

    def _fee(amt, side, day):
        if gross:
            return 0.0
        return trade_cost(amt, side, day, fee_p)

    trail = 36
    pos, last_px, stale = {}, {}, {}
    cash = float(capital)
    prev_exec = None
    rows = []
    for k in range(trail, len(idx)):
        i = pos_of.get(idx[k])
        if i is None or i + 1 >= len(dates):
            continue
        e = i + 1
        rp, rpc, ra = op_raw[e], pc_raw[e], adj[e]
        dstr = dates[e].strftime("%Y%m%d")

        if prev_exec is not None and pos:
            a_prev = adj[prev_exec]
            for c in list(pos):
                j = cp[c]
                if np.isfinite(ra[j]) and np.isfinite(a_prev[j]) and a_prev[j] > 0:
                    pos[c] *= ra[j] / a_prev[j]

        mv = 0.0
        for c, sh in pos.items():
            j = cp[c]
            p = rp[j]
            if np.isfinite(p) and p > 0:
                last_px[c], stale[c] = float(p), 0
            else:
                stale[c] = stale.get(c, 0) + 1
                p = last_px.get(c, 0.0)
            mv += sh * p
        nav = cash + mv

        # ---- 打分 ----
        if fixed:
            # 直接指定信号族(仍用**只用过去**的 IC 定方向), 不做 31 因子融合
            use = []
            for n in fixed:
                if n not in IC:
                    continue
                past = IC[n].to_numpy()[max(0, k - IC_LOOKBACK - 1):k - 1]
                past = past[np.isfinite(past)]
                if len(past) >= IC_MIN and abs(float(past.mean())) > 1e-9:
                    use.append((n, 1.0 if float(past.mean()) > 0 else -1.0))
        elif rolling_select:
            scored = []
            for n in names:
                past = IC[n].to_numpy()[max(0, k - IC_LOOKBACK - 1):k - 1]
                past = past[np.isfinite(past)]
                if len(past) >= IC_MIN:
                    mu = float(past.mean())
                    if abs(mu) > 1e-9:
                        scored.append((abs(mu), n, 1.0 if mu > 0 else -1.0))
            scored.sort(reverse=True)
            use = [(n, s) for _a, n, s in scored[:TOPK]]
        else:
            use = [(n, 1.0 if float(np.nanmean(IC[n].to_numpy()[max(0, k - IC_LOOKBACK - 1):k - 1])) >= 0 else -1.0)
                   for n in names]
        if len(use) < (1 if fixed else 3):      # 单信号臂允许只有 1 个信号
            continue
        comp = np.zeros(len(cols))
        cnt = np.zeros(len(cols))
        for n, sgn in use:
            v = X[n][k] * sgn
            okv = np.isfinite(v)
            comp[okv] += v[okv]
            cnt[okv] += 1
        with np.errstate(invalid="ignore"):
            comp = np.where(cnt > 0, comp / np.maximum(cnt, 1), np.nan)

        ok = mask_np[k] & np.isfinite(comp) & np.isfinite(rp) & (rp > 0)
        if floor:
            a = ctx["amt20"][k]
            v = ctx["mv"][k]
            ok &= np.isfinite(a) & (a >= np.nanquantile(a, FLOOR_PCTL))
            ok &= np.isfinite(v) & (v >= np.nanquantile(v, FLOOR_PCTL))
        if ok.sum() < 200:
            continue
        score = comp.copy()
        if hyst and pos:
            sd = float(np.nanstd(score[ok]))
            for c in pos:
                j = cp[c]
                if ok[j] and np.isfinite(score[j]):
                    score[j] += HYST * sd          # 持仓名加成 ⇒ 抑制无谓换手
        cand = [cols[j] for j in np.argsort(-np.where(ok, score, -np.inf))[:top_n]]

        def _limits(c):
            j = cp[c]
            pc = rpc[j]
            if not (np.isfinite(pc) and pc > 0):
                return None, None
            nm = pit.name_at(c, dates[e]) or ""
            ratio = board_mod.limit_ratio(c, "ST" in nm, dstr, nm, pit.name_at)
            pcq = board_mod.round2(float(pc))
            return (board_mod.round2(pcq * (1 + ratio)),
                    board_mod.round2(pcq * (1 - ratio)))

        for c in list(pos):
            if c in cand:
                continue
            j = cp[c]
            p = rp[j]
            if np.isfinite(p) and p > 0:
                px_s = round(float(p) * (1 - slip), 2)
                _lu, ld = _limits(c)
                if not gross and ld is not None and px_s <= ld + 0.001:
                    continue
                amt = pos[c] * px_s
                cash += amt - _fee(amt, "sell", dstr)
                last_px[c] = float(p)
                del pos[c]
                stale.pop(c, None)
            elif stale.get(c, 0) >= 3 and c in last_px:
                amt = pos[c] * round(float(last_px[c]) * (1 - slip), 2)
                cash += amt - _fee(amt, "sell", dstr)
                del pos[c]
                stale.pop(c, None)

        equity = nav * leverage                     # 杠杆: 目标权益敞口
        budget = equity / len(cand)
        for c in cand:
            if c in pos:
                continue
            j = cp[c]
            p = rp[j]
            if not (np.isfinite(p) and p > 0):
                continue
            px_b = round(float(p) * (1 + slip), 2)
            lu, _ld = _limits(c)
            if not gross and lu is not None and px_b >= lu - 0.001:
                continue
            afford = int(np.floor(budget / (px_b * (1 + float(cfg.get("single_side_cost", 0.003)))) + 1e-9))
            sh = board_mod.lot_shares(c, afford)
            if sh <= 0:
                continue
            amt = sh * px_b
            fee = _fee(amt, "buy", dstr)
            cash -= amt + fee             # 现金不足即为融资余额(负数)
            if cash < 0:
                cash -= (-cash) * MARGIN_RATE / 52.0    # 融资余额按 5.5%/年计息(按周摊)
            pos[c] = float(sh)
            last_px[c] = float(p)
            stale[c] = 0

        rows.append({"date": dstr, "anchor_pos": k, "nav": nav, "n": len(pos),
                     "cash": cash, "picks": list(pos)})
        prev_exec = e

    if len(rows) < 5:
        return None
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df["ret"] = df["nav"].pct_change()
    to = [1.0]
    for a, b in zip(df["picks"], df["picks"][1:]):
        to.append(1 - len(set(a) & set(b)) / max(1, len(b)))
    df["turnover"] = to
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=f1b.SINCE)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since=a.since)
    ctx = build_context(m, cfg, a.since)
    wk, cols = ctx["wk"], ctx["cols"]
    mask = ctx["mask"]
    log.info("周锚点 %d | 因子 %d", len(wk), len(ctx["X"]))

    op_adj = (m["open"] * m["adj"]).reindex(index=ctx["dates"], columns=cols).astype(np.float64)
    op_adj_np = op_adj.to_numpy()
    pos_of = {d: i for i, d in enumerate(ctx["dates"])}
    mask_np = mask.to_numpy()

    def with_bench(df):
        bench, prev_exec, prev_ap = [], None, None
        for _, r in df.iterrows():
            e = pos_of[r["date"]]
            if prev_exec is None or prev_ap is None:
                bench.append(np.nan)
            else:
                ok = mask_np[int(prev_ap)]
                p0, p1 = op_adj_np[prev_exec], op_adj_np[e]
                good = ok & np.isfinite(p0) & np.isfinite(p1) & (p0 > 0)
                bench.append(float(np.mean(p1[good] / p0[good] - 1)) if good.sum() >= 200
                             else np.nan)
            prev_exec, prev_ap = e, int(r["anchor_pos"])
        df = df.copy()
        df["bench_ret"] = bench
        return df

    def report(tag, df, gross_note=""):
        if df is None:
            log.warning("[%s] 无有效期数", tag)
            return None
        df = with_bench(df)
        ok = df["ret"].notna() & df["bench_ret"].notna()
        a_full, b_full = f1b.ann(df.loc[ok, "ret"], 52.0), f1b.ann(df.loc[ok, "bench_ret"], 52.0)
        out = {"n": int(ok.sum()), "ann": round(a_full, 4), "bench": round(b_full, 4),
               "excess_pp": round(100 * (a_full - b_full), 2),
               "turnover": round(float(df.loc[ok, "turnover"].mean()), 3),
               "hit": round(float((df.loc[ok, "ret"] > 0).mean()), 3),
               "DEAD": f1b.zone_ann(df, "DEAD", 52.0),
               "ACTIVE": f1b.zone_ann(df, "ACTIVE", 52.0), "note": gross_note}
        log.info("[%s] n=%d 组合 %.2f%% / 基准 %.2f%% ⇒ 超额 %+.2fpp | 换手 %.3f 命中 %.3f %s",
                 tag, out["n"], 100 * a_full, 100 * b_full, out["excess_pp"],
                 out["turnover"], out["hit"], gross_note)
        log.info("      DEAD %s", out["DEAD"])
        log.info("      ACTIVE %s", out["ACTIVE"])
        return out

    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "since": a.since, "n_week": int(len(wk)), "arms": {}, "verdict": {}}
    cap0 = float(cfg.get("capital", 20000))

    res["arms"]["A0_baseline"] = report("A0_baseline", portfolio_arm(
        ctx, m, cfg, rolling_select=False, floor=False, hyst=False, capital=cap0))
    res["arms"]["A1_topk"] = report("A1_topk", portfolio_arm(
        ctx, m, cfg, rolling_select=True, floor=False, hyst=False, capital=cap0))
    res["arms"]["A2_topk_floor"] = report("A2_topk_floor", portfolio_arm(
        ctx, m, cfg, rolling_select=True, floor=True, hyst=False, capital=cap0))
    res["arms"]["A3_topk_floor_hyst"] = report("A3_topk_floor_hyst", portfolio_arm(
        ctx, m, cfg, rolling_select=True, floor=True, hyst=True, capital=cap0))
    res["arms"]["A3_gross"] = report("A3_gross", portfolio_arm(
        ctx, m, cfg, rolling_select=True, floor=True, hyst=True, capital=cap0, gross=True),
        "(零成本)")
    for cap in CAPITALS:
        if cap == cap0:
            continue
        res["arms"][f"A4_cap{int(cap/10000)}w"] = report(f"A4_cap{int(cap/10000)}w", portfolio_arm(
            ctx, m, cfg, rolling_select=True, floor=True, hyst=True, capital=cap))
    for lev in (1.5, 2.0):
        res["arms"][f"A5_lev{lev}"] = report(f"A5_lev{lev}", portfolio_arm(
            ctx, m, cfg, rolling_select=True, floor=True, hyst=True, capital=cap0, leverage=lev))

    # ---- B 组: 单信号/小信号集直接构造(不做 31 因子等权融合) ----
    SIG_REV = ["rev_5"]
    SIG_RLV = ["rev_5", "illiq", "bp"]
    for tag, sigs, floor, hyst, cap, lev, gro in (
            ("B0_rev5_2w", SIG_REV, False, False, cap0, 1.0, False),
            ("B1_rev5_floor_2w", SIG_REV, True, False, cap0, 1.0, False),
            ("B2_rev5_floor_hyst_2w", SIG_REV, True, True, cap0, 1.0, False),
            ("B2_rev5_floor_hyst_gross", SIG_REV, True, True, cap0, 1.0, True),
            ("B2_rev5_floor_hyst_20w", SIG_REV, True, True, 200000.0, 1.0, False),
            ("B3_rlv_floor_hyst_2w", SIG_RLV, True, True, cap0, 1.0, False),
            ("B3_rlv_floor_hyst_20w", SIG_RLV, True, True, 200000.0, 1.0, False),
            ("B4_rev5_lev15_20w", SIG_REV, True, True, 200000.0, 1.5, False),
    ):
        res["arms"][tag] = report(tag, portfolio_arm(
            ctx, m, cfg, rolling_select=False, floor=floor, hyst=hyst, capital=cap,
            leverage=lev, gross=gro, fixed=sigs), "(零成本)" if gro else "")

    best = res["arms"].get("A3_topk_floor_hyst")
    res["verdict"]["Q3_pass_10pp"] = None if not best else bool(best["excess_pp"] >= 10.0)
    res["verdict"]["A3_excess_pp"] = None if not best else best["excess_pp"]
    res["verdict"]["A3_active_excess_pp"] = None if not best else best["ACTIVE"].get("excess_pp")
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 完成 %.1fs → %s ==", res["secs"], OUT)


if __name__ == "__main__":
    main()
