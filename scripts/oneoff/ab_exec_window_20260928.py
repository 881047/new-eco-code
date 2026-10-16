"""P1 执行窗口 A/B（只改成交时点，不动打分表 ⇒ 不换 pin、不重训）

预注册：plans/exec-window-ab-prereg-20260928.md
产物：experiments/ab_exec_window_20260928.json

四臂 × 双窗：
    base  ""      现役 pin 复现（缺省路径，全局 FILL_AT_OPEN=True）
    open  "open"  开盘价  —— **控制臂**（现役 pin 同口径）
    close "close" 收盘价  —— 历史对照（G0b 交叉校验，不参与准入判决）
    vwapt "vwap_tail" 尾盘 1h VWAP（min60，2020-01 起）
    vwap  "vwap"  全日 VWAP = amount*10/vol（daily 全史，W2 的廉价代理）

⚠️ 与上游计划的一处实质偏离：计划把 P1 排在 P1.5 之前，控制臂定义为"收盘价"；
实际 P1.5 先落地，现役 pin 已是开盘价撮合 ⇒ 控制臂改为开盘价，否则 Δ 测的是
相对一个已废弃的口径。详见预注册 §0 偏离 1。

用法：
    .venv/Scripts/python.exe -X utf8 scripts/oneoff/ab_exec_window_20260928.py
"""
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import db                      # noqa: E402
from app.backtest import engine as E    # noqa: E402

OUT = ROOT / "experiments" / "ab_exec_window_20260928.json"
MIN60_DIR = ROOT / "data" / "baostock_min60"
P2020 = "20200101"
TAIL_TIME = "150000000"          # min60 的 time = YYYYMMDD + HHMMSSmmm（9 位时刻）
G1_MEDIAN_BP = 50.0              # 差异中位数 >50bp ⇒ 数据不可用
G1_OUTLIER_FRAC = 0.01           # 异常格（|差|>5%）占比 >1% ⇒ 数据不可用
G2_ANN_PROD = 0.020              # +2.0pp
G2_ANN_FULL = 0.010              # +1.0pp
G2_MDD_PROD = 0.020              # 回撤改善 ≥2pp
G2_MDD_FULL = 0.030              # 回撤改善 ≥3pp
G2_TURNOVER = 0.050              # 换手恶化 ≤+5pp
STOP_LOSS_PP = 0.010             # Δ 均 <+1.0pp ⇒ 执行窗口作为 alpha 来源结清


class LazyMin60Tail:
    """未复权尾盘 1h VWAP 查找表：get((ts_code, "YYYYMMDD")) -> float

    懒加载：首次查询某只股票时才读它的 parquet（全量 5,446 只常驻不必要）。
    内部按股票分桶 `_by_code`，供 G1 自检按只遍历。生产路径恒不注入。
    min60 是 adjustflag=3（**不复权**），与 daily 裸价同基准 ⇒ 下游绝不二次乘 adj。
    """

    def __init__(self, root: Path):
        self.root = root
        self._by_code: dict = {}
        self._loaded: set = set()
        self.n_files = 0
        # 命中率统计（防假盈亏：必须证明 W2 臂真的用上了 min60 价，而不是大面积顺延）
        self.n_hit = 0
        self.n_miss = 0

    def _load(self, code: str) -> None:
        if code in self._loaded:
            return
        self._loaded.add(code)
        f = self.root / f"{code}.parquet"
        if not f.exists():
            self._by_code[code] = {}
            return
        d = pd.read_parquet(f, columns=["date", "time", "amount", "volume"])
        d = d[d["time"].astype(str).str.endswith(TAIL_TIME)]
        if d.empty:
            self._by_code[code] = {}
            return
        vol = d["volume"].to_numpy()
        amt = d["amount"].to_numpy()
        v = np.where(vol > 0, amt / np.where(vol > 0, vol, 1.0), np.nan)
        per = {}
        for dt, val in zip(d["date"].astype(str).str.replace("-", "", regex=False), v):
            if np.isfinite(val) and val > 0:
                per[dt] = float(val)
        self._by_code[code] = per
        self.n_files += 1

    def per_code(self, code: str) -> dict:
        """触发懒加载并返回该股票的全部 {(YYYYMMDD): VWAP}。"""
        self._load(code)
        return self._by_code.get(code, {})

    def get(self, key, default=None):
        code, dt = key
        self._load(code)
        v = self._by_code.get(code, {}).get(dt)
        if v is None:
            self.n_miss += 1
            return default
        self.n_hit += 1
        return v

    def hit_rate(self) -> float:
        tot = self.n_hit + self.n_miss
        return round(self.n_hit / tot, 4) if tot else float("nan")


def _g1_selfcheck(tail: LazyMin60Tail, m: dict, n_sample: int = 300,
                  cap: int = 60000) -> dict:
    """G1 单位/量级自检：尾盘 1h VWAP 与当日**未复权**收盘价的差异分布。

    门槛（预注册 §2.2）：差异中位数 >50bp 或 |差|>5% 的异常格 >1% ⇒ 判数据不可用。
    用 numpy 位置索引取价（逐格 .loc 在 10^5 量级上太慢）。
    """
    close = m["close"]
    dpos = {d: i for i, d in enumerate(close.index.strftime("%Y%m%d"))}
    cpos = {c: i for i, c in enumerate(close.columns)}
    vals = close.to_numpy()
    diffs, n_abn, n_tot = [], 0, 0
    for code in list(close.columns[:n_sample]):
        per = tail.per_code(code)
        if not per:
            continue
        j = cpos[code]
        for dt, v in per.items():
            i = dpos.get(dt)
            if i is None:
                continue
            cv = vals[i, j]
            if not np.isfinite(cv) or cv <= 0:
                continue
            d = v / float(cv) - 1.0
            diffs.append(d)
            n_tot += 1
            if abs(d) > 0.05:
                n_abn += 1
        if n_tot >= cap:
            break
    if not diffs:
        return {"ok": False, "reason": "无可用对照样本", "n": 0}
    arr = np.asarray(diffs)
    med_bp = float(np.median(np.abs(arr)) * 1e4)
    frac = float(n_abn / max(1, n_tot))
    return {"ok": bool(med_bp <= G1_MEDIAN_BP and frac <= G1_OUTLIER_FRAC),
            "median_abs_bp": round(med_bp, 2), "median_abs_bp_bar": G1_MEDIAN_BP,
            "outlier_frac": round(frac, 5), "outlier_frac_bar": G1_OUTLIER_FRAC,
            "outlier_def": "|vwap/close-1|>5%", "n": int(n_tot),
            "mean_signed_bp": round(float(np.mean(arr) * 1e4), 2),
            "p95_abs_bp": round(float(np.percentile(np.abs(arr), 95) * 1e4), 2)}


def _nav_series(run_date: str) -> pd.Series:
    """按 run_date 取系统净值序列（索引=trade_date）。配对必须按 trade_date，
    绝不能用 ORDER BY nav（顺序本身就是数据）。"""
    conn = sqlite3.connect(f"{(ROOT / 'data' / 'neweco.db').as_uri()}?mode=ro", uri=True)
    try:
        df = pd.read_sql(
            "SELECT trade_date, sys_nav FROM backtest_nav WHERE run_date=? ORDER BY trade_date",
            conn, params=(run_date,))
    finally:
        conn.close()
    df["trade_date"] = df["trade_date"].astype(str)
    return pd.Series(df["sys_nav"].to_numpy(), index=df["trade_date"])


def _paired_t(a: pd.Series, b: pd.Series) -> dict:
    """逐期月度收益的配对差 t 检验（a - b），按 trade_date 严格对齐。"""
    idx = a.index.intersection(b.index)
    if len(idx) < 3:
        return {"n": int(len(idx)), "t": None, "mean_pp": None, "sign_agree": None}
    ra = a.loc[idx].astype(float).pct_change().dropna()
    rb = b.loc[idx].astype(float).pct_change().dropna()
    idx2 = ra.index.intersection(rb.index)
    d = (ra.loc[idx2] - rb.loc[idx2]).to_numpy()
    if len(d) < 3 or np.std(d, ddof=1) == 0:
        return {"n": int(len(d)), "t": None, "mean_pp": None, "sign_agree": None}
    t = float(np.mean(d) / (np.std(d, ddof=1) / np.sqrt(len(d))))
    return {"n": int(len(d)), "t": round(t, 3),
            "mean_pp": round(float(np.mean(d)) * 100, 4),
            "sign_agree": round(float(np.mean(np.sign(d) == np.sign(np.mean(d)))), 4)}


def main() -> int:
    print("=" * 92)
    print("执行窗口 A/B —— 预注册 plans/exec-window-ab-prereg-20260928.md")
    print("控制臂 = 开盘价（现役 pin 20260928_exe 同口径）；Δ 均相对控制臂")
    print("=" * 92)

    db.init_db()
    from app.factors.factor_engine import load_market
    m = load_market()
    print(f"市场矩阵: {len(m['close'])} 日 × {len(m['close'].columns)} 只; "
          f"amount 字段 {'在' if 'amount' in m else '缺失'}")

    tail = LazyMin60Tail(MIN60_DIR)
    E.set_min60_tail(tail)

    g1 = _g1_selfcheck(tail, m)
    print(f"[G1] 尾盘 VWAP 自检: ok={g1.get('ok')} 中位差 {g1.get('median_abs_bp')}bp "
          f"异常格 {g1.get('outlier_frac')} n={g1.get('n')}")

    # (臂名, exec_window, 窗) —— 窗 None=全史（现役评估窗）, P2020=2020 起（W2 可测最大窗）
    runs = [
        ("base", "", None), ("base", "", P2020),
        ("open", "open", None), ("open", "open", P2020),
        ("close", "close", None), ("close", "close", P2020),
        ("vwap", "vwap", None), ("vwap", "vwap", P2020),
        ("vwap_tail", "vwap_tail", P2020),
    ]
    out = {}
    for arm, w, win in runs:
        tag = f"exwin_{arm}_{'full' if win is None else 'p2020'}"
        met = E.run_backtest(score_table="model_score", run_tag=tag,
                             start=win, exec_window=w)
        key = f"{arm}_{'full' if win is None else 'p2020'}"
        out[key] = {
            "arm": arm, "exec_window": w, "window": ("full" if win is None else "p2020"),
            "run_tag": tag, "run_date": met["run_date"],
            "periods": int(met["periods"]),
            "sys_ann": float(met["sys_ann_return"]),
            "sys_mdd": float(met["sys_max_drawdown"]),
            "sys_mdd_daily": float(met.get("sys_mdd_daily") or float("nan")),
            "ann": float(met["ann_return"]), "mdd": float(met["max_drawdown"]),
            "turnover": float(met["avg_turnover"]) if met.get("avg_turnover") is not None else None,
            "ic": float(met["ic_mean"]) if met.get("ic_mean") is not None else None,
            "avg_stocks": float(met.get("avg_stocks") or float("nan")),
            "avg_idle": float(met.get("avg_cash_idle") or float("nan")),
        }
        print(f"  {key:16s} 期数 {out[key]['periods']:3d}  "
              f"sys_ann {out[key]['sys_ann']*100:7.2f}%  sys_mdd {out[key]['sys_mdd']*100:7.2f}%  "
              f"换手 {out[key]['turnover']}")

    E.set_min60_tail(None)

    # ---- G0：缺省路径 与 显式 open 必须逐位一致 ----
    keys = ["sys_ann", "sys_mdd", "ann", "mdd", "turnover", "ic", "avg_stocks"]
    g0 = {k: {"base_full": out["base_full"][k], "open_full": out["open_full"][k],
              "identical": out["base_full"][k] == out["open_full"][k]} for k in keys}
    g0_ok = all(v["identical"] for v in g0.values())
    print(f"[G0] 缺省路径 vs 显式 open 逐位一致: {g0_ok}")

    # ---- G0b：close vs open 的 Δ 应与 E6 同号（open 优于 close ⇒ Δ<0）----
    d_close_full = out["close_full"]["sys_ann"] - out["open_full"]["sys_ann"]
    g0b_ok = bool(d_close_full < 0)
    print(f"[G0b] close-open 全史 Δsys_ann = {d_close_full*100:+.2f}pp（E6 同号判据：<0）")

    # ---- 配对差 t 检验（按 trade_date 严格配对）----
    def nav(key):
        return _nav_series(out[key]["run_date"])

    paired = {}
    for win in ("full", "p2020"):
        ck = f"open_{win}"
        for arm in ("close", "vwap", "vwap_tail"):
            ak = f"{arm}_{win}"
            if ak not in out:
                continue
            paired[ak] = _paired_t(nav(ak), nav(ck))
            print(f"  [配对] {ak} vs {ck}: t={paired[ak]['t']} "
                  f"mean={paired[ak]['mean_pp']}pp n={paired[ak]['n']}")

    # ---- G2 准入（相对控制臂，p2020 同窗）----
    ctrl = out["open_p2020"]
    gate = {}
    for arm in ("vwap_tail", "vwap", "close"):
        k = f"{arm}_p2020"
        if k not in out:
            continue
        a = out[k]
        d_ann = a["sys_ann"] - ctrl["sys_ann"]
        d_mdd = a["sys_mdd"] - ctrl["sys_mdd"]          # 正=改善
        d_to = (a["turnover"] or 0) - (ctrl["turnover"] or 0)
        pt = paired.get(k, {})
        t_ok = pt.get("t") is not None and abs(pt["t"]) >= 2
        sign_ok = pt.get("mean_pp") is not None and np.sign(pt["mean_pp"]) == np.sign(d_ann)
        gate[arm] = {
            "d_sys_ann_prod_pp": round(d_ann * 100, 3),
            "d_sys_mdd_prod_pp": round(d_mdd * 100, 3),
            "d_turnover_pp": round(d_to * 100, 3),
            "paired_t": pt.get("t"), "paired_t_ok": bool(t_ok),
            "sign_consistent": bool(sign_ok),
            "g2_ann": bool(d_ann >= G2_ANN_PROD),
            "g2_mdd": bool(d_mdd >= G2_MDD_PROD),
            "g2_turnover": bool(d_to <= G2_TURNOVER),
            "PASS": bool(d_ann >= G2_ANN_PROD and d_mdd >= G2_MDD_PROD
                         and d_to <= G2_TURNOVER and t_ok and sign_ok),
        }

    # 双窗同号（vwap 有全史与 p2020 两个窗）
    dual = {}
    for arm in ("vwap", "close"):
        if f"{arm}_full" in out and f"{arm}_p2020" in out:
            d_f = out[f"{arm}_full"]["sys_ann"] - out["open_full"]["sys_ann"]
            d_p = out[f"{arm}_p2020"]["sys_ann"] - out["open_p2020"]["sys_ann"]
            dual[arm] = {"d_full_pp": round(d_f * 100, 3), "d_p2020_pp": round(d_p * 100, 3),
                         "same_sign": bool(np.sign(d_f) == np.sign(d_p))}

    best = max((v["d_sys_ann_prod_pp"] for v in gate.values()), default=float("nan"))
    verdict = {
        "G0_bitwise_identical": bool(g0_ok),
        "G0b_close_worse_than_open": bool(g0b_ok),
        "G1_data_usable": bool(g1.get("ok", False)),
        "G2_pass": {k: bool(v["PASS"]) for k, v in gate.items()},
        "dual_window_same_sign": dual,
        "best_delta_pp": best,
        "stop_loss_triggered": bool(best < STOP_LOSS_PP * 100),
        "conclusion": None,
    }
    if not g0_ok:
        verdict["conclusion"] = "G0 未过：缺省路径与显式 open 不逐位一致 ⇒ 本轮作废，先修引擎"
    elif not g1.get("ok", False):
        verdict["conclusion"] = "G1 未过：尾盘 VWAP 数据不可用 ⇒ W2 判「未测」而非判负"
    elif any(v["PASS"] for v in gate.values()):
        w = [k for k, v in gate.items() if v["PASS"]]
        verdict["conclusion"] = f"G2 通过：{w} ⇒ 可进 P4 组合层准入"
    elif verdict["stop_loss_triggered"]:
        verdict["conclusion"] = ("止损：所有臂 Δ 均 <+1.0pp ⇒ 执行窗口作为 alpha 来源结清"
                                 "（读数转为口径测量性质，不采纳为改进动作）")
    else:
        verdict["conclusion"] = "未达 G2 门槛但 Δ≥+1.0pp ⇒ 记 INDETERMINATE，不采纳、不结清"

    result = {"ran_at": pd.Timestamp.now().isoformat(timespec="seconds"),
              "prereg": "plans/exec-window-ab-prereg-20260928.md",
              "note": ("控制臂 = 开盘价（现役 pin 20260928_exe 同口径）。偏离计划：原控制臂"
                       "为收盘价，但 P1.5 已先落地 ⇒ 收盘价降级为历史对照（G0b），不参与准入。"),
              "min60_files_loaded": int(tail.n_files),
              "tail_hit_rate": tail.hit_rate(),
              "tail_n_hit": int(tail.n_hit), "tail_n_miss": int(tail.n_miss),
              "g1_selfcheck": g1, "g0_bitwise": g0, "arms": out,
              "paired_tests": paired, "gate": gate, "verdict": verdict}
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[G1b] 尾盘价命中率 {tail.hit_rate()}"
          f"（hit {tail.n_hit} / miss {tail.n_miss}）—— 低于 0.8 说明 W2 大量顺延，读数不可信")
    print("=" * 92)
    print(f"结论: {verdict['conclusion']}")
    print(f"产物: {OUT}")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
