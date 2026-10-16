"""F24 · 生产构型的损益归因：风格 / 行业 / 个股 / 成本 —— 预注册

## 为什么做这一件

`plans/weekly-terminal-plan-20260912.md` §6.1-5 要求终端的归因报告覆盖
**因子 / 行业 / 风格 / 成本**。现状只有 **成本 + 可交易性闸门** 两轴（F4 的四臂分解、
F5 的打板归因），另外三轴是空的 —— 于是有一个必须**定量**回答的疑点：

    §2.10-④ 已登记：2024 年以后对「等权小盘」超额 ≈ 0（对中证800 ≈ +4pp）。
    ⇒ 现役那 +14.6pp 里，到底有多少是**小盘/风格 beta**，多少是**选股**？

不知道这个，终端就没法诚实报预期（把 beta 当 alpha 报，是最典型的自欺）。

## 方法（跑数前冻结，见 §6.2「报告超额必须同时给窗口/基准/成本口径」）

1. 组合臂 = **现役生产构型**：2×W5+pit、100 万、top100、退出带 8×、持仓加成 0.2σ、两套种子；
2. 逐期横截面回归（Fama-MacBeth，每期独立，**决策日**信息）：

       r_i − mean_pool(r) = Σ_k x̃_{i,k}·λ_k + Σ_g d̃_{i,g}·λ_g + ε_i

   · `r_i` = 该期**实际持有窗口**的复权开盘→开盘收益 `op_adj[e_cur]/op_adj[e_prev] − 1`
     （若某期被跳过，窗口自动变长，不用固定 5 日假装对齐）；
   · 池子 = **决策日可交易域**（与引擎基准 `bench_ret` 同口径）；
   · `x̃` = 决策日截面 z 标准化（±3 截尾）；`d̃` = 申万一级 **PIT 时点行业**去均值哑变量
     （无归属单独一个桶，保证覆盖 100%）；
   风格集 9 个：size=log(total_mv)、ep=1/PE、bp=1/PB、dv、mom_20、mom_60、rev_5、vol_20、turn_20。
3. 组合侧贡献 = `Σ_{i∈持仓} w_i·x̃_{i,k} · λ_k`（w=等权）；
   **个股(残差) = 等权持仓超额 − 风格 − 行业**（按构造守恒）；
   **实施差异 = 引擎净值超额 − 等权持仓超额**（现金拖累/整手粒度/闸门/费用）；
4. 再跑一版把**模型打分本身**当第 10 个因子放进回归 ⇒ 看风格贡献里有多少其实由打分解释；
5. 成本轴复核：net / gross(零费零滑点) / no_gates(关涨跌停闸门) 三臂，与 F4 口径一致。

## 预注册判据（决定终端**怎么报数**）

- `|风格合计| ≥ 50% × |总超额|` 且 size 主导风格 ⇒ 判决「**风格依赖**」：
  终端预期必须标注"小盘 beta 依赖"，并开 F25（规模中性对照臂）量纯选股还剩多少；
- `|个股合计| ≥ 60% × |总超额|` ⇒ 判决「**选股 alpha 为主**」；
- 其他 ⇒ 判决「**混合**」。

口径提醒：λ 是估计量，本表是**会计分解**不是显著性检验；列的 t 值只描述贡献时序的噪声。
「pp/年」一律用「平均每期 × 52」的**算术**年化，这样各行可以相加；复利年化另列一行。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F24_attribution.py
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
f22 = _load("f22", HERE / "F22_pair_final_selection.py")   # 只借它的 wr() 融合口径

from app import config as cfg_mod             # noqa: E402
from app.factors import factor_engine as fe   # noqa: E402
from app.factors.holder_factors import sw_industry_map_pit  # noqa: E402
from app.pit.engine import PITEngine          # noqa: E402

log = logging.getLogger("f24")
OUT = HERE / "F24_attribution_result.json"
CAPITAL, TOP_N, BAND, HYST = 1_000_000.0, 100, 8.0, 0.2
SEED_FILES = {"seedA": "F4_scores_seedA.npz", "seedB": "F4_scores_seedB.npz"}
# 核心风格(必定可得, 缺失即剔除该股) + 估值/股息(亏损股/不分红股天然缺失 ⇒ 截面中位数填充,
# 填充率如实报出; 中位数填充对贡献是**保守**的: 填出来的暴露≈池子均值 ⇒ 贡献≈0)
CORE_STYLES = ("size", "mom_20", "mom_60", "vol_20", "turn_20")
FILL_STYLES = ("ep", "bp", "dv", "rev_5")
STYLES = CORE_STYLES + FILL_STYLES
MIN_NAMES = 200          # 横截面样本下限
CLIP = 3.0               # z 值截尾
HAC_LAG = 2
PER_YEAR = 52.0
ZONES = f1.ZONES


def style_panels(m: dict, wk: pd.DatetimeIndex, cols) -> dict:
    """决策日即可算得的 9 个风格暴露（无前视：全部只用 ≤ 锚点日的数据）。"""
    px = (m["close"] * m["adj"]).astype(np.float64)
    r1 = px.pct_change()
    raw = {
        "size": np.log(m["total_mv"].replace(0, np.nan)),
        "ep": 1.0 / m["pe_ttm"].where(m["pe_ttm"] > 0),
        "bp": 1.0 / m["pb"].where(m["pb"] > 0),
        "dv": m["dv_ttm"],
        "mom_20": px / px.shift(20) - 1.0,
        "mom_60": px / px.shift(60) - 1.0,
        "rev_5": -(px / px.shift(5) - 1.0),
        "vol_20": r1.rolling(20).std(),
        "turn_20": m["turnover"].rolling(20).mean(),
    }
    return {k: v.reindex(index=wk, columns=cols).astype(np.float64) for k, v in raw.items()}


def _regress(A: np.ndarray, y: np.ndarray, ok: np.ndarray):
    """y 已去均值；返回 OLS 系数（行数不足/病态返回 None）。"""
    if ok.sum() < MIN_NAMES:
        return None
    Aok, yok = A[ok], y[ok]
    if not np.isfinite(Aok).all() or not np.isfinite(yok).all():
        return None
    beta, *_ = np.linalg.lstsq(Aok, yok, rcond=None)
    return beta


def _design(k: int, styles_np: dict, ind_np: np.ndarray,
            score_np: np.ndarray | None, reg_ok: np.ndarray, use_ind: bool = True):
    """构造设计矩阵：z 标准化风格 + 去均值行业哑变量 (+ 打分)。列名一并返回。

    2026-09-13 独立审查(Claude Code)后修正: FILL 类风格(ep/bp/dv/rev_5)缺失时填中位数
    会把 lambda 向 0 衰减(errors-in-variables), 且误差全灌进 specific ⇒ 现在**再补一列
    "缺失指示"控制项**(在 reg_ok 上中心化), 让 lambda 由有值子样本识别。
    """
    names, cols = [], []
    for name in STYLES:
        v = styles_np[name][k].copy()
        miss = ~np.isfinite(v)
        med = float(np.nanmedian(v[reg_ok]))
        if np.isfinite(med):                    # 估值/股息类: 缺失填截面中位数(缺失率在外面报)
            v[miss] = med
        x = v[reg_ok]
        sd = float(np.nanstd(x))
        if not np.isfinite(sd) or sd <= 1e-12:
            continue
        names.append(name)
        cols.append(np.clip((v - float(np.nanmean(x))) / sd, -CLIP, CLIP))
        if miss[reg_ok].any():
            d = miss.astype(np.float64)
            names.append(f"MISS::{name}")
            cols.append(d - d[reg_ok].mean())
    if len(names) < 5:
        return None
    if use_ind:
        dummies = {}
        bucket = pd.Series(np.where(pd.isna(ind_np[k]), "未归属", ind_np[k]))
        for g, cnt in bucket[reg_ok].value_counts().items():
            if cnt < 5:
                continue
            d = (bucket.to_numpy() == g).astype(np.float64)
            # 无申万归属单独一桶, 但**不计入"行业合计"**(holder_factors 的告警: 无归属≠一个行业)
            dummies[f"UIND::{g}" if g == "未归属" else f"IND::{g}"] = d - d[reg_ok].mean()
        for nm, d in dummies.items():
            names.append(nm)
            cols.append(d)
    if score_np is not None:
        v = score_np[k].copy()
        miss_s = ~np.isfinite(v)
        if miss_s[reg_ok].all():
            return None                          # 整期没有打分 ⇒ 这一臂该期作废(会被计数)
        med_s = float(np.nanmedian(v[reg_ok]))
        if np.isfinite(med_s):                   # PIT 打分有 ~34% 缺失(无申万归属), 同口径处理
            v[miss_s] = med_s
        x = v[reg_ok]
        sd = float(np.nanstd(x))
        if np.isfinite(sd) and sd > 1e-12:
            names.append("信号::模型打分")
            cols.append(np.clip((v - float(np.nanmean(x))) / sd, -CLIP, CLIP))
            if miss_s[reg_ok].any():
                d = miss_s.astype(np.float64)
                names.append("MISS::信号::模型打分")
                cols.append(d - d[reg_ok].mean())
    return names, np.column_stack(cols)


def attribute_seed(df: pd.DataFrame, cp, pos_of, mask_np, op_adj,
                   styles_np, ind_np, score_df) -> dict:
    """逐期归因。返回每期记录（用 numpy 直算，避免 491 次 pandas 开销）。"""
    rows = list(df.itertuples())
    recs = []
    dropped = {"no_pool": 0, "no_hold": 0, "design": 0}   # 静默丢期曾经藏过一个 bug, 现在上报
    present = {n: [0, 0] for n in FILL_STYLES}       # [有值, 样本] ⇒ 缺失率 = 1 − 该比值
    for prev, cur in zip(rows, rows[1:]):
        k = int(prev.anchor_pos)
        e0, e1 = pos_of[prev.date], pos_of[cur.date]
        if e1 <= e0:
            continue
        r = op_adj[e1] / op_adj[e0] - 1.0
        # 收益口径(只看两端有没有价) 与 回归样本(还要求设计矩阵有值) **必须分开** ——
        # 2026-09-13 独立审查: 混用会把"被剔出可交易域的持仓(ST/停牌)收益"整段塞进
        # 实施缺口, 而这批票恰恰是坏消息票 ⇒ 系统性高估 specific、低估 impl_gap。
        ret_ok = np.isfinite(r) & np.isfinite(op_adj[e0]) & (op_adj[e0] > 0)
        pool = mask_np[k] & ret_ok
        if pool.sum() < MIN_NAMES:
            dropped["no_pool"] += 1
            continue
        y = r - float(np.mean(r[pool]))
        hold_idx = np.array([cp[c] for c in prev.picks if c in cp], dtype=int)
        if len(hold_idx) < 20:
            dropped["no_hold"] += 1
            continue
        hold_mask = np.zeros(len(cp), dtype=bool)
        hold_mask[hold_idx] = True
        hold_ret = hold_mask & ret_ok                    # 收益口径的持仓(不被 mask/风格裁剪)
        if hold_ret.sum() < 20:
            dropped["no_hold"] += 1
            continue
        reg_ok = pool.copy()
        for n in CORE_STYLES:                   # 核心风格缺失才剔除该股
            reg_ok &= np.isfinite(styles_np[n][k])
        if reg_ok.sum() < MIN_NAMES:
            dropped["no_pool"] += 1
            continue
        for n in FILL_STYLES:
            fv = np.isfinite(styles_np[n][k])[reg_ok]
            present[n][0] += int(fv.sum())
            present[n][1] += int(reg_ok.sum())
        h_ok = hold_mask & reg_ok
        if h_ok.sum() < 20:
            dropped["no_hold"] += 1
            continue
        out = {"date": str(cur.date), "n_pool": int(pool.sum()), "n_hold": int(len(hold_idx)),
               "hold_cov": round(float(h_ok.sum() / len(hold_idx)), 4),
               "ew_cov": round(float(hold_ret.sum() / len(hold_idx)), 4)}
        base = _design(k, styles_np, ind_np, None, reg_ok)
        full = _design(k, styles_np, ind_np, score_df, reg_ok)
        noid = _design(k, styles_np, ind_np, None, reg_ok, use_ind=False)
        if base is None or full is None or noid is None:
            dropped["design"] += 1
            continue
        bench = float(np.mean(r[pool]))
        out["bench_ret"] = bench
        out["ew_ret"] = float(np.mean(r[hold_ret]))          # 等权持仓收益(全持仓口径)
        out["ew_excess"] = out["ew_ret"] - bench
        out["eng_excess"] = float(cur.ret - bench)           # 引擎净值口径(含现金/整手)
        for tag, (names, A) in (("base", base), ("score", full), ("noid", noid)):
            beta = _regress(A, y, reg_ok)
            if beta is None:
                continue
            contrib = A[h_ok].mean(axis=0) * beta     # 组合暴露(相对池子) × λ
            for j, nm in enumerate(names):
                out[f"{tag}::{nm}"] = float(contrib[j])
            # 逐期残差 = 等权持仓超额 − 该臂全部因子贡献 ⇒ 恒等式**逐期**守恒(不是事后插补)
            keys = [f"{tag}::{nm}" for nm in names if f"{tag}::{nm}" in out]
            out[f"{tag}::__specific__"] = out["ew_excess"] - sum(out[k] for k in keys)
        # 实施缺口拆两半: 现金拖累(生息资产只有 invested 部分) + 权重/整手(等权 vs 实际股数)
        inv_prev = float(getattr(prev, "invested_ratio", np.nan))
        out["invested_prev"] = inv_prev
        out["cash_drag"] = -(1.0 - inv_prev) * out["ew_ret"] if np.isfinite(inv_prev) else np.nan
        recs.append(out)
    return {"recs": recs,
            "dropped_periods": dropped,
            "missing_rate": {n: (round(1 - v[0] / v[1], 4) if v[1] else None)
                             for n, v in present.items()}}


def _zone_slice(recs: list, zone: str) -> list:
    lo, hi = ZONES[zone]
    return [r for r in recs if lo <= r["date"] <= hi]


def summarize(recs: list, tag_prefix: str = "base") -> dict:
    """把一个时期的记录汇总成归因表（pp/年，算术年化，行可相加）。

    2026-09-13 独立审查后: specific 改成**逐期**「等权持仓超额 − 该臂全部列贡献」的均值
    (原来的写法是"总量 − 各分项平均值之和", 支撑期不齐时会静默把差额灌进 specific)。
    """
    n = len(recs)
    empty = {"n": n, "excess_pp_arith": None, "engine_excess_pp_arith": None,
             "style": {}, "style_total_pp": None, "industry": {}, "industry_total_pp": None,
             "signal": {}, "signal_total_pp": None, "missing": {}, "missing_total_pp": None,
             "unmapped": {}, "unmapped_total_pp": None, "specific_pp": None,
             "impl_gap_pp": None, "cash_drag_pp": None, "weight_gap_pp": None,
             "ew_cov": None, "hold_cov": None, "t": {}}
    if n < 5:
        return empty
    keys = sorted({k[len(tag_prefix) + 2:] for r in recs for k in r
                   if k.startswith(tag_prefix + "::") and not k.endswith("__specific__")})

    def mean_pp(key):
        s = pd.Series([r.get(key, np.nan) for r in recs], dtype=float).dropna()
        return None if s.empty else round(100 * 52.0 * float(s.mean()), 3)
    def raw(key):
        return pd.Series([r.get(key, np.nan) for r in recs], dtype=float).dropna()
    def tstat(key, lag: int = HAC_LAG):
        s = pd.Series([r.get(key, np.nan) for r in recs], dtype=float).dropna().to_numpy()
        t = f1b.hac_t(s, lag)
        return None if not np.isfinite(t) else round(float(t), 2)

    style_rows, ind_rows, sig_rows, miss_rows, unmapped_rows = {}, {}, {}, {}, {}
    for key in keys:
        full = f"{tag_prefix}::{key}"
        target = (miss_rows if key.startswith("MISS::")
                  else unmapped_rows if key.startswith("UIND::")
                  else ind_rows if key.startswith("IND::")
                  else sig_rows if key.startswith("信号::") else style_rows)
        target[key] = {
            "pp": mean_pp(full), "t": tstat(full)}
    def tot(rows):
        return round(sum(v["pp"] for v in rows.values() if v["pp"] is not None), 3)

    style_tot, ind_tot = tot(style_rows), tot(ind_rows)
    sig_tot, miss_tot, unmapped_tot = tot(sig_rows), tot(miss_rows), tot(unmapped_rows)
    ew_ex = mean_pp("ew_excess")
    eng_ex = mean_pp("eng_excess")
    specific = mean_pp(f"{tag_prefix}::__specific__")
    impl = None if (eng_ex is None or ew_ex is None) else round(eng_ex - ew_ex, 3)
    cash_drag = mean_pp("cash_drag")
    weight_gap = None if (impl is None or cash_drag is None) else round(impl - cash_drag, 3)
    tser = raw("ew_excess").to_numpy()
    ac1 = (round(float(pd.Series(tser).autocorr(1)), 3) if len(tser) > 5 else None)
    return {"n": n,
            "excess_pp_arith": ew_ex,            # 等权持仓的超额(分解对象)
            "engine_excess_pp_arith": eng_ex,    # 引擎净值超额(含现金/整手/闸门/费用)
            "style": style_rows, "style_total_pp": round(style_tot, 3),
            "industry_total_pp": round(ind_tot, 3), "industry": ind_rows,
            "signal": sig_rows, "signal_total_pp": round(sig_tot, 3),
            "missing": miss_rows, "missing_total_pp": round(miss_tot, 3),
            "unmapped": unmapped_rows, "unmapped_total_pp": round(unmapped_tot, 3),
            "specific_pp": specific, "impl_gap_pp": impl,
            "cash_drag_pp": cash_drag, "weight_gap_pp": weight_gap,
            "ew_cov": round(float(np.mean([r.get("ew_cov", np.nan) for r in recs])), 4),
            "hold_cov": round(float(np.mean([r.get("hold_cov", np.nan) for r in recs])), 4),
            "t": {"ew_excess": tstat("ew_excess"), "eng_excess": tstat("eng_excess"),
                  "ew_excess_lag4": tstat("ew_excess", 4), "ew_excess_lag8": tstat("ew_excess", 8),
                  "specific": tstat(f"{tag_prefix}::__specific__"),
                  "autocorr1_ew_excess": ac1},
            "identity_check_pp": None if ew_ex is None else round(
                ew_ex - style_tot - ind_tot - sig_tot - miss_tot - unmapped_tot
                - (specific or 0.0), 3)}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    cols = list(m["close"].columns)
    cp = {c: j for j, c in enumerate(cols)}
    pit = PITEngine(m=m, cfg=cfg)
    mask = f1.tradable_frame(pit, wk, cols)
    mask_np = mask.fillna(False).to_numpy()
    log.info("交易日 %d | 周锚点 %d | 股票 %d", len(dates), len(wk), len(cols))

    conn = sqlite3.connect(f"file:{BASE/'data'/'neweco.db'}?mode=ro", uri=True)
    PITSC = f17.table_scores(conn, "model_score_die_pit", wk, cols)
    conn.close()
    log.info("风格面板…")
    styles = style_panels(m, wk, cols)
    styles_np = {k: v.to_numpy(dtype=np.float64) for k, v in styles.items()}
    ind = sw_industry_map_pit(wk, "L1").reindex(index=wk, columns=cols)
    ind_np = ind.to_numpy(dtype=object)
    log.info("PIT 行业覆盖 %.0f%%", 100 * float(ind.notna().to_numpy().mean()))
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}

    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "config": {"capital": CAPITAL, "top_n": TOP_N, "band": BAND, "hyst": HYST,
                      "styles": list(STYLES), "clip": CLIP, "min_names": MIN_NAMES},
           "window": {"first_anchor": str(wk[0].date()), "last_anchor": str(wk[-1].date())},
           "seeds": {}, "arms": {}, "verdict": {}, "secs": None}

    for seed, fn in SEED_FILES.items():
        z = np.load(HERE / fn, allow_pickle=True)
        a5 = pd.to_datetime([str(d) for d in z["anchors"]])
        c5 = [str(c) for c in z["cols"]]
        W5 = pd.DataFrame(z["scores"], index=a5, columns=c5).reindex(index=wk, columns=cols)
        S = f22.wr((W5, 2), (PITSC, 1))
        score_np = S.to_numpy(dtype=np.float64)
        df = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, {**cfg, "capital": CAPITAL}, pit, 0,
                                       score_matrix=pd.DataFrame(S, index=wk, columns=cols),
                                       top_n=TOP_N, rank_band=BAND, hyst=HYST)
        if df is None:
            log.error("[%s] 组合臂无输出", seed)
            continue
        att = attribute_seed(df, cp, pos_of, mask_np, op_adj, styles_np, ind_np, score_np)
        recs = att["recs"]
        blocks = {"FULL": recs}
        for zone in ZONES:
            blocks[zone] = _zone_slice(recs, zone)
        out = {"n_periods": len(recs), "turnover": round(float(df["turnover"].mean()), 3),
               "invested": round(float(df["invested_ratio"].mean()), 4),
               "missing_rate": att["missing_rate"],
               "dropped_periods": att["dropped_periods"]}
        for bname, brecs in blocks.items():
            out[bname] = summarize(brecs, "base")
            out[bname + "_with_score"] = summarize(brecs, "score")
            out[bname + "_no_industry"] = summarize(brecs, "noid")
        res["seeds"][seed] = out
        f = out["FULL"]
        log.info("[%s] 期数 %d | 等权持仓超额 %s pp/年 (t=%s) | 引擎净值超额 %s pp/年 | 实施差异 %s",
                 seed, f["n"], f["excess_pp_arith"], f["t"]["ew_excess"],
                 f["engine_excess_pp_arith"], f["impl_gap_pp"])
        log.info("     风格合计 %s | 行业合计 %s | 个股(残差) %s | 恒等式校验 %s | 收益覆盖 %s",
                 f["style_total_pp"], f["industry_total_pp"], f["specific_pp"],
                 f["identity_check_pp"], f["ew_cov"])
        log.info("     实施缺口拆解: 现金拖累 %s | 权重/整手 %s",
                 f["cash_drag_pp"], f["weight_gap_pp"])
        log.info("     风格明细 %s", {k: v["pp"] for k, v in f["style"].items()})
        log.info("     缺失控制项合计 %s | 未归属行业 %s | 缺失率 %s | 丢弃期 %s",
                 f["missing_total_pp"], f["unmapped_total_pp"], att["missing_rate"],
                 att["dropped_periods"])
        log.info("     把打分本身放进回归: 风格 %s | 打分 %s | 缺失项 %s | 个股(残差) %s | 恒等式 %s",
                 out["FULL_with_score"]["style_total_pp"],
                 out["FULL_with_score"]["signal_total_pp"],
                 out["FULL_with_score"]["missing_total_pp"],
                 out["FULL_with_score"]["specific_pp"],
                 out["FULL_with_score"]["identity_check_pp"])
        log.info("     自相关/t 敏感性: lag1ρ=%s | t(lag2)=%s | t(lag4)=%s | t(lag8)=%s",
                 f["t"]["autocorr1_ew_excess"], f["t"]["ew_excess"],
                 f["t"]["ew_excess_lag4"], f["t"]["ew_excess_lag8"])
        log.info("     稳健性(去掉行业哑变量): 风格 %s | 行业 %s | 个股(残差) %s",
                 out["FULL_no_industry"]["style_total_pp"],
                 out["FULL_no_industry"]["industry_total_pp"],
                 out["FULL_no_industry"]["specific_pp"])
        log.info("     DEAD   %s", {k: out["DEAD"][k] for k in
                                    ("n", "excess_pp_arith", "style_total_pp",
                                     "industry_total_pp", "specific_pp")})
        log.info("     ACTIVE %s", {k: out["ACTIVE"][k] for k in
                                    ("n", "excess_pp_arith", "style_total_pp",
                                     "industry_total_pp", "specific_pp")})

    # ---- 成本/闸门轴复核（同窗口、同构型；只跑 seedA 以省机器时间）----
    z = np.load(HERE / SEED_FILES["seedA"], allow_pickle=True)
    a5 = pd.to_datetime([str(d) for d in z["anchors"]])
    c5 = [str(c) for c in z["cols"]]
    W5 = pd.DataFrame(z["scores"], index=a5, columns=c5).reindex(index=wk, columns=cols)
    S = f22.wr((W5, 2), (PITSC, 1))
    for tag, kw in (("net", {}), ("gross", {"gross": True}), ("no_gates", {"no_gates": True})):
        d = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, {**cfg, "capital": CAPITAL}, pit, 0,
                                      score_matrix=pd.DataFrame(S, index=wk, columns=cols),
                                      top_n=TOP_N, rank_band=BAND, hyst=HYST, **kw)
        if d is None:
            continue
        bench = []
        prev_exec, prev_ap = None, None
        for _, r in d.iterrows():
            e = pos_of[r["date"]]
            if prev_exec is None:
                bench.append(np.nan)
            else:
                good = (mask_np[int(prev_ap)] & np.isfinite(op_adj[prev_exec])
                        & np.isfinite(op_adj[e]) & (op_adj[prev_exec] > 0))
                bench.append(float(np.mean(op_adj[e][good] / op_adj[prev_exec][good] - 1))
                             if good.sum() >= MIN_NAMES else np.nan)
            prev_exec, prev_ap = e, int(r["anchor_pos"])
        d["bench_ret"] = bench
        okrows = d["ret"].notna() & d["bench_ret"].notna()
        a_ = f1b.ann(d.loc[okrows, "ret"], PER_YEAR)
        b_ = f1b.ann(d.loc[okrows, "bench_ret"], PER_YEAR)
        res["arms"][tag] = {"n": int(okrows.sum()), "excess_pp": round(100 * (a_ - b_), 2),
                            "turnover": round(float(d["turnover"].mean()), 3),
                            "invested": round(float(d["invested_ratio"].mean()), 4)}
        log.info("[arm %s] 超额 %+.2fpp | 换手 %.3f | 投入率 %.3f",
                 tag, res["arms"][tag]["excess_pp"], res["arms"][tag]["turnover"],
                 res["arms"][tag]["invested"])
    if "net" in res["arms"] and "gross" in res["arms"]:
        res["arms"]["friction_pp"] = round(
            res["arms"]["gross"]["excess_pp"] - res["arms"]["net"]["excess_pp"], 2)
    if "net" in res["arms"] and "no_gates" in res["arms"]:
        res["arms"]["gate_pp"] = round(
            res["arms"]["no_gates"]["excess_pp"] - res["arms"]["net"]["excess_pp"], 2)

    # ---- 预注册判据 ----
    def _verdict(f: dict, tag: str = "") -> dict:
        tot, sty, spec = f["excess_pp_arith"], f["style_total_pp"], f["specific_pp"]
        size = f["style"].get("size", {}).get("pp")
        t_tot = f["t"].get("ew_excess")
        # 2026-09-13 独立审查: 份额判据在小分母下没有意义 ⇒ 先过"绝对量 + 显著性"下限
        if (tot is None or sty is None or spec is None or abs(tot) < 3.0
                or t_tot is None or abs(t_tot) < 2.0):
            v = "insufficient"
        elif abs(sty) >= 0.5 * abs(tot) and size is not None and abs(size) >= 0.5 * abs(sty):
            v = "风格依赖"
        elif abs(spec) >= 0.6 * abs(tot):
            v = "选股 alpha 为主"
        else:
            v = "混合"
        return {"verdict": v, "zone": tag, "excess_pp": tot, "t": t_tot,
                "style_pp": sty, "size_pp": size,
                "style_share": None if (tot in (None, 0) or sty is None)
                else round(sty / tot, 3),
                "specific_share": None if (tot in (None, 0) or spec is None)
                else round(spec / tot, 3),
                "specific_pp": spec}

    verdicts = {}
    for seed, o in res["seeds"].items():
        verdicts[seed] = {z: _verdict(o[z], z) for z in ("FULL", "DEAD", "ACTIVE")}
    res["verdict"] = verdicts
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 判决 %s | %.1fs → %s ==", verdicts, res["secs"], OUT)


if __name__ == "__main__":
    main()
