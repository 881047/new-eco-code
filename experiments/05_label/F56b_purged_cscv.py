"""F56b purged-CSCV：给 F56 的 PBO 补上"边界标签泄漏"这个已知口子（2026-09-19）

**为什么是它**：F56 的 CSCV 把 606 期切 S 块对半取 IS/OOS，但**没有 purge**——
锚点的 5 日标签窗若跨过 IS/OOS 边界，IS 与 OOS 的收益就共享同一段行情，
PBO 读数对泄漏不设防（López de Prado 2018 的标准批评）。F56 结论
（S=16 PBO=0.4339 < 0.5 ⇒ 未见明显选择过拟合）是否经得起 purge，
正是 plans/p0-validation-upgrade-20260919.md Phase B 冻结的问题。

**purge 半径为什么是 ±1 锚点（跑前推导，冻结）**：F56 的 20 臂全是周频锚点
（间距 ≈5 个交易日），`sc.forward_excess(horizon=5)` 的标签 = 锚点后第 1~5 个
交易日的收益。锚点 k 与 k+j 的标签窗重叠天数 = max(0, 5−j)——**只有紧邻
锚点（短周间距 4 时）共享 1 个交易日**，j≥2 即不相交。⇒ 跨 IS/OOS 边界的
标签泄漏最远到 ±1 锚点，purge=1 已把直接重叠清零；purge=2 作 belt-and-braces
敏感性（对照 purge=1 是否敏感）。

**协议（跑前冻结，照抄 plans/... Phase B）**
  臂集合      与 F56 **逐字同源**（直接 import F56 模块复用 ARMS/load_arm——比抄写
              更同源，臂清单不可能漂移）：主读数 12 个融合级臂（F4×2 + F47×10），
              敏感性 20 臂全量。
  收益矩阵    与 F56 同一条管线：生产 PIT 掩码 → 等权 top100 → 5 日 forward_excess
              → 换手 × 真实卖出费率 → 606 期 × N 臂 R 矩阵（逐期净超额）。
  复核锚      purge=0 在**同一份 R** 上重算 ⇒ 应逐位复现 F56 的存档值
              （S=8: 0.4000 / S=10: 0.3730 / S=16: 0.4339）；不合 ⇒ R 构造漂移，
              如实报（本身就是发现）。
  purge 内核  相邻块分属 IS/OOS 的边界，两侧各去 purge 个锚点（两侧都删，
              IS/OOS 都不用受污染期）；同块相邻（都在 IS 或都在 OOS）的边界不删。
  判据        |ΔPBO| = |PBO(purge=1) − PBO(purge=0)|，S=16 主读数：
              **< 0.05 ⇒ F56 结论在 purge 下稳定**（27 号 §七/§十二#2 的更正
              只需补一句"已补 purge 验证，结论不变"）；
              **≥ 0.05 ⇒ 不稳定**，F56 的 PBO 结论必须带"purge 后翻盘"限定。
  自检        F56 同款合成数据自检（同种子 20260913）：purge=0 应复现
              纯噪声≈0.5 / 真优势臂≈0；purge=1/2 同样必须跟着数据动。
  不做        不重训、不调参、不写任何 DB 表/生产产物；只读 npz + 行情缓存。

运行：``.venv\\Scripts\\python.exe -X utf8 experiments/05_label/F56b_purged_cscv.py``
"""
import importlib.util
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
OUT = HERE / "F56b_purged_cscv_result.json"
F56_RESULT = HERE / "F56_pbo_cscv_result.json"
log = logging.getLogger("f56b")

TOP_N = 100                  # F56 同源读数口径（等权 top100）
CSCV_S = (8, 10, 16)         # 与 F56 相同的块数网格；主读数 16
MAIN_S = 16
MAIN_PURGE = 1               # 冻结的 purge 半径（±1 锚点）
STABILITY_GATE = 0.05        # |ΔPBO| < 0.05 ⇒ 稳定（Phase B 冻结判据）

# ---- 直接 import F56：臂清单 / 载入器 / 自检种子全部同源，不可能漂移 ----------
_spec = importlib.util.spec_from_file_location("f56_ref", HERE / "F56_pbo_cscv.py")
f56 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(f56)


def cscv_pbo_purged(R: pd.DataFrame, S: int, purge: int) -> dict:
    """F56 的 CSCV 内核 + purge：只清"真跨界"的边界（相邻块分属 IS/OOS）。"""
    T, N = R.shape
    blocks = np.array_split(np.arange(T), S)
    combos = list(itertools.combinations(range(S), S // 2))
    lam, picks, ranks, dropped = [], [], [], []
    for is_blocks in combos:
        is_set = set(is_blocks)
        is_idx = np.concatenate([blocks[b] for b in is_blocks])
        oos_idx = np.concatenate([blocks[b] for b in range(S) if b not in is_set])
        if purge:
            drop = set()
            for b in range(S - 1):
                if (b in is_set) != (b + 1 in is_set):
                    drop.update(int(i) for i in blocks[b][-purge:])
                    drop.update(int(i) for i in blocks[b + 1][:purge])
            if drop:
                dropped.append(len(drop))
                is_idx = np.array([i for i in is_idx if int(i) not in drop])
                oos_idx = np.array([i for i in oos_idx if int(i) not in drop])
        is_sh = R.iloc[is_idx].mean() / R.iloc[is_idx].std(ddof=1)
        oos_sh = R.iloc[oos_idx].mean() / R.iloc[oos_idx].std(ddof=1)
        best = is_sh.idxmax()
        # 方向照抄 F56（r=1 最差、N=最好；ω=r/(N+1)，PBO=P(λ≤0)），由自检兜住
        r = int(oos_sh.rank(ascending=True)[best])
        omega = r / (N + 1)
        lam.append(np.log(omega / (1 - omega)))
        picks.append(best)
        ranks.append(r)
    lam = np.asarray(lam)
    return {"S": S, "purge": purge, "n_combos": len(combos),
            "pbo": round(float((lam <= 0).mean()), 4),
            "median_oos_rank_of_is_best": int(np.median(ranks)),
            "mean_oos_rank_of_is_best": round(float(np.mean(ranks)), 2),
            "is_best_pick_counts": pd.Series(picks).value_counts().head(8).to_dict(),
            "mean_dropped_per_combo": round(float(np.mean(dropped)), 1) if dropped else 0}


def selftest_cscv(n_iter: int = 40) -> dict:
    """F56 同款合成自检（同种子 20260913），对三个 purge 档各跑一遍。

    纯噪声 ⇒ PBO≈0.5（选冠军=抛硬币）；给第 0 臂恒定 +0.3σ ⇒ PBO≈0。
    purge=0 档同时是"与 F56 内核逐位同源"的复核锚。
    """
    rng0 = np.random.default_rng(20260913)
    out = {}
    for purge in (0, MAIN_PURGE, MAIN_PURGE + 1):
        rng = np.random.default_rng(20260913)      # 每个 purge 档同一份数据
        vals = {"noise_only": [], "one_true_arm": []}
        for _ in range(n_iter):
            X = rng.standard_normal((600, 20))
            vals["noise_only"].append(cscv_pbo_purged(pd.DataFrame(X), 8, purge)["pbo"])
            X2 = X + 0.0
            X2[:, 0] += 0.3
            vals["one_true_arm"].append(cscv_pbo_purged(pd.DataFrame(X2), 8, purge)["pbo"])
        out[f"purge={purge}"] = {
            "noise_only": round(float(np.mean(vals["noise_only"])), 4),
            "one_true_arm": round(float(np.mean(vals["one_true_arm"])), 4)}
        out[f"purge={purge}"]["pass"] = bool(
            out[f"purge={purge}"]["noise_only"] > 0.35
            and out[f"purge={purge}"]["one_true_arm"] < 0.15)
    return out


def build_R() -> tuple:
    """F56 同一条收益矩阵管线（main() 内联段的逐字同源拷贝）。"""
    arms, anchors_all, cols_ref = {}, None, None
    for name, path in f56.ARMS:
        S, a, cols = f56.load_arm(path)
        if cols_ref is None:
            cols_ref = cols
        elif cols != cols_ref:
            raise SystemExit(f"{name} 的 cols 与首个臂不一致 ⇒ 不能直接比较")
        arms[name] = pd.DataFrame(S, index=a, columns=cols)
        anchors_all = a if anchors_all is None else anchors_all.intersection(a)
        log.info("载入 %-22s %d 期", name, len(a))
    anchors = anchors_all.sort_values()

    m = fe.load_market(since=sc.SINCE)
    cfg = cfg_mod.load_config()
    pit = PITEngine(m=m, cfg=cfg)
    mask = pit.tradable_matrix(anchors, columns=cols_ref).fillna(False)
    Y = sc.forward_excess(m, mask, sc.HORIZON)
    sell_rate = fee_mod.effective_rate(25_000.0, "sell", "20260914")

    series = {}
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
    R = pd.DataFrame(series).dropna()
    return R, sell_rate


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    st = selftest_cscv()
    log.info("自检(同种子 20260913): %s | %.0fs",
             {k: v for k, v in st.items()}, time.time() - t0)

    R, sell_rate = build_R()
    armsets = {"primary": list(f56.PRIMARY), "all": list(f56.ALL_ARMS),
               "excluded": dict(f56.EXCLUDED)}
    log.info("收益矩阵 %s（%s ~ %s）| 卖出费率 %.4f%% | %.0fs",
             R.shape, R.index[0].date(), R.index[-1].date(),
             sell_rate * 100, time.time() - t0)
    R_primary = R[armsets["primary"]]

    res = {"selftest": st,
           "protocol": {"arms_primary": armsets["primary"], "arms_all": armsets["all"],
                        "excluded": armsets["excluded"],
                        "top_n": TOP_N, "cscv_S": list(CSCV_S), "purge_main": MAIN_PURGE,
                        "purge_rationale": "周锚点间距≈5 交易日, 5 日标签窗重叠最远 ±1 锚点"
                                           "（短周间距 4 时共享 1 日）⇒ purge=1 清零直接重叠,"
                                           " purge=2 敏感性",
                        "stability_gate": STABILITY_GATE,
                        "decision_rule": "|ΔPBO(S=16, purge=1)| < 0.05 ⇒ F56 结论在 purge 下稳定"},
           "n_periods": int(len(R)), "window": [str(R.index[0].date()), str(R.index[-1].date())],
           "corr_mean": round(float(R_primary.corr().to_numpy()[
               np.triu_indices(R_primary.shape[1], 1)].mean()), 4)}

    # 复核锚：F56 存档值 vs 同一 R 上的 purge=0 重算
    stored = (json.loads(F56_RESULT.read_text(encoding="utf-8"))["pbo"]
              if F56_RESULT.exists() else {})
    repl = {}
    for s in CSCV_S:
        mine = cscv_pbo_purged(R_primary, s, 0)["pbo"]
        repl[f"S={s}"] = {"f56_stored": stored.get(f"S={s}", {}).get("pbo"),
                          "purge0_recomputed": mine,
                          "match": stored.get(f"S={s}", {}).get("pbo") == mine}
    res["replication_vs_f56"] = repl
    log.info("复核锚(purge=0 vs F56 存档): %s",
             {k: v["match"] for k, v in repl.items()})

    grid = {}
    for s in CSCV_S:
        for purge in (0, MAIN_PURGE, MAIN_PURGE + 1):
            grid[f"S={s}|purge={purge}"] = cscv_pbo_purged(R_primary, s, purge)
    res["pbo_grid_primary"] = grid
    for s in CSCV_S:
        row = {f"purge={p}": grid[f"S={s}|purge={p}"]["pbo"] for p in (0, MAIN_PURGE, MAIN_PURGE + 1)}
        log.info("主读数 S=%-2d: %s", s, row)

    d_main = abs(grid[f"S={MAIN_S}|purge={MAIN_PURGE}"]["pbo"]
                 - grid[f"S={MAIN_S}|purge=0"]["pbo"])
    d_p2 = abs(grid[f"S={MAIN_S}|purge={MAIN_PURGE + 1}"]["pbo"]
               - grid[f"S={MAIN_S}|purge=0"]["pbo"])
    stable = bool(d_main < STABILITY_GATE)
    res["delta_pbo_main"] = {"S": MAIN_S, "purge": MAIN_PURGE,
                             "abs_delta": round(d_main, 4),
                             "abs_delta_purge2": round(d_p2, 4),
                             "gate": STABILITY_GATE, "stable": stable}
    res["verdict"] = ("F56 结论在 purge 下稳定（|ΔPBO|=%.4f < %.2f）⇒ 27号 §七/§十二#2 "
                      "更正只需补'已补 purge 验证，结论不变'" % (d_main, STABILITY_GATE)
                      if stable else
                      "purge 后 |ΔPBO|=%.4f ≥ %.2f ⇒ F56 的 PBO 结论不稳定，"
                      "27号 §七/§十二#2 更正必须带限定" % (d_main, STABILITY_GATE))
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    log.info("判决: %s", res["verdict"])
    log.info("结果 → %s | %.0fs", OUT.name, time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
