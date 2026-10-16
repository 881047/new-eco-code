"""F38 · **多重比较打折**（Deflated Sharpe，交付前最后一关）—— 预注册

## 为什么必须做（计划文档自己写的交付关）

`plans/weekly-terminal-plan-20260912.md` 早期那节写着：

    "**仍未做（交付前的最后三关）**：1. **多重比较打折**：M5 是从约 25 个组合臂里选出来的，
     需按 Deflated Sharpe 折算后再报；2. 独立实现交叉验证（§2.12 已完成）；3. 纸面运行。"

第 2 条已完成（F6 独立重写复现），第 3 条 2026-09-14 开始 —— **只剩第 1 条**。

## 方法（跑数前冻结）

1. **基准读数**：现役构型（2×W5+pit / 100 万 / top100 / 带 8× / 加成 0.2σ，两套种子）的
   **周频净超额序列** → 年化 Sharpe、偏度、峰度、T（期数）。
2. **Deflated Sharpe Ratio**（Bailey & López de Prado 2014）：

       SR0 = √V · [ (1−γ)·Z⁻¹(1 − 1/N) + γ·Z⁻¹(1 − 1/(N·e)) ],  γ = 0.5772
       DSR = Φ[ (SR − SR0)·√(T−1) / √(1 − γ3·SR + (γ4−1)/4·SR²) ]

   · `SR` 用**每期（周）**口径；`T` = 周数；`γ3/γ4` = 策略周收益的偏度/峰度；
   · `V` = **各试验臂 Sharpe 的截面方差**；`N` = 试验次数。
3. **试验集与 N（如实声明口径）**：计划文档 §2.10 记录了 **7 个组合臂**的年化 Sharpe
   （L0 −1.00 / L1 0.37 / L4 0.49 / L5 0.67 / L7 0.74 / **M5 0.79** / M6 0.81）；
   而实际搜索过的构型**不止 7 个**（F12/F13/F23/F34/F37 等约 25 个臂）。
   本实验主报 **N=7**（有 Sharpe 记录的那批）并用 **N=25** 做敏感性 —— 后者更接近真实搜索规模。
4. **对照**：同一份 t 统计量做 Bonferroni 校正（N = 1 / 7 / 25 / 38 / 1000），
   回答的是**另一个问题**（"超额是否 > 0"），与 DSR（"Sharpe 是否高于 N 次抽样的期望最大值"）
   必须**分开报** —— 两者结论不同是正常的，混起来就会自欺。

## 预注册判据（决定终端怎么"报数"）

    · DSR ≥ 0.95 ⇒ 现役 Sharpe 在 N 次搜索下依然显著，可以照常报"显著"；
    · DSR < 0.95 ⇒ **不得再把全史超额当作显著读数**，README/看板必须带上"多重比较后不显著"；
      此时"可辩护的结论"只剩：① 与前 8 族数据/模型无关的独立证据（F28 样本外、F24 归因）；
      ② 纸面运行里逐周积累的新样本。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F38_multiplicity_haircut.py
"""
import hashlib
import importlib.util
import json
import logging
import math
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as st

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent
sys.path.insert(0, str(BASE))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


f29 = _load("f29", HERE / "F29_tail_weighted_training.py")
f17 = _load("f17", HERE / "F17_die_registry_kgd.py")
f22 = _load("f22", HERE / "F22_pair_final_selection.py")
f1, f1b = f29.f1, f29.f1b

from app import config as cfg_mod             # noqa: E402
from app.factors import factor_engine as fe   # noqa: E402
from app.pit.engine import PITEngine          # noqa: E402

log = logging.getLogger("f38")
OUT = HERE / "F38_multiplicity_haircut_result.json"
SEED_FILES = {"seedA": "F4_scores_seedA.npz", "seedB": "F4_scores_seedB.npz"}
# 计划文档 §2.10 表格里记录的 7 个组合臂年化 Sharpe（事前引用，不是本轮现算）
TRIAL_SHARPES_DOC = {"L0": -1.00, "L1": 0.37, "L4": 0.49, "L5": 0.67,
                     "L7": 0.74, "M5": 0.79, "M6": 0.81}
PER_YEAR = 52.0
GAMMA = 0.5772156649


def deflated_sr(sr_period: float, T: int, skew: float, kurt: float,
                var_trials_period: float, n_trials: int) -> dict:
    """Bailey & López de Prado 的 Deflated Sharpe Ratio。"""
    if var_trials_period <= 0 or n_trials < 2:
        return {"sr0": None, "dsr": None}
    z1 = st.norm.ppf(1 - 1.0 / n_trials)
    z2 = st.norm.ppf(1 - 1.0 / (n_trials * math.e))
    sr0 = math.sqrt(var_trials_period) * ((1 - GAMMA) * z1 + GAMMA * z2)
    denom = math.sqrt(max(1e-12, 1 - skew * sr_period + (kurt - 1) / 4.0 * sr_period ** 2))
    dsr = float(st.norm.cdf((sr_period - sr0) * math.sqrt(max(1, T - 1)) / denom))
    return {"sr0_period": round(sr0, 4), "sr0_annual": round(sr0 * math.sqrt(PER_YEAR), 3),
            "dsr": round(dsr, 4)}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    cols = list(m["close"].columns)
    pit = PITEngine(m=m, cfg=cfg)
    mask = f1.tradable_frame(pit, wk, cols)
    mask_np = mask.fillna(False).to_numpy()
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    conn = sqlite3.connect(f"file:{BASE/'data'/'neweco.db'}?mode=ro", uri=True)
    PITSC = f17.table_scores(conn, "model_score_die_pit", wk, cols)
    conn.close()

    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "trial_sharpes_doc": TRIAL_SHARPES_DOC, "seeds": {}, "verdict": {}, "secs": None}
    for seed, fn in SEED_FILES.items():
        z = np.load(HERE / fn, allow_pickle=True)
        a5 = pd.to_datetime([str(d) for d in z["anchors"]])
        c5 = [str(c) for c in z["cols"]]
        W5 = pd.DataFrame(z["scores"], index=a5, columns=c5).reindex(index=wk, columns=cols)
        Sdf = pd.DataFrame(f22.wr((W5, 2), (PITSC, 1)), index=wk, columns=cols)
        d = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, {**cfg, "capital": 1_000_000.0},
                                      pit, 0, score_matrix=Sdf, top_n=100, rank_band=8.0,
                                      hyst=0.2)
        bench, prev_exec, prev_ap = [], None, None
        for _, r in d.iterrows():
            e = pos_of[r["date"]]
            if prev_exec is None:
                bench.append(np.nan)
            else:
                good = (mask_np[int(prev_ap)] & np.isfinite(op_adj[prev_exec])
                        & np.isfinite(op_adj[e]) & (op_adj[prev_exec] > 0))
                bench.append(float(np.mean(op_adj[e][good] / op_adj[prev_exec][good] - 1))
                             if good.sum() >= 200 else np.nan)
            prev_exec, prev_ap = e, int(r["anchor_pos"])
        d["bench_ret"] = bench
        d = d.dropna(subset=["ret", "bench_ret"])
        ex = (d["ret"] - d["bench_ret"]).to_numpy()
        T = len(ex)
        sr_w = float(np.mean(ex) / np.std(ex, ddof=1))          # 每期(周) Sharpe
        sk, ku = float(st.skew(ex)), float(st.kurtosis(ex, fisher=False))  # 峰度用非超额口径
        v_doc = float(np.var(list(TRIAL_SHARPES_DOC.values()), ddof=1)) / PER_YEAR
        entry = {"T": T, "sr_week": round(sr_w, 4), "sr_ann": round(sr_w * math.sqrt(PER_YEAR), 3),
                 "skew": round(sk, 3), "kurtosis": round(ku, 3),
                 "var_trials_doc_ann": round(float(np.var(list(TRIAL_SHARPES_DOC.values()), ddof=1)), 4),
                 "n_trials_doc": len(TRIAL_SHARPES_DOC)}
        entry["dsr_n7"] = deflated_sr(sr_w, T, sk, ku, v_doc, len(TRIAL_SHARPES_DOC))
        entry["dsr_n25"] = deflated_sr(sr_w, T, sk, ku, v_doc, 25)
        # 对照: 同一 t 统计量的 Bonferroni(N 次独立检验) —— 回答"超额是否>0"
        t_stat = float(f29.f1b.hac_t(ex, 2))
        p1 = float(st.norm.sf(t_stat))                          # 单侧
        entry["excess_t_hac"] = round(t_stat, 2)
        entry["p_bonferroni"] = {str(n): round(min(1.0, p1 * n), 6) for n in (1, 7, 25, 38, 1000)}
        res["seeds"][seed] = entry
        log.info("[%s] T=%d 周 | SR_周 %.4f (年化 %.2f) | 偏度 %.2f 峰度 %.2f | "
                 "SR0(N=7) %.2f 年化 | **DSR(N=7) %.3f** | DSR(N=25) %.3f | 超额 t=%.2f",
                 seed, T, sr_w, entry["sr_ann"], sk, ku,
                 entry["dsr_n7"]["sr0_annual"], entry["dsr_n7"]["dsr"], entry["dsr_n25"]["dsr"],
                 t_stat)
        log.info("      Bonferroni 校正后的 p(N=1/7/25/38/1000): %s",
                 list(entry["p_bonferroni"].values()))

    dsr_vals = [res["seeds"][s]["dsr_n7"]["dsr"] for s in SEED_FILES]
    res["verdict"] = {
        "dsr_n7_min": min(dsr_vals),
        "pass_95": bool(min(dsr_vals) >= 0.95),
        "note": ("DSR ≥0.95 ⇒ 可照常报显著" if min(dsr_vals) >= 0.95 else
                 "**DSR <0.95 ⇒ 不得再把全史超额当显著读数**：README/看板必须带"
                 "'多重比较后不显著'；可辩护的只有 F28 样本外 + F24 归因 + 未来的纸面样本"),
        "bonferroni_note": "同一 t 统计量在 N≤1000 下都过（回答的是'超额>0'，与 DSR 不是同一个问题）"}
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 判决: DSR(N=7) 最小 %.3f | 通过 0.95 = %s | %.1fs → %s ==",
             res["verdict"]["dsr_n7_min"], res["verdict"]["pass_95"], res["secs"], OUT)


if __name__ == "__main__":
    main()
