"""Round-6 供给侧合并剔除器（2026-09-19；细化预注册 plans/supply-excluder-round6-20260919.md）

母计划 plans/p0-validation-upgrade-20260919.md Phase C。信号（PIT 已物化、量纲已修）:
kg_insider_sell_60d（60 日高管减持事件数）/ kg_unlock_90d_ratio（未来 90 日解禁股数/
流通股本，×1e4 修正后 —— 这是与 round-5 的实质差异：round-5 跑的是 ×10 放大的错版）。

冻结判据（跑前写死，见预注册 §2~§4）:
- 锚点 = kg 锚点 ∩ model_score 贸易日 精确相等（73 个月末，2020-08-31~2026-08-31；
  kg 的 29 个日频锚点不使用）。"全史" = kg 覆盖窗全史，引用必须带此限定。
- z 域 = PIT 可交易 ∩ 当日有分数（不含标签条件）；只有非缺失值参与 z；
  NaN（无事件/无数据）不参与 z 也不被剔除（"双缺失=不剔除"延伸到单信号臂）。
- 合并: 双有值 0.5·z_sell+0.5·z_unlock；单有值用单值；θ∈{1.0,1.5,2.0,2.5}。
- 读数 = F56 同源 top100 等权 5 日 forward_excess + 换手×真实卖出费率；
  ok.sum()<200 记 NaN（F56 守卫），配对差在联合有效锚点上算。
- 过屏 = 全史与活跃段（expert.ACTIVE=2024-01~2026-08）配对差同正 且 全史 t>1.6；
  同族多 θ 取最小 θ，跨族取全史 t 最大，再并列 merge>sell>unlock。至多 1 臂进复验。
- 复验（仅过屏才跑）: verify_excluder_production 模式 + 新鲜控制臂（同窗同管线
  不加剔除，不引用历史 pin run）。采纳须三条全过:
  ① 全史超额 Δ≥10%×|ctrl| 且月度配对 t>1.6；② MDD 不劣；③ 活跃段月度配对差均值≥0。
  重钉 = 用户拍板（不在本轮）。分母 = 12 研究臂 + 1 复验 = 13 判决全登记。

用法:
  .venv/Scripts/python -X utf8 scripts/supply_excluder_r6.py --stage screen    # 只读
  .venv/Scripts/python -X utf8 scripts/supply_excluder_r6.py --stage verify   # 写隔离表+隔离 run
"""
import argparse
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np                                    # noqa: E402
import pandas as pd                                   # noqa: E402

from app import config as cfg_mod                     # noqa: E402
from app import db                                    # noqa: E402
from app.backtest import engine as E                  # noqa: E402
from app.factors import factor_engine as fe           # noqa: E402
from app.logger import setup_logger                   # noqa: E402
from app.models.expert import ACTIVE as ACTIVE_SEG    # noqa: E402  (与生产同源, 不抄写)
from app.pit.engine import PITEngine                  # noqa: E402
from app.portfolio import builder as B                # noqa: E402
from app.portfolio import fees as fee_mod             # noqa: E402
from app.weekly import scores as sc                   # noqa: E402

KG_PARQUET = BASE / "data" / "kg" / "kg_features.parquet"
OUT_JSON = BASE / "data" / "kg" / "supply_excluder_r6_result.json"

SIGNALS = ("kg_insider_sell_60d", "kg_unlock_90d_ratio")
THETAS = (1.0, 1.5, 2.0, 2.5)
FAMILIES = ("merge", "sell", "unlock")     # 优先序（预注册 §3, 再并列时用）
TOP_N = 100
HORIZON = 5                                # 预注册: 5 日 forward_excess
FEE_AMT, FEE_DATE = 25_000.0, "20260914"   # F56 同源
GATE_T = 1.6
MIN_POOL = 200                             # F56 守卫

# 复验（round-4 verify 模式 + 新鲜控制臂）
BACKTEST_START = "20190722"
ST_ARM, ST_CTRL = "model_score_r6excl", "model_score_r6ctl"
TAG_ARM, TAG_CTRL = "r6excl", "r6ctl"
SCORE_DDL = ("CREATE TABLE IF NOT EXISTS {t} (ts_code TEXT, trade_date TEXT, score REAL,"
             " pred_ret REAL, PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID")
CRIT = {"rel_gain": 0.10, "gate_t": GATE_T, "active_floor": 0.0}   # ①②③


def _merge_json(patch: dict) -> None:
    data = json.loads(OUT_JSON.read_text(encoding="utf-8")) if OUT_JSON.exists() else {}
    data.update(patch)
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")


def _common() -> dict:
    """锚点交集 + 行情/PIT/标签/分数面板（screen 与 verify 共用一条构造）。"""
    kg = pd.read_parquet(KG_PARQUET,
                         columns=["anchor", "ts_code", *SIGNALS])
    conn = db.get_conn()
    try:
        ms_dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM model_score ORDER BY trade_date")]
        msdf = pd.read_sql(
            "SELECT trade_date, ts_code, score FROM model_score WHERE trade_date>=? "
            "ORDER BY trade_date", conn, params=(min(kg["anchor"].unique()),))
    finally:
        conn.close()
    anchors = sorted(set(kg["anchor"].unique()) & set(ms_dates))
    log.info("锚点交集 %d 个月末（%s ~ %s）| kg 日频锚点不用",
             len(anchors), anchors[0], anchors[-1])

    m = fe.load_market(since=sc.SINCE)
    pit = PITEngine(m=m, cfg=cfg_mod.load_config())
    cols = sorted(msdf["ts_code"].unique())
    mask = pit.tradable_matrix(anchors, columns=cols).fillna(False)
    Y = sc.forward_excess(m, mask, HORIZON)
    sell_rate = fee_mod.effective_rate(FEE_AMT, "sell", FEE_DATE)
    S = (msdf.pivot_table(index="trade_date", columns="ts_code", values="score")
         .reindex(index=anchors, columns=cols))
    kg73 = kg[kg["anchor"].isin(anchors)]

    # 逐锚点覆盖率登记（数据现实, 预注册 §1: 不修饰）
    cov = {}
    for a in anchors:
        sub = kg73[kg73["anchor"] == a]
        cov[a] = {"ms_n": int(S.loc[a].notna().sum()), "kg_n": int(len(sub)),
                  "sell_nonnull": int(sub[SIGNALS[0]].notna().sum()),
                  "unlock_nonnull": int(sub[SIGNALS[1]].notna().sum())}
    log.info("kg/model_score 覆盖比: min %.2f / median %.2f / max %.2f | 卖出费率 %.4f%%",
             min(c["kg_n"] / max(1, c["ms_n"]) for c in cov.values()),
             float(pd.Series([c["kg_n"] / max(1, c["ms_n"]) for c in cov.values()]).median()),
             max(c["kg_n"] / max(1, c["ms_n"]) for c in cov.values()), sell_rate * 100)

    # z 矩阵（z 域 = mask ∩ 有分数, 不含标签条件; 只有非缺失参与 z）
    zMats = {}
    for sig in SIGNALS:
        Z = np.full((len(anchors), len(cols)), np.nan)
        pvt = (kg73.dropna(subset=[sig])
               .pivot_table(index="anchor", columns="ts_code", values=sig)
               .reindex(index=anchors, columns=cols))
        Sv = S.to_numpy(dtype=float)
        for k, a in enumerate(anchors):
            dom = mask.iloc[k].to_numpy() & np.isfinite(Sv[k])
            x = pvt.iloc[k].to_numpy(dtype=float)[dom]
            if len(x) >= 2 and np.nanstd(x, ddof=1) > 0:
                z = (x - np.nanmean(x)) / np.nanstd(x, ddof=1)
                Z[k, dom] = z           # 域外/缺失保持 NaN ⇒ 不参与剔除
        zMats[sig] = Z
        log.info("z(%s): 非缺失格 %s / %d, 阈上(θ=1)格 %d",
                 sig, np.isfinite(Z).sum(), Z.size,
                 int((Z >= 1.0).sum()))
    return {"anchors": anchors, "cols": cols, "mask": mask, "Y": Y,
            "S": Sv, "sell_rate": sell_rate, "cov": cov, "zMats": zMats}


def _family_z(zMats: dict, family: str) -> np.ndarray:
    """合并规则（预注册 §2 冻结）: 双有值 0.5/0.5, 单有值用单值, 双缺失 NaN=不剔除。"""
    a, b = zMats[SIGNALS[0]], zMats[SIGNALS[1]]
    if family == "sell":
        return a
    if family == "unlock":
        return b
    has_a, has_b = np.isfinite(a), np.isfinite(b)
    both = has_a & has_b
    return np.where(both, 0.5 * a + 0.5 * b, np.where(has_a, a, b))


def _series(C: dict, keep: np.ndarray | None) -> pd.Series:
    """F56 同源 top100 等权 5 日读数（逐字同构, 见 F56b.build_R）; keep=None 即基线。"""
    anchors, _cols, mask, Y, S = (C["anchors"], C["cols"], C["mask"],
                                 C["Y"], C["S"])
    rets, prev, nan_dates = [], None, []
    for k in range(len(anchors)):
        y, s = Y[k], S[k]
        ok = mask.iloc[k].to_numpy() & np.isfinite(s) & np.isfinite(y)
        if keep is not None:
            ok = ok & keep[k]
        if ok.sum() < MIN_POOL:
            rets.append(np.nan)
            nan_dates.append(anchors[k])
            continue
        idx = np.argsort(-np.where(ok, s, -np.inf))[:TOP_N]
        w = np.full(len(idx), 1.0 / len(idx))
        gross = float(np.dot(w, y[idx].astype(float)))
        cost = 0.0
        cur = dict(zip(idx, w))
        if prev:
            names = set(cur) | set(prev)
            cost = sum(abs(cur.get(i, 0.0) - prev.get(i, 0.0)) for i in names) * C["sell_rate"]
        prev = cur
        rets.append(gross - cost)
    if nan_dates:
        log.info("  ok<%d 记 NaN 的锚点: %s", MIN_POOL, nan_dates)
    # DatetimeIndex: 分段统计(活跃段)要与 expert.ACTIVE 的 Timestamp 比较
    return pd.Series(rets, index=pd.to_datetime(pd.Index(anchors), format="%Y%m%d"))


def _seg_stats(diff: pd.Series, seg: str) -> dict:
    if seg == "active":
        lo, hi = pd.Timestamp(ACTIVE_SEG[0]), pd.Timestamp(ACTIVE_SEG[1])
        d = diff[(diff.index >= lo) & (diff.index <= hi)].dropna()
    else:
        d = diff.dropna()
    if len(d) < 3 or d.std(ddof=1) == 0:
        return {"n": int(len(d)), "mean": None, "mean_pp": None, "t": None}
    t = float(d.mean() / (d.std(ddof=1) / np.sqrt(len(d))))
    return {"n": int(len(d)), "mean": round(float(d.mean()), 6),
            "mean_pp": round(float(d.mean()) * 100, 4), "t": round(t, 4)}


def stage_screen() -> None:
    t0 = time.time()
    C = _common()
    anchors = C["anchors"]

    base = _series(C, None)
    arms, drop_cnt = {}, {}
    for fam in FAMILIES:
        Zf = _family_z(C["zMats"], fam)
        for th in THETAS:
            name = f"{fam}_theta{th}"
            keep = ~(Zf >= th)          # NaN ≥ θ = False ⇒ 不剔除
            arms[name] = _series(C, keep)
            drop_cnt[name] = int(((Zf >= th) & np.isfinite(Zf)).sum())

    stats = {}
    for name, ser in arms.items():
        d = ser - base
        full, act = _seg_stats(d, "full"), _seg_stats(d, "active")
        gate = bool(full["mean_pp"] is not None and act["mean_pp"] is not None
                    and full["mean_pp"] > 0 and act["mean_pp"] > 0
                    and full["t"] is not None and full["t"] > GATE_T)
        fam, th = name.split("_theta")
        stats[name] = {"family": fam, "theta": float(th),
                       "excluded_cells": drop_cnt[name],
                       "full": full, "active": act, "gate_pass": gate}
        log.info("%-18s 全史 %+6.2fpp (t=%s) | 活跃 %+6.2fpp (t=%s) | %s",
                 name, full["mean_pp"] or 0.0, full["t"], act["mean_pp"] or 0.0,
                 act["t"], "过屏 ✓" if gate else "")

    # 选择规则（预注册 §3 冻结: 同族最小 θ → 跨族全史 t 最大 → 族优先序）
    selected, note = None, "无臂过屏"
    fam_min = {}
    for fam in FAMILIES:
        cand = [(stats[n]["full"]["t"], n) for n in stats
                if stats[n]["gate_pass"] and stats[n]["family"] == fam]
        if cand:
            fam_min[fam] = min(cand, key=lambda x: stats[x[1]]["theta"])[1]
    if fam_min:
        best = max(fam_min.items(), key=lambda kv: stats[kv[1]]["full"]["t"])
        winners = [f for f in fam_min
                   if stats[fam_min[f]]["full"]["t"] == stats[best[1]]["full"]["t"]]
        best_fam = min(winners, key=FAMILIES.index)     # 族优先序
        selected = fam_min[best_fam]
        note = (f"过屏族 {list(fam_min)} → 全史 t 最大族 '{best_fam}' 的最小 θ 臂"
                if len(fam_min) > 1 else f"仅 {best_fam} 族过屏 → 最小 θ")

    n_pass = sum(stats[n]["gate_pass"] for n in stats)
    verdict = (f"过屏 {n_pass}/12; 选中 '{selected}'（{note}）" if selected
               else f"过屏 0/12 ⇒ 生产复验不触发（分母 13 中第 13 判决登记为'未触发'）")
    log.info("判决: %s", verdict)

    _merge_json({
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "protocol": {"prereg": "plans/supply-excluder-round6-20260919.md",
                     "signals": list(SIGNALS), "thetas": list(THETAS),
                     "families": list(FAMILIES), "top_n": TOP_N, "horizon": HORIZON,
                     "fee": {"amount": FEE_AMT, "date": FEE_DATE},
                     "z_domain": "PIT可交易∩当日有分数(不含标签); 非缺失参与z, NaN不剔除",
                     "merge": "双有值0.5/0.5, 单有值用单值, 双缺失=不剔除",
                     "gate": "两段配对差同正 且 全史 t>1.6",
                     "selection": "同族最小θ→跨族全史t最大→族优先序 merge>sell>unlock"},
        "data_quality": {"n_anchors": len(anchors),
                         "window": [anchors[0], anchors[-1]],
                         "active_seg": list(ACTIVE_SEG), "coverage": C["cov"]},
        "baseline": {"n": int(base.notna().sum()),
                     "mean_pp": round(float(base.dropna().mean()) * 100, 4)},
        "arms": stats,
        "screen": {"n_pass": n_pass, "selected_arm": selected, "note": note},
        "denominator": {"n_research": 12, "n_verify": 1, "total": 13},
        "verdict": verdict,
    })
    log.info("结果 → %s | %.0fs", OUT_JSON, time.time() - t0)


def stage_verify() -> int:
    t0 = time.time()
    res = json.loads(OUT_JSON.read_text(encoding="utf-8")) if OUT_JSON.exists() else {}
    sel = (res.get("screen") or {}).get("selected_arm")
    if not sel:
        _merge_json({"denominator": {"n_research": 12, "n_verify": 1, "total": 13,
                                     "verify_status": "not_triggered"}})
        log.info("研究侧无臂过屏 ⇒ 复验不触发（预注册 §4），登记分母后退出。")
        return 0
    fam, th = sel.split("_theta")
    theta = float(th)
    log.info("复验臂 '%s'（family=%s, θ=%.1f）—— 预注册 §4 三条判据", sel, fam, theta)

    C = _common()
    Zf = _family_z(C["zMats"], fam)
    hot = {a: set(np.asarray(C["cols"])[np.isfinite(Zf[k]) & (Zf[k] >= theta)])
           for k, a in enumerate(C["anchors"])}

    # 两张隔离表: 控制臂 = 同管线不加剔除; 复验臂 = 候选 − hot(仅 kg 覆盖的 73 锚点月)
    conn = db.get_conn()
    try:
        all_days = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM model_score ORDER BY trade_date").fetchall()]
    finally:
        conn.close()
    plan_days = [d for d in all_days if d >= BACKTEST_START]   # YYYYMMDD 字典序=时间序
    rows_arm, rows_ctl, skipped = [], [], []
    for d in plan_days:
        try:
            top, _state, _latest, _ld = B._top_candidates(d)
        except Exception as e:
            skipped.append(d)
            log.warning("  %s: _top_candidates 失败 %s, 跳过", d, type(e).__name__)
            continue
        if top.empty:
            continue
        hotd = hot.get(d, set())
        arm_top = top[~top.index.isin(hotd)] if hotd else top
        for code, row in arm_top.iterrows():
            rows_arm.append((code, d, float(row["score"]),
                             float(row.get("pred_ret") or 0.0)))
        for code, row in top.iterrows():
            rows_ctl.append((code, d, float(row["score"]),
                             float(row.get("pred_ret") or 0.0)))
    log.info("锚点月 %d（跳过 %d）| 剔除后 %s 行 / 控制 %s 行 | %.0fs",
             len(plan_days), len(skipped), f"{len(rows_arm):,}",
             f"{len(rows_ctl):,}", time.time() - t0)

    conn = db.get_conn()
    try:
        for st, rows in ((ST_ARM, rows_arm), (ST_CTRL, rows_ctl)):
            conn.execute(f"DROP TABLE IF EXISTS {st}")
            conn.execute(SCORE_DDL.format(t=st))
            conn.executemany(f"INSERT OR REPLACE INTO {st} VALUES (?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()

    _orig = cfg_mod.load_config

    def _cap(*a, **k):
        c = _orig(*a, **k)
        c["capital"] = 20000.0
        return c

    runs = {}
    cfg_mod.load_config = _cap
    try:
        for st, tag in ((ST_CTRL, TAG_CTRL), (ST_ARM, TAG_ARM)):
            mtr = E.run_backtest(score_table=st, run_tag=tag,
                                 start=BACKTEST_START, end=None)
            runs[tag] = mtr["run_date"]
            log.info("  %s → run %s: sys_ann %.4f, sys_mdd %.4f",
                     tag, mtr["run_date"], mtr["sys_ann_return"],
                     mtr["sys_max_drawdown"])
    finally:
        cfg_mod.load_config = _orig

    # 判决（预注册 §4 冻结三判据）
    conn = db.get_conn()
    try:
        navs = {}
        for tag, run in runs.items():
            d = pd.DataFrame(conn.execute(
                "SELECT trade_date, sys_nav FROM backtest_nav WHERE run_date=? "
                "ORDER BY trade_date", (run,)).fetchall(),
                columns=["trade_date", "sys_nav"])
            d["trade_date"] = pd.to_datetime(d["trade_date"])
            navs[tag] = d.set_index("trade_date")["sys_nav"]
        summ = pd.DataFrame(conn.execute(
            "SELECT run_date, metrics FROM backtest_summary WHERE run_date IN (?, ?)",
            (runs[TAG_CTRL], runs[TAG_ARM])).fetchall(), columns=["run_date", "metrics"])
    finally:
        conn.close()
    sm = {r.run_date: json.loads(r.metrics) for r in summ.itertuples()}
    ctl, arm = sm[runs[TAG_CTRL]], sm[runs[TAG_ARM]]

    common = navs[TAG_CTRL].index.intersection(navs[TAG_ARM].index)
    r = (navs[TAG_ARM].reindex(common).pct_change()
         - navs[TAG_CTRL].reindex(common).pct_change()).dropna()
    t_full = float(r.mean() / (r.std(ddof=1) / np.sqrt(len(r)))) if len(r) > 2 else None
    lo, hi = pd.Timestamp(ACTIVE_SEG[0]), pd.Timestamp(ACTIVE_SEG[1])
    r_act = r[(r.index >= lo) & (r.index <= hi)]
    d_full = arm["sys_ann_return"] - ctl["sys_ann_return"]
    d_mdd = arm["sys_max_drawdown"] - ctl["sys_max_drawdown"]
    c1 = bool(d_full >= CRIT["rel_gain"] * abs(ctl["sys_ann_return"])
              and t_full is not None and t_full > CRIT["gate_t"])
    c2 = bool(arm["sys_max_drawdown"] >= ctl["sys_max_drawdown"])
    c3 = bool(len(r_act) > 0 and r_act.mean() >= CRIT["active_floor"])
    passed = bool(c1 and c2 and c3)
    log.info("① 全史Δ %+5.2fpp (t=%+.2f, 门 ≥%.0f%%×|ctl| 且 t>%.1f): %s",
             d_full * 100, t_full or 0, CRIT["rel_gain"] * 100, CRIT["gate_t"],
             "✓" if c1 else "✗")
    log.info("② MDD 不劣: arm %.4f vs ctrl %.4f (Δ%+.2fpp): %s",
             arm["sys_max_drawdown"], ctl["sys_max_drawdown"], d_mdd * 100,
             "✓" if c2 else "✗")
    log.info("③ 活跃段月度配对差均值 %+5.2fpp (n=%d, 门 ≥0): %s",
             (r_act.mean() if len(r_act) else float("nan")) * 100, len(r_act),
             "✓" if c3 else "✗")
    verdict = (f"复验 {'通过（采纳候选; 重钉=用户拍板）' if passed else '不通过'}"
               f" —— ①{'✓' if c1 else '✗'} ②{'✓' if c2 else '✗'} ③{'✓' if c3 else '✗'}")
    log.info("判决: %s", verdict)

    _merge_json({
        "verify": {"arm": sel, "theta": theta, "family": fam,
                   "runs": {"arm": runs[TAG_ARM], "ctrl": runs[TAG_CTRL]},
                   "excluded": len(rows_ctl) - len(rows_arm),
                   "skipped_plan_dates": skipped,
                   "d_full_ann": round(d_full, 4), "t_full_monthly": round(t_full, 4),
                   "d_mdd": round(d_mdd, 4),
                   "active_mean_pp": round(float(r_act.mean()) * 100, 4),
                   "active_n": int(len(r_act)),
                   "criteria": CRIT, "c1": c1, "c2": c2, "c3": c3,
                   "passed": passed},
        "denominator": {"n_research": 12, "n_verify": 1, "total": 13,
                        "verify_status": "triggered"},
        "final_verdict": verdict,
    })
    log.info("结果 → %s | %.0fs", OUT_JSON, time.time() - t0)
    return 0 if passed else 1


def main() -> int:
    global log
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=["screen", "verify"])
    args = ap.parse_args()
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    log = logging.getLogger("r6")
    setup_logger()
    db.init_db()
    if args.stage == "screen":
        stage_screen()
        return 0
    return stage_verify()


if __name__ == "__main__":
    raise SystemExit(main())
