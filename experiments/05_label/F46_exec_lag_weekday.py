"""F46 · **执行滞后**：信号每周五收盘算出，是**周一开盘**下单好，还是**周二开盘**？

## 为什么这条值得单独测（它不碰信号、不碰数据、只碰"哪一天动手"）

现役链路：周五收盘 → 出方案 → **下一个交易日（周一）09:32 开盘价成交** → 持有一周。
也就是说袖套**必然吃下周一那一整段**（周一开盘→下一个周一开盘）。而 A 股有据可查的
"周一效应"（周末信息集中释放、周一开盘情绪化）意味着：**周一那段可能系统性偏负**。

这条线的好处是：**不需要新数据、不需要换因子、不需要重训模型** —— 同一套周五分数，
只把下单推迟一个交易日（周一→周二），持有窗口随之整体平移一天。
真有效的话落地极便宜（把 09:32 档挪到周二，其余不动）。

## 两个臂（预注册就这两档，不扫更多滞后）

    LAG0（现役）: 锚点=周五 → 执行=下一个交易日（周一）开盘
    LAG1         : 锚点=周五 → 执行=**再下一个交易日（周二）开盘**（用**同一套周五分数**）

实现上不写第二套调度：把分数矩阵**按索引平移到周一**、可交易掩码**按周一重算**，
评估器自然就在"锚点之后的下一个交易日"= 周二成交。

## 预注册判据（两轴；与 F43/F44 同一套）

    ① 全史（491 期同窗口）两套种子超额 ≥ LAG0 + **1.0pp**；
    ② ACTIVE = 留出集(2024-01~2026-08) 两套种子 ≥ LAG0；
    ③ 全史 MDD 两套种子不劣于 LAG0 × 1.10；
    ④ **对照**：LAG0 必须逐位复现 F41 记录的满仓读数（全史 +18.00/+17.30、ACTIVE +10.38/+11.80，
       同窗口 491 期）—— 复现不了 ⇒ 口径漂了，LAG1 一律不得解读。

## 机制诊断（只作解释，不作判据）

在**票池层面**（等权可交易域）算三段平均收益，看"周一效应"在这份数据里到底存不存在：

    周末跳空 = 周一开盘 / 上周五收盘 − 1
    周一日内 = 周一收盘 / 周一开盘 − 1
    周二日内 = 周二收盘 / 周二开盘 − 1

池子层面的数**不等于**组合层面（组合只吃前 100 名），所以它只能解释方向、不能替代两轴判决。

## 诚实边界

- 又一次看那段已开封的留出集 ⇒ ACTIVE 栏只作方向；
- 这是"择日"，天然容易过拟合日历：**只测两档**、判据事前冻结、两套种子同时要求 ——
  若 LAG1 只是"全史略好、留出集也略好"但都不显著，正确结论是**证据不足**，不是"换到周二"。

**结果（2026-09-13 跑完；判据未动）**：修掉两处脚本缺陷（pit 分按位置平移、配对键改锚点位置 k，
见 docs/know/07 号 #24）后，**对照组通过**（LAG0 逐位复现 F41：491 期、+18.00/+17.30pp、
MDD −36.9%、ACTIVE +10.38/+11.80）。LAG1（周二执行）全史 **+15.58/+15.46pp**（比 LAG0 低
2.42/1.84pp）、MDD −37.4%/−37.5%、ACTIVE +8.40/+10.96；逐期差 HAC t=−0.25/−0.13，
留出集 +0.02/+0.04pp/期（t=0.11/0.20）⇒ **不采纳**。机制：推迟一天 = 放弃周一那段，
而票池周一日内 **+22.4bp**（正）⇒ 等一天是净损失。

**历史状态（2026-09-13 早）**：首跑在 `S1` 构造处撞了 shape 缺陷（`wk1` 比 `wk` 少一期 —— 最后一个
周五之后没有交易日），已修；但**优先级被 F47（周频超参网格）挤后，尚未跑完**。
判据已冻结，可直接重跑。已跑出来的**票池层面星期诊断**（非判据）：
周一跳空 −1.7bp / 周一日内 **+22.4bp**；周二跳空 **−30.1bp** / 周二日内 **+62.0bp**
—— ⇒ "周一负效应"在这份数据里**不明显**，所以这条线索的先验并不强。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F46_exec_lag_weekday.py
"""
import hashlib
import importlib.util
import json
import logging
import sqlite3
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
f29 = _load("f29", HERE / "F29_tail_weighted_training.py")

from app import config as cfg_mod                 # noqa: E402
from app.factors import factor_engine as fe       # noqa: E402
from app.pit.engine import PITEngine              # noqa: E402

log = logging.getLogger("f46")
OUT = HERE / "F46_exec_lag_weekday_result.json"
FULL = ("2010-01-01", "2030-12-31")
DEAD = ("2020-09-01", "2023-12-31")
ACTIVE = ("2024-01-01", "2026-08-31")
SEED_FILES = {"seedA": "F4_scores_seedA.npz", "seedB": "F4_scores_seedB.npz"}
W = 1.0
BASE_REF = {"full": [18.00, 17.30], "active": [10.38, 11.80], "n_common": 491}


def pool_weekday_diagnostic(m: dict, mask: pd.DataFrame) -> dict:
    """票池层面的周末跳空 / 周一日内 / 周二日内（等权可交易域，只用于解释方向）。"""
    close = (m["close"] * m["adj"]).astype(np.float64)
    open_ = (m["open"] * m["adj"]).astype(np.float64)
    idx = close.index
    dts = pd.to_datetime(idx)
    out = {}
    for tag, dow in (("周一", 0), ("周二", 1)):
        sel = [i for i, d in enumerate(dts) if d.weekday() == dow]
        if not sel:
            continue
        gaps, intra = [], []
        for i in sel:
            if i == 0:
                continue
            row_ok = mask.iloc[i].to_numpy() if i < len(mask) else None
            if row_ok is None:
                continue
            o, c, pc = open_.iloc[i].to_numpy(), close.iloc[i].to_numpy(), close.iloc[i - 1].to_numpy()
            g = (o / pc - 1.0)[row_ok & np.isfinite(o / pc - 1.0)]
            n = (c / o - 1.0)[row_ok & np.isfinite(c / o - 1.0)]
            if len(g):
                gaps.append(float(np.mean(g)))
            if len(n):
                intra.append(float(np.mean(n)))
        if gaps:
            out[f"{tag}_跳空"] = {"mean_bp": round(1e4 * float(np.mean(gaps)), 1),
                                  "n": len(gaps)}
        if intra:
            out[f"{tag}_日内"] = {"mean_bp": round(1e4 * float(np.mean(intra)), 1),
                                  "n": len(intra)}
    return out


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)                      # 周五锚点
    cols = list(m["close"].columns)
    pit = PITEngine(m=m, cfg=cfg)
    pos_of = {d: i for i, d in enumerate(dates)}
    # 周一 = 每个周五锚点之后的第一个交易日
    dlist = list(dates)
    wk1 = pd.DatetimeIndex([dlist[pos_of[a] + 1] for a in wk if pos_of[a] + 1 < len(dlist)])
    log.info("锚点 %d（周五）→ 平移后 %d（周一）", len(wk), len(wk1))
    # ---- 决策期(周五)矩阵：值一个不动，只**按位置**平移到本臂的锚点索引（2026-09-13 修复）----
    # 缺陷原状：LAG1 臂拿周一索引的分数去加周五索引的 pit 分 ⇒ pandas 按索引对齐 ⇒ 融合分全 NaN
    # ⇒ 该臂跑出 inf/换手 0.5 的垃圾（实测）。修复后两路 die 都按位置平移，索引一致。
    def _shift_by_pos(mat: pd.DataFrame, index: pd.DatetimeIndex) -> pd.DataFrame:
        return pd.DataFrame(mat.iloc[:len(index)].to_numpy(), index=index, columns=mat.columns)

    mask0 = f1.tradable_frame(pit, wk, cols)
    mask_dec = {"LAG0": _shift_by_pos(mask0, wk), "LAG1": _shift_by_pos(mask0, wk1)}
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    conn = sqlite3.connect(f"file:{BASE / 'data' / 'neweco.db'}?mode=ro", uri=True)
    PITSC = f17.table_scores(conn, "model_score_die_pit", wk, cols)
    conn.close()

    def _run_arm(anch, tag, Sdf):
        """跑一遍组合并接上**决策期票池**基准；返回带 `k`（锚点位置=配对键）的序列。"""
        msk = mask_dec[tag]
        df = f1b.run_overlap_portfolio(m, {}, anch, msk, 5, c, pit, 0, score_matrix=Sdf,
                                       top_n=f29.TOP_N, rank_band=f29.BAND, hyst=f29.HYST)
        if df is None:
            return None
        mask_np = msk.fillna(False).to_numpy()
        bench, prev_exec, prev_ap = [], None, None
        for _, r in df.iterrows():
            e = pos_of[r["date"]]
            if prev_exec is None:
                bench.append(np.nan)
            else:
                good = (mask_np[int(prev_ap)] & np.isfinite(op_adj[prev_exec])
                        & np.isfinite(op_adj[e]) & (op_adj[prev_exec] > 0))
                bench.append(float(np.mean(op_adj[e][good] / op_adj[prev_exec][good] - 1))
                             if good.sum() >= f29.MIN_NAMES else np.nan)
            prev_exec, prev_ap = e, int(r["anchor_pos"])
        df["bench_ret"] = bench
        df = df[["date", "ret", "bench_ret", "turnover", "anchor_pos"]].copy()
        df["k"] = df["anchor_pos"].astype(int)      # 配对键：锚点位置（**不是成交日**）
        return df

    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "equity_weight": W, "arms_definition": {"LAG0": "锚点后第 1 个交易日(周一)",
                                                   "LAG1": "锚点后第 2 个交易日(周二)"},
           "windows": {"full": FULL, "dead": DEAD, "active": ACTIVE},
           "base_reference": BASE_REF,
           "weekday_diagnostic": pool_weekday_diagnostic(m, mask0),
           "arms": {}, "increment": {}, "verdict": {}, "secs": None}
    log.info("票池层面星期诊断: %s", res["weekday_diagnostic"])

    c = {**cfg, "capital": f29.CAPITAL,
         "regime_equity_weight": {"aggressive": {"attack": W, "neutral": W, "defense": W}}}
    series = {}
    for seed, fn in SEED_FILES.items():
        z = np.load(HERE / fn, allow_pickle=True)
        S0 = pd.DataFrame(z["scores"],
                          index=pd.to_datetime([str(d) for d in z["anchors"]]),
                          columns=[str(c_) for c_ in z["cols"]]).reindex(index=wk, columns=cols)
        # LAG1 用的**还是同一套周五分数与周五 pit 分**，只把两路矩阵按位置平移到周一 ⇒
        # 评估器在"锚点后的下一个交易日"= 周二成交。分数值一个没动 ⇒ 无前视、不换模型。
        for tag, anch in (("LAG0", wk), ("LAG1", wk1)):
            sm = f22.wr((_shift_by_pos(S0, anch), 2), (_shift_by_pos(PITSC, anch), 1))
            series[(seed, tag)] = _run_arm(anch, tag, sm)

    # 配对键 = **锚点位置 k**（LAG0 成交在周一、LAG1 在周二：按成交日取交集只剩 5 期 —— 2026-09-13 修复）
    # 共同窗 = **所有种子 × 两臂**锚点位置的交集（与 F41 的 `common` 同语义；只用一套种子会多出一期）
    all_k = None
    for seed in SEED_FILES:
        s0, s1 = series[(seed, "LAG0")], series[(seed, "LAG1")]
        ks = set(s0["k"]) & set(s1["k"])
        all_k = ks if all_k is None else (all_k & ks)
    common_k = sorted(all_k)
    n_common = len(common_k)
    pair = {}
    for seed in SEED_FILES:
        s0, s1 = series[(seed, "LAG0")], series[(seed, "LAG1")]
        pair[seed] = {"k": common_k,
                      "dates": {"LAG0": set(pd.to_datetime(s0.loc[s0["k"].isin(common_k), "date"])),
                                "LAG1": set(pd.to_datetime(s1.loc[s1["k"].isin(common_k), "date"]))}}
    log.info("同窗口对齐（按锚点位置）: %d 期", n_common)
    for (seed, tag), s in series.items():
        full = f29._aligned_stats(s, sorted(pair[seed]["dates"][tag]))
        dead = f29._zone(s, *DEAD)
        active = f29._zone(s, *ACTIVE)
        res["arms"].setdefault(seed, {})[tag] = {"full": full, "dead": dead, "active": active}
        log.info("[%s|%s] 全史 %spp (MDD %.1f%%, 换手 %s) | DEAD %spp | ACTIVE %spp (MDD %.1f%%)",
                 seed, tag, full.get("excess_pp"), 100 * full.get("mdd", float("nan")),
                 full.get("turnover"), dead.get("excess_pp"), active.get("excess_pp"),
                 100 * active.get("mdd", float("nan")))

    ctrl = {}
    for i, seed in enumerate(SEED_FILES):
        b = res["arms"][seed]["LAG0"]
        ctrl[seed] = {"full_was": BASE_REF["full"][i], "full_now": b["full"].get("excess_pp"),
                      "active_was": BASE_REF["active"][i], "active_now": b["active"].get("excess_pp")}
        ctrl[seed]["ok"] = (abs(float(b["full"].get("excess_pp")) - BASE_REF["full"][i]) < 0.05
                            and abs(float(b["active"].get("excess_pp")) - BASE_REF["active"][i]) < 0.05)
    # 对照比的是**对齐后实际期数**（= 有 ret+bench 的期数，F41 记的 491），
    # 不是"共同锚点数"（= 492；其中 1 期因首个执行日没有基准而被 dropna 剔除 —— 2026-09-13 登记）
    n_used = {seed: res["arms"][seed]["LAG0"]["full"].get("n") for seed in SEED_FILES}
    ctrl_ok = (all(v["ok"] for v in ctrl.values())
               and all(n == BASE_REF["n_common"] for n in n_used.values()))
    res["control"] = {"ok": ctrl_ok, "detail": ctrl, "n_used": n_used,
                      "n_k_intersection": n_common,
                      "note": "LAG0 必须逐位复现 F41 满仓读数，且**对齐后实际期数** = 491"}
    log.info("对照(LAG0 复现 F41 / 窗口 %d): %s", n_common, "通过" if ctrl_ok else "**失败**")

    for seed in SEED_FILES:
        b, a = series[(seed, "LAG0")], series[(seed, "LAG1")]
        # 逐期差也按 **k** 配对（成交日对齐会把两臂错开一整天 —— 2026-09-13 修复）
        j = pd.DataFrame({"ret1": a.set_index("k")["ret"], "ret0": b.set_index("k")["ret"],
                          "date0": pd.to_datetime(b.set_index("k")["date"])}).dropna(subset=["ret1", "ret0"])
        d = (j["ret1"] - j["ret0"])
        act = d[(j["date0"] >= ACTIVE[0]) & (j["date0"] <= ACTIVE[1])]
        res["increment"][seed] = {
            "t_diff_full": round(float(f1b.hac_t(d.to_numpy(), 2)), 2) if len(d) > 10 else None,
            "active_mean_pp_per_period": round(100 * float(act.mean()), 3) if len(act) else None,
            "t_diff_active": (round(float(f1b.hac_t(act.to_numpy(), 2)), 2)
                              if len(act) > 10 else None)}
        log.info("[%s] LAG1−LAG0: 全史 t=%s | 留出集 每期 %+.3fpp (t=%s)", seed,
                 res["increment"][seed]["t_diff_full"],
                 res["increment"][seed]["active_mean_pp_per_period"] or 0.0,
                 res["increment"][seed]["t_diff_active"])

    passed = True
    for seed in SEED_FILES:
        b, a = res["arms"][seed]["LAG0"], res["arms"][seed]["LAG1"]
        if not (a["full"].get("excess_pp") is not None
                and a["full"]["excess_pp"] >= b["full"]["excess_pp"] + 1.0
                and a["active"].get("excess_pp") is not None
                and a["active"]["excess_pp"] >= b["active"]["excess_pp"]
                and a["full"].get("mdd") is not None
                and a["full"]["mdd"] >= b["full"]["mdd"] * 1.10):
            passed = False
    res["verdict"] = {
        "control_ok": ctrl_ok,
        "adopt_lag1": bool(passed and ctrl_ok),
        "criteria": "两套种子同时: ①全史 ≥ LAG0+1.0pp ②ACTIVE ≥ LAG0 ③全史 MDD ≥ LAG0×1.10; ④对照须复现",
        "note": "择日类改动最易过拟合日历: 只测两档、判据事前冻结、两套种子同时要求; "
                "若只是略好且不显著 ⇒ 结论是证据不足, 不是'换到周二'"}
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== F46 判决: 换到周二=%s (对照=%s) | %.1fs → %s ==",
             res["verdict"]["adopt_lag1"], ctrl_ok, res["secs"], OUT)


if __name__ == "__main__":
    main()
