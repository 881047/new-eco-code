"""F56 PBO（CSCV）：多重比较下"最佳构型"到底有几分可信（2026-09-13）

**为什么是它**：F38/F48 已经把 **DSR** 结清（N=7 → 0.876/0.850；N=25 → 0.597/0.544，门槛 0.95
⇒ 不通过），F48 同时证明**卡点是多重比较 N，不是样本长度**（MinTRL 只要 129~136 期，现有 491）。
缺口只剩一个：**N 到底该取多少、以及"我们挑出来的那个构型"在样本外还剩多少** ——
这正是 PBO（Probability of Backtest Overfitting, Bailey et al. 2014 的 CSCV 实现）回答的问题。
KB 14 把它列为未结项（"需 T×N 收益矩阵、~25 臂、贵、需预注册"）；2026-09-13 的
`load_market` 内容指纹缓存把面板加载从 326s 降到 3s，成本前提已变，故现在做。

**预注册协议（跑数前冻结，写死在本文件里，不许事后改）**

  用途          描述性读数，不改策略、不选构型、不上线；只回答"选出来的那个有多可信"
  臂集合        主读数 = **同层的 12 个融合级候选**（线上信号就是 `2×W5+1×pit` 的融合分）：
                  F4 ×2   = 生产构型的两套种子臂（seedA/seedB；与线上存档 rank-corr≈0.96，同构型）
                  F47 ×10 = 周频超参网格 C1~C5 × 两套种子（同样是融合级）
                敏感性 = 再加 F29 ×8（**单 die 组件级**尾部加权臂）一起算 —— 混层会改变 IS 冠军
                  的分布，故只作敏感性，不作主读数（写进结果文件）
  排除           F32_SA_PERM（置换对照＝零模型，不是候选构型）
                 F33_*_M21（月频锚点 153 期，T 网格与周频臂不同，混进来会比较不同频率）
  收益口径       与 F55 逐字同源：生产 PIT 掩码 → 等权 top100 → 5 日 forward_excess →
                 换手 × 真实卖出费率（`fee_mod.effective_rate(25000,'sell','20260914')`）
  对齐          各臂锚点数组先转 Timestamp，取**全部 20 臂的共同锚点**（F46 的教训：不同臂的
                共同窗口不许用成交日、也不许各臂各算）
  统计量         逐期净超额序列的 Sharpe（mean/sd，ddof=1，不年化）—— 选构型的人看的就是它
  CSCV          S=16 个等长块 → 取全部 C(16,8)=12,870 个组合为 IS，其余为 OOS
                IS 选 Sharpe 最大者；OOS 按 Sharpe 排名 → 名次 r（1=最差，N=20）
                ω = r/(N+1)（越小越差）→ λ = ln(ω/(1-ω)) → **PBO = P(λ ≤ 0)**
  敏感性        同时报 S=8 / S=10 的 PBO（块数越多组合越多、单块越短，看结论是否随切法漂移）
  判据（描述性）PBO < 0.5 ⇒ 未见明显选择过拟合；**≥ 0.5 ⇒ "最佳构型"大概率是选择噪声，
                禁止据此上线**（与 F38 的 DSR 不通过并列为同一结论的两个角度）
  不做         不重训、不调参、不看未实现标签、不写生产产物（只写本目录结果文件）

运行：``D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F56_pbo_cscv.py``（约 40 秒；行情面板命中缓存）
"""
import itertools
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfg_mod                                   # noqa: E402
from app.factors import factor_engine as fe                         # noqa: E402
from app.pit.engine import PITEngine                                # noqa: E402
from app.portfolio import fees as fee_mod                           # noqa: E402
from app.weekly import scores as sc                                 # noqa: E402

HERE = Path(__file__).resolve().parent
LAB = BASE / "experiments" / "05_label"
OUT = HERE / "F56_pbo_cscv_result.json"
log = logging.getLogger("f56")

TOP_N = 100
CSCV_S = (8, 10, 16)          # 敏感性用的块数；主读数取 16
MAIN_S = 16
PER_YEAR = 52.0

ARMS = []
for _f in ("F4_scores_seedA", "F4_scores_seedB"):
    ARMS.append((_f, LAB / f"{_f}.npz"))
for _set in ("SA", "SB"):
    for _n in ("A0_base", "A1_abs", "A2_pos", "A3_abs2"):
        ARMS.append((f"F29_{_set}_{_n}", LAB / f"F29_scores_{_set}_{_n}.npz"))
for _set in ("SA", "SB"):
    for _n in ("C1_small", "C2_mid", "C3_strong", "C4_capacity", "C5_qlib"):
        ARMS.append((f"F47_{_set}_{_n}", LAB / f"F47_scores_{_set}_{_n}.npz"))

# 排除项（写进结果，便于复核为什么不是 24 个）
EXCLUDED = {"F32_scores_SA_PERM.npz": "置换对照臂=零模型，不是候选构型",
            "F33_scores_SA_M21.npz": "月频锚点(153 期)，T 网格与周频臂不同",
            "F33_scores_SB_M21.npz": "月频锚点(153 期)，T 网格与周频臂不同"}
PRIMARY = [n for n, _ in ARMS if n.startswith(("F4_", "F47_"))]     # 12 个融合级臂
ALL_ARMS = [n for n, _ in ARMS]                                     # +8 个单 die 组件臂


def load_arm(path: Path) -> tuple:
    with np.load(path, allow_pickle=True) as z:
        S = np.asarray(z["scores"], dtype=float)
        a = pd.to_datetime([str(d) for d in z["anchors"]])
        cols = [str(c) for c in z["cols"]]
    return S, pd.DatetimeIndex(a), cols


def cscv_pbo(R: pd.DataFrame, S: int) -> dict:
    """Bailey et al. 的 CSCV：S 个等长块，取一半为 IS、其余为 OOS。"""
    T, N = R.shape
    blocks = np.array_split(np.arange(T), S)
    combos = list(itertools.combinations(range(S), S // 2))
    lam, picks, ranks = [], [], []
    for is_blocks in combos:
        is_idx = np.concatenate([blocks[b] for b in is_blocks])
        oos_idx = np.concatenate([blocks[b] for b in range(S) if b not in is_blocks])
        is_sh = R.iloc[is_idx].mean() / R.iloc[is_idx].std(ddof=1)
        oos_sh = R.iloc[oos_idx].mean() / R.iloc[oos_idx].std(ddof=1)
        best = is_sh.idxmax()
        # Bailey et al. 的 r 是 **1 = 最差、N = 最好**（ω=r/(N+1)，λ=ln(ω/(1-ω))，
        # PBO=P(λ≤0)=P(样本外落在后半区)）。方向写反会把结论整个翻过来 ——
        # 这里由 `selftest_cscv()` 兜住（真优势臂必须给 PBO≈0，纯噪声给≈0.5）。
        r = int(oos_sh.rank(ascending=True)[best])           # 1 = 最差
        omega = r / (N + 1)
        lam.append(np.log(omega / (1 - omega)))
        picks.append(best)
        ranks.append(r)
    lam = np.asarray(lam)
    return {"S": S, "n_combos": len(combos), "pbo": round(float((lam <= 0).mean()), 4),
            "median_oos_rank_of_is_best": int(np.median(ranks)),
            "mean_oos_rank_of_is_best": round(float(np.mean(ranks)), 2),
            "is_best_pick_counts": pd.Series(picks).value_counts().head(8).to_dict()}


def selftest_cscv(n_iter: int = 40) -> dict:
    """合成数据自检：证明这个指标会**跟着数据动**，而不是一个常数。

    ① 20 个**纯噪声**臂（iid 正态，无任何真实优势）⇒ PBO 应当 ≈ **0.5**（选冠军＝抛硬币）；
    ② 同一批臂里给第 0 臂加一个**恒定** +0.3σ 优势 ⇒ PBO 应当 ≈ **0**（真优势能被选出来）。
    两组都用 S=8（70 组合）重复 n_iter 次取均值 —— 单次噪声大，均值才有判别力。
    """
    rng = np.random.default_rng(20260913)
    T, N, S = 600, 20, 8
    out = {"S": S, "n_iter": n_iter, "T": T, "N": N}
    for tag, edge in (("noise_only", 0.0), ("one_true_arm", 0.3)):
        vals = []
        for _ in range(n_iter):
            X = rng.standard_normal((T, N))
            if edge:
                X[:, 0] += edge
            vals.append(cscv_pbo(pd.DataFrame(X), S)["pbo"])
        out[tag] = round(float(np.mean(vals)), 4)
    out["pass"] = bool(out["noise_only"] > 0.35 and out["one_true_arm"] < 0.15)
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    st = selftest_cscv()
    log.info("CSCV 自检: 纯噪声 PBO=%.3f(应≈0.5) | 真优势臂 PBO=%.3f(应≈0) | 通过=%s",
             st["noise_only"], st["one_true_arm"], st["pass"])

    arms, anchors_all, cols_ref = {}, None, None
    for name, path in ARMS:
        S, a, cols = load_arm(path)
        if cols_ref is None:
            cols_ref = cols
        elif cols != cols_ref:
            raise SystemExit(f"{name} 的 cols 与首个臂不一致 ⇒ 不能直接比较")
        arms[name] = pd.DataFrame(S, index=a, columns=cols)
        anchors_all = a if anchors_all is None else anchors_all.intersection(a)
        log.info("载入 %-22s %d 期", name, len(a))
    anchors = anchors_all.sort_values()
    log.info("共同锚点 %d 期（%s ~ %s）| %.0fs", len(anchors),
             anchors[0].date(), anchors[-1].date(), time.time() - t0)

    m = fe.load_market(since=sc.SINCE)
    pit = PITEngine(m=m, cfg=cfg)
    mask = pit.tradable_matrix(anchors, columns=cols_ref).fillna(False)
    Y = sc.forward_excess(m, mask, sc.HORIZON)
    sell_rate = fee_mod.effective_rate(25_000.0, "sell", "20260914")
    log.info("面板就绪 | 掩码非空均值 %.3f | 卖出费率 %.4f%% | %.0fs",
             float(mask.to_numpy().mean()), sell_rate * 100, time.time() - t0)

    series, per_arm = {}, {}
    for name in arms:
        df = arms[name].reindex(index=anchors, columns=cols_ref)
        S = df.to_numpy(dtype=float)
        rets, prev = [], None
        for k in range(len(anchors)):
            y = Y[k]
            s = S[k]
            ok = mask.iloc[k].to_numpy() & np.isfinite(s) & np.isfinite(y)
            if ok.sum() < 200:
                rets.append(np.nan)
                continue
            idx = np.argsort(-np.where(ok, s, -np.inf))[:TOP_N]
            w = np.full(len(idx), 1.0 / len(idx))
            gross = float(np.dot(w, y[idx].astype(float)))
            cost = 0.0
            cur = dict(zip(idx, w))
            if prev:
                names = set(cur) | set(prev)
                cost = sum(abs(cur.get(i, 0.0) - prev.get(i, 0.0)) for i in names) * sell_rate
            prev = cur
            rets.append(gross - cost)
        series[name] = pd.Series(rets, index=anchors)
        r = series[name].dropna()
        per_arm[name] = {"n": int(len(r)), "mean_pp": round(100 * float(r.mean()), 3),
                         "sd_pp": round(100 * float(r.std(ddof=1)), 3),
                         "sharpe": round(float(r.mean() / r.std(ddof=1)), 3),
                         "excess_pp": round(100 * float((1 + r).prod() ** (PER_YEAR / len(r)) - 1), 2)}
    R = pd.DataFrame(series).dropna()
    log.info("收益矩阵 %s | %.0fs", R.shape, time.time() - t0)

    R_primary = R[PRIMARY]
    res = {"selftest": st,
           "protocol": {"arms_primary": PRIMARY, "n_arms_primary": len(PRIMARY),
                        "arms_sensitivity": ALL_ARMS, "excluded": EXCLUDED,
                        "note": "主读数只用**同层（融合级）**候选；F29 是单 die 组件臂，混层只作敏感性",
                        "top_n": TOP_N, "sell_rate_bp": round(sell_rate * 1e4, 3),
                        "metric": "sharpe(net excess, equal weight top100)",
                        "cscv_S_main": MAIN_S, "cscv_S_sensitivity": list(CSCV_S),
                        "decision_rule": "PBO<0.5 未见明显选择过拟合; >=0.5 禁止据'最佳构型'上线"},
           "n_periods": int(len(R)), "window": [str(anchors[0].date()), str(anchors[-1].date())],
           "per_arm": per_arm,
           "pbo": {f"S={s}": cscv_pbo(R_primary, s) for s in CSCV_S},
           "pbo_sensitivity_mixed_20arms": {f"S={s}": cscv_pbo(R, s) for s in CSCV_S},
           "corr_mean": round(float(R.corr().to_numpy()[np.triu_indices(len(R.columns), 1)].mean()), 4)}
    res["verdict"] = ("未见明显选择过拟合" if res["pbo"][f"S={MAIN_S}"]["pbo"] < 0.5
                      else "最佳构型大概率是选择噪声")
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    print(f"\n共同窗口 {res['window'][0]} ~ {res['window'][1]} | {len(R)} 期 | "
          f"主读数 {len(PRIMARY)} 臂（融合级）/ 敏感性 {len(R.columns)} 臂")
    for s in CSCV_S:
        p = res["pbo"][f"S={s}"]
        q = res["pbo_sensitivity_mixed_20arms"][f"S={s}"]
        print(f"  PBO(S={s:>2}, {p['n_combos']:>6} 组合) = {p['pbo']:.4f}（同层 {len(PRIMARY)} 臂）"
              f" | 混层 {len(R.columns)} 臂 = {q['pbo']:.4f} | "
              f"IS 冠军的 OOS 名次中位数 {p['median_oos_rank_of_is_best']}/{len(PRIMARY)}")
    sr = pd.Series({k: v["sharpe"] for k, v in per_arm.items()}).sort_values(ascending=False)
    print("\n全窗口 Sharpe 前 5 臂:", ", ".join(f"{k}={v:.3f}" for k, v in sr.head(5).items()))
    print("生产臂(F4):", ", ".join(f"{k}={per_arm[k]['sharpe']:.3f}"
                                   for k in ("F4_scores_seedA", "F4_scores_seedB")))
    print(f"臂间平均相关 {res['corr_mean']:.3f} ⇒ 结论：{res['verdict']}")
    print(f"结果 → {OUT.name} | {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
