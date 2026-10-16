"""周频标签"复权 vs 未复权"的**量级实测**（2026-09-14，外部审查 P1-1）

## 起因（我逐行复核为真）

`app/weekly/scores.py::forward_excess` 用 **未复权** 开盘价算前向超额：

    fwd = m["open"].shift(-(h+1)) / m["open"].shift(-1) - 1.0       # h = 5

而复权因子 `m["adj"]` 是**独立一列**，项目里其它位置一律乘它（`weekly/attrib.py:128`
`m["open"] * m["adj"]`、`weekly/health.py:92`、`models/trainer.py:405`），知识库 10 号也写着
"日线用 close × adj_factor 计算收益"。⇒ 除权除息日会出现**机械性**标签（10 送 10 = −50%，
2% 分红 = −2%），而这个标签是**训练目标**（LightGBM rmse），不只是读数。

## 本脚本做什么（**阶段 A：只读，不训练**）

把两种标签在**同一组锚点 × 同一张 PIT 可交易掩码**上逐格比一遍，回答"量级到底多大"：

  · 受影响格数占比（|Δ| > 10bp / 50bp / 100bp）、`|Δ|` 的均值/分位/最大；
  · **符号翻转**格数（未复权说跌、复权说涨 —— 伪规律的直接来源）；
  · 逐锚点截面的 **Spearman(未复权, 复权)** 与 **前十分位重合度**（"完美预言机"会换掉多少票）；
  · 按年月看分布（A 股分红集中在 6~8 月，若集中在分红季则机制成立）。

## 阶段 B（**本脚本不跑**，需要训练时段）：预注册判据 —— **先写死，再看数**

采用复权标签 ⇔ 三条同时成立：

  ① 复权臂的**留出集超额**（生产构型、两套种子均值）≥ 未复权臂 − 0.5pp（不劣化）；
  ② 复权臂的**全史 MDD** 不差于未复权臂 + 2pp；
  ③ 复权臂的**逐锚点 rank IC 均值** ≥ 未复权臂（严格不降）。

三条任一不满足 ⇒ **不采纳**（维持未复权，并在 KB 记为"已知且被测试钉住的口径"）。
**阶段 A 的读数不构成采纳理由** —— 它只说数据侧被污染多少，不说钱会不会变多。

用法：

    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\exp_weekly_label_adj.py
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from app import config as cfg_mod
    from app.factors.factor_engine import load_market
    from app.pit.engine import PITEngine
    from app.weekly import scores as W

    cfg = cfg_mod.load_config()
    m = load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    anchors = W.week_end_dates(dates)
    cols = list(m["close"].columns)
    pit = PITEngine(m=m, cfg=cfg)
    mask = pit.tradable_matrix(anchors, columns=cols).fillna(False)

    y_raw = W.forward_excess(m, mask, W.HORIZON)
    m_adj = dict(m)
    m_adj["open"] = m["open"] * m["adj"]
    y_adj = W.forward_excess(m_adj, mask, W.HORIZON)

    both = np.isfinite(y_raw) & np.isfinite(y_adj)
    only_raw = np.isfinite(y_raw) & ~np.isfinite(y_adj)
    d = (y_adj - y_raw)[both]
    ad = np.abs(d)
    n = int(both.sum())

    def _frac(t: float) -> float:
        return float((ad > t).sum()) / n if n else float("nan")

    # 逐锚点：Spearman 与"前十分位重合度"（完美预言机会换掉多少票）
    spearman, overlap, mk = [], [], []
    for i, a in enumerate(anchors):
        r, j, ok = y_raw[i], y_adj[i], both[i]
        if ok.sum() < 200:
            continue
        rr = pd.Series(r[ok]).rank().to_numpy()
        jj = pd.Series(j[ok]).rank().to_numpy()
        if rr.std() == 0 or jj.std() == 0:
            continue
        spearman.append(float(np.corrcoef(rr, jj)[0, 1]))
        k = max(1, int(ok.sum() * 0.10))
        top_r = set(np.argsort(np.where(ok, r, -np.inf))[-k:])
        top_j = set(np.argsort(np.where(ok, j, -np.inf))[-k:])
        overlap.append(len(top_r & top_j) / float(k))
        mk.append(a)

    ym = pd.Series(np.array([a.to_period("M").strftime("%Y-%m") for a in anchors])[:, None]
                   .repeat(both.shape[1], axis=1)[both])
    by_month = pd.DataFrame({"ym": ym.values, "ad": ad}).groupby("ym")["ad"].agg(
        n="size", frac_gt_50bp=lambda s: float((s > 0.005).mean()))
    worst = by_month.sort_values("frac_gt_50bp", ascending=False).head(8)

    out = {
        "n_cells_both": n,
        "cells_without_adj_label": int(only_raw.sum()),
        "frac_gt_10bp": _frac(0.001), "frac_gt_50bp": _frac(0.005), "frac_gt_100bp": _frac(0.01),
        "mean_abs_delta": float(ad.mean()), "p99_abs_delta": float(np.quantile(ad, 0.99)),
        "max_abs_delta": float(ad.max()),
        "sign_flips": int((np.sign(y_raw[both]) != np.sign(y_adj[both])).sum()),
        "sign_flip_frac": float((np.sign(y_raw[both]) != np.sign(y_adj[both])).mean()),
        "anchor_spearman_mean": float(np.mean(spearman)) if spearman else None,
        "anchor_spearman_min": float(np.min(spearman)) if spearman else None,
        "top_decile_overlap_mean": float(np.mean(overlap)) if overlap else None,
        "n_anchors_compared": len(spearman),
        "worst_months": worst.reset_index().to_dict("records"),
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
