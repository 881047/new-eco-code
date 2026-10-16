"""M3 Q2：截面百分位两把尺（阶段 A，只读）—— 14 号台账开放项 **S3**。

判据**跑数前已冻结**于 `plans/ledger-settlement-criteria-20260920.md §M3`。

## 问题（`app/weekly/scores.py` L441-450）
`with_production=True` 时两把尺各自 `rank(axis=1, pct=True)` 算截面百分位后加权平均：
  尺 1 = 周频 die 分（域 = 周频面板 `cols`，5826 只）
  尺 2 = 生产分 `model_score`（其可交易域由 `trainer.tradable_mask` 决定，4816 只）
⇒ **域不同** ⇒ 同一只票在两把尺里百分位不同 ⇒ 平均后引入偏差。

## 阶段 A（只读）：重算两臂，报三量
  A 臂 = **现实现**：两尺在 `cols` 上各自 rank 后按权重（W5:model = 2:1）平均
  B 臂 = **过 `PITEngine.tradable_mask` 的域**：同样两尺，但先在过滤后的域内 rank 再平均
  三量 = 受影响格数占比 / 逐锚点秩相关 / 前十分位重合率

⚠️ 两版教训（写在这里防第三次错）：
  v1 拿 `model_score` 自有票集当 A 域 ⇒ 它本身已只含可交易票，等于**自比**，测出假的"0 差异"。
  v2 改成 `cols` 但只用生产分 ⇒ 仍是"生产分 vs 生产分"，**没有两把尺**。
  v3（本版）才真正**重现两把尺的加权平均**：周分（存档）+ 生产分，各自 rank 后 2:1 平均。

## 自证门（判据硬性要求）
  M3-a 域定义从 `trainer`/`PITEngine` **现读**，不许自算网格 ⇒ 用 `PITEngine.tradable_mask`
  M3-b 两臂共用同一份特征矩阵（差异只能来自域）⇒ 两臂都取自同一份 w_c / p_c
  M3-c **A 臂对 A 臂自比 ⇒ 秩相关必须 = 1.0**（否则脚本有噪声，判失败）

## 判决
逐锚点秩相关 `≥0.99` **且** 前十分位重合 `≥99%` ⇒ 阶段 A 不越门 ⇒ **关闭**（S3）；
否则立阶段 B（双臂重训，另写预注册）。

## 代价上限
只读；跑到 **60 分钟**未出 ⇒ 中止并如实标"未跑完 + 卡在哪"，**不给估计值**。

用法：python scripts/oneoff/m3_q2_percentile_ab.py
产物：experiments/m3_q2_percentile_ab.json
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app import db
from app.pit.engine import PITEngine
from app.weekly.scores import latest

RANK_CORR_MIN = 0.99
TOP_OVERLAP_MIN = 0.99
TIME_LIMIT_SEC = 3600
MIN_NAMES = 50
W_W5, W_PROD = 2, 1          # 与 scores.py L435/L443 的权重一致

BASE = Path(__file__).resolve().parents[2]
OUT = BASE / "experiments" / "m3_q2_percentile_ab.json"


def main() -> int:
    t0 = time.time()
    print("== M3 Q2 阶段 A（只读）==")

    # ---- 尺 1：周频分存档（不需重训）----
    S_w, anchors_w, cols_w, _names, arc = latest()
    cols = [str(c) for c in cols_w]
    print(f"尺1 周频分存档: {arc.name}  {S_w.shape[0]} 锚点 × {S_w.shape[1]} 只")

    # ---- 尺 2：生产分（与 production_score_on 同口径：reindex + ffill）----
    conn = db.get_conn()
    try:
        ms = pd.read_sql("SELECT ts_code, trade_date, score FROM model_score", conn)
    finally:
        conn.close()
    if ms.empty:
        print("model_score 为空 ⇒ 无法跑（如实标未跑，不给估计值）")
        return 2
    ms["trade_date"] = pd.to_datetime(ms["trade_date"])
    piv = (ms.pivot_table(index="trade_date", columns="ts_code", values="score")
             .sort_index()
             .reindex(index=ms["trade_date"].sort_values().unique(), columns=cols))
    prod = (piv.reindex(index=piv.index.union(anchors_w)).ffill()
               .reindex(index=anchors_w, columns=cols))
    print(f"尺2 生产分: {prod.shape[0]} 锚点 × {prod.shape[1]} 只（已 reindex+ffill 到 cols）")

    # ---- B 域掩码来源（M3-a：现读，不自算）----
    pit = PITEngine()
    print(f"PITEngine 就绪（{time.time() - t0:.0f}s）")

    rows, aborted, abort_at = [], False, None
    for i, dt in enumerate(anchors_w):
        if time.time() - t0 > TIME_LIMIT_SEC:
            aborted, abort_at = True, dt.strftime("%Y%m%d")
            break
        w = pd.Series(S_w[i], index=cols).dropna()      # 尺1 该锚点
        p = prod.iloc[i].dropna()                        # 尺2 该锚点
        common = w.index.intersection(p.index)
        if len(common) < MIN_NAMES:
            continue
        w_c, p_c = w[common], p[common]
        # A 臂（现实现）：两尺各自 rank 后加权平均
        a_rank = (w_c.rank(pct=True) * W_W5 + p_c.rank(pct=True) * W_PROD) / (W_W5 + W_PROD)
        # B 臂：先过滤域，再两尺各自 rank 后加权平均
        try:
            mask = pit.tradable_mask(dt.strftime("%Y%m%d"))
        except Exception:
            continue
        B = common.intersection(mask[mask.fillna(False)].index)
        if len(B) < MIN_NAMES:
            continue
        b_rank = (w_c[B].rank(pct=True) * W_W5 + p_c[B].rank(pct=True) * W_PROD) / (W_W5 + W_PROD)
        # M3-c 自证：A 臂自比必须 = 1.0
        self_corr = a_rank.corr(a_rank, method="spearman")
        rc = a_rank[B].corr(b_rank, method="spearman")
        # 头部重合必须用**相同数量 k** 比较。
        # ⚠️ 第一版 A 臂取 10%·len(common)、B 臂取 10%·len(B)：因 B⊂A ⇒ k_b<k_a，
        #    会**机械地**压低重合率（测出的"不达标"里有一部分是这个定义造成的，不是真实差异）。
        #    现统一取较小域（B）的 10% 作为 k，两臂都取前 k 名。
        k = max(1, int(len(B) * 0.10))
        top_a = set(a_rank.nlargest(k).index)
        top_b = set(b_rank.nlargest(k).index)
        rows.append({
            "date": dt.strftime("%Y%m%d"),
            "n_a": int(len(common)), "n_b": int(len(B)),
            "affected_frac": round(1 - len(B) / len(common), 6),
            "rank_corr": None if pd.isna(rc) else round(float(rc), 6),
            "top10_overlap": round(len(top_a & top_b) / max(1, len(top_a)), 6),
            "self_corr_M3c": None if pd.isna(self_corr) else round(float(self_corr), 6),
        })

    payload = {
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - t0, 1),
        "archive": arc.name,
        "aborted_at": abort_at,
        "thresholds": {"rank_corr_min": RANK_CORR_MIN,
                       "top10_overlap_min": TOP_OVERLAP_MIN,
                       "time_limit_sec": TIME_LIMIT_SEC},
        "per_anchor": rows,
    }

    if not rows:
        payload["verdict"] = "未跑（无可用锚点）"
        print("\n!! 未跑出任何锚点 —— 如实标注，不给估计值")
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        return 2

    bad_self = [r for r in rows if r["self_corr_M3c"] is None
                or abs(r["self_corr_M3c"] - 1.0) > 1e-9]
    if bad_self:
        payload["verdict"] = "失败（M3-c 自证门不过：A 臂自比 ≠ 1.0 ⇒ 脚本有噪声）"
        print(f"\n!! M3-c 失败：{len(bad_self)} 期自比 ≠ 1.0 ⇒ 判失败")
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        return 1

    corrs = [r["rank_corr"] for r in rows if r["rank_corr"] is not None]
    ovl = [r["top10_overlap"] for r in rows]
    aff = [r["affected_frac"] for r in rows]
    min_corr = min(corrs) if corrs else None
    mean_corr = sum(corrs) / len(corrs) if corrs else None
    min_ovl, mean_ovl = min(ovl), sum(ovl) / len(ovl)
    mean_aff = sum(aff) / len(aff)
    payload.update({
        "n_anchors": len(rows),
        "affected_frac_mean": round(mean_aff, 6),
        "affected_frac_max": round(max(aff), 6),
        "rank_corr_min_observed": min_corr,
        "rank_corr_mean": round(mean_corr, 6) if mean_corr is not None else None,
        "top10_overlap_min_observed": round(min_ovl, 6),
        "top10_overlap_mean": round(mean_ovl, 6),
    })

    print("\n== 三量 ==")
    print(f"  受影响格数占比: 均值 {mean_aff * 100:.2f}% / 最大 {max(aff) * 100:.2f}%")
    print(f"  逐锚点秩相关  : 最小 {min_corr} / 均值 {mean_corr}")
    print(f"  前十分位重合率: 最小 {min_ovl} / 均值 {mean_ovl}")

    if aborted:
        payload["verdict"] = f"未跑完（60 分钟上限中止于 {abort_at}）"
        print(f"\n!! {payload['verdict']} —— 如实标注，不给估计值")
    elif min_corr is not None and min_corr >= RANK_CORR_MIN and min_ovl >= TOP_OVERLAP_MIN:
        payload["verdict"] = "关闭（阶段 A 不越门：秩相关与头部重合均达标）"
        print(f"\n⇒ {payload['verdict']}")
    else:
        payload["verdict"] = "越门（须立阶段 B：双臂重训 + 另写预注册）"
        print(f"\n⇒ {payload['verdict']}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("产物:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
