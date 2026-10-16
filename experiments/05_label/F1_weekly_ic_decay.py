"""F1 · 周频(5 交易日)IC 衰减 + 成本后 top-6 探针 —— 预注册脚本

## 回答什么问题

用户目标：「周频收益超过 10% 的个人量化终端，方法不限，但要真实、正确」。
本脚本只回答**第一个可证伪的问题**：

    Q1  现有 31 个生产因子，在 5/10/21 个交易日持有期上还有没有横截面预测力？
        （周频 vs 月频的 IC 衰减曲线，分死区/活跃区）
    Q2  用项目自己的逐笔费用模型 + 滑点 + 板块整手规则，周频 top-6 组合
        能不能跑赢「可交易域等权」这个最朴素的基准？成本后是多少？

## 口径（与生产同源，不另造一套真相）

- 票池：`app.pit.engine.PITEngine.tradable_mask`（生产六层，star/bse 按 config 关闭）
- 因子：`factor_engine.compute_price_factors` 的 22 个量价 + 6 个财务 + 3 个龙虎榜
        （= 生产 `factor` 表的 31 个；**不含** 2026-09-11 新增的资金/股东类未接线因子）
- 中性化：`factor_engine.neutralize`（去极值 2% → 行业中性 → z-score）
- 标签：复权价未来 H 个交易日收益 − **当日可交易域等权收益**（= 超额收益）
- 费用：`app.portfolio.fees.trade_cost`（佣金 max(额×万2.5, 5 元) + 卖出印花税 + 过户费）
- 滑点：`config.paper.slip`（0.1%/边）
- 整手：`app.portfolio.board.lot_shares`（按板块，真实起买量）

## 预注册判据（写于跑数之前，跑完不得修改）

**主判据（Q1）**：把「周频 alpha 存在」定义为 **满足以下三条的因子数 ≥ 3**：
  ① 死区(2020-09-01~2023-12-31)与活跃区(2024-01-01~2026-08-31)的 5 日 IC **同号**
  ② |IC_死区| ≥ 0.02
  ③ 死区 t ≥ 2.0（单侧），且经 **Bonferroni 校正**（分母 = 被测因子数 N=31）后仍显著
  另报 10 日 / 21 日 剂量-反应臂；**若只有 5 日过而 10/21 日不过，按噪声处理，不算通过**。

**副判据（Q2，成本闸门）**：
  ④ 周频 top-6 组合在**真实费用后**，全史窗年化收益 − 等权基准年化收益 ≥ +10.0pp
  ⑤ 同一组合在活跃区(2024 起)成本后超额 > 0
  ⑥ 月频对照臂同口径可复现（用于证明本脚本的构造不偏向任何一方）

**判决**：① ~ ③ 全不满足 ⇒ **「现有因子集无周频 alpha」成立**（有效结论，写入登记处），
周频路线只能靠新信息源（资金流/融资/大宗/股东户数/事件），不能靠改持有期。
④ 不满足 ⇒ **周频在 2 万账户下无经济可行性**，即使 Q1 通过。

## 已知的口径偏差（如实声明，不掩盖）

1. 中性化只对锚点行做（生产对全历史行做）。`neutralize` 逐行独立，
   但 float32 下**行数改变会让末位漂移 1e-8**（见该函数 docstring 的实测），
   对 IC 探针无影响，**但本脚本的因子值不得当作生产 `factor` 表的替代品**。
2. 财务因子用「公告日 → 下一交易日」对齐（复用 `compute_fina_factors`），
   与生产同一实现，但生产只取月末截面，这里取周锚点。
3. 等权基准**不计费用**（基准本来就不交易），组合臂计全部费用。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F1_weekly_ic_decay.py
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F1_weekly_ic_decay.py --skip-portfolio
"""
import argparse
import datetime
import hashlib
import json
import logging
import math
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfg_mod          # noqa: E402
from app import db                         # noqa: E402
from app.factors import factor_engine as fe  # noqa: E402
from app.pit.engine import PITEngine       # noqa: E402
from app.portfolio import board as board_mod  # noqa: E402
from app.portfolio.fees import fee_params, trade_cost  # noqa: E402

log = logging.getLogger("f1")

OUT = Path(__file__).resolve().parent / "F1_weekly_ic_decay_result.json"
SINCE = "20151101"          # 2017 起评估; 留 250 个交易日给 beta_250/52 周位置类因子
ZONES = {"DEAD": ("2020-09-01", "2023-12-31"), "ACTIVE": ("2024-01-01", "2026-12-31")}
HORIZONS = (5, 10, 21)      # 剂量-反应臂(锚点=周)
MIN_NAMES = 200             # 单截面有效样本下限
TRAIL_IC = 36               # 组合臂: 用过去 36 个锚点的 IC 定方向(walk-forward, 无前视)
TOP_N = 6


# --------------------------------------------------------------------------
# 锚点
# --------------------------------------------------------------------------
def week_end_dates(dates: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """每个 ISO 周的最大交易日(周频调仓锚点)。"""
    iso = dates.isocalendar()
    key = pd.MultiIndex.from_arrays([np.asarray(iso["year"]), np.asarray(iso["week"])],
                                    names=["iso_year", "iso_week"])
    s = pd.Series(np.asarray(dates), index=key)
    return pd.DatetimeIndex(sorted(s.groupby(level=[0, 1]).max().values))


# --------------------------------------------------------------------------
# 因子矩阵(生产同源)
# --------------------------------------------------------------------------
def build_factors(m: dict, anchors: pd.DatetimeIndex) -> dict:
    """返回 {因子名: anchors × 股票} 的中性化因子面板。"""
    conn = db.get_conn()
    try:
        ind = fe.industry_map(conn)
    finally:
        conn.close()

    log.info("计算量价因子(全历史日频面板)…")
    t0 = time.time()
    pf = fe.compute_price_factors(m)
    log.info("  量价因子 %d 个, %.1fs", len(pf), time.time() - t0)

    dates = pd.to_datetime(m["close"].index)
    want = anchors.strftime("%Y%m%d").tolist()
    log.info("计算财务因子(周锚点 %d 个)…", len(want))
    ff = fe.compute_fina_factors(want, calendar=dates.strftime("%Y%m%d").tolist())
    for name in ff:                      # 生产 compute_all 同款: 字符串索引 → datetime
        ff[name].index = pd.to_datetime(ff[name].index)
    log.info("计算龙虎榜因子…")
    lf = fe.compute_lhb_factors(dates)

    out = {}
    cols = m["close"].columns
    for name, df in {**pf, **ff, **lf}.items():
        # 列必须对齐到行情面板(财务/龙虎榜因子的列集与行情不同), 否则下游 numpy 形状不齐
        sub = df.reindex(index=anchors, columns=cols)
        if sub.empty or sub.notna().sum().sum() == 0:
            log.warning("因子 %s 在锚点上全空, 跳过", name)
            continue
        out[name] = fe.neutralize(sub, ind)
        log.info("  中性化 %-16s %s", name, out[name].shape)
    return out


# --------------------------------------------------------------------------
# 标签与 IC
# --------------------------------------------------------------------------
def tradable_frame(pit: PITEngine, anchors: pd.DatetimeIndex, cols) -> pd.DataFrame:
    """anchors × 股票 的可交易掩码(一次算完, 所有 horizon 复用 —— 掩码与持有期无关)。"""
    if hasattr(pit, "tradable_matrix"):
        return pit.tradable_matrix(anchors, columns=cols).fillna(False)
    return pd.DataFrame(
        [pit.tradable_mask(d).reindex(cols).fillna(False).to_numpy() for d in anchors],
        index=anchors, columns=cols)


def forward_excess(m: dict, mask: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """anchors × 股票: 未来 horizon 个交易日复权收益 − 当日可交易域等权收益。"""
    ac = (m["close"] * m["adj"]).astype(np.float32)
    ac.index = pd.to_datetime(ac.index)
    fwd = ac.shift(-horizon) / ac - 1.0
    sub = fwd.reindex(index=mask.index, columns=mask.columns).where(mask)
    return sub.sub(sub.mean(axis=1), axis=0)


def _rank(df: pd.DataFrame) -> np.ndarray:
    """逐行转成平均秩(pct), NaN 保持 NaN。"""
    return df.rank(axis=1, pct=True, na_option="keep").to_numpy(dtype=np.float64)


def row_ic(Xr: np.ndarray, Yr: np.ndarray, M: np.ndarray) -> np.ndarray:
    """逐行 Spearman IC(输入已是秩)。M = 有效掩码。"""
    Xm = np.where(M, Xr, np.nan)
    Ym = np.where(M, Yr, np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)   # 全 NaN 行 → NaN, 不是错误
        with np.errstate(invalid="ignore"):
            mx = np.nanmean(Xm, axis=1, keepdims=True)
            my = np.nanmean(Ym, axis=1, keepdims=True)
            dx, dy = Xm - mx, Ym - my
            num = np.nansum(dx * dy, axis=1)
            den = np.sqrt(np.nansum(dx * dx, axis=1) * np.nansum(dy * dy, axis=1))
            return np.where((den > 0) & np.isfinite(den), num / den, np.nan)


def zone_stats(ic: pd.Series, zone: str) -> dict:
    lo, hi = ZONES[zone]
    s = ic.loc[(ic.index >= lo) & (ic.index <= hi)].dropna()
    if len(s) < 5:
        return {"n": int(len(s)), "ic": None, "t": None, "hit": None}
    m, sd = float(s.mean()), float(s.std(ddof=1))
    t = m / (sd / math.sqrt(len(s))) if sd > 0 else 0.0
    p = 0.5 * math.erfc(t / math.sqrt(2))          # 单侧
    return {"n": int(len(s)), "ic": round(m, 4), "t": round(t, 2),
            "p_one_sided": p, "hit": round(float((s > 0).mean()), 3)}


def norm_p(t: float) -> float:
    return 0.5 * math.erfc(t / math.sqrt(2))


# --------------------------------------------------------------------------
# 组合臂
# --------------------------------------------------------------------------
def run_portfolio(m: dict, factors: dict, anchors: pd.DatetimeIndex,
                  labels: pd.DataFrame, mask: pd.DataFrame, horizon: int,
                  cfg: dict, top_n: int = TOP_N):
    """walk-forward 等权秩融合 → top-N → 次日开盘买 / H 日后开盘卖, 真实费用+滑点+整手。

    同期等权基准 = 可交易域内所有股票的「入场开盘 → 出场开盘」等权收益(不计费),
    存在 bench_ret 列里, 供超额口径使用。"""
    capital0 = float(cfg.get("capital", 20000))
    weight = float(cfg.get("regime_equity_weight", {}).get("aggressive", {}).get("attack", 0.873))
    slip = float(cfg.get("paper", {}).get("slip", 0.001))
    fee_p = fee_params(cfg)

    dates = pd.to_datetime(m["close"].index)
    op = m["open"].reindex(index=dates, columns=labels.columns).astype(np.float64)
    op_np = op.to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}

    names = sorted(factors)
    X = {n: _rank(factors[n].reindex(index=anchors)) for n in names}
    Y = _rank(labels)
    cols = list(labels.columns)
    # 票池掩码: 用外部传入的 PIT 可交易域(与标签同源), 不要在这里重新定义
    mask_np = mask.reindex(index=anchors, columns=cols).fillna(False).to_numpy()
    lab_ok = labels.notna().to_numpy()
    # 逐因子 IC(用于 walk-forward 定方向)
    ic_by_f = {}
    for n in names:
        ic_by_f[n] = row_ic(X[n], Y, lab_ok & np.isfinite(X[n]))

    idx = list(anchors)
    rows = []
    for k in range(TRAIL_IC, len(idx) - 1):
        d = idx[k]
        i = pos_of.get(d)
        if i is None or i + 1 >= len(dates) or i + 1 + horizon >= len(dates):
            continue
        # walk-forward 方向: 只用过去 TRAIL_IC 个锚点的 IC
        signs = {}
        for n in names:
            past = ic_by_f[n][k - TRAIL_IC:k]
            past = past[np.isfinite(past)]
            if len(past) < 10:
                continue
            mu = float(past.mean())
            if abs(mu) > 1e-9:
                signs[n] = 1.0 if mu > 0 else -1.0
        if len(signs) < 5:
            continue
        comp = np.zeros(len(cols))
        cnt = np.zeros(len(cols))
        for n, sgn in signs.items():
            v = X[n][k] * sgn
            ok = np.isfinite(v)
            comp[ok] += v[ok]
            cnt[ok] += 1
        with np.errstate(invalid="ignore"):
            comp = np.where(cnt > 0, comp / np.maximum(cnt, 1), np.nan)
        ok = mask_np[k] & np.isfinite(comp)
        if ok.sum() < MIN_NAMES:
            continue
        cand = np.argsort(-np.where(ok, comp, -np.inf))[:top_n]

        entry_i, exit_i = i + 1, i + 1 + horizon
        entry_d = dates[entry_i].strftime("%Y%m%d")
        exit_d = dates[exit_i].strftime("%Y%m%d")
        # 同期等权基准(可交易域, 开盘→开盘, 不计费)
        ok_b = mask_np[k]
        p0, p1 = op_np[entry_i], op_np[exit_i]
        good = ok_b & np.isfinite(p0) & np.isfinite(p1) & (p0 > 0)
        bench_ret = float(np.mean(p1[good] / p0[good] - 1.0)) if good.sum() >= MIN_NAMES else np.nan
        cash = capital0 if not rows else rows[-1]["nav_end"]
        budget = cash * weight / top_n
        spent = 0.0                            # 实际投入(含买入费), 未用预算留在现金
        proceeds = 0.0                         # 卖出净额(已扣卖出费)
        picked = []
        for j in cand:
            code = cols[j]
            p_in, p_out = op_np[entry_i, j], op_np[exit_i, j]
            if not (np.isfinite(p_in) and np.isfinite(p_out) and p_in > 0):
                continue
            px = round(float(p_in) * (1 + slip), 2)
            afford = int(np.floor(budget / (px * (1 + float(cfg.get("single_side_cost", 0.003)))) + 1e-9))
            sh = board_mod.lot_shares(code, afford)
            if sh <= 0:
                continue
            amt_in = sh * px
            fee_in = trade_cost(amt_in, "buy", entry_d, fee_p)
            if amt_in + fee_in > budget * 1.02:
                continue
            px_out = round(float(p_out) * (1 - slip), 2)
            amt_out = sh * px_out
            fee_out = trade_cost(amt_out, "sell", exit_d, fee_p)
            spent += amt_in + fee_in
            proceeds += amt_out - fee_out
            picked.append(code)
        if not picked:
            continue
        nav_end = cash - spent + proceeds
        rows.append({
            "date": d.strftime("%Y%m%d"), "nav_end": nav_end,
            "ret": nav_end / cash - 1.0, "n": len(picked),
            "bench_ret": bench_ret,
            "turnover": (1 - len(set(picked) & set(rows[-1]["picks"])) / max(1, len(picked)))
                        if rows else 1.0,
            "picks": picked,
        })
    if not rows:
        return None
    df = pd.DataFrame([{k: v for k, v in r.items() if k != "picks"} for r in rows])
    df["date"] = pd.to_datetime(df["date"])
    df["excess"] = df["ret"] - df["bench_ret"]
    return df


def ann(ret: pd.Series, per_year: float) -> float:
    if len(ret) == 0:
        return float("nan")
    return float((1 + ret).prod() ** (per_year / len(ret)) - 1)


def zone_ann(df: pd.DataFrame, zone: str, per_year: float) -> dict:
    lo, hi = ZONES[zone]
    s = df[(df["date"] >= lo) & (df["date"] <= hi)]
    if len(s) == 0:
        return {"n": 0}
    return {"n": int(len(s)), "ann": round(ann(s["ret"], per_year), 4),
            "bench": round(ann(s["bench_ret"], per_year), 4),
            "excess_pp": round(100 * (ann(s["ret"], per_year) - ann(s["bench_ret"], per_year)), 2),
            "hit": round(float((s["ret"] > 0).mean()), 3),
            "turnover": round(float(s["turnover"].mean()), 3)}


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-portfolio", action="store_true")
    ap.add_argument("--since", default=SINCE, help="行情加载起点(冒烟测试用, 会改变评估区间)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t_start = time.time()
    cfg = cfg_mod.load_config()

    log.info("加载行情(since=%s)…", a.since)
    m = fe.load_market(since=a.since)
    dates = pd.to_datetime(m["close"].index)
    wk = week_end_dates(dates)
    mt = fe.month_end_dates(dates)
    ik = wk.intersection(mt)          # 周锚点里落在月末的少数日期, 保持两臂可比
    log.info("交易日 %d | 周锚点 %d | 月锚点 %d | 交集 %d", len(dates), len(wk), len(mt), len(ik))

    pit = PITEngine(m=m, cfg=cfg)
    factors = build_factors(m, wk)
    log.info("因子面板 %d 个", len(factors))
    cols = m["close"].columns
    log.info("可交易掩码: 周锚点 …")
    mask_wk = tradable_frame(pit, wk, cols)
    log.info("可交易掩码: 月锚点 …")
    mask_mt = tradable_frame(pit, mt, cols)
    log.info("  周锚点平均可交易只数 %.0f | 月锚点 %.0f",
             float(mask_wk.sum(axis=1).mean()), float(mask_mt.sum(axis=1).mean()))

    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "since": a.since, "n_days": int(len(dates)), "n_week": int(len(wk)),
           "n_month": int(len(mt)), "factors": sorted(factors),
           "ic": {}, "portfolio": {}, "verdict": {}}

    # ---------------- Q1: IC 衰减 ----------------
    labels = {}
    for h in HORIZONS:
        log.info("标签: 未来 %d 交易日超额 …", h)
        labels[h] = forward_excess(m, mask_wk, h)
    log.info("月频对照标签: 未来 21 交易日超额 …")
    labels_m = {21: forward_excess(m, mask_mt, 21)}

    Yr = {h: _rank(labels[h]) for h in HORIZONS}
    Mr = {h: labels[h].notna().to_numpy() for h in HORIZONS}

    n_factors = len(factors)
    for h in HORIZONS:
        res["ic"][h] = {}
        for name, panel in factors.items():
            Xr = _rank(panel.reindex(index=wk))
            valid = Mr[h] & np.isfinite(Xr)
            ic = pd.Series(row_ic(Xr, Yr[h], valid), index=wk).dropna()
            z = {zz: zone_stats(ic, zz) for zz in ("DEAD", "ACTIVE")}
            m_all, sd_all = float(ic.mean()), float(ic.std(ddof=1)) if len(ic) > 1 else (0, 0)
            t_all = m_all / (sd_all / math.sqrt(len(ic))) if sd_all > 0 else 0.0
            z["FULL"] = {"n": int(len(ic)), "ic": round(m_all, 4), "t": round(t_all, 2),
                         "hit": round(float((ic > 0).mean()), 3)}
            res["ic"][h][name] = z

    # 主判据
    passed = []
    for name, z in res["ic"][5].items():
        d, act = z["DEAD"], z["ACTIVE"]
        if d["ic"] is None or act["ic"] is None or d["ic"] == 0 or act["ic"] == 0:
            continue
        same = (d["ic"] > 0) == (act["ic"] > 0)
        t = d["t"] or 0.0
        p_adj = min(1.0, norm_p(t) * n_factors)
        if same and abs(d["ic"]) >= 0.02 and t >= 2.0 and p_adj < 0.05:
            passed.append({"factor": name, "ic_dead": d["ic"], "ic_active": act["ic"],
                           "t_dead": t, "p_bonferroni": round(p_adj, 5)})
    res["verdict"]["Q1_weekly_alpha_exists"] = len(passed) >= 3
    res["verdict"]["Q1_n_passed"] = len(passed)
    res["verdict"]["Q1_passed"] = sorted(passed, key=lambda x: -abs(x["ic_dead"]))
    res["verdict"]["Q1_n_tested"] = n_factors

    # 月频对照(同一判据, 排除"探针本身有问题")
    passed_m = []
    for name, z in res["ic"][21].items():
        d, act = z["DEAD"], z["ACTIVE"]
        if d["ic"] is None or act["ic"] is None:
            continue
        t = d["t"] or 0.0
        p_adj = min(1.0, norm_p(t) * n_factors)
        if (d["ic"] > 0) == (act["ic"] > 0) and abs(d["ic"]) >= 0.02 and t >= 2.0 and p_adj < 0.05:
            passed_m.append(name)
    res["verdict"]["Q1_month_control_n_passed"] = len(passed_m)

    # ---------------- Q2: 成本后组合 ----------------
    if not a.skip_portfolio:
        log.info("组合臂: 周频 top%d (5 日持有) …", TOP_N)
        pw = run_portfolio(m, factors, wk, labels[5], mask_wk, 5, cfg)
        log.info("组合臂: 月频对照 top%d (21 日持有) …", TOP_N)
        pm = run_portfolio(m, factors, mt, labels_m[21], mask_mt, 21, cfg)
        for tag, df, per_year in (("week", pw, 52.0), ("month", pm, 12.0)):
            if df is None:
                res["portfolio"][tag] = None
                continue
            a_full = ann(df["ret"], per_year)
            b_full = ann(df["bench_ret"], per_year)
            res["portfolio"][tag] = {
                "n_periods": int(len(df)),
                "full": {"ann": round(a_full, 4), "bench": round(b_full, 4),
                         "excess_pp": round(100 * (a_full - b_full), 2),
                         "hit": round(float((df["ret"] > 0).mean()), 3),
                         "turnover": round(float(df["turnover"].mean()), 3)},
                "DEAD": zone_ann(df, "DEAD", per_year),
                "ACTIVE": zone_ann(df, "ACTIVE", per_year),
            }
            log.info("[%s] 全史 组合 %.2f%% / 基准 %.2f%% ⇒ 超额 %+.2fpp | 命中 %.3f 换手 %.3f",
                     tag,
                     100 * res["portfolio"][tag]["full"]["ann"],
                     100 * res["portfolio"][tag]["full"]["bench"],
                     res["portfolio"][tag]["full"]["excess_pp"],
                     res["portfolio"][tag]["full"]["hit"],
                     res["portfolio"][tag]["full"]["turnover"])
            log.info("      DEAD   %s", res["portfolio"][tag]["DEAD"])
            log.info("      ACTIVE %s", res["portfolio"][tag]["ACTIVE"])
        w = res["portfolio"].get("week")
        if w:
            res["verdict"]["Q2_weekly_net_excess_pp"] = w["full"]["excess_pp"]
            res["verdict"]["Q2_pass_10pp"] = bool(w["full"]["excess_pp"] >= 10.0)
            res["verdict"]["Q2_active_excess_pp"] = w["ACTIVE"].get("excess_pp")

    res["secs"] = round(time.time() - t_start, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("== 完成 %.1fs → %s ==", res["secs"], OUT)
    log.info("判决 Q1: 周频 alpha 存在 = %s (因子 %d/%d 通过; 月频对照 %d 个通过)",
             res["verdict"]["Q1_weekly_alpha_exists"], res["verdict"]["Q1_n_passed"],
             n_factors, res["verdict"]["Q1_month_control_n_passed"])


if __name__ == "__main__":
    main()
