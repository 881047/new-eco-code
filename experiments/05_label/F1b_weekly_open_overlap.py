"""F1b v2 · 周频(5 日)「可交易窗口 + 真实换手 + 前复权账户」—— 预注册脚本

## 这一版修正了什么（来源：两轮对抗性审查，逐条成立）

F1（初版）的 5 条方法学缺陷 + v1 审查的 5 条实现缺陷，全部在本版修掉：

| # | 缺陷 | 本版做法 |
|---|---|---|
| 1 | IC 用 close(t)→close(t+H)，组合却在 open(t+1) 成交 ⇒ 标签窗口 ≠ 交易窗口 | 主标签 = `open(t+1)→open(t+1+H)`（可交易窗口）；生产口径 close 标签作附属臂 |
| 2 | 组合每期全卖全买 ⇒ 按 100% 换手收费，周频被多收 3~4 倍 | 留存股票**不动**（只交易名单变化部分），费用只按真实 delta 计 |
| 3 | t 统计量按 iid 算，而 H 日标签重叠 | Newey-West HAC(lag=ceil(H/5)+1) |
| 4 | Bonferroni 分母按 31 个独立因子 | 按 IC 时序相关聚类；**单链接与完全链接都算，取较大者**作分母 |
| 5 | 周频 36 锚点(0.7y) vs 月频 36 锚点(3y)，口径不等 | 36 / 180 两臂剂量-反应 |
| 6 | **停牌/退市股当前价 NaN ⇒ 按 0 计价**（一周停牌 = 假亏 14.5%，退市股永久挂账为 0） | 停牌按**最后可得价**盯市；连续 ≥3 期无价 ⇒ 按最后价强制出清并计费 |
| 7 | **基准用「窗口末」票池**（把窗口内涨上来的票追溯纳入） | 基准用**决策日(=上一锚点)的票池**，与策略同口径 |
| 8 | **没有涨跌停闸门**（一字涨停买得进、封死跌停卖得出） | 用生产同源 `board.limit_ratio` + `round2(pre_close)` 判封板，买/卖分别顺延 |
| 9 | 全用**未复权**开盘价 ⇒ 除权日假亏 | 持仓市值用未复权价 + **按 adj 因子比例缩放股数**（分红送转自动再投资）；基准用复权价 |
| 10 | walk-forward 方向窗 `[k-trail, k)` 含标签未走完的那期 | 改成 `[k-trail-1, k-1)`（只用已实现 IC） |
| 11 | 两臂年化用了不同的有效样本 | 年化在**同一有效掩码**上做 |

## 预注册判据（跑数前冻结；覆盖不足则判决标记为 provisional）

Q1「周频 alpha 存在」= 主标签(可交易窗口, H=5)下，同时满足：
  ① 死区(2020-09-01~2023-12-31)与活跃区(2024-01-01~) IC **同号**
  ② |IC_死区| ≥ 0.02   ③ HAC t ≥ 2.0   ④ 聚类校正后 p < 0.05
  ⑤ 通过因子覆盖 **≥3 个聚类**，且通过数 ≥3
  剂量-反应：H=10 / H=21 用**同一谓词**计数（若只有 H=5 过、10/21 不过 ⇒ 按噪声处理）
Q2 成本闸门 = 周频 top-6 全史**成本后**超额 ≥ +10.0pp（账户口径），且活跃区超额 > 0；
  同时必须给出**零成本臂**（分离信号与摩擦）

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F1b_weekly_open_overlap.py
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F1b_weekly_open_overlap.py --since 20230101
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


f1 = _load("f1", HERE / "F1_weekly_ic_decay.py")      # 复用 F1 的锚点/因子/IC/统计工具

from app import config as cfg_mod          # noqa: E402
from app.factors import factor_engine as fe  # noqa: E402
from app.pit.engine import PITEngine       # noqa: E402
from app.portfolio import board as board_mod  # noqa: E402
from app.portfolio.fees import fee_params, trade_cost  # noqa: E402

log = logging.getLogger("f1b")

OUT = HERE / "F1b_weekly_open_overlap_result.json"
SINCE = "20151101"
ZONES = f1.ZONES
HORIZONS = (5, 10, 21)
TOP_N = 6
TRAILS = (36, 180)
CLUSTER_RHO = 0.70
MIN_NAMES = 200
COVER_MIN = 20          # 每个区段至少这么多个锚点, 否则判决标 provisional
STALE_EXIT = 3          # 连续 N 期拿不到价 ⇒ 按最后价强制出清(退市/长停)


# ---------------------------------------------------------------- 标签
def forward_excess_open(m: dict, mask: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """主标签: 可交易窗口 open(t+1) → open(t+1+H) 的超额(相对当日票池等权)。"""
    op = m["open"].copy()
    op.index = pd.to_datetime(op.index)
    fwd = op.shift(-(horizon + 1)) / op.shift(-1) - 1.0
    sub = fwd.reindex(index=mask.index, columns=mask.columns).where(mask)
    return sub.sub(sub.mean(axis=1), axis=0)


# ---------------------------------------------------------------- HAC t
def hac_t(x: np.ndarray, lag: int) -> float:
    x = np.asarray([v for v in x if np.isfinite(v)], dtype=float)
    n = len(x)
    if n < 10:
        return float("nan")
    mu = x.mean()
    d = x - mu
    s = float(d @ d) / n
    for l in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - l / (lag + 1)) * float(d[l:] @ d[:-l]) / n
    if s <= 0:
        return float("nan")
    return mu / math.sqrt(s / n)


def zone_stat_hac(ic: pd.Series, zone: str, lag: int) -> dict:
    lo, hi = ZONES[zone]
    s = ic.loc[(ic.index >= lo) & (ic.index <= hi)].dropna()
    if len(s) < 5:
        return {"n": int(len(s)), "ic": None, "t": None}
    t = hac_t(s.to_numpy(), lag)
    return {"n": int(len(s)), "ic": round(float(s.mean()), 4),
            "t": None if not np.isfinite(t) else round(float(t), 2)}


# ---------------------------------------------------------------- 聚类
def _linkage_clusters(ic_mat: pd.DataFrame, rho: float, complete: bool) -> dict:
    """complete=False 单链接(并查集); True 完全链接(凝聚: 仅当簇内两两都 ≥ρ 才合并)。"""
    names = list(ic_mat.columns)
    cm = ic_mat.corr().abs()
    if not complete:
        parent = {n: n for n in names}

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for i, a in enumerate(names):
            for b in names[i + 1:]:
                v = cm.at[a, b]
                if np.isfinite(v) and v >= rho:
                    ra, rb = find(a), find(b)
                    if ra != rb:
                        parent[rb] = ra
        grp = {}
        for n in names:
            grp.setdefault(find(n), []).append(n)
    else:
        clusters = [[n] for n in names]
        while True:
            best, bv = None, rho
            for i in range(len(clusters)):
                for j in range(i + 1, len(clusters)):
                    v = min(cm.at[a, b] for a in clusters[i] for b in clusters[j])
                    if np.isfinite(v) and v > bv:
                        best, bv = (i, j), v
            if best is None:
                break
            i, j = best
            clusters[i] = clusters[i] + clusters[j]
            clusters.pop(j)
        grp = {i: c for i, c in enumerate(clusters)}
    out = {}
    for cid, members in grp.items():
        for n in members:
            out[n] = cid
    return out


def q1_pass(d: dict, act: dict, n_denom: int) -> bool:
    """预注册 Q1 谓词（主标签与剂量-反应臂共用同一个谓词）。"""
    if not d.get("ic") or not act.get("ic") or d.get("t") is None:
        return False
    if (d["ic"] > 0) != (act["ic"] > 0):
        return False
    if abs(d["ic"]) < 0.02 or d["t"] < 2.0:
        return False
    return min(1.0, f1.norm_p(d["t"]) * n_denom) < 0.05


# ---------------------------------------------------------------- 组合(真实账户口径)
def run_overlap_portfolio(m: dict, factors: dict, anchors: pd.DatetimeIndex,
                          mask: pd.DataFrame, horizon: int, cfg: dict, pit: PITEngine,
                          trail: int, top_n: int = TOP_N,
                          gross: bool = False, score_matrix: pd.DataFrame = None,
                          hyst: float = 0.0, no_fees: bool = None,
                          no_gates: bool = None, rank_band: float = 0.0,
                          greedy_budget: bool = False,
                          fill_through: bool = False,
                          budget_cap_mult: float = 0.0,
                          max_lot_cost_mult: float = 0.0) -> pd.DataFrame | None:
    """周频/月频 top-N。留存股票不动; 名单变化才交易; 开盘价±滑点; 涨跌停闸门;
    持仓市值用未复权价 + 股数按 adj 比例缩放(除权自动再投资); 费用逐笔。

    gross=True ⇒ 零费用零滑点(把「信号」与「成本」分开: 净额 = 毛额 − 摩擦)。

    score_matrix: 外部打分(anchors × 股票, 如 LightGBM 预测)。给了它就直接用,
    **跳过 31 因子秩融合那条路径**(F4 用)。None ⇒ 原行为(逐位不变)。
    hyst: 持仓名加成(单位 = 当日截面标准差), 抑制无谓换手; 0.0 = 关闭。

    no_fees / no_gates: 把「费用」与「可交易性闸门」**分开**做归因(2026-09-12 F4 复盘需要):
      no_fees=True ⇒ 零佣金/印花税/滑点(仍保留涨跌停闸门)
      no_gates=True ⇒ 关掉涨跌停闸门(仍计费用) —— 用来量化「买得到但买不到」值多少
    gross=True 等价于两者均为 True(向后兼容)。
    rank_band: 排名退出带(>1 生效)。建仓看前 `top_n` 名, **卖出要等排名掉出 top_n×rank_band**;
      空出的名额由最高分非持仓补满 ⇒ 直接砍掉"擦边进出"的换手。0.0 = 关闭(原行为)。

    greedy_budget / fill_through(2026-09-12, 针对"新账户只投出 55%"这个实跑暴露的缺口):
      greedy_budget=True ⇒ 每笔预算 = **剩余权益预算 / 剩余名额**(而非固定 nav×w/top_n)。
        买不起一手的票被跳过时, 钱自动分给后面买得起的票 ⇒ 投入率上升。
      fill_through=True ⇒ 候选**向下顺延**: 目标名单里买不起的名额, 由排名更靠后的票补上
        (持仓上限仍是 top_n)。
      budget_cap_mult > 0 ⇒ 单票预算上限 = 该倍数 × (nav×w/top_n)。
        贪婪预算会把买不起的票的钱堆给后面的票(实测：持仓 100→62 只、回撤 −43%→−55.7%),
        加上限就是中间路线: **多买几只、每只不超配**。0.0 = 不限(原行为)。"""
    capital = float(cfg.get("capital", 20000))
    weight = float(cfg.get("regime_equity_weight", {}).get("aggressive", {}).get("attack", 0.873))
    no_fees = bool(gross) if no_fees is None else bool(no_fees)
    no_gates = bool(gross) if no_gates is None else bool(no_gates)
    slip = 0.0 if no_fees else float(cfg.get("paper", {}).get("slip", 0.001))
    fee_p = None if no_fees else fee_params(cfg)
    flat = 0.0 if no_fees else float(cfg.get("single_side_cost", 0.003))

    def _fee(amt, side, day):
        return 0.0 if no_fees else trade_cost(amt, side, day, fee_p)

    dates = pd.to_datetime(m["close"].index)
    cols = list(mask.columns)
    op_raw = m["open"].reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pc_raw = m["pre_close"].reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    adj = m["adj"].reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    cp = {c: j for j, c in enumerate(cols)}

    names = sorted(factors)
    X = {n: f1._rank(factors[n].reindex(index=anchors)).astype(np.float64) for n in names}
    mask_np = mask.reindex(index=anchors, columns=cols).fillna(False).to_numpy()

    lab = forward_excess_open(m, mask, horizon)
    Yr = f1._rank(lab)
    lab_ok = lab.notna().to_numpy()
    ic_by_f = {n: f1.row_ic(X[n], Yr, lab_ok & np.isfinite(X[n])) for n in names}

    idx = list(anchors)
    pos: dict[str, float] = {}
    last_px: dict[str, float] = {}
    stale: dict[str, int] = {}
    cash = capital
    prev_exec = None
    rows = []
    for k in range(trail, len(idx)):
        i = pos_of.get(idx[k])
        if i is None or i + 1 >= len(dates):
            continue
        e = i + 1
        rp, rpc, ra = op_raw[e], pc_raw[e], adj[e]
        dstr = dates[e].strftime("%Y%m%d")

        # 0) 除权/送转: 股数按 adj 比例缩放(等价于把分红再投资), 保住市值连续性
        if prev_exec is not None and pos:
            a_prev = adj[prev_exec]
            for c in list(pos):
                j = cp[c]
                if np.isfinite(ra[j]) and np.isfinite(a_prev[j]) and a_prev[j] > 0:
                    pos[c] = pos[c] * ra[j] / a_prev[j]

        # 1) 盯市(停牌用最后可得价, 不要按 0 计)
        mv = 0.0
        for c, sh in pos.items():
            j = cp[c]
            p = rp[j]
            if np.isfinite(p) and p > 0:
                last_px[c] = float(p)
                stale[c] = 0
            else:
                stale[c] = stale.get(c, 0) + 1
                p = last_px.get(c, 0.0)
            mv += sh * p
        nav = cash + mv

        # 2) 目标名单
        can_trade = np.isfinite(rp) & (rp > 0)
        if score_matrix is not None:
            # copy(): reindex 出来的块可能是只读视图, 而下面滞回带要就地加分
            comp = score_matrix.reindex(index=anchors, columns=cols).to_numpy(
                dtype=np.float64)[k].copy()
        else:
            # 秩融合路径: walk-forward 方向, 只用**已实现**的 IC(去掉标签未走完的那期)
            signs = {}
            for n in names:
                past = ic_by_f[n][max(0, k - trail - 1):k - 1]
                past = past[np.isfinite(past)]
                if len(past) >= 10:
                    mu = float(past.mean())
                    if abs(mu) > 1e-9:
                        signs[n] = 1.0 if mu > 0 else -1.0
            if len(signs) < 5:
                continue
            comp = np.zeros(len(cols))
            cnt = np.zeros(len(cols))
            for n, sgn in signs.items():
                v = X[n][k] * sgn
                okv = np.isfinite(v)
                comp[okv] += v[okv]
                cnt[okv] += 1
            with np.errstate(invalid="ignore"):
                comp = np.where(cnt > 0, comp / np.maximum(cnt, 1), np.nan)
        if hyst and pos:
            _m = mask_np[k] & np.isfinite(comp)
            if _m.sum() > 50:
                sd = float(np.nanstd(comp[_m]))
                for c in pos:
                    j = cp[c]
                    if np.isfinite(comp[j]):
                        comp[j] += hyst * sd
        ok = mask_np[k] & np.isfinite(comp) & can_trade
        if max_lot_cost_mult > 0:
            # 选股阶段就要求"一手买得起": 价格×100 ≤ 倍数 × 等权预算。
            # 0.0 = 关(买不起的在买入环节才被跳过 ⇒ 名额空置、投入率下降)。
            eq_budget = nav * weight / max(1, top_n)
            ok &= (rp * 100.0) <= (max_lot_cost_mult * eq_budget)
        if ok.sum() < MIN_NAMES:
            continue
        order = np.argsort(-np.where(ok, comp, -np.inf))
        n_ext = int(top_n * 5) if fill_through else top_n    # 顺延候选池
        if rank_band and pos:
            band = set(cols[j] for j in order[:int(top_n * rank_band)])
            cand = [c for c in pos if c in band]
            for j in order:
                if len(cand) >= n_ext:
                    break
                c = cols[j]
                if c not in cand:
                    cand.append(c)
        else:
            cand = [cols[j] for j in order[:n_ext]]

        # 涨跌停闸门(与生产 board/round2 同源)
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

        # 3) 卖出: 不在目标名单里的持仓
        for c in list(pos):
            if c in cand:
                continue
            j = cp[c]
            p = rp[j]
            if np.isfinite(p) and p > 0:
                px_s = round(float(p) * (1 - slip), 2)
                lu, ld = _limits(c)
                if not no_gates and ld is not None and px_s <= ld + 0.001:
                    continue                      # 封死跌停: 卖不出, 顺延
                amt = pos[c] * px_s
                cash += amt - _fee(amt, "sell", dstr)
                last_px[c] = float(p)
                del pos[c]
                stale.pop(c, None)
            elif stale.get(c, 0) >= STALE_EXIT and c in last_px:
                px_s = round(float(last_px[c]) * (1 - slip), 2)
                amt = pos[c] * px_s
                cash += amt - _fee(amt, "sell", dstr)
                del pos[c]
                stale.pop(c, None)

        # 4) 买入: 目标名单里的新面孔(持仓上限 top_n; 买不起的票可被顺延/重新分配预算)
        equity_budget = nav * weight
        spend = 0.0
        for c in cand:
            if c in pos:
                continue
            if len(pos) >= top_n:
                break
            j = cp[c]
            p = rp[j]
            if not (np.isfinite(p) and p > 0):
                continue
            px_b = round(float(p) * (1 + slip), 2)
            lu, _ld = _limits(c)
            if not no_gates and lu is not None and px_b >= lu - 0.001:
                continue                          # 一字涨停: 买不进
            if greedy_budget:
                # 剩余预算 / 剩余名额: 前面有买不起的票时, 后面的票分到更多钱
                budget = max(0.0, equity_budget - spend) / max(1, top_n - len(pos))
            else:
                budget = equity_budget / max(1, top_n)
            if budget_cap_mult > 0:
                budget = min(budget, budget_cap_mult * equity_budget / max(1, top_n))
            afford = int(np.floor(budget / (px_b * (1 + flat)) + 1e-9))
            sh = board_mod.lot_shares(c, afford)
            if sh <= 0:
                continue
            amt = sh * px_b
            fee = _fee(amt, "buy", dstr)
            if amt + fee > cash:
                continue
            cash -= amt + fee
            spend += amt + fee
            pos[c] = float(sh)
            last_px[c] = float(p)
            stale[c] = 0

        rows.append({"date": dstr, "anchor_pos": k, "nav": nav, "n": len(pos),
                     "cash": cash, "picks": list(pos),
                     # 投入率(盯市时点的持仓市值占净值) —— 新账户"只投出 55%"那个缺口的度量
                     "invested_ratio": round((nav - cash) / nav, 4) if nav > 0 else 0.0})
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


def ann(ret: pd.Series, per_year: float) -> float:
    s = ret.dropna()
    if len(s) < 2:
        return float("nan")
    return float((1 + s).prod() ** (per_year / len(s)) - 1)


def zone_ann(df: pd.DataFrame, zone: str, per_year: float) -> dict:
    lo, hi = ZONES[zone]
    s = df[(df["date"] >= lo) & (df["date"] <= hi)].dropna(subset=["ret", "bench_ret"])
    if len(s) < 2:
        return {"n": int(len(s))}
    a, b = ann(s["ret"], per_year), ann(s["bench_ret"], per_year)
    return {"n": int(len(s)), "ann": round(a, 4), "bench": round(b, 4),
            "excess_pp": round(100 * (a - b), 2),
            "hit": round(float((s["ret"] > 0).mean()), 3),
            "turnover": round(float(s["turnover"].mean()), 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=SINCE)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()

    m = fe.load_market(since=a.since)
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    mt = fe.month_end_dates(dates)
    anchors = wk.union(mt)
    log.info("交易日 %d | 周锚点 %d | 月锚点 %d | 并集 %d",
             len(dates), len(wk), len(mt), len(anchors))

    pit = PITEngine(m=m, cfg=cfg)
    cols = m["close"].columns
    factors = f1.build_factors(m, anchors)
    mask_wk = f1.tradable_frame(pit, wk, cols)
    mask_mt = f1.tradable_frame(pit, mt, cols)
    log.info("因子 %d 个 | 周锚点可交易均值 %.0f", len(factors),
             float(mask_wk.sum(axis=1).mean()))

    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "since": a.since, "n_week": int(len(wk)), "n_month": int(len(mt)),
           "factors": sorted(factors), "ic": {}, "portfolio": {}, "verdict": {}}

    # ---------------- Q1 ----------------
    lab_open = {h: forward_excess_open(m, mask_wk, h) for h in HORIZONS}
    lab_close = {h: f1.forward_excess(m, mask_wk, h) for h in HORIZONS}
    ic_series = {}
    for tag, labs in (("open", lab_open), ("close", lab_close)):
        res["ic"][tag] = {}
        for h in HORIZONS:
            Yr = f1._rank(labs[h])
            ok_lab = labs[h].notna().to_numpy()
            lag = int(math.ceil(h / 5)) + 1
            res["ic"][tag][h] = {}
            for name, panel in factors.items():
                Xr = f1._rank(panel.reindex(index=wk))
                ic = pd.Series(f1.row_ic(Xr, Yr, ok_lab & np.isfinite(Xr)), index=wk).dropna()
                if tag == "open" and h == 5:
                    ic_series[name] = ic
                res["ic"][tag][h][name] = {zz: zone_stat_hac(ic, zz, lag)
                                           for zz in ("DEAD", "ACTIVE")}

    ic_mat = pd.DataFrame(ic_series)
    cl_single = _linkage_clusters(ic_mat, CLUSTER_RHO, complete=False)
    cl_complete = _linkage_clusters(ic_mat, CLUSTER_RHO, complete=True)
    n_single = len(set(cl_single.values()))
    n_complete = len(set(cl_complete.values()))
    n_denom = max(n_single, n_complete)          # 最保守(最大)分母
    res["clusters"] = {"single_linkage": n_single, "complete_linkage": n_complete,
                       "n_denom_used": n_denom,
                       "assign_complete": {k: int(v) for k, v in sorted(cl_complete.items())}}
    log.info("聚类: 单链接 %d | 完全链接 %d ⇒ Bonferroni 分母用 %d", n_single, n_complete, n_denom)

    def verdict_for(block: dict, cl: dict, need_clusters: int = 3):
        passed = [n for n, z in block.items() if q1_pass(z["DEAD"], z["ACTIVE"], n_denom)]
        cov = {len(set(cl[n] for n in passed))}
        return {"n_passed": len(passed),
                "n_clusters_covered": len(set(cl[n] for n in passed)),
                "exists": bool(len(passed) >= 3 and len(set(cl[n] for n in passed)) >= need_clusters),
                "passed": sorted(passed)}

    v_open5 = verdict_for(res["ic"]["open"][5], cl_complete)
    _probe = res["ic"]["open"][5][sorted(factors)[0]]
    nobs = {z: _probe[z]["n"] for z in ("DEAD", "ACTIVE")}
    # 覆盖充分 = 两区段样本够 **且** 数据真的从死区起点之前就开始(防止截断窗伪装成"覆盖充分")
    ic_first = min(ic_series[n].index.min() for n in ic_series)
    cov_ok = (nobs["DEAD"] >= COVER_MIN and nobs["ACTIVE"] >= COVER_MIN
              and ic_first <= pd.Timestamp(ZONES["DEAD"][0]) + pd.Timedelta(days=90))
    res["verdict"]["Q1_open_h5"] = v_open5
    res["verdict"]["Q1_coverage"] = {"ok": bool(cov_ok), "ic_first_date": str(ic_first.date()),
                                     "dead_zone_start": ZONES["DEAD"][0], **nobs}
    res["verdict"]["Q1_close_h5"] = verdict_for(res["ic"]["close"][5], cl_complete)
    res["verdict"]["Q1_open_h10_dose"] = verdict_for(res["ic"]["open"][10], cl_complete)
    res["verdict"]["Q1_open_h21_dose"] = verdict_for(res["ic"]["open"][21], cl_complete)
    res["verdict"]["Q1_provisional"] = bool(not cov_ok)
    log.info("Q1(主, 可交易窗口 H=5): 通过 %d 个 / 覆盖 %d 聚类 ⇒ 存在=%s (区间样本 DEAD=%d ACTIVE=%d, %s)",
             v_open5["n_passed"], v_open5["n_clusters_covered"], v_open5["exists"],
             nobs["DEAD"], nobs["ACTIVE"], "覆盖充分" if cov_ok else "**覆盖不足·provisional**")
    log.info("    剂量-反应: H=10 通过 %d | H=21 通过 %d | close 标签 H=5 通过 %d",
             res["verdict"]["Q1_open_h10_dose"]["n_passed"],
             res["verdict"]["Q1_open_h21_dose"]["n_passed"],
             res["verdict"]["Q1_close_h5"]["n_passed"])

    # ---------------- Q2 ----------------
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64)
    op_adj_np = op_adj.to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    arms = [("week", wk, mask_wk, 5, 52.0, 36, False),
            ("month", mt, mask_mt, 21, 12.0, 36, False),
            ("week_gross", wk, mask_wk, 5, 52.0, 36, True)]
    arms += [("week", wk, mask_wk, 5, 52.0, t, False) for t in TRAILS if t != 36]
    for tag, anchors_arm, mask_arm, h, per_year, trail, gross in arms:
        df = run_overlap_portfolio(m, factors, anchors_arm, mask_arm, h, cfg, pit, trail,
                                   gross=gross)
        if df is None or len(df) < 2:
            log.warning("[%s trail=%d gross=%s] 无有效期数", tag, trail, gross)
            continue
        mask_np_arm = mask_arm.reindex(index=anchors_arm, columns=cols).fillna(False).to_numpy()
        bench, prev_exec, prev_ap = [], None, None
        for _, r in df.iterrows():
            e = pos_of[r["date"]]
            if prev_exec is None or prev_ap is None:
                bench.append(np.nan)
            else:
                ok = mask_np_arm[int(prev_ap)]        # 决策日票池(不是窗口末)
                p0, p1 = op_adj_np[prev_exec], op_adj_np[e]
                good = ok & np.isfinite(p0) & np.isfinite(p1) & (p0 > 0)
                bench.append(float(np.mean(p1[good] / p0[good] - 1)) if good.sum() >= MIN_NAMES
                             else np.nan)
            prev_exec, prev_ap = e, int(r["anchor_pos"])
        df["bench_ret"] = bench
        key = f"{tag}_trail{trail}"
        ok_rows = df["ret"].notna() & df["bench_ret"].notna()
        a_full, b_full = ann(df.loc[ok_rows, "ret"], per_year), ann(df.loc[ok_rows, "bench_ret"], per_year)
        res["portfolio"][key] = {
            "n": int(ok_rows.sum()),
            "full": {"ann": round(a_full, 4), "bench": round(b_full, 4),
                     "excess_pp": round(100 * (a_full - b_full), 2),
                     "hit": round(float((df.loc[ok_rows, "ret"] > 0).mean()), 3),
                     "turnover": round(float(df.loc[ok_rows, "turnover"].mean()), 3)},
            "DEAD": zone_ann(df, "DEAD", per_year),
            "ACTIVE": zone_ann(df, "ACTIVE", per_year),
        }
        if len(df) < per_year:
            res["portfolio"][key]["insufficient"] = True
        log.info("[%s] n=%d 组合 %.2f%% / 基准 %.2f%% ⇒ 超额 %+.2fpp | 命中 %.3f 换手 %.3f%s",
                 key, int(ok_rows.sum()), 100 * a_full, 100 * b_full, 100 * (a_full - b_full),
                 float((df.loc[ok_rows, "ret"] > 0).mean()),
                 float(df.loc[ok_rows, "turnover"].mean()),
                 "  [样本不足]" if len(df) < per_year else "")
        log.info("      DEAD   %s", res["portfolio"][key]["DEAD"])
        log.info("      ACTIVE %s", res["portfolio"][key]["ACTIVE"])

    net = res["portfolio"].get("week_trail36")
    gro = res["portfolio"].get("week_gross_trail36")
    res["verdict"]["Q2"] = None if not net else {
        "excess_pp_full": net["full"]["excess_pp"],
        "pass_10pp": bool(net["full"]["excess_pp"] >= 10.0),
        "active_excess_pp": net["ACTIVE"].get("excess_pp"),
        "gross_excess_pp_full": None if not gro else gro["full"]["excess_pp"],
        "friction_pp": None if not gro else round(
            gro["full"]["excess_pp"] - net["full"]["excess_pp"], 2),
    }
    b = m["bench"].dropna()
    if len(b) > 10:
        yrs = (pd.to_datetime(b.index[-1]) - pd.to_datetime(b.index[0])).days / 365.25
        res["verdict"]["bench800_ann"] = round(float((b.iloc[-1] / b.iloc[0]) ** (1 / yrs) - 1), 4)
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 完成 %.1fs → %s ==", res["secs"], OUT)


if __name__ == "__main__":
    main()
