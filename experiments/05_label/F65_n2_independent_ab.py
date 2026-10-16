"""F65 `N2`（合成坏分前 5% 剔除）的**独立 A/B** —— 生产构型模拟器，两套种子（2026-09-13）

**为什么是这一步**：F59/F60/F61/F63 都是在**连续近似** harness 上测的（每周重选 top100、无出带、
无整手、无现金拖累）。F63 里 `N2` 拿到 **+0.46pp 全史 / +0.44pp 活跃区、成对 t=2.17、MDD 与换手不变**
⇒ 按纪律它只是"候选"，必须过**独立 A/B**：换用**生产构型模拟器**（`F1b.run_overlap_portfolio`：
100 万 / top100 / **8× 出带** / `hyst=0.0` / 整手 / 逐笔费用 / 涨跌停闸门）+ **两套种子**重测。

**实现方式（不改冻结脚本）**：把要剔除的名字在该锚点的**分数设为 −inf** 再喂给 `score_matrix`
⇒ 模拟器自然永不选它们（等价于"剔掉"，且保留它原有的出带/整手/费用逻辑）。

**预注册判据（跑数前冻结；与 F63 同门槛 + 追加"两套种子都要过"）**
  ① 全史净超额：N2 ≥ BASE（每套种子各自成立）
  ② 活跃区(2024-01~2026-08)净超额：N2 ≥ BASE
  ③ |MDD| ≤ |MDD_BASE| + 2pp
  ④ 换手（模拟器 `turnover` 列均值）≤ 1.20 × BASE
  **两套种子同时满足四条**才算通过 ⇒ 才谈 `plan.py` 加过滤器（换 pin 流程）。
  ⚠️ 若不过：这条线（负因子筛选）与前面 13 个数据族一样**结清**，不再在它上面花时间。

运行：``D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F65_n2_independent_ab.py``
（预计 10~15 分钟：4 次生产构型模拟：BASE/N2 × seedA/seedB）
"""
import importlib.util
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BASE_DIR = HERE.parent.parent
sys.path.insert(0, str(BASE_DIR))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


f1 = _load("f1", HERE / "F1_weekly_ic_decay.py")
f1b = _load("f1b", HERE / "F1b_weekly_open_overlap.py")

from app import config as cfg_mod                                     # noqa: E402
from app.factors import factor_engine as fe                           # noqa: E402
from app.factors.flow_factors import compute_broker_factors           # noqa: E402
from app.factors.flow_factors import compute_top10_holder_factors     # noqa: E402
from app.pit.engine import PITEngine                                  # noqa: E402

OUT = HERE / "F65_n2_independent_ab_result.json"
CAPITAL, TOP_N, BAND, HYST = 1_000_000.0, 100, 8.0, 0.0
NEG_Q = 0.05
NEG_FACTORS = ["br_cnt", "ih_fund_ratio"]
SEEDS = {"seedA": "F4_scores_seedA.npz", "seedB": "F4_scores_seedB.npz"}
FULL = ("2010-01-01", "2030-12-31")
ACTIVE = ("2024-01-01", "2026-08-31")
DEAD = ("2020-09-01", "2023-12-31")
MAX_MDD_WORSE_PP, MAX_TURNOVER_RATIO = 2.0, 1.20
log = logging.getLogger("f65")


def _stats(df: pd.DataFrame, tag: str) -> dict:
    lo, hi = {"FULL": FULL, "ACTIVE": ACTIVE, "DEAD": DEAD}[tag]
    s = df[(df["date"] >= lo) & (df["date"] <= hi)].dropna(subset=["ret", "bench_ret"])
    if len(s) < 3:
        return {"n": int(len(s))}
    ann_s, ann_b = f1b.ann(s["ret"], 52.0), f1b.ann(s["bench_ret"], 52.0)
    nav = (1 + s["ret"]).cumprod()
    return {"n": int(len(s)),
            "excess_pp": round(100 * (ann_s - ann_b), 2),
            "turnover": round(float(s["turnover"].mean()), 4),
            "mdd": round(float((nav / nav.cummax() - 1).min()), 4)}


def main() -> int:
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
    log.info("周锚点 %d | 票池均值 %.0f", len(wk), mask.sum(axis=1).mean())

    # ---- 剔除名单：**并集定义**（F59/F60 的有作用对象版）----
    # 为什么不用 F63 的"合成分前 5%"：实测它每期 121 只，但落在模型 top100 里只有 **0.15 只/期**
    # ⇒ 对生产组合几乎不动手（不可测，不是"有用"）。并集定义每期剔到模型 top100 里约 8 只 ⇒ 有对象。
    # 这个选择在**跑 A/B 之前**写死在这里（不是看结果后改的）。
    daily = pd.to_datetime(m["close"].index)
    fac = {}
    fac.update(compute_broker_factors(daily))
    fac.update(compute_top10_holder_factors(m, daily))
    bad_mask = np.zeros((len(wk), len(cols)), dtype=bool)
    n_narrow = []
    for k, d in enumerate(wk):
        comp = np.zeros(len(cols))
        cnt = np.zeros(len(cols))
        for f in NEG_FACTORS:
            v = fac[f].reindex(index=daily, columns=cols).loc[d].to_numpy(dtype=float) \
                if d in fac[f].index else np.full(len(cols), np.nan)
            ok = np.isfinite(v)
            if ok.sum() >= 100:
                sd = float(np.nanstd(v[ok]))
                if sd > 0:
                    comp[ok] += (v[ok] - float(np.nanmean(v[ok]))) / sd
                    cnt[ok] += 1
                # 并集定义：每个因子各自的高分位十分位
                thr90 = np.quantile(v[ok], 1 - 0.10)
                bad_mask[k] |= ok & (v >= thr90)
        both = cnt > 0
        if both.sum() >= 100:
            comp[both] /= cnt[both]
            thr = np.quantile(comp[both], 1 - NEG_Q)
            n_narrow.append(int((both & (comp >= thr)).sum()))
    log.info("剔除名单就绪（并集定义）：平均 %.2f 只/期；对照：合成分前5%%口径平均 %.0f 只/期 | %.0fs",
             bad_mask.sum(axis=1).mean(), float(np.mean(n_narrow)) if n_narrow else 0.0,
             time.time() - t0)

    res = {"protocol": {"capital": CAPITAL, "top_n": TOP_N, "rank_band": BAND, "hyst": HYST,
                        "neg_rule": "并集：两负因子各自高分位十分位（F59/F60 版，有作用对象）",
                        "narrow_rule_excluded_per_period": round(float(np.mean(n_narrow)), 1) if n_narrow else None,
                        "neg_q": NEG_Q, "neg_factors": NEG_FACTORS, "seeds": list(SEEDS),
                        "windows": {"full": FULL, "active": ACTIVE, "dead": DEAD},
                        "criteria": "两套种子同时满足：全史与活跃区净超额不劣化 + |MDD| 不劣化超 2pp + 换手 ≤1.20×",
                        "mean_excluded_per_period": round(float(bad_mask.sum(axis=1).mean()), 2)},
           "arms": {}, "verdict": {}}
    series = {}
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    mask_np = mask.reindex(index=wk, columns=cols).fillna(False).to_numpy()
    for seed, fn in SEEDS.items():
        z = np.load(HERE / fn, allow_pickle=True)
        a = pd.to_datetime([str(x) for x in z["anchors"]])
        c = [str(x) for x in z["cols"]]
        W = pd.DataFrame(z["scores"], index=a, columns=c).reindex(index=wk, columns=cols)
        Wn = W.copy()
        arr = np.array(Wn.to_numpy(dtype=float), copy=True)   # to_numpy 可能是只读视图
        arr[bad_mask] = -np.inf                     # ← 剔除：分数设 −inf
        Wn = pd.DataFrame(arr, index=wk, columns=cols)
        for arm, Sx in (("BASE", W), ("N2", Wn)):
            df = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, {**cfg, "capital": CAPITAL}, pit, 0,
                                           score_matrix=Sx, top_n=TOP_N, rank_band=BAND, hyst=HYST)
            if df is None:
                log.error("%s/%s 回测失败", seed, arm)
                return 2
            # 基准：同一持仓窗口内、可交易票池的等权收益（与 F16/F62 同法）
            bench, prev_exec, prev_ap = [], None, None
            for _, r in df.iterrows():
                e = pos_of[r["date"]]
                if prev_exec is None or prev_ap is None:
                    bench.append(np.nan)
                else:
                    good = (mask_np[int(prev_ap)] & np.isfinite(op_adj[prev_exec])
                            & np.isfinite(op_adj[e]) & (op_adj[prev_exec] > 0))
                    p0, p1 = op_adj[prev_exec], op_adj[e]
                    bench.append(float(np.mean(p1[good] / p0[good] - 1)) if good.sum() >= 200 else np.nan)
                prev_exec, prev_ap = e, int(r["anchor_pos"])
            df["bench_ret"] = bench
            st = {tag: _stats(df, tag) for tag in ("FULL", "ACTIVE", "DEAD")}
            series[f"{seed}_{arm}"] = df[["date", "ret", "bench_ret"]].copy()   # 只留内存算成对 t
            res["arms"][f"{seed}_{arm}"] = {"stats": st}
            log.info("  [%s/%s] 全史超额 %+.2fpp 活跃区 %+.2fpp | MDD %.1f%% 换手 %.3f | %.0fs",
                     seed, arm, st["FULL"]["excess_pp"], st["ACTIVE"]["excess_pp"],
                     100 * st["FULL"]["mdd"], st["FULL"]["turnover"], time.time() - t0)

    for seed in SEEDS:
        b = res["arms"][f"{seed}_BASE"]["stats"]
        n = res["arms"][f"{seed}_N2"]["stats"]
        db = series[f"{seed}_BASE"].set_index("date")["ret"]
        dn = series[f"{seed}_N2"].set_index("date")["ret"]
        diff = (dn - db).dropna()
        t_full = float(diff.mean() / (diff.std(ddof=1) / np.sqrt(len(diff)))) if len(diff) > 2 else None
        v = {"delta_full_pp": round(n["FULL"]["excess_pp"] - b["FULL"]["excess_pp"], 2),
             "delta_active_pp": round(n["ACTIVE"]["excess_pp"] - b["ACTIVE"]["excess_pp"], 2),
             "full_excess_ok": bool(n["FULL"]["excess_pp"] >= b["FULL"]["excess_pp"]),
             "active_excess_ok": bool(n["ACTIVE"]["excess_pp"] >= b["ACTIVE"]["excess_pp"]),
             "mdd_ok": bool(abs(n["FULL"]["mdd"]) <= abs(b["FULL"]["mdd"]) + MAX_MDD_WORSE_PP / 100),
             "turnover_ok": bool(n["FULL"]["turnover"] <= MAX_TURNOVER_RATIO * b["FULL"]["turnover"]),
             "turnover_base": b["FULL"]["turnover"], "turnover_n2": n["FULL"]["turnover"],
             "paired_t_n2_minus_base": round(t_full, 2) if t_full is not None else None}
        v["pass"] = bool(all(v[x] for x in ("full_excess_ok", "active_excess_ok",
                                            "mdd_ok", "turnover_ok")))
        res["verdict"][seed] = v
    res["verdict"]["adopt"] = bool(all(res["verdict"][s]["pass"] for s in SEEDS))
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    print(f"\n生产构型独立 A/B（100万/top100/8×出带/hyst=0/整手/逐笔费用）| 剔除 "
          f"{res['protocol']['mean_excluded_per_period']} 只/期")
    print(f"{'种子/臂':<12}{'全史超额':>10}{'活跃区':>9}{'MDD':>9}{'换手':>8}")
    for seed in SEEDS:
        for arm in ("BASE", "N2"):
            st = res["arms"][f"{seed}_{arm}"]["stats"]
            print(f"{seed+'/'+arm:<12}{st['FULL']['excess_pp']:>10.2f}{st['ACTIVE']['excess_pp']:>9.2f}"
                  f"{st['FULL']['mdd']:>9.4f}{st['FULL']['turnover']:>8.4f}")
        v = res["verdict"][seed]
        print(f"   Δ全史 {v['delta_full_pp']:+.2f}pp | Δ活跃 {v['delta_active_pp']:+.2f}pp | "
              f"t={v['paired_t_n2_minus_base']} | {'过' if v['pass'] else '不过'}")
    print(f"\n== 两套种子同时通过? {res['verdict']['adopt']} ==  | {res['secs']}s → {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
