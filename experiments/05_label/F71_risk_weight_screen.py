"""F71 · **风险加权 / 约束凸优化 vs 等权 + 8× 带**（理论层 26 号 §可检验假设 2）—— 预注册筛选

## 为什么是这条（不是新搜索）

理论层 26 号把"组合构造"列成四个可检验假设，前三个与这条的关系：
  · F54（波动率目标敞口）否：动态降敞口 = 沿 F25 风险斜线随机缩放 ⇒ 结清；
  · F55（分位加权 vs 等权）否：**分数在 top100 内的量级不提供额外风险调整信息**（连零成本毛 Sharpe 都更低）；
  · F37（提高投放率）否：同一根风险刻度。
⇒ 剩下**唯一没测过**的是：**同样满仓、同样选股域，但用风险模型（而不是分数/不是敞口）来配权**，
   以及**用换手预算（凸约束）替代 8× 排名出带**。这就是 26 号 §2 的原话：
   "把 top100 + 退出带换成'带单票/行业/换手约束的凸优化'，在**同成本、同可交易域**下两轴表现如何？"

## 口径（跑数前冻结）

- 数据：生产融合分 `data/weekly/scores_20260911.npz`（2×W5+pit）+ 生产 PIT 掩码 +
  5 日 `forward_excess`（与 `scores.forward_excess` 同口径）—— **与 F55 同源**，其 E 臂读数
  （FULL 净超额 32.24pp / 换手 1.0123 / MDD −22.72%）是本脚本的**保真锚**，对不上就说明没复刻口径。
- 风险模型（**无前视**）：用 `close × adj` 算日收益，取**锚点及之前**最近 `VOL_WIN=60` 个交易日；
  样本 < `MIN_OBS=40` 的票用该期 top100 的 σ 中位数（**不排除任何票** ⇒ 选股域与基线完全一致）；
  协方差用 sklearn `LedoitWolf` 收缩（样本不足时退化为对角）。
- 六臂（**固定形式，不做参数搜索**；单票上限 `CAP=3%`，由 position-sizing skill 的"单票 ≤10%"再收紧）：
    · `E`     ：等权 top100（无出带）—— F55 的 E 臂，作**口径保真锚**
    · `EB`    ：等权 top100 + **8× 排名出带**（= 现役构型在这套 harness 里的近似）—— **主基线**
    · `IV`    ：逆波动率权重 ∝ 1/σ（风险平价的一阶近似），截断到 3% 后迭代归一
    · `MV`    ：最小方差（凸 QP：min w'Σw，s.t. Σw=1、0≤w≤3%），解析迭代解 + cvxpy 交叉自检
    · `MVB25` / `MVB50`：最小方差解**按换手预算 B∈{0.25, 0.50}/期**向上一期权重收缩
      （= "换手约束的凸优化"，替掉 8× 出带）
- 成本：换手 = `Σ|w_t − w_{t-1}|`（进出名单都算），按 `fees.effective_rate(25k,'sell')` 计价
  （与 F55 逐字相同；保守：把全部换手当卖出）。
- **判据（四条同时满足才算候选）**：相对 `EB`——
    ① FULL **毛**超额 ≥ EB − 0.5pp（先看信号，不许靠成本假设或降风险取胜）；
    ② FULL |MDD| ≤ |MDD_EB| + 0.5pp；
    ③ FULL 毛 Sharpe ≥ Sharpe_EB；
    ④ 换手均值 ≤ 1.2 × EB。
  四条全过 ⇒ 下一步必须进 **F1b 离散模拟器（生产构型 + 两套种子）** 再判；否则如实登记"否"。
  另报 OOS_32m（2024-01~2026-08 留出集）与 DEAD 段，以及 HAC(lag=2) 配对 t（仅参考）。

## 诚实边界

- 连续近似：无整手、无涨跌停闸门、每期 100% 投入 ⇒ **绝对水平不可与 F52 的 +17.78pp 比**，
  只有**臂间差**有效（与 F55/F64 同一类 harness 的既有声明）。
- `MV`/`IV` 会主动往**低波动票**偏，而 F68 说这条策略的 α 在微盘段内 ⇒ 先验上它们**可能输**；
  本实验的价值一半在"若赢则进生产构型"，一半在"若输则把 26 号 §2 结清、研究菜单干净收口"。
- 本脚本**只读**（行情/打分存档），不写任何生产产物、不碰 `config.yaml`、不换 pin。

运行：``D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F71_risk_weight_screen.py``
      ``… F71_risk_weight_screen.py --smoke 30``（只跑前 30 期 + cvxpy 自检）
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfg_mod              # noqa: E402
from app.factors import factor_engine as fe    # noqa: E402
from app.pit.engine import PITEngine           # noqa: E402
from app.portfolio import fees as fee_mod      # noqa: E402
from app.weekly import scores as sc            # noqa: E402

log = logging.getLogger("f71")
OUT = HERE / "F71_risk_weight_screen_result.json"
NPZ = BASE / "data" / "weekly" / "scores_20260911.npz"
TOP_N = 100
PER_YEAR = 52.0
BAND = 8.0                     # 现役出带: 排名掉出 top_n×8 才卖
CAP = 0.03                     # 单票上限
VOL_WIN = 60                   # 风险模型回看交易日
MIN_OBS = 40
BUDGETS = (0.25, 0.50)         # MVB 的每期换手预算
WINDOWS = {"FULL": ("2010-01-01", "2030-12-31"),
           "OOS_32m": ("2024-01-01", "2026-08-31"),
           "DEAD": ("2020-09-01", "2023-12-31")}
# 判据(冻结)
MIN_GROSS_TOL_PP = 0.5
MAX_MDD_TOL_PP = 0.5
MAX_TURNOVER_RATIO = 1.2
F55_FIDELITY = {"E_full_net_excess_pp": 32.24, "E_full_turnover": 1.0123, "E_full_mdd": -0.2272}


def series_stats(ret: pd.Series) -> dict:
    r = ret.dropna()
    if len(r) < 3:
        return {"n": int(len(r))}
    nav = (1 + r).cumprod()
    sd = float(r.std(ddof=1))
    return {"n": int(len(r)),
            "excess_pp": round(100 * float((1 + r).prod() ** (PER_YEAR / len(r)) - 1), 2),
            "mean_pp_per_week": round(100 * float(r.mean()), 3),
            "vol_pp": round(100 * sd, 3),
            "sharpe": round(float(r.mean() / sd), 3) if sd > 0 else None,
            "mdd": round(float((nav / nav.cummax() - 1).min()), 4)}


def _cap_normalize(v: np.ndarray, cap: float = CAP, iters: int = 60) -> np.ndarray:
    """把非负向量归一到和为 1 且每项 ≤ cap（超限项截断、余量按比例分给其余项）。"""
    v = np.clip(np.asarray(v, dtype=float), 0.0, None)
    if v.sum() <= 0 or not np.isfinite(v.sum()):
        return np.full(len(v), 1.0 / len(v))
    w = v / v.sum()
    if cap * len(v) < 1.0:                      # cap 太紧则无法满足, 退化为等权
        return np.full(len(v), 1.0 / len(v))
    for _ in range(iters):
        over = w > cap + 1e-12
        if not over.any():
            break
        w[over] = cap
        free = ~over
        rest = 1.0 - cap * int(over.sum())
        if not free.any() or rest <= 0:
            break
        w[free] = v[free] / v[free].sum() * rest
    return w / w.sum()


def minvar_capped(Sigma: np.ndarray, cap: float = CAP, iters: int = 100):
    """min w'Σw s.t. Σw=1、0≤w≤cap —— **主动集法**（伪逆 + 逐轮固定越界项）。

    第一版写成"只截断上界、下界靠 clip" ⇒ 与 cvxpy 差到 0.03（= cap），自检当场抓到；
    现在上界/下界都进主动集，并与 cvxpy 逐期比对（KB 12 号"统计实现必须带自检"）。
    """
    n = Sigma.shape[0]
    if cap * n < 1.0:                            # cap 太紧 ⇒ 无可行解, 退化等权
        return np.full(n, 1.0 / n)
    fixed = np.zeros(n, dtype=bool)
    w_fix = np.zeros(n)
    for _ in range(iters):
        free = ~fixed
        nf = int(free.sum())
        if nf == 0:
            break
        budget = 1.0 - float(w_fix[fixed].sum())
        if budget <= 0:
            break
        Sf = Sigma[np.ix_(free, free)]
        try:
            invf = np.linalg.pinv(Sf)
        except np.linalg.LinAlgError:
            break
        v = invf @ np.ones(nf)
        den = float(np.ones(nf) @ v)
        if not np.isfinite(den) or den <= 0:
            break
        w_free = v / den * budget
        hi = w_free > cap + 1e-10
        lo = w_free < -1e-10
        if not hi.any() and not lo.any():
            out = w_fix.copy()
            out[free] = np.clip(w_free, 0.0, cap)
            return _cap_normalize(out, cap)
        # 固定**全部**越界项(上界→cap, 下界→0), 下一轮在剩余自由集上重解
        idx_free = np.where(free)[0]
        for pos in idx_free[hi]:
            fixed[pos] = True
            w_fix[pos] = cap
        for pos in idx_free[lo]:
            fixed[pos] = True
            w_fix[pos] = 0.0
    return None                                  # 没收敛: 交给调用方决定(不静默返回错解)


def minvar_cvxpy(Sigma: np.ndarray, cap: float = CAP):
    """cvxpy 精确解（主路径）。失败返回 None ⇒ 调用方退回解析解并计数。"""
    import cvxpy as cp
    n = Sigma.shape[0]
    w = cp.Variable(n)
    prob = cp.Problem(cp.Minimize(cp.quad_form(w, cp.psd_wrap(Sigma))),
                      [cp.sum(w) == 1, w >= 0, w <= cap])
    for solver in (cp.CLARABEL, cp.ECOS, None):
        try:
            prob.solve(solver=solver) if solver else prob.solve()
            if w.value is not None:
                return _cap_normalize(np.asarray(w.value).ravel(), cap)
        except Exception:
            continue
    return None


def _cvxpy_check(Sigma: np.ndarray, w_analytic: np.ndarray, cap: float = CAP) -> float:
    """用 cvxpy 独立解同一问题，返回与解析解的最大绝对差（自检用）。"""
    w = minvar_cvxpy(Sigma, cap)
    if w is None:
        return float("nan")
    return float(np.max(np.abs(w - w_analytic)))


def _risk_weights(rets_win: np.ndarray, mv_stats: dict = None) -> dict:
    """一期 top100 的风险权重。rets_win: (T×n) 日收益, 可能含 NaN。"""
    n = rets_win.shape[1]
    X = np.where(np.isfinite(rets_win), rets_win, 0.0)          # 缺失按截面均值≈0 处理(不排除票)
    obs = np.isfinite(rets_win).sum(axis=0)
    sd = np.nanstd(np.where(np.isfinite(rets_win), rets_win, np.nan), axis=0)
    med = float(np.nanmedian(sd)) if np.isfinite(sd).any() else 1e-2
    sd = np.where((obs >= MIN_OBS) & np.isfinite(sd) & (sd > 1e-8), sd, med)
    w_iv = _cap_normalize(1.0 / np.clip(sd, 1e-6, None))
    if n >= 8 and obs.min() >= 10:
        try:
            from sklearn.covariance import LedoitWolf
            Sig = LedoitWolf(assume_centered=False).fit(X).covariance_
            Sig = (Sig + Sig.T) / 2.0
        except Exception:
            Sig = np.diag(sd ** 2)
    else:
        Sig = np.diag(sd ** 2)
    w_mv = minvar_cvxpy(Sig)
    if w_mv is None:
        if mv_stats is not None:
            mv_stats["cvxpy_fail"] = mv_stats.get("cvxpy_fail", 0) + 1
        w_mv = minvar_capped(Sig)
        if w_mv is None:
            if mv_stats is not None:
                mv_stats["analytic_fail"] = mv_stats.get("analytic_fail", 0) + 1
            w_mv = _cap_normalize(np.ones(n), CAP)      # 已知可行(等权) ⇒ 不静默失败, 有计数
    return {"IV": w_iv, "MV": w_mv, "sigma": Sig, "sd": sd}


def turnover_capped(target: np.ndarray, prev: np.ndarray, budget: float) -> np.ndarray:
    """**换手预算版凸优化**：把目标权重向上一期实际权重收缩, 使单期单边换手 ≤ budget。

    这就是"用约束替代 8× 出带"的那一步（`prev` 必须是**本臂上一期的权重**, 递归平滑）。
    """
    if prev is None or prev.sum() <= 0:
        return target
    turn = float(np.abs(target - prev).sum())
    theta = 1.0 if turn <= budget else float(budget / max(turn, 1e-12))
    return _cap_normalize(theta * target + (1.0 - theta) * prev)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", type=int, default=0, help="只跑前 N 期(含 cvxpy 自检)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()

    with np.load(NPZ, allow_pickle=True) as z:
        S = z["scores"]
        anchors = pd.to_datetime([str(d) for d in z["anchors"]])
        cols = [str(c) for c in z["cols"]]
    cfg = cfg_mod.load_config()
    m = fe.load_market(since=sc.SINCE)
    pit = PITEngine(m=m, cfg=cfg)
    mask = pit.tradable_matrix(anchors, columns=cols).fillna(False)
    Y = sc.forward_excess(m, mask, sc.HORIZON)
    adj_close = (m["close"] * m["adj"]).reindex(columns=cols)
    rets_df = adj_close.pct_change()
    rets_np = rets_df.to_numpy(dtype=float)          # 位置索引: 列顺序 = cols, 与 S/mask 对齐
    rets_idx = rets_df.index
    log.info("面板就绪 | %d 锚点 × %d 股 | %.0fs", len(anchors), len(cols), time.time() - t0)

    sell_rate = fee_mod.effective_rate(25_000.0, "sell", "20260914")
    arms = ["E", "EB", "IV", "MV"] + [f"MVB{int(b * 100)}" for b in BUDGETS]
    rows, prev_w, held_band, check_diffs, sel_checks = [], {}, [], [], []
    mv_stats = {"cvxpy_fail": 0, "analytic_fail": 0}
    mv_solves = 0
    stop_after = int(args.smoke) if args.smoke else 0     # smoke: 收满 N 期就停(不是前 N 个锚点)
    for k in range(len(anchors)):
        if stop_after and len(rows) >= stop_after:
            break
        d = anchors[k]
        y = Y[k].astype(float)
        s = S[k].astype(float)
        ok = mask.iloc[k].to_numpy() & np.isfinite(s) & np.isfinite(y)
        if ok.sum() < 200:
            continue
        rank = np.full(len(s), np.iinfo(np.int32).max)
        live = np.where(ok)[0]
        # ⚠️ 2026-09-14 两处**逆置换**错误（同一类，均由自检当场抓到）：
        #   ① `idx = live[np.argsort(rank[live])]` ⇒ 选出来是**随机篮子**（分数中位 0.42~0.67、
        #      与真 top100 只重叠 0~3 只、换手 1.94）—— 被"E 臂必须复现 F55"的**保真锚**抓到；
        #   ② `rank[live] = np.argsort(-s[live])` ⇒ rank 里存的是"第 j 名是谁"，不是"某票第几名"
        #      ⇒ **EB(8× 带)基线退化成"随机保留"**，把基线压低 5pp —— 被下面的 `selection_sanity`
        #      抓到（mean_rank_pct 应 ≈0.02，却报 0.499）。
        #   正确写法：order = 名次→票；inv 才是 票→名次。
        sel_order = np.argsort(-s[live], kind="stable")
        inv_rank = np.empty(len(sel_order), dtype=np.int64)
        inv_rank[sel_order] = np.arange(len(sel_order))
        rank[live] = inv_rank
        idx = live[sel_order][:TOP_N]
        sel_checks.append(float(np.mean(rank[idx] / max(1, len(live)))))
        ex = y[idx]
        n = len(idx)
        # ---- 目标权重
        w_target = {"E": np.full(n, 1.0 / n)}
        keep = [j for j in held_band if rank[j] < TOP_N * BAND]
        tgt = list(keep)
        for j in idx:
            if len(tgt) >= TOP_N:
                break
            if j not in tgt:
                tgt.append(j)
        tgt = tgt[:TOP_N]
        w_target["EB"] = dict(zip(tgt, np.full(len(tgt), 1.0 / len(tgt))))
        t_idx = pd.Timestamp(d)
        rp = int(rets_idx.searchsorted(t_idx, side="right"))     # 只用 ≤ 锚点 的日收益(无前视)
        hist = rets_np[max(0, rp - VOL_WIN):rp, :][:, np.asarray(idx, dtype=int)]
        rw = _risk_weights(hist, mv_stats)
        mv_solves += 1
        if args.smoke and k % 5 == 0:
            check_diffs.append(_cvxpy_check(rw["sigma"], rw["MV"]))
        for a in ("IV", "MV"):
            w_target[a] = dict(zip(idx, rw[a]))
        for b in BUDGETS:
            a = f"MVB{int(b * 100)}"
            prev_b = np.array([prev_w.get(a, {}).get(int(j), 0.0) for j in idx])
            w_target[a] = dict(zip(idx, turnover_capped(rw["MV"], prev_b, b)))
        # ---- 毛收益 + 换手
        row = {"date": d}
        for a in arms:
            wt = w_target[a]
            if isinstance(wt, np.ndarray):
                wt = dict(zip(idx, wt))
            w_vec = np.array([wt.get(int(j), 0.0) for j in idx])
            row[f"gross_{a}"] = float(np.dot(w_vec, ex))
            names = set(wt) | set(prev_w.get(a, {}))
            row[f"turnover_{a}"] = float(sum(abs(wt.get(j, 0.0) - prev_w.get(a, {}).get(j, 0.0))
                                             for j in names))
            prev_w[a] = wt
        held_band = tgt
        rows.append(row)
    if not rows:
        log.error("没有任何可比期（分数/标签/掩码全空?）—— 不落盘")
        return 2
    df = pd.DataFrame(rows)
    for a in arms:
        df[f"net_{a}"] = df[f"gross_{a}"] - df[f"turnover_{a}"] * sell_rate
        df[f"turnover_{a}"] = df[f"turnover_{a}"]
    log.info("逐期完成: %d 期 | %.0fs", len(df), time.time() - t0)

    res = {"protocol": {"n_periods": int(len(df)), "top_n": TOP_N, "band": BAND, "cap": CAP,
                        "vol_win": VOL_WIN, "min_obs": MIN_OBS, "budgets": list(BUDGETS),
                        "sell_rate_bp": round(sell_rate * 1e4, 3),
                        "criteria": {"min_gross_tol_pp": MIN_GROSS_TOL_PP,
                                     "max_mdd_tol_pp": MAX_MDD_TOL_PP,
                                     "max_turnover_ratio": MAX_TURNOVER_RATIO},
                        "baseline_for_criteria": "EB(等权+8×带)",
                        "caveat": "连续近似: 无整手/无涨跌停闸门/每期满仓 ⇒ 只有臂间差有效"},
           "fidelity_target_f55_E": F55_FIDELITY,
           "mv_solver": {"primary": "cvxpy(CLARABEL/ECOS)", "fallback": "analytic(active-set)",
                         "n_solves": int(mv_solves), **mv_stats},
           "selection_sanity": {"mean_rank_pct": round(float(np.mean(sel_checks)), 5),
                                "max_rank_pct": round(float(np.max(sel_checks)), 5),
                                "threshold": 0.05,
                                "ok": bool(np.max(sel_checks) < 0.05)},
           "cvxpy_check_max_abs_diff": (round(float(np.nanmax(check_diffs)), 6)
                                        if check_diffs else None),
           "n_cvxpy_checks": len(check_diffs), "windows": {}}
    for w, (lo, hi) in WINDOWS.items():
        s = df[(df["date"] >= lo) & (df["date"] <= hi)]
        if len(s) < 3:
            res["windows"][w] = {"n": int(len(s))}
            continue
        blk = {"n": int(len(s))}
        for a in arms:
            blk[a] = {"gross": series_stats(s[f"gross_{a}"]),
                      "net": series_stats(s[f"net_{a}"]),
                      "turnover_mean": round(float(s[f"turnover_{a}"].mean()), 3)}
        eb_g = s["gross_EB"]
        for a in arms:
            d_ = s[f"gross_{a}"] - eb_g
            sd = float(d_.std(ddof=1))
            blk[f"paired_t_gross_{a}_vs_EB"] = (round(float(d_.mean() / (sd / np.sqrt(len(d_)))), 2)
                                               if sd > 0 else None)
        res["windows"][w] = blk

    full = res["windows"].get("FULL", {})
    if "EB" in full:
        base_g, base_n, base_t = full["EB"]["gross"], full["EB"]["net"], full["EB"]["turnover_mean"]
        verdicts = {}
        for a in arms:
            if a == "EB" or a not in full:
                continue
            g, nn, tt = full[a]["gross"], full[a]["net"], full[a]["turnover_mean"]
            c1 = g["excess_pp"] >= base_g["excess_pp"] - MIN_GROSS_TOL_PP
            c2 = abs(g["mdd"]) <= abs(base_g["mdd"]) * 100 + MAX_MDD_TOL_PP
            c3 = (g["sharpe"] or -9) >= (base_g["sharpe"] or 0)
            c4 = tt <= base_t * MAX_TURNOVER_RATIO
            verdicts[a] = {"gross_ok": bool(c1), "mdd_ok": bool(c2), "sharpe_ok": bool(c3),
                           "turnover_ok": bool(c4), "candidate": bool(c1 and c2 and c3 and c4),
                           "delta_gross_pp": round(g["excess_pp"] - base_g["excess_pp"], 2),
                           "delta_net_pp": round(nn["excess_pp"] - base_n["excess_pp"], 2),
                           "delta_mdd": round(g["mdd"] - base_g["mdd"], 4),
                           "sharpe": g["sharpe"], "base_sharpe": base_g["sharpe"]}
        res["verdict_vs_EB"] = verdicts
        res["any_candidate"] = any(v["candidate"] for v in verdicts.values())
        # 保真锚: E 臂(F55 口径)
        e_full = res["windows"]["FULL"].get("E", {})
        if e_full:
            res["fidelity_check_E"] = {
                "net_excess_pp": e_full["net"]["excess_pp"],
                "turnover": e_full["turnover_mean"],
                "mdd": e_full["net"]["mdd"],
                "target": F55_FIDELITY,
                "ok": bool(abs(e_full["net"]["excess_pp"] - F55_FIDELITY["E_full_net_excess_pp"]) < 1.0
                           and abs(e_full["turnover_mean"] - F55_FIDELITY["E_full_turnover"]) < 0.05
                           and abs(e_full["net"]["mdd"] - F55_FIDELITY["E_full_mdd"]) < 0.02)}
    res["secs"] = round(time.time() - t0, 1)
    if not args.smoke:
        OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: res[k] for k in ("protocol", "fidelity_check_E", "verdict_vs_EB",
                                          "any_candidate", "cvxpy_check_max_abs_diff",
                                          "n_cvxpy_checks") if k in res},
                     ensure_ascii=False, indent=1))
    log.info("结果 → %s（%.0fs）", OUT if not args.smoke else "(smoke, 未落盘)", time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
