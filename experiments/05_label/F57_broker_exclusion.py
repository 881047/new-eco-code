"""F57 券商金股「反向剔除器」—— 生产构型下的两轴判决（2026-09-13）

## 背景（读数从哪来）

阶段 0c 死区体检（`scripts/oneoff/exp_deadzone_ic.py`，2026-09-13 复跑）把第 11 族
「券商月度金股」接进同一套预注册门禁：4 个因子按门禁（要求**正向**头部超额）**全部不通过**，
但方向是**统计上很强的负向** —— `br_cnt` / `br_share` 死区头部 **−1.64%/月（t=−4.66，
Bonferroni 校正 p=0.0001，n=36）**、活跃区 −0.35%。
⇒ 假设：**被越多券商推荐 = 注意力/拥挤越重 ⇒ 未来 21 日越差**，可用作**剔除器**。

⚠️ 该读数的截面只覆盖"曾被推荐过"的 2196 只票（未上榜取 0）⇒ **不许把 −1.64%/月外推**，
必须在**生产构型**下直接做组合层判决 —— 这就是本实验。

## 预注册协议（跑数前冻结；与 05 号台账里写下的四条判据逐字一致）

  基准臂 BASE   生产融合分（`data/weekly/scores_20260911.npz`）+ 生产 PIT 掩码 + 等权 top100
  处理臂 X1     **同一套打分**下：剔除"券商推荐家数在该月**已上榜票**里排前 10%"的名字，
                从落选名单里按分数顺位补足 100 只
  敏感性 X2     剔除**全部**被推荐过的名字（"全截面十分位"的极端读法），只报数、不参与判决
  成本          换手 × 真实卖出费率（与 F55/F56 逐字同源）
  窗口          FULL = 全部共同锚点；ACTIVE = 2024-01-01~2026-08-31；DEAD = 2020-09-01~2023-12-31
  判据（四条全过才算通过）:
    ① FULL 净超额：X1 ≥ BASE
    ② ACTIVE 净超额：X1 ≥ BASE
    ③ |MDD_X1| ≤ |MDD_BASE| + 2pp
    ④ 换手：X1 ≤ 1.20 × BASE
  ⇒ 通过也只算"值得进下一步 A/B"，**不直接上线**（上线要过 F1b 模拟器 + 换 pin 流程）。

  不做：不重训模型、不改打分、不碰生产账本/方案；只读打分存档 + 只写本目录结果文件。

运行：``D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F57_broker_exclusion.py``（约 1 分钟）
"""
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from app import config as cfg_mod                                   # noqa: E402
from app.factors import factor_engine as fe                         # noqa: E402
from app.factors.flow_factors import compute_broker_factors         # noqa: E402
from app.pit.engine import PITEngine                                # noqa: E402
from app.portfolio import fees as fee_mod                           # noqa: E402
from app.weekly import scores as sc                                 # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "F57_broker_exclusion_result.json"
NPZ = BASE_DIR / "data" / "weekly" / "scores_20260911.npz"
log = logging.getLogger("f57")

TOP_N = 100
PER_YEAR = 52.0
TAIL_Q = 0.90                     # X1: 已上榜票里前 10%
MAX_MDD_WORSE_PP = 2.0            # 判据 ③
MAX_TURNOVER_RATIO = 1.20         # 判据 ④
WINDOWS = {"FULL": ("2010-01-01", "2030-12-31"),
           "ACTIVE": ("2024-01-01", "2026-08-31"),
           "DEAD": ("2020-09-01", "2023-12-31")}


def window_stats(ret: pd.Series, tag: str) -> dict:
    lo, hi = WINDOWS[tag]
    r = ret.loc[(ret.index >= lo) & (ret.index <= hi)].dropna()
    if len(r) < 3:
        return {"n": int(len(r))}
    nav = (1 + r).cumprod()
    return {"n": int(len(r)),
            "mean_pp": round(100 * float(r.mean()), 3),
            "excess_pp": round(100 * float((1 + r).prod() ** (PER_YEAR / len(r)) - 1), 2),
            "sharpe": round(float(r.mean() / r.std(ddof=1)), 3),
            "mdd": round(float((nav / nav.cummax() - 1).min()), 4)}


def paired_t(a: pd.Series, b: pd.Series, tag: str) -> dict:
    """成对逐期差 (a−b) 的 t 值 —— **诊断读数, 不是判决判据**(判据已在跑数前冻结)。

    本项目 F46 的教训: 两臂的"共同窗口"必须按对齐后的实际期数算, 且小增量(零点几 pp)极容易
    被噪声吃掉 —— 所以任何"通过"的剔除器/叠加器都要报这一列, 免得把噪声当信号。
    """
    lo, hi = WINDOWS[tag]
    d = (a - b).loc[(a.index >= lo) & (a.index <= hi)].dropna()
    if len(d) < 3:
        return {"n": int(len(d))}
    sd = float(d.std(ddof=1))
    t = float(d.mean() / (sd / np.sqrt(len(d)))) if sd > 0 else 0.0
    return {"n": int(len(d)), "mean_pp": round(100 * float(d.mean()), 3),
            "t": round(t, 2), "positive_share": round(float((d > 0).mean()), 3)}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    with np.load(NPZ, allow_pickle=True) as z:
        S = np.asarray(z["scores"], dtype=float)
        anchors = pd.to_datetime([str(d) for d in z["anchors"]])
        cols = [str(c) for c in z["cols"]]
    m = fe.load_market(since=sc.SINCE)
    pit = PITEngine(m=m, cfg=cfg)
    mask = pit.tradable_matrix(anchors, columns=cols).fillna(False)
    Y = sc.forward_excess(m, mask, sc.HORIZON)
    sell_rate = fee_mod.effective_rate(25_000.0, "sell", "20260914")
    # 券商金股面板(交易日索引, 值=该交易日在榜的券商家数; 未覆盖=NaN)
    daily = pd.to_datetime(m["close"].index)
    br = compute_broker_factors(daily)["br_cnt"].reindex(columns=cols)
    log.info("面板就绪 | %d 锚点 | 券商覆盖均值 %.0f 只/日 | %.0fs",
             len(anchors), float(br.notna().sum(axis=1).mean()), time.time() - t0)

    rows, prev = [], {"BASE": None, "X1": None, "X2": None}
    turn_acc = {"BASE": 0.0, "X1": 0.0, "X2": 0.0}
    ov = {"n": 0, "in_bad": 0, "rec_in_top": 0, "top_n": 0}
    for k, d in enumerate(anchors):
        y, s = Y[k], S[k]
        ok = mask.iloc[k].to_numpy() & np.isfinite(s) & np.isfinite(y)
        if ok.sum() < 200:
            continue
        order = np.argsort(-np.where(ok, s, -np.inf))
        cnt = br.loc[d].to_numpy(dtype=float) if d in br.index else np.full(len(cols), np.nan)
        covered = np.isfinite(cnt)
        rec = {}
        picks = {"BASE": order[:TOP_N]}
        # X1: 剔除"已上榜票里家数前 10%"
        if covered.any():
            pos_vals = cnt[np.isfinite(cnt) & (cnt > 0)]
            thr = float(np.quantile(pos_vals, TAIL_Q)) if len(pos_vals) else np.inf
            bad1 = np.isfinite(cnt) & (cnt >= thr) & (cnt > 0)
        else:
            bad1 = np.zeros(len(cols), dtype=bool)
        picks["X1"] = order[~bad1[order]][:TOP_N]
        picks["X2"] = order[~np.isfinite(cnt) | (cnt <= 0)][:TOP_N] if covered.any() else order[:TOP_N]
        # 机制读数: 模型 top100 里有多少只真的落在"券商头部"(=剔除器能起作用的位置)
        ov["n"] += 1
        ov["in_bad"] += int(bad1[order[:TOP_N]].sum())
        ov["rec_in_top"] += int((cnt[order[:TOP_N]] > 0).sum())
        ov["top_n"] += len(picks["BASE"])
        for arm, sel in picks.items():
            if len(sel) < TOP_N:                      # 覆盖不足时如实记录, 不硬凑
                rec[arm] = np.nan
                continue
            w = np.full(len(sel), 1.0 / len(sel))
            gross = float(np.dot(w, y[sel].astype(float)))
            cost, delta = 0.0, 0.0
            cur = dict(zip(sel, w))
            if prev[arm]:
                names = set(cur) | set(prev[arm])
                delta = sum(abs(cur.get(i, 0.0) - prev[arm].get(i, 0.0)) for i in names)
                cost = delta * sell_rate
            prev[arm] = cur
            turn_acc[arm] += delta
            rec[arm] = gross - cost
        rows.append({"date": d, **rec})
    df = pd.DataFrame(rows).set_index("date")

    stats = {arm: {tag: window_stats(df[arm], tag) for tag in WINDOWS} for arm in ("BASE", "X1", "X2")}
    # 换手 = 单期 |Δw| 之和（与成本同源：cost = turnover × sell_rate）
    turns = {a: round(v / max(1, len(df)), 4) for a, v in turn_acc.items()}

    b, x1 = stats["BASE"], stats["X1"]
    verdict = {
        "full_excess_ok": bool(x1["FULL"]["excess_pp"] >= b["FULL"]["excess_pp"]),
        "active_excess_ok": bool(x1["ACTIVE"]["excess_pp"] >= b["ACTIVE"]["excess_pp"]),
        "mdd_ok": bool(abs(x1["FULL"]["mdd"]) <= abs(b["FULL"]["mdd"]) + MAX_MDD_WORSE_PP / 100),
        "turnover_ok": bool(turns["X1"] <= MAX_TURNOVER_RATIO * turns["BASE"]),
        "delta_full_pp": round(x1["FULL"]["excess_pp"] - b["FULL"]["excess_pp"], 2),
        "delta_active_pp": round(x1["ACTIVE"]["excess_pp"] - b["ACTIVE"]["excess_pp"], 2)}
    diag = {tag: paired_t(df["X1"], df["BASE"], tag) for tag in WINDOWS}
    verdict["adopt"] = bool(all(verdict[k] for k in
                                ("full_excess_ok", "active_excess_ok", "mdd_ok", "turnover_ok")))

    res = {"protocol": {"top_n": TOP_N, "tail_q": TAIL_Q, "sell_rate_bp": round(sell_rate * 1e4, 3),
                        "windows": WINDOWS, "max_mdd_worse_pp": MAX_MDD_WORSE_PP,
                        "max_turnover_ratio": MAX_TURNOVER_RATIO,
                        "arms": {"BASE": "生产融合分 top100", "X1": "剔除已上榜票里家数前 10%",
                                 "X2": "剔除全部被推荐过的票(敏感性, 不判决)"}},
           "n_periods": int(len(df)), "stats": stats, "turnover": turns, "verdict": verdict}
    res["paired_t_X1_minus_BASE"] = diag
    res["overlap"] = {"periods": ov["n"],
                      "mean_top100_in_broker_head": round(ov["in_bad"] / max(1, ov["n"]), 2),
                      "mean_top100_ever_recommended": round(ov["rec_in_top"] / max(1, ov["n"]), 2),
                      "note": "剔除器只有在「模型 top100 ∩ 券商头部」非空时才会改变持仓"}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    print(f"\n共同窗口 {df.index[0].date()} ~ {df.index[-1].date()} | {len(df)} 期 | 卖出费率 "
          f"{sell_rate*1e4:.2f}bp")
    for tag in WINDOWS:
        line = f"  {tag:7s}"
        for arm in ("BASE", "X1", "X2"):
            s = stats[arm][tag]
            line += f" | {arm}: 超额 {s.get('excess_pp','-'):>7} MDD {s.get('mdd','-'):>8}"
        print(line)
    print(f"  换手(平均单期): BASE {turns['BASE']} | X1 {turns['X1']} | X2 {turns['X2']}")
    print(f"  成对诊断(X1−BASE): " + " | ".join(
        f"{tag} 均差 {diag[tag].get('mean_pp','-')}pp t={diag[tag].get('t','-')}"
        for tag in ("FULL", "ACTIVE")))
    print(f"  机制: top100 里落在券商头部的均值 {res['overlap']['mean_top100_in_broker_head']} 只/期"
          f"（曾被推荐过 {res['overlap']['mean_top100_ever_recommended']} 只/期）")
    print(f"  判据: 全史 {verdict['delta_full_pp']:+.2f}pp | 活跃区 {verdict['delta_active_pp']:+.2f}pp | "
          f"MDD {'OK' if verdict['mdd_ok'] else 'NO'} | 换手 {'OK' if verdict['turnover_ok'] else 'NO'}"
          f" ⇒ {'通过(进下一步 A/B)' if verdict['adopt'] else '不通过(写入证伪登记)'}")
    print(f"结果 → {OUT.name} | {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
