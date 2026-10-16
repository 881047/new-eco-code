"""F50 · 生产打分的**逐期 IC 与 IC_IR**（2026-09-13，补上 F49 体检算不了的那条）

## 为什么做

F49 用第三方回测体检（`quantskills/skill-backtest`）对照本项目时，"Sharpe ≈ IC_IR×0.3~0.5"
这条**算不了**：项目只落盘均值 IC（0.0773），没有序列 ⇒ 算不出 IC_IR。
`scores.rank_ic_by_anchor` 已经补进生产打分流程（以后每次打分自带 IC 序列），但**已存的那份
打分没有** —— 本实验把它补算出来，回答两个问题：

  1. 生产融合分（2×W5+pit）在 5 日标签上的**逐期秩相关 IC 的 mean / sd / IC_IR** 是多少？
  2. 用第三方体检的经验关系 `Sharpe ≈ IC_IR × 0.3~0.5` 反查：我们落盘的周频 Sharpe(0.1927/期)
     是否**与该 IC 量级自洽**？（不自洽 = 要么回测偏乐观，要么 IC 算错 —— 都值得深挖）

## 判据（跑数前冻结，纯度量、不搜索）

- IC 只在"分数有限 且 标签可用"的票上算（`min_n=200`，与生产 `rank_ic_by_anchor` 同一口径）；
  最后一个锚点的未来收益尚未实现 ⇒ 天然 NaN，**不填 0**；
- 结论同时给：IC mean / IC sd / IC_IR / 有效期数 / 覆盖的锚点区间；
- 与 0.3~0.5 的对照只作**量级自洽性**判断，不当显著性证据（显著性见 F38/F48）。

用法：
    D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F50_ic_ir_of_prod_scores.py
"""
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

from app import config as cfg_mod                      # noqa: E402
from app.factors import factor_engine as fe            # noqa: E402
from app.pit.engine import PITEngine                   # noqa: E402
from app.weekly import scores as sc                    # noqa: E402

OUT = HERE / "F50_ic_ir_result.json"
NPZ = BASE / "data" / "weekly" / "scores_20260911.npz"
SHARPE_WEEK = 0.1927          # F38 落盘的周频 Sharpe（现役 2×W5+pit, seedA, 491 期）
LOW, HIGH = 0.3, 0.5          # 第三方体检给的经验系数区间


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    if not NPZ.exists():
        print(f"缺打分存档 {NPZ}")
        return 2
    with np.load(NPZ, allow_pickle=True) as z:
        S = z["scores"]
        anchors = pd.to_datetime([str(d) for d in z["anchors"]])
        cols = [str(c) for c in z["cols"]]
        meta = [str(m) for m in z["meta"]]
    print(f"打分存档 {NPZ.name}: {S.shape[0]} 锚点 × {len(cols)} 股 | {anchors[0].date()} ~ {anchors[-1].date()}")

    cfg = cfg_mod.load_config()
    m = fe.load_market(since=sc.SINCE)
    dates = pd.to_datetime(m["close"].index)
    print(f"行情载入: {len(dates)} 交易日 ({time.time()-t0:.0f}s)")
    pit = PITEngine(m=m, cfg=cfg)
    mask = pit.tradable_matrix(anchors, columns=cols).fillna(False)
    print(f"PIT 可交易矩阵: {mask.shape} ({time.time()-t0:.0f}s)")
    Y = sc.forward_excess(m, mask, sc.HORIZON)
    ic, n_ok = sc.rank_ic_by_anchor(S, Y)
    s = sc.ic_summary(ic)

    valid = np.isfinite(ic)
    span = (anchors[valid][0].strftime("%Y%m%d"), anchors[valid][-1].strftime("%Y%m%d")) if valid.any() else None
    lo_sr, hi_sr = (s.get("ic_ir", 0) * LOW, s.get("ic_ir", 0) * HIGH)
    within = lo_sr <= SHARPE_WEEK <= hi_sr
    res = {
        "scores_file": NPZ.name, "n_anchors": int(S.shape[0]), "n_stocks": len(cols),
        "horizon": int(sc.HORIZON), "min_n": 200,
        "ic": s, "valid_anchors": int(valid.sum()),
        "ic_span": span,
        "ic_by_anchor": [None if not np.isfinite(x) else round(float(x), 5) for x in ic],
        "n_by_anchor": [int(x) for x in n_ok],
        "sharpe_week_from_F38": SHARPE_WEEK,
        "rule_of_thumb": {"IC_IR_based_low": round(lo_sr, 3), "IC_IR_based_high": round(hi_sr, 3),
                          "sharpe_within_band": bool(within),
                          "note": "第三方体检的经验关系 Sharpe ≈ IC_IR × 0.3~0.5（周频口径）；"
                                  "只作量级自洽性检查, 不是显著性证据"},
        "meta_of_scores": meta,
        "secs": round(time.time() - t0, 1),
    }
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print()
    print(f"逐期 IC: {s}")
    print(f"有效期数 {int(valid.sum())}/{len(ic)} | 区间 {span}")
    print(f"第三方经验带: Sharpe ∈ [{lo_sr:.3f}, {hi_sr:.3f}]（本策略 {SHARPE_WEEK}）"
          f" ⇒ {'量级自洽' if within else '**不在带内**'}")
    print(f"写入 {OUT.name}（{res['secs']}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
