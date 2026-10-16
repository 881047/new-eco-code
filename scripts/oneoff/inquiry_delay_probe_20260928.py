# -*- coding: utf-8 -*-
"""问询回复延迟因子探针 —— plans/inquiry-delay-probe-prereg-20260928.md 的跑数器。

构造/判据全部冻结在预注册。与 text_factors_eval_20260927 同机器同口径：
中性化（生产写入口径）→ gate.evaluate → verdict_single（S1-S3）
→ S4 与 31 在池去相关（秩相关，隔 2 锚点抽样）。
只读研究层，零写库。产物 experiments/inquiry_delay_probe_20260928.json。
"""
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app.alpha import gate
from app.factors import factor_engine as fe
from app.models import trainer as T
from app.pit.engine import PITEngine

ROOT = Path(__file__).resolve().parents[2]
TAGGED = ROOT / "data" / "text_corpus" / "tagged"
OUT = ROOT / "experiments" / "inquiry_delay_probe_20260928.json"
PREREG = "plans/inquiry-delay-probe-prereg-20260928.md"
WINDOW_DAYS = 365
CLIP_HI = 365
MIN_COVERAGE = 0.05          # 预注册 G：截面覆盖率中位数门槛


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def to_ts_code(code: str) -> str:
    c = str(code).strip()
    if c.endswith((".SZ", ".SH", ".BJ")):
        return c
    return c + (".SH" if c.startswith("6") else ".BJ" if c[0] in "489" else ".SZ")


def load_inquiry_pairs() -> pd.DataFrame:
    """问询发起/回复配对 → (ts_code, reply_t, delay_days)。"""
    has_ask = re.compile(r"问询")
    has_reply = re.compile(r"回复")
    parts = []
    for p in sorted(TAGGED.glob("*.parquet")):
        try:
            df = pd.read_parquet(p, columns=["代码", "公告标题", "公告时间"])
        except Exception as e:  # noqa: BLE001
            log(f"读取失败 {p.name}: {e}")
            continue
        t = df["公告标题"].astype(str)
        m_ask = t.str.contains(has_ask) & ~t.str.contains(has_reply)
        m_rep = t.str.contains(has_ask) & t.str.contains(has_reply)
        if m_ask.any() or m_rep.any():
            parts.append(df.loc[m_ask | m_rep].assign(kind=np.where(m_rep[m_ask | m_rep],
                                                                    "reply", "ask")))
    df = pd.concat(parts, ignore_index=True)
    df["ts_code"] = df["代码"].map(to_ts_code)
    df["t"] = pd.to_datetime(df["公告时间"], errors="coerce")
    df = df.dropna(subset=["t"]).sort_values(["ts_code", "t", "kind"])
    log(f"问询类公告 {len(df):,} 条（ask/reply 混合）")
    rows = []
    for code, g in df.groupby("ts_code"):
        ask_t = None
        for _, r in g.iterrows():
            if r["kind"] == "ask":
                ask_t = r["t"]
            elif ask_t is not None:
                delay = (r["t"] - ask_t).days
                if 1 <= delay <= CLIP_HI * 3:      # 粗上限防错配，构造层 clip
                    rows.append({"ts_code": code, "reply_t": r["t"],
                                 "delay": float(min(delay, CLIP_HI))})
                ask_t = None                       # 消费掉，防一笔问询重复配对
    out = pd.DataFrame(rows).sort_values(["ts_code", "reply_t"])
    log(f"配对完成 {len(out):,} 对（{out['ts_code'].nunique()} 只股票）")
    return out


def build_panel(pairs: pd.DataFrame, anchors: pd.DatetimeIndex,
                ref_codes: list) -> pd.DataFrame:
    """锚点 × 股票 → 窗口内最近一次配对延迟。"""
    codes = sorted(set(ref_codes))
    code_ix = {c: i for i, c in enumerate(codes)}
    arr = np.full((len(anchors), len(codes)), np.nan)
    by_stock = {c: g.sort_values("reply_t") for c, g in pairs.groupby("ts_code")
                if c in code_ix}
    for r, a in enumerate(anchors):
        a64 = np.int64(a.value)
        lo = a64 - WINDOW_DAYS * 86400 * 10**9
        for c, g in by_stock.items():
            ts = g["reply_t"].values.astype("datetime64[ns]").astype("int64")
            i1 = np.searchsorted(ts, a64 + 86400 * 10**9)   # PIT：公告日 ≤ 锚点次日
            i0 = np.searchsorted(ts, lo)
            if i1 > i0:
                arr[r, code_ix[c]] = g["delay"].iloc[i1 - 1]  # 最近一次
    return pd.DataFrame(arr, index=anchors, columns=codes)


def main() -> int:
    t0 = time.time()
    wide31 = T._load_factors_wide()
    ref_index = pd.to_datetime(wide31.index)
    ref_codes = sorted(set(wide31.columns.get_level_values("ts_code")))
    log(f"参考宽表 {wide31.shape}")

    pairs = load_inquiry_pairs()
    panel = build_panel(pairs, ref_index, ref_codes).reindex(index=ref_index,
                                                             columns=ref_codes)
    cov = panel.notna().mean(axis=1)
    cov_med = float(cov.median())
    log(f"覆盖率中位 {cov_med:.3%}（门槛 {MIN_COVERAGE:.0%}）")

    from app import db
    db.init_db()
    with db.conn_ctx() as conn:
        industry = fe.industry_map(conn)
    sr = float(fe.sparse_ratio(panel))
    mode = "sparse" if sr < fe.SPARSE_NONZERO_RATIO else "dense"
    out = (fe.neutralize_sparse(panel, industry) if mode == "sparse"
           else fe.neutralize(panel, industry))
    log(f"sparse_ratio {sr:.3f} → {mode}")

    m = fe.load_market()
    md_all = fe.month_end_dates(pd.DatetimeIndex(m["close"].index))
    md_eval = pd.DatetimeIndex(md_all[md_all >= pd.Timestamp("2019-01-01")])
    zones = gate.zone_of(md_eval)
    fwd = gate.forward_return(m, gate.HORIZON)
    tradable = PITEngine(m=m).tradable_matrix(md_eval)

    pm = out.reindex(index=sorted(set(md_eval) & set(out.index)), columns=out.columns)
    pm = pm.where(tradable.reindex(index=md_eval, columns=pm.columns).fillna(False))
    det = gate.evaluate(pm, fwd, md_eval, zones)
    res = {"prereg": PREREG, "window_days": WINDOW_DAYS, "clip_hi": CLIP_HI,
           "n_pairs": int(len(pairs)), "n_stocks": int(pairs["ts_code"].nunique()),
           "coverage_median": round(cov_med, 4), "coverage_gate_pass": bool(cov_med >= MIN_COVERAGE),
           "sparse_ratio": round(sr, 4), "neutralize_mode": mode, "gate": {}}
    if det.empty:
        res["gate"] = {"note": "无有效截面"}
    else:
        dead, act = det[det.zone == "DEAD"], det[det.zone == "ACTIVE"]
        h_d, h_a = float(dead.head_excess.mean()), float(act.head_excess.mean())
        t_d = float(h_d / (dead.head_excess.std(ddof=1) / np.sqrt(len(dead))))
        v = gate.verdict_single(h_d, t_d, h_a, None)
        res["gate"] = {"dead_head": round(h_d, 4), "dead_t": round(t_d, 2),
                       "active_head": round(h_a, 4), "same_sign": bool(h_d * h_a > 0),
                       "n_dead": int(len(dead)), "n_active": int(len(act)), **v}
        log(str(res["gate"]))

        # S4：与 31 在池秩相关（隔 2 锚点）
        sub_anchors = list(md_eval[::2])
        wide_r = wide31.reorder_levels(["factor", "ts_code"], axis=1).sort_index(axis=1)
        pool31 = sorted(set(wide31.columns.get_level_values("factor")))
        rhos = {f: [] for f in pool31}
        for dt in sub_anchors:
            if dt not in out.index:
                continue
            ra = out.loc[dt].rank()
            for f in pool31:
                sub = wide_r.xs(f, axis=1, level="factor")
                sub.index = pd.to_datetime(sub.index)
                if dt not in sub.index:
                    continue
                j = pd.concat([ra.rename("a"), sub.loc[dt].rank().rename("b")], axis=1).dropna()
                if len(j) >= 200:
                    rhos[f].append(abs(j["a"].corr(j["b"])))
        s4 = {f: round(float(np.mean(v)), 3) for f, v in rhos.items() if v}
        res["s4_max_abs_rho_vs_pool31"] = {k: v for k, v in sorted(s4.items(),
                                                 key=lambda kv: -kv[1])[:5]}
        mx = max(s4.values()) if s4 else float("nan")
        res["s4_pass"] = bool(mx <= 0.7)
        res["s4_max"] = round(mx, 3) if mx == mx else None

    res["verdict"] = ("覆盖不足 ⇒ 线结清（数据不支持，非因子无效）"
                      if not res["coverage_gate_pass"] else
                      ("S 判据见 gate 段；过闸 ⇒ 组合层 A/B 另立预注册"
                       if res["gate"].get("pass") else "未过单因子档 ⇒ 本构造下未证实"))
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
