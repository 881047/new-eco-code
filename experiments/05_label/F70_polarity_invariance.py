"""F70 因子极性（方向对齐 / 极性统一 / 符号调整）在模型层是不是 no-op？（2026-09-14 早，预注册）

**用户口径（2026-09-14 早）**：这套做法的标准名字是 **因子方向对齐 / 因子极性统一 / 因子符号调整**。
**本实验要回答的是它的下一层问题**：要不要**为它改造模型层**（用户原话："如果这个方法可行也要把
模型层面改造适配这个方法"）。

**机制判断（跑数前写死，可被本实验推翻）**：
生产两个模型都是 **LightGBM 回归树**（`trainer.PARAMS`：`objective=regression`、**没有**
`monotone_constraints`、**没有** `linear_tree`）。树只按**特征的序**切分 ⇒ 把任一因子取负，只是把它
的候选切分**镜像**，"能达到的划分集合与每个划分的增益"逐一对应 ⇒ **符号在模型层是 no-op**，
不存在"因为没对齐极性而吃亏"的模型；极性统一要起作用，得先有一个**对符号敏感**的环节
（等权合成 / 线性打分 / IC 加权 / 门禁筛选）。

**两路证据（跑数前冻结）**：
  1. **已拟合的生产模型**（`data/models/ens/lgb_202608_s{42,7,123,2024}.txt`，月频生产 4 种子，
     2026-09-11 归档）：同一截面 X 与 −X 各推一次分 —— 用**生产产物本身**，不重训（秒级）。
  2. **周频袖套重训**（走生产函数 `app.weekly.scores.build_panel` + `scores.train`：生产面板、
     生产 PARAMS、4 种子、50 轮、月度重训、36 个月窗、禁运 3 锚点）：
     · `A_BASE` 原样；· `B_ALLNEG` 面板 31 个因子**全部取负**；
     · `C_SIGN` 按**极性表**取负（极性表 = 各因子在**死区** 2020-09~2023-12 的头部超额符号，
       一次性、等权、不搜索 —— 即标准做法里那张方向表）。
     三臂逐票比：最大绝对差 / 完全逐位相同的比例 / 各锚点 **top100 集合重叠率** / 全史与分段 IC。

**判据（冻结）**：`B_ALLNEG` 与 `C_SIGN` 相对 `A_BASE` 同时满足
  ① 所有可比锚点 **top100 集合重叠 = 100%**；② 逐票分数最大绝对差 **< 1e-6**（float32 精度级）
  ⇒ 判「**模型层对极性不变 ⇒ 不需要改造模型层**」（并据此把术语与结论写进 13 号）。
  任一不满足 ⇒ 判「机制判断被推翻」，如实登记实测差异，再谈怎么改。
  附带读数（**不作判据**）：三臂 IC、死区/活跃区分段 IC、单锚点最小重叠率、极性表本身。
**追加读数（2026-09-14 07:0x，判据未改、跑完第一遍之后补）**：首跑判据已被**推翻**（见 05 号），
  于是补测"换了持仓之后头部超额有没有变"——`head_excess_stats`（top100 等权 − 同截面等权，
  逐期配对 t），并把三臂逐期分数落盘 `F70_scores_<臂>.npz`，供日后当"噪声标尺"复用。
  另加 `--diag`：2×2 对称性诊断（单重训点、单种子），把"训练前取负"与"推分端取负"分开看。

**诚实限定**：① 极性表取自死区 ⇒ 它是**样本内**表（本实验只用它构造一个"取负的列集合"，不用它
  选策略，故不影响判据的样本外性质）；② 本实验**只解释、只判"要不要改模型层"**，不产生交易信号、
  不据此调参（红线见 14 号 §三补七）；③ A 臂同时充当**保真对照**：它的全史 IC 应 ≈ 0.0840
  （`app/weekly/scores.py` docstring 的既有读数），对不上说明本脚本没复刻生产路径。

**不变量**：只读行情库 / 因子表与生产归档模型，**不写任何生产产物**、不碰 `config.yaml`、不换 pin。
运行：``D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F70_polarity_invariance.py --probe``（先量成本）
      ``D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F70_polarity_invariance.py``
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
BASE_DIR = HERE.parent.parent
sys.path.insert(0, str(BASE_DIR))

from app import config as cfg_mod                                      # noqa: E402
from app import db                                                     # noqa: E402
from app.factors import factor_engine as fe                            # noqa: E402
from app.models import trainer as tr                                   # noqa: E402
from app.weekly import scores as sc                                    # noqa: E402

OUT = HERE / "F70_polarity_invariance_result.json"
ARCHIVE = BASE_DIR / "data" / "models" / "ens"
HEAD_N = 100                       # 组合头部(生产构型 top100) —— 只用于比"选中集合", 不跑模拟器
HEAD_FRAC = 0.10                   # 极性表的"头部"定义(与门禁逐字一致: 分数前 10%)
MIN_NAMES = 200                    # 截面股票数下限(与门禁一致)
DEAD = ("2020-09-01", "2023-12-31")
ACTIVE = ("2024-01-01", "2026-08-31")
SEEDS = (42, 7, 123, 2024)
log = logging.getLogger("f70")


# ------------------------------------------------------------------ 工具
def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _dead_zone_head(panel: dict, k: int) -> dict:
    """因子 k 在死区的头部超额（%/期，等权锚点平均）。口径与阶段 0c 门禁一致：
    头部 = 该锚点可交易且该因子有限的票里分数前 10%；超额 = 相对**同截面被打分股票**均值。"""
    F, mask = panel["F"], panel["mask"].to_numpy(dtype=bool)
    anchors = panel["anchors"]
    Y = sc.forward_excess(panel["m"], panel["mask"], sc.HORIZON)
    sel = np.where((anchors >= DEAD[0]) & (anchors <= DEAD[1]))[0]
    vals = []
    for i in sel:
        ok = mask[i] & np.isfinite(F[i, :, k]) & np.isfinite(Y[i])
        idx = np.where(ok)[0]
        if idx.size < MIN_NAMES:
            continue
        v = F[i, idx, k]
        order = np.argsort(-v, kind="stable")
        nhead = max(1, round(idx.size * HEAD_FRAC))
        vals.append(float(Y[i, idx[order[:nhead]]].mean() - Y[i, idx].mean()))
    if not vals:
        return {"n": 0, "head_pp": None, "sign": 0, "t_naive": None}
    mu = float(np.mean(vals)) * 100.0
    sd = float(np.std(vals, ddof=1)) if len(vals) > 2 else 0.0
    return {"n": len(vals), "head_pp": round(mu, 4), "sign": _sign(mu),
            "t_naive": round(float(np.mean(vals) / (np.std(vals, ddof=1) / np.sqrt(len(vals)))), 2)
            if len(vals) > 2 and sd > 0 else None}


def _ic_stats(S: np.ndarray, panel: dict) -> dict:
    Y = sc.forward_excess(panel["m"], panel["mask"], sc.HORIZON)
    ic, _n = sc.rank_ic_by_anchor(S, Y)
    anchors = panel["anchors"]

    def agg(lo: str, hi: str) -> dict:
        s = ic[(anchors >= lo) & (anchors <= hi)]
        s = s[np.isfinite(s)]
        if s.size < 3:
            return {"n": int(s.size), "mean": None, "t_naive": None}
        sd = float(s.std(ddof=1))
        return {"n": int(s.size), "mean": round(float(s.mean()), 5),
                "t_naive": round(float(s.mean() / (sd / np.sqrt(s.size))), 2) if sd > 0 else None}
    return {"FULL": agg("2000-01-01", "2030-12-31"), "DEAD": agg(*DEAD), "ACTIVE": agg(*ACTIVE)}


def _top_set(row: np.ndarray, ok: np.ndarray, n: int):
    idx = np.where(ok)[0]
    if idx.size <= n:
        return None
    return set(idx[np.argsort(-row[idx], kind="stable")][:n].tolist())


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    ra = pd.Series(a).rank().to_numpy()
    rb = pd.Series(b).rank().to_numpy()
    if ra.std() == 0 or rb.std() == 0:
        return float("nan")
    return float(np.corrcoef(ra, rb)[0, 1])


def compare(SA: np.ndarray, SB: np.ndarray, mask: np.ndarray) -> dict:
    """两臂逐票 / 逐锚点对照（可比 = 两侧都有限）。"""
    both = np.isfinite(SA) & np.isfinite(SB)
    rows = np.where(both.any(axis=1))[0]
    diffs, overlaps, rhos, shifts = [], [], [], []
    for i in rows:
        ok = both[i]
        a, b = SA[i, ok], SB[i, ok]
        diffs.append(np.abs(a - b))
        rhos.append(_spearman(a, b))
        o = mask[i] & np.isfinite(SA[i]) & np.isfinite(SB[i])
        ta, tb = _top_set(SA[i], o, HEAD_N), _top_set(SB[i], o, HEAD_N)
        if ta is not None and tb is not None:
            overlaps.append(len(ta & tb) / len(ta))
            common = sorted(ta & tb)
            if common:
                ra = pd.Series(SA[i][common]).rank(ascending=False).to_numpy()
                rb = pd.Series(SB[i][common]).rank(ascending=False).to_numpy()
                shifts.append(float(np.max(np.abs(ra - rb))))
    d = np.concatenate(diffs) if diffs else np.array([0.0])
    return {
        "n_anchors": int(rows.size),
        "max_abs_diff": float(np.max(d)),
        "p999_abs_diff": float(np.quantile(d, 0.999)),
        "frac_bitwise_equal": float(np.mean(d == 0.0)),
        "spearman_min": float(np.nanmin(rhos)) if rhos else None,
        "spearman_mean": float(np.nanmean(rhos)) if rhos else None,
        "top_overlap_min": float(np.min(overlaps)) if overlaps else None,
        "top_overlap_mean": float(np.mean(overlaps)) if overlaps else None,
        "n_anchors_top_overlap_lt_1": int(sum(1 for x in overlaps if x < 1.0)),
        "max_rank_shift_in_common_top": float(max(shifts)) if shifts else None,
    }


def head_excess_series(S: np.ndarray, panel: dict) -> np.ndarray:
    """逐锚点"头部超额"（top100 等权 − 同截面被打分股票等权），%/期。
    口径与极性表一致（都相对**同截面被打了分的票**），是研究构面上的读数，不是生产模拟器。"""
    Y = sc.forward_excess(panel["m"], panel["mask"], sc.HORIZON)
    mask = panel["mask"].to_numpy(dtype=bool)
    out = np.full(S.shape[0], np.nan)
    for i in range(S.shape[0]):
        ok = mask[i] & np.isfinite(S[i]) & np.isfinite(Y[i])
        idx = np.where(ok)[0]
        if idx.size <= HEAD_N:
            continue
        top = idx[np.argsort(-S[i, idx], kind="stable")][:HEAD_N]
        out[i] = (float(Y[i, top].mean()) - float(Y[i, idx].mean())) * 100.0
    return out


def _head_stats(series: np.ndarray, base: np.ndarray, anchors) -> dict:
    res = {}
    for tag, lo, hi in (("FULL", "2000-01-01", "2030-12-31"), ("DEAD", *DEAD),
                        ("ACTIVE", *ACTIVE)):
        # anchors 是 datetime64/Index: 直接与字符串比会抛 UFuncNoLoopError ⇒ 统一用 pandas 比
        a = pd.DatetimeIndex(anchors)
        sel = ((a >= pd.Timestamp(lo)) & (a <= pd.Timestamp(hi))
               & np.isfinite(series))
        s = series[sel]
        if s.size < 3:
            res[tag] = {"n": int(s.size)}
            continue
        d = series[sel] - base[sel]
        sd = float(d.std(ddof=1))
        res[tag] = {"n": int(s.size), "mean_pp": round(float(s.mean()), 4),
                    "delta_pp": round(float(d.mean()), 4),
                    "paired_t": round(float(d.mean() / (sd / np.sqrt(d.size))), 2) if sd > 0 else None}
    return res


def post_from_scores(panel: dict) -> dict:
    """**收尾重算**：三臂逐期分数已落盘 `F70_scores_*.npz` ⇒ 不重训，只重算对照与判决。

    起因（2026-09-14 07:0x）：首版收尾里 `_head_stats` 把 datetime64 与字符串比较 ⇒
    三臂跑完（~16 分钟）后收尾崩掉。分数已落盘 ⇒ 收尾重算即可，训练不必重来。
    """
    p = OUT
    res = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    mask = panel["mask"].to_numpy(dtype=bool)
    arms = {}
    for tag in ("A_BASE", "B_ALLNEG", "C_SIGN"):
        f = HERE / f"F70_scores_{tag}.npz"
        if not f.exists():
            continue
        z = np.load(f, allow_pickle=True)
        assert list(map(str, z["anchors"])) == list(
            panel["anchors"].strftime("%Y%m%d")), f"{tag} 的锚点与本次面板不一致, 拒绝对照"
        arms[tag] = z["scores"]
    hx = {t: head_excess_series(S, panel) for t, S in arms.items()}
    res["arms_ic"] = {t: {**res.get("arms_ic", {}).get(t, {}), "ic": _ic_stats(S, panel)}
                      for t, S in arms.items()}
    if "A_BASE" in arms:
        res["comparisons"] = {t: compare(arms["A_BASE"], arms[t], mask)
                              for t in ("B_ALLNEG", "C_SIGN") if t in arms}
        res["head_excess_stats"] = {t: _head_stats(hx[t], hx["A_BASE"], panel["anchors"])
                                    for t in hx}
        c = res["comparisons"]
        ok = bool(c) and all(v["top_overlap_min"] == 1.0 and v["max_abs_diff"] < 1e-6
                             for v in c.values())
        res["verdict"] = {
            "model_layer_polarity_is_noop": ok,
            "reading": ("模型层对因子极性不变 ⇒ 不需要为极性统一改造模型层" if ok else
                        "机制判断被推翻: 符号确实改变了重训输出, 需按实测差异重估"),
            "fidelity_check_ic_full_should_be_about": 0.0840,
            "note": "A_BASE 的 FULL IC 若明显偏离 0.0840 ⇒ 本脚本没复刻生产路径, 读数不可用"}
    p.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: res[k] for k in ("arms_ic", "comparisons", "head_excess_stats",
                                          "verdict") if k in res},
                     ensure_ascii=False, indent=1))
    return res


# ------------------------------------------------------------------ 证据 1：已拟合的生产模型
def part1_archived_models() -> dict:
    import lightgbm as lgb
    files = sorted(ARCHIVE.glob("lgb_202608_s*.txt"))
    if not files:
        return {"skipped": f"没有归档模型: {ARCHIVE}"}
    conn = db.get_conn()
    try:
        dt = conn.execute("SELECT MAX(trade_date) FROM factor").fetchone()[0]
        fac = pd.read_sql("SELECT ts_code, factor, value FROM factor WHERE trade_date=?",
                          conn, params=[dt])
    finally:
        conn.close()
    wide = fac.pivot_table(index="ts_code", columns="factor", values="value")
    out = {"asof": dt, "n_models": len(files), "n_stocks": len(wide), "per_seed": {}}
    for f in files:
        bo = lgb.Booster(model_file=str(f))
        names = bo.feature_name()
        miss = [n for n in names if n not in wide.columns]
        if miss:
            out["per_seed"][f.stem] = {"skipped": f"factor 表缺列 {miss[:5]}"}
            continue
        X = wide[names].to_numpy(dtype=np.float64)
        X = X[np.isfinite(X).any(axis=1)]
        p_pos, p_neg = bo.predict(X), bo.predict(-X)
        d = np.abs(p_pos - p_neg)
        rho = _spearman(p_pos, p_neg) if p_pos.std() > 0 else float("nan")
        out["per_seed"][f.stem] = {
            "n": int(X.shape[0]), "max_abs_diff": float(d.max()),
            "frac_bitwise_equal": float(np.mean(d == 0.0)),
            "spearman": None if not np.isfinite(rho) else round(rho, 10),
            "n_trees": int(bo.num_trees()), "n_features": len(names)}
    return out


# ------------------------------------------------------------------ 证据 2：周频重训三臂
def build_panel(cfg: dict) -> dict:
    m = fe.load_market(since=sc.SINCE)
    anchors = sc.week_end_dates(pd.to_datetime(m["close"].index))
    return sc.build_panel(m, anchors, cfg, industry="snapshot")


def run_arm(panel: dict, cfg: dict, neg_cols, progress=None) -> tuple:
    """在同一块 F 上**原地取负 → 训练 → 还原**（浮点取负精确可逆，省一份 ~470MB 拷贝）。"""
    F = panel["F"]
    flipped = False
    if neg_cols:
        F[:, :, list(neg_cols)] *= -1.0
        flipped = True
    t0 = time.time()
    try:
        S = sc.train(panel, cfg, progress=progress)
    finally:
        if flipped:
            F[:, :, list(neg_cols)] *= -1.0
    return S, time.time() - t0


def probe(panel: dict) -> None:
    import lightgbm as lgb
    F, anchors = panel["F"], panel["anchors"]
    A, C, K = F.shape
    Y = sc.forward_excess(panel["m"], panel["mask"], sc.HORIZON)
    month_idx = _month_idx(anchors)
    j = month_idx[len(month_idx) // 2]
    lo = anchors[j] - pd.DateOffset(months=sc.TRAIN_MONTHS)
    tr_idx = np.where((anchors >= lo) & (np.arange(A) <= j - 1 - sc.EMBARGO))[0]
    Xtr = F[tr_idx].reshape(-1, K)
    ytr = Y[tr_idx].reshape(-1)
    ok = np.isfinite(ytr) & np.isfinite(Xtr).any(axis=1)
    Xtr, ytr = Xtr[ok], ytr[ok]
    params = dict(tr.PARAMS)
    params.update(n_jobs=sc.N_JOBS, verbose=-1, deterministic=True)
    t0 = time.time()
    lgb.train({**params, "seed": int(SEEDS[0])}, lgb.Dataset(Xtr, label=ytr),
              num_boost_round=sc.ROUNDS)
    dt = time.time() - t0
    print(f"[probe] 面板 {A} 锚点 × {C} 股 × {K} 因子；抽测重训点 #{j} ({anchors[j].date()}) "
          f"训练行 {len(ytr):,}")
    print(f"[probe] 单种子 {dt:.1f}s ⇒ 4 种子 ≈ {4 * dt:.1f}s/重训点 × {len(month_idx)} 个重训点 "
          f"≈ {4 * dt * len(month_idx) / 60:.0f} 分钟/臂 ⇒ 三臂 ≈ "
          f"{3 * 4 * dt * len(month_idx) / 60:.0f} 分钟")


def _month_idx(anchors) -> np.ndarray:
    """与 `scores.train` 逐字同源的"月末重训点"（该模块用 to_period('M') 取每月最后一个锚点）。"""
    seen, is_last = set(), np.zeros(len(anchors), dtype=bool)
    for i in range(len(anchors) - 1, -1, -1):
        p = anchors[i].to_period("M")
        if p not in seen:
            seen.add(p)
            is_last[i] = True
    return np.where(is_last)[0]


def diag_symmetry(panel: dict, seed_: int = 42) -> dict:
    """2×2 对称性诊断：**只训练一次**（最后一个重训点、单种子），看符号到底在哪一层改变结果。

    四个格子（模型 × 输入方向）：
      · `A(X)`   —— 基准：原样训练、原样推分
      · `A(-X)`  —— 已拟合模型喂取负输入（= "推分端改符号"，**不是**极性统一）
      · `B(X)`   —— 在取负面板上训练、用原样输入推分
      · `B(-X)`  —— 在取负面板上训练、用取负输入推分（**这才是**"训练前极性统一"）
    若树对符号不变，应见 `A(X) ≈ B(-X)`（同票同分）；`A(X) vs A(-X)` 的差异则是**推分端**
    改符号的破坏量（只作对照，不是极性统一的读数）。"""
    import lightgbm as lgb
    F, anchors, names = panel["F"], panel["anchors"], panel["names"]
    A, C, K = F.shape
    Y = sc.forward_excess(panel["m"], panel["mask"], sc.HORIZON)
    month_idx = _month_idx(anchors)
    j = int(month_idx[-1])                      # 最后一个重训点 = 产出"今天要执行"的那份打分
    lo = anchors[j] - pd.DateOffset(months=sc.TRAIN_MONTHS)
    tr_idx = np.where((anchors >= lo) & (np.arange(A) <= j - 1 - sc.EMBARGO))[0]

    def _mat() -> tuple:
        X = F[tr_idx].reshape(-1, K)
        y = Y[tr_idx].reshape(-1)
        ok = np.isfinite(y) & np.isfinite(X).any(axis=1)
        return X[ok], y[ok]

    XtrA, ytrA = _mat()          # ⚠️ 必须在取负**之前**拷贝(第一版写反: B 臂吃的是原样数据 ⇒ 假等价)
    F[:, :, :] *= -1.0
    XtrB, ytrB = _mat()
    F[:, :, :] *= -1.0           # 立即还原(取负精确可逆)
    # 注意 np.array_equal 默认把 NaN 判为不等, 而面板里到处是 NaN ⇒ 必须 equal_nan=True
    assert (np.array_equal(XtrB, -XtrA, equal_nan=True)
            and np.array_equal(ytrA, ytrB, equal_nan=True)), \
        "取负面板拷贝错位 ⇒ 诊断无效"
    tgt = np.arange(j, A)
    Xte = F[tgt].reshape(-1, K).reshape(len(tgt), C, K)
    mask = panel["mask"].to_numpy(dtype=bool)[tgt]
    params = dict(tr.PARAMS)
    params.update(n_jobs=sc.N_JOBS, verbose=-1, deterministic=True, seed=int(seed_))

    boB = lgb.train(params, lgb.Dataset(XtrB, label=ytrB), num_boost_round=sc.ROUNDS)
    boA = lgb.train(params, lgb.Dataset(XtrA, label=ytrA), num_boost_round=sc.ROUNDS)

    def score(bo, neg: bool) -> np.ndarray:
        acc = np.zeros((len(tgt), C), dtype=np.float64)
        for t in range(len(tgt)):
            X = -Xte[t] if neg else Xte[t]
            acc[t] = sc._rank_pct(bo.predict(X.astype(np.float32)).reshape(1, C))[0]
        return acc.astype(np.float32)

    grids = {"A_X": score(boA, False), "A_negX": score(boA, True),
             "B_X": score(boB, False), "B_negX": score(boB, True)}
    out = {"refit_anchor": str(anchors[j].date()), "target_anchors": [str(anchors[k].date())
                                                                     for k in tgt],
           "n_train_rows": len(ytrA), "seed": int(seed_),
           "assert_neg_panel_ok": True,
           "n_trees": {"A": int(boA.num_trees()), "B": int(boB.num_trees())},
           "imp_gain_top5": {
               "A": [names[i] for i in np.argsort(-boA.feature_importance("gain"))[:5]],
               "B": [names[i] for i in np.argsort(-boB.feature_importance("gain"))[:5]]},
           "cells": {}}
    base = grids["A_X"]
    for k, v in grids.items():
        out["cells"][k] = {"vs_A_X": compare(base, v, mask)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="只量单重训点成本后退出")
    ap.add_argument("--diag", action="store_true", help="2×2 对称性诊断（单重训点、单种子）")
    ap.add_argument("--post", action="store_true", help="不重训：从落盘分数重算对照与判决")
    ap.add_argument("--arms", default="A,B,C", help="要跑哪几臂(A=基准 B=全取负 C=按极性表取负)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    t_all = time.time()
    cfg = cfg_mod.load_config()
    panel = build_panel(cfg)
    mask = panel["mask"].to_numpy(dtype=bool)
    log.info("面板就绪 %.0fs", time.time() - t_all)

    if args.probe:
        probe(panel)
        return 0
    if args.diag:
        d = diag_symmetry(panel)
        (HERE / "F70_symmetry_2x2_result.json").write_text(
            json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(d, ensure_ascii=False, indent=1))
        return 0
    if args.post:
        post_from_scores(panel)
        return 0

    poles = {nm: _dead_zone_head(panel, k) for k, nm in enumerate(panel["names"])}
    neg_cols = [k for k, nm in enumerate(panel["names"]) if poles[nm]["sign"] < 0]
    log.info("极性表: 负极性 %d / 正极性 %d / 无法判定 %d", len(neg_cols),
             sum(1 for v in poles.values() if v["sign"] > 0),
             sum(1 for v in poles.values() if v["sign"] == 0))

    res = {"protocol": {
        "panel": "app.weekly.scores.build_panel(industry=snapshot) —— 生产 W5 die 同源",
        "trainer": "app.weekly.scores.train(生产 PARAMS/4 种子/50 轮/36 月窗/禁运 3 锚点)",
        "head_n": HEAD_N, "head_frac": HEAD_FRAC, "min_names": MIN_NAMES,
        "dead": list(DEAD), "active": list(ACTIVE),
        "polarity_rule": "死区头部超额符号(一次性、不搜索)",
        "verdict_rule": "top100 集合重叠=100% 且 逐票最大绝对差<1e-6 ⇒ 模型层对极性不变",
        "caveat": "极性表取自死区(样本内), 仅用于构造取负列集合; 本实验只解释不改模型"},
        "model": {"lightgbm_params": dict(tr.PARAMS), "kind": "回归树(无单调约束/无线性树)",
                  "why_no_op": "树只按特征序切分 ⇒ 取负只是镜像候选切分, 划分与增益逐一对应"},
        "polarity_table": poles, "neg_cols": [panel["names"][k] for k in neg_cols]}

    res["part1_archived_models"] = part1_archived_models()
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

    want = {x.strip().upper() for x in args.arms.split(",") if x.strip()}
    arms, res["arms_ic"], hx = {}, {}, {}
    for tag, cols in (("A_BASE", []), ("B_ALLNEG", list(range(len(panel["names"])))),
                      ("C_SIGN", neg_cols)):
        if tag[0] not in want:
            continue
        log.info("== 训练臂 %s（取负列数 %d）==", tag, len(cols))
        S, el = run_arm(panel, cfg, cols,
                        progress=lambda i, n, s, t=tag: log.info("  %s 已训练 %d/%d (%.0fs)",
                                                                t, i, n, s))
        arms[tag] = {"S": S, "elapsed_s": round(el, 1)}
        np.savez_compressed(HERE / f"F70_scores_{tag}.npz", scores=S,
                            anchors=panel["anchors"].strftime("%Y%m%d").to_numpy(),
                            cols=np.array(panel["cols"], dtype=object))
        res["arms_ic"][tag] = {"elapsed_s": round(el, 1), "ic": _ic_stats(S, panel)}
        hx[tag] = head_excess_series(S, panel)
        log.info("臂 %s 完成 %.0fs", tag, el)
        OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

    if "A_BASE" in arms:
        base = arms["A_BASE"]["S"]
        res["comparisons"] = {tag: compare(base, arms[tag]["S"], mask)
                              for tag in ("B_ALLNEG", "C_SIGN") if tag in arms}
        c = res["comparisons"]
        ok = bool(c) and all(v["top_overlap_min"] == 1.0 and v["max_abs_diff"] < 1e-6
                             for v in c.values())
        res["head_excess_stats"] = {
            tag: _head_stats(hx[tag], hx["A_BASE"], np.asarray(panel["anchors"]))
            for tag in hx}
        res["verdict"] = {
            "model_layer_polarity_is_noop": ok,
            "reading": ("模型层对因子极性不变 ⇒ 不需要为极性统一改造模型层" if ok else
                        "机制判断被推翻: 符号确实改变了重训输出, 需按实测差异重估"),
            "fidelity_check_ic_full_should_be_about": 0.0840,
            "note": "A_BASE 的 FULL IC 若明显偏离 0.0840 ⇒ 本脚本没复刻生产路径, 读数不可用"}
    res["elapsed_total_s"] = round(time.time() - t_all, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: res[k] for k in ("arms_ic", "comparisons", "verdict") if k in res},
                     ensure_ascii=False, indent=1))
    log.info("结果 → %s（总 %.0fs）", OUT, time.time() - t_all)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
