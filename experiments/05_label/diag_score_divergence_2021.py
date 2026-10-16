r"""只读诊断：生产 `model_score` 与现快照重训（`model_score_f78_p1`）**为何从 2021 起分歧**。

## 由来
F78b 探针 P1（生产种子集、现快照重训）全窗年化 7.13pp，而生产表 10.16pp ⇒ 差 −3.03pp。
逐锚点比较发现：**2017–2020 逐位一致**（Spearman=1.0000、top100 重合 1.000），
**2021 起才分歧**（Spearman 0.958~0.990、top100 重合 0.58~0.91）。

⇒ 代码路径相同（前 48 锚逐位一致即为证）⇒ 分歧只能来自**数据**。

## 本脚本要回答（决定 P3 能否推进）
分歧是 **前视（数据被重述）** 还是 **补全（新增历史可得信息）**？
判据：**补全** ⇒ 刷新臂在 2021+ 的 IC 应**更高**（信息更全）；
       **前视/重述** ⇒ IC 未必更高，且会伴随"历史行修订"。

已有证据（本轮只读查证）：
- `daily` / `adj_factor` / `daily_basic` / `fina_indicator`：0913→0921 **历史行修订 0**
  （`diag_data_drift_0916.py`，另 09-16 那份 0913→0916 同样为 0）
- `lhb` / `lhb_org`：按年比对 0913 备份 vs 当前，**差异年份 0 / 11**
⇒ 被覆盖的表里**没有重述** ⇒ 前视风险低，倾向"补全"。本脚本用 IC 做交叉验证。

## 安全
全程 `mode=ro` 只读，**不写任何表**（含隔离表）。
"""
import io
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

PROD = "model_score"
FRESH = "model_score_f78_p1"
TOP_N = 100
HORIZON = 21
SELL_REF_DATE = "20260914"
SPLIT = "2021-01-01"          # 分歧起点
OUT = Path(__file__).resolve().parent / "diag_score_divergence_2021_result.json"


def load_scores(con, table):
    df = pd.read_sql(f"SELECT trade_date, ts_code, score FROM {table}", con)
    # ⚠️ trade_date 是 'YYYYMMDD' 字符串 ⇒ 必须先转成 datetime，否则后续与掩码/标签对不上（全 NaN）
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    return df.pivot_table(index="trade_date", columns="ts_code", values="score").sort_index()


def main():
    con = sqlite3.connect(f"file:{BASE / 'data' / 'neweco.db'}?mode=ro", uri=True)
    prod, fresh = load_scores(con, PROD), load_scores(con, FRESH)
    con.close()
    anchors = pd.DatetimeIndex(sorted(set(prod.index) & set(fresh.index)))
    cols = sorted(set(prod.columns) & set(fresh.columns))
    print(f"共同锚点 {len(anchors)}（{anchors[0].date()} ~ {anchors[-1].date()}）| 共同列 {len(cols)}")

    from app import config as cfg_mod
    from app import db
    from app.factors import factor_engine as fe
    from app.pit.engine import PITEngine
    from app.portfolio import fees as fee_mod
    from app.weekly import scores as sc

    db.init_db()
    m = fe.load_market(since=sc.SINCE)
    cfg = cfg_mod.load_config()
    mask = PITEngine(m=m, cfg=cfg).tradable_matrix(anchors, columns=cols).fillna(False)
    Y = sc.forward_excess(m, mask, HORIZON)
    sell = fee_mod.effective_rate(25_000.0, "sell", SELL_REF_DATE)
    mask_np = mask.to_numpy(dtype=bool)
    print(f"面板就绪 | 掩码均值 {mask_np.mean():.3f} | 卖出费 {sell * 1e4:.2f}bp")

    def series_and_ic(W):
        S = W.reindex(index=anchors, columns=cols).to_numpy(dtype=float)
        rets, ics, prev = [], [], None
        for k in range(S.shape[0]):
            y, s = Y[k], S[k]
            ok = mask_np[k] & np.isfinite(s) & np.isfinite(y)
            if ok.sum() < 200:
                rets.append(np.nan); ics.append(np.nan); continue
            yv, sv = y[ok], s[ok]
            ics.append(float(pd.Series(yv).corr(pd.Series(sv), method="spearman")))
            idx = np.argsort(-np.where(ok, s, -np.inf))[:TOP_N]
            w = np.full(len(idx), 1.0 / len(idx))
            gross = float(np.dot(w, y[idx].astype(float)))
            cur = dict(zip(idx, w)); cost = 0.0
            if prev:
                cost = sum(abs(cur.get(i, 0.) - prev.get(i, 0.)) for i in set(cur) | set(prev)) * sell
            prev = cur
            rets.append(gross - cost)
        return pd.Series(rets, index=anchors), pd.Series(ics, index=anchors)

    rp, ip = series_and_ic(prod)
    rf, iff = series_and_ic(fresh)

    out = {"anchors": len(anchors), "split": SPLIT, "top_n": TOP_N, "horizon": HORIZON,
           "sell_rate_bp": round(sell * 1e4, 3), "segments": {}}
    print("\n分段（pp/年化超额，IC 为 spearman 均值）")
    print(f"  {'段':<22}{'生产 超额':>10}{'刷新 超额':>10}{'Δ':>9}{'生产 IC':>9}{'刷新 IC':>9}{'ΔIC':>8}")
    for name, sel in (("2017-01~2020-12", anchors < pd.Timestamp(SPLIT)),
                      ("2021-01~2026-08", anchors >= pd.Timestamp(SPLIT)),
                      ("全窗", pd.Series(True, index=anchors))):
        a, b = rp[sel].mean() * 1200, rf[sel].mean() * 1200
        ic_a, ic_b = ip[sel].mean(), iff[sel].mean()
        out["segments"][name] = {"prod_excess_pp_yr": round(a, 3), "fresh_excess_pp_yr": round(b, 3),
                                 "delta_pp": round(b - a, 3), "prod_ic": round(ic_a, 4),
                                 "fresh_ic": round(ic_b, 4), "delta_ic": round(ic_b - ic_a, 4),
                                 "n": int(sel.sum())}
        print(f"  {name:<22}{a:>10.2f}{b:>10.2f}{b - a:>9.2f}{ic_a:>9.4f}{ic_b:>9.4f}{ic_b - ic_a:>8.4f}")

    d = out["segments"]["2021-01~2026-08"]
    out["verdict"] = {
        "ic_higher_on_2021plus": bool(d["delta_ic"] > 0),
        "interpretation": ("刷新臂 IC 更高 ⇒ 倾向『数据补全』（历史可得信息补齐），非重述/前视"
                           if d["delta_ic"] > 0 else
                           "刷新臂 IC 未更高 ⇒ 不能排除『重述/前视或数据质量退化』，P3 须谨慎"),
        "note": "与『历史行修订=0』的只读查证互为交叉验证；两者一致才算 PIT 安全。",
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n判定: {out['verdict']['interpretation']}")
    print("产物:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
