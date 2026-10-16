"""F29 · 尾部加权训练 —— 目标函数层面唯一没试过的方向（预注册）

## 为什么是这里

F22/F24/F26/F27/F28 之后的全部事实是：
  · **8 个数据族全部测否**（F3/F26/F27）⇒ 换数据没用；
  · 近端留出集超额只有 **+7.3~8.6pp 且 t≈0.8**（F28）⇒ 现有信号近端不显著；
  · 毛超额的 ~53% 是**风格暴露**（F24）⇒ 现有 die 主要在做量价/规模/流动性的排序；
  · "换模型族"已试过 10+ 轮全败（§7 证伪登记）。

还剩一个**从没测过**的层面：**训练目标本身**。现有 die 用 `rmse` 在**全截面**上训练，
可是组合只持有 `top100 / ~3700 ≈ 2.7%` —— 损失函数在优化"平均意义上的好"，
而组合赚的是"尾部的好"。这是经典的**目标错配**，且与本项目已证伪的那些方向都不同。

## 臂（同一面板 / 标签 horizon=5 / 月末重训 / 禁运 3 / 与生产同参数）

| 臂 | 样本权重 |
|---|---|
| `A0 base` | 无（= **生产 W5 组件**的现状，逐位同路径） |
| `A1 abs`  | `w = 1 + 4·1{|y| ≥ 训练集 80 分位}`（双尾） |
| `A2 pos`  | `w = 1 + 4·1{ y  ≥ 训练集 80 分位}`（只放大正尾，贴合只做多） |
| `A3 abs2` | 同 A1 但 `1+2`（**剂量臂**：A1 把 56% 的损失质量压到 20% 样本上，若 A1 失败需要知道是"方向错"还是"用力过猛"） |

种子：`SA=(42,7,123,2024)`（生产同款）、`SB=(43,8,124,2025)`（独立确认）。
⇒ 4 臂 × 2 套种子 = 8 次训练（逐次落盘，带**配置指纹**，可断点续训）。

**两阶段（都在跑数前写死）**：
  · 阶段 1（单 die）：上表 4 臂直接比 —— 回答"尾部加权对 W5 这个组件有没有用"；
  · 阶段 2（**融合确认**）：`A0` 只是生产的**组件**，线上是 `2×W5 + 1×pit`（F22）。
    所以任何通过阶段 1 的臂，必须在**同一窗口**上再过一遍
    `2×A_tail + 1×pit` vs `2×A0 + 1×pit` 的同一套三条判据 —— 否则"单 die 变好"
    不蕴含"线上变好"（F22 实测过融合不单调）。

## 预注册判据（只看**组合两轴**，不看 IC 单轴 —— 本项目 6 次"IC 变好组合变差"的教训）

采纳 = **两套种子上同时**：
  ① 组合超额 ≥ 同套种子 `A0` + **0.5pp**（全史 491 期同窗口）；
  ② MDD 不劣于同套种子 `A0` 的 **1.10 倍**；
  ③ IC ≥ 同套种子 `A0` 的 IC。
多个臂满足 ⇒ 取两套种子超额均值最高者（平手取臂更简单者）。
留出集（2024-01~2026-08）与死区读数一并报出，但**不作采纳判据**（F28 已一次性开封）。

## 必须如实声明的口径边界

- 阈值取的是**跨期合并**的 `|y|` 分位（`y` 已按截面 demean），**不是**每个截面内的分位 ⇒
  高波动周会成片入选、低波动周的极端票反而进不去。结论只能声称"跨期绝对 |y| 尾部加权…",
  不能外推成"尾部加权"的一般命题；
- LightGBM 的 `lambda_l2=1.0` 是绝对值、**不随权重放大** ⇒ 尾部主导的叶子收缩相对弱 5 倍；
  A3 剂量臂就是为此留的诊断；
- 两套种子只换随机数、共用同一段历史 ⇒ 控制的是**种子噪声**，不是数据噪声；
  判据①是**点估计**门槛（+0.5pp），因此另报"差值配对 HAC t"作为诊断（不升为判据）。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F29_tail_weighted_training.py
"""
import hashlib
import importlib.util
import inspect
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


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


f1 = _load("f1", HERE / "F1_weekly_ic_decay.py")
f1b = _load("f1b", HERE / "F1b_weekly_open_overlap.py")
f17 = _load("f17", HERE / "F17_die_registry_kgd.py")
f22 = _load("f22", HERE / "F22_pair_final_selection.py")

from app import config as cfg_mod             # noqa: E402
from app import db                            # noqa: E402
from app.factors import factor_engine as fe   # noqa: E402
from app.weekly import scores as wsc          # noqa: E402

log = logging.getLogger("f29")
OUT = HERE / "F29_tail_weighted_training_result.json"
CAPITAL, TOP_N, BAND, HYST = 1_000_000.0, 100, 8.0, 0.2
MIN_NAMES = 200
PER_YEAR = 52.0
SETS = {"SA": (42, 7, 123, 2024), "SB": (43, 8, 124, 2025)}
ARMS = {"A0_base": {"tail_weight": 0.0},
        "A1_abs": {"tail_weight": 4.0, "tail_side": "abs", "tail_quantile": 0.80},
        "A2_pos": {"tail_weight": 4.0, "tail_side": "pos", "tail_quantile": 0.80},
        "A3_abs2": {"tail_weight": 2.0, "tail_side": "abs", "tail_quantile": 0.80}}
TAIL_ARMS = ("A1_abs", "A2_pos", "A3_abs2")
CONFIG_VERSION = "v2"          # 训练代码路径变了要手动 +1(常量/函数源码已在指纹里)
FULL = ("2010-01-01", "2030-12-31")
OOS = ("2024-01-01", "2026-08-31")
DEAD = ("2020-09-01", "2023-12-31")


def _zone(df, lo, hi):
    s = df[(df["date"] >= lo) & (df["date"] <= hi)].dropna(subset=["ret", "bench_ret"])
    if len(s) < 5:
        return {"n": int(len(s))}
    a, b = f1b.ann(s["ret"], PER_YEAR), f1b.ann(s["bench_ret"], PER_YEAR)
    nav = (1 + s["ret"]).cumprod()
    return {"n": int(len(s)), "ann": round(float(a), 4),
            "excess_pp": round(100 * (a - b), 2),
            "mdd": round(float((nav / nav.cummax() - 1).min()), 4),
            "turnover": round(float(s["turnover"].mean()), 3)}


def _run_series(m, wk, mask, pit, cfg, pos_of, mask_np, op_adj, Sdf):
    """跑一遍组合并接上**决策日票池**基准 —— 与 F1b/F22 同一口径(唯一实现见其 main)。"""
    df = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, {**cfg, "capital": CAPITAL}, pit, 0,
                                   score_matrix=Sdf, top_n=TOP_N, rank_band=BAND, hyst=HYST)
    if df is None:
        return None
    bench, prev_exec, prev_ap = [], None, None
    for _, r in df.iterrows():
        e = pos_of[r["date"]]
        if prev_exec is None:
            bench.append(np.nan)
        else:
            good = (mask_np[int(prev_ap)] & np.isfinite(op_adj[prev_exec])
                    & np.isfinite(op_adj[e]) & (op_adj[prev_exec] > 0))
            bench.append(float(np.mean(op_adj[e][good] / op_adj[prev_exec][good] - 1))
                         if good.sum() >= MIN_NAMES else np.nan)
        prev_exec, prev_ap = e, int(r["anchor_pos"])
    df["bench_ret"] = bench
    return df[["date", "ret", "bench_ret", "turnover"]].copy()


def _aligned_stats(s: pd.DataFrame, common: list) -> dict:
    s2 = s.copy()
    s2["date"] = pd.to_datetime(s2["date"])
    s2 = s2[s2["date"].isin(set(common))].dropna(subset=["ret", "bench_ret"])
    if len(s2) < 5:
        return {"n": int(len(s2))}
    a, b = f1b.ann(s2["ret"], PER_YEAR), f1b.ann(s2["bench_ret"], PER_YEAR)
    nav = (1 + s2["ret"]).cumprod()
    return {"n": int(len(s2)), "excess_pp": round(100 * (a - b), 2),
            "mdd": round(float((nav / nav.cummax() - 1).min()), 4),
            "turnover": round(float(s2["turnover"].mean()), 3)}


def _fingerprint(panel, set_name, arm, kw) -> str:
    """缓存指纹 —— 少了它, 改配置重跑会**静默复用旧分数**而结果 JSON 记的是新配置。"""
    from app.models.trainer import PARAMS
    payload = {
        "ver": CONFIG_VERSION, "arm": arm, "kw": kw, "seeds": list(SETS[set_name]),
        "horizon": wsc.HORIZON, "rounds": wsc.ROUNDS, "months": wsc.TRAIN_MONTHS,
        "embargo": wsc.EMBARGO, "n_jobs": wsc.N_JOBS, "params": {k: str(v) for k, v in PARAMS.items()},
        "train_src": hashlib.sha256(inspect.getsource(wsc.train).encode()).hexdigest()[:16],
        "label_src": hashlib.sha256(inspect.getsource(wsc.forward_excess).encode()).hexdigest()[:16],
        "panel": [str(panel["anchors"][0]), str(panel["anchors"][-1]), len(panel["anchors"]),
                  len(panel["cols"]), hashlib.sha256(
                      "\x00".join(map(str, panel["cols"])).encode()).hexdigest()[:16]],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def train_arm(panel, cfg, set_name, arm, kw):
    """训练一个臂并落盘（幂等: 指纹一致才复用; 不一致 = 重训, 绝不静默用旧分数）。"""
    p = HERE / f"F29_scores_{set_name}_{arm}.npz"
    fp = _fingerprint(panel, set_name, arm, kw)
    if p.exists():
        z = np.load(p, allow_pickle=True)
        same = str(z["fp"]) == fp if "fp" in z else False
        anchors_ok = (len(z["anchors"]) == len(panel["anchors"])
                      and str(z["anchors"][0])[:10] == str(panel["anchors"][0])[:10]
                      and str(z["anchors"][-1])[:10] == str(panel["anchors"][-1])[:10])
        cols_ok = list(map(str, z["cols"][:5])) == list(map(str, panel["cols"][:5]))
        if same and anchors_ok and cols_ok:
            log.info("  [%s|%s] 复用已训练存档 (指纹 %s)", set_name, arm, fp)
            return np.asarray(z["scores"], dtype=np.float32), p
        log.warning("  [%s|%s] 存档指纹/锚点不一致 ⇒ 重训(旧 fp=%s, 新 fp=%s)",
                    set_name, arm, z["fp"] if "fp" in z else "无", fp)
    t0 = time.time()
    S = wsc.train(panel, cfg, seeds=SETS[set_name],
                  progress=lambda i, n, s: log.info("  [%s|%s] %d/%d (%.0fs)",
                                                    set_name, arm, i, n, s), **kw)
    np.savez_compressed(p, scores=S, anchors=panel["anchors"].to_numpy(), cols=panel["cols"],
                        fp=fp, names=panel["names"], meta=json.dumps(kw))
    log.info("  [%s|%s] 训练完成 %.0fs → %s", set_name, arm, time.time() - t0, p.name)
    return S, p


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    # 实验口径用一个比生产高的线程数(生产 N_JOBS=8 是给 Web/日链留余量; 本机 32 逻辑核)
    wsc.N_JOBS = 24                       # 只影响本次进程, 不写回生产常量
    m = fe.load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    panel = wsc.build_panel(m, wk, cfg)
    cols, mask, pit = panel["cols"], panel["mask"], panel["pit"]
    log.info("面板 %s | 锚点 %d | 股票 %d", panel["F"].shape, len(wk), len(cols))

    lab = f1b.forward_excess_open(m, mask, 5)
    Yr = f1._rank(lab)
    okv = lab.notna().to_numpy()
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    mask_np = mask.fillna(False).to_numpy()
    conn = db.get_conn()
    try:
        PIT = f17.table_scores(conn, "model_score_die_pit", wk, cols)   # 阶段 2 要用(生产第二路)
    finally:
        conn.close()

    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "config_version": CONFIG_VERSION,
           "sets": {k: list(v) for k, v in SETS.items()}, "arms": ARMS,
           "windows": {"full": FULL, "oos": OOS, "dead": DEAD},
           "results": {}, "stage2_fusion": {}, "verdict": {}, "secs": None}
    series = {}
    scores = {}
    for set_name in SETS:
        for arm, kw in ARMS.items():
            S, p = train_arm(panel, cfg, set_name, arm, kw)
            Sdf = pd.DataFrame(S, index=wk, columns=cols)
            scores[(set_name, arm)] = Sdf
            Xr = f1._rank(Sdf)
            ic = pd.Series(f1.row_ic(Xr, Yr, okv & np.isfinite(Xr)), index=wk).dropna()
            df = _run_series(m, wk, mask, pit, cfg, pos_of, mask_np, op_adj, Sdf)
            if df is None:
                continue
            series[(set_name, arm)] = df
            res["results"].setdefault(set_name, {})[arm] = {
                "ic": round(float(ic.mean()), 4) if len(ic) else None,
                "ic_t": round(float(f1b.hac_t(ic.to_numpy(), 2)), 2) if len(ic) > 10 else None,
                "full": _zone(df, *FULL), "OOS": _zone(df, *OOS), "DEAD": _zone(df, *DEAD),
                "ic_n": int(len(ic)),
                "scores_file": p.name}
            r = res["results"][set_name][arm]
            log.info("[%s|%s] IC %s (t=%s) | 全史 %+.2fpp (MDD %.1f%%) | 留出集 %+.2fpp | 死区 %+.2fpp",
                     set_name, arm, r["ic"], r["ic_t"], r["full"]["excess_pp"],
                     100 * r["full"]["mdd"], r["OOS"]["excess_pp"], r["DEAD"]["excess_pp"])

    # ---- 同窗口对齐后再比较(所有臂都要求同一组日期) ----
    common = None
    for s in series.values():
        ds = set(pd.to_datetime(s.loc[s["ret"].notna() & s["bench_ret"].notna(), "date"]))
        common = ds if common is None else (common & ds)
    common = sorted(common or [])
    log.info("同窗口对齐: %d 期 (%s ~ %s)", len(common),
             common[0].date() if common else "-", common[-1].date() if common else "-")
    rows = {}
    for key, s in series.items():
        set_name, arm = key
        s2 = s.copy()
        s2["date"] = pd.to_datetime(s2["date"])
        s2 = s2[s2["date"].isin(set(common))].dropna(subset=["ret", "bench_ret"])
        a, b = f1b.ann(s2["ret"], PER_YEAR), f1b.ann(s2["bench_ret"], PER_YEAR)
        nav = (1 + s2["ret"]).cumprod()
        # 与同套种子的 A0 做**配对差值** HAC t(诊断, 不是判据): 判据①只有点估计门槛
        base = series[(set_name, "A0_base")]
        d = (s2.set_index("date")["ret"] - base.set_index("date")["ret"].reindex(s2["date"].to_numpy())
             ).dropna()
        t_diff = f1b.hac_t(d.to_numpy(), 2) if len(d) > 10 else float("nan")
        # IC 也限制在同一窗口(避免"IC 在全锚点上、超额在对齐窗口上"的口径错位)
        ic_s = pd.Series(f1.row_ic(f1._rank(scores[key]), Yr, okv & np.isfinite(f1._rank(scores[key]))),
                         index=wk).dropna()
        ic_win = pd.Series(ic_s[ic_s.index.isin(common)])
        rows[f"{key[0]}|{key[1]}"] = {
            "n": int(len(s2)), "excess_pp": round(100 * (a - b), 2),
            "mdd": round(float((nav / nav.cummax() - 1).min()), 4),
            "turnover": round(float(s2["turnover"].mean()), 3),
            "ic_common": round(float(ic_win.mean()), 4) if len(ic_win) else None,
            "t_diff_vs_A0": None if not np.isfinite(t_diff) else round(float(t_diff), 2)}

    # ---- 预注册判据 ----
    passed = []
    for arm in TAIL_ARMS:
        ok = True
        for set_name in SETS:
            base = rows.get(f"{set_name}|A0_base")
            cur = rows.get(f"{set_name}|{arm}")
            ic0 = (base or {}).get("ic_common") or 0
            ic1 = (cur or {}).get("ic_common") or 0
            if cur is None or base is None:
                ok = False
                break
            if not (cur["excess_pp"] >= base["excess_pp"] + 0.5
                    and cur["mdd"] >= base["mdd"] * 1.10 and ic1 >= ic0):
                ok = False
        if ok:
            passed.append((arm, np.mean([rows[f"{s}|{arm}"]["excess_pp"] for s in SETS])))
    passed.sort(key=lambda kv: -kv[1])

    # ---- 阶段 2(融合确认): 线上是 2×W5 + 1×pit, 不把 pit 加回来就不知道"线上会不会变好" ----
    for arm, _ in passed:
        okset = True
        for set_name in SETS:
            b_fus = f22.wr((scores[(set_name, "A0_base")], 2), (PIT, 1))
            c_fus = f22.wr((scores[(set_name, arm)], 2), (PIT, 1))
            s0 = _run_series(m, wk, mask, pit, cfg, pos_of, mask_np, op_adj,
                             pd.DataFrame(b_fus, index=wk, columns=cols))
            s1 = _run_series(m, wk, mask, pit, cfg, pos_of, mask_np, op_adj,
                             pd.DataFrame(c_fus, index=wk, columns=cols))
            r0 = _aligned_stats(s0, common) if s0 is not None else {"n": 0}
            r1 = _aligned_stats(s1, common) if s1 is not None else {"n": 0}
            res["stage2_fusion"].setdefault(set_name, {})[arm] = {"base": r0, "cand": r1}
            if not (r1.get("excess_pp", -99) >= r0.get("excess_pp", 99) + 0.5
                    and r1.get("mdd", -99) >= r0.get("mdd", 99) * 1.10):
                okset = False
        res["verdict"].setdefault("stage2", {})[arm] = bool(okset)
    res["common_window"] = {"n": len(common), "rows": rows}
    stage2_pass = [a for a, ok in (res["verdict"].get("stage2") or {}).items() if ok]
    res["verdict"].update({
        "stage1_candidates": [[k, round(float(v), 2)] for k, v in passed],
        "stage2_pass": stage2_pass,
        "adopt": (stage2_pass[0] if stage2_pass else None),
        "note": ("阶段1=单 die 三条判据; 阶段2=把通过的臂按 2:1 与 pit 融合后, 同一三条判据"
                 "再过一遍(线上是融合分, 单 die 变好不蕴含线上变好)")})
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 判决: 采纳=%s | 阶段1候选=%s | 阶段2通过=%s | %.1fs → %s ==",
             res["verdict"]["adopt"] or "维持 A0(base)",
             res["verdict"]["stage1_candidates"], res["verdict"].get("stage2_pass"),
             res["secs"], OUT)


if __name__ == "__main__":
    main()
