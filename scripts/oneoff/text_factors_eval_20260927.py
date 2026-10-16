# -*- coding: utf-8 -*-
"""公告文本因子族构造 + 门禁读数 —— `plans/text-factor-llm-labeling-prereg-20260927.md`
§七 的执行体（构造前锁定）。

五族七因子（text_n1m/n3m/n6m, text_netflow3m, text_earn3m, text_novel3m, text_gap），
PIT = 公告时间 ≤ 锚点，中性化按生产写入口径（稀疏守卫），判读 = gate.evaluate
（md_eval ≥ 2019-01）→ verdict_single（S1-S4，S4 与 31 在池去重）。
只读研究层，零写库。产物 experiments/text_factors_eval_20260927.json +
data/text_corpus/factors_v1.parquet（长表留档）。
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
OUT = ROOT / "experiments" / "text_factors_eval_20260927.json"
PANEL_OUT = ROOT / "data" / "text_corpus" / "factors_v1.parquet"
PREREG = "plans/text-factor-llm-labeling-prereg-20260927.md"

EARN_POS = re.compile(r"预增|略增|扭亏|扭盈")
EARN_NEG = re.compile(r"预减|略减|首亏|续亏|预亏")
STRIP = re.compile(r"[0-9０-９一二三四五六七八九十]+|年|月|日|第|届|次|\s+")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def to_ts_code(code: str) -> str:
    c = str(code).strip()
    if c.endswith((".SZ", ".SH", ".BJ")):
        return c
    return c + (".SH" if c.startswith("6") else ".BJ" if c[0] in "489" else ".SZ")


def load_tagged() -> pd.DataFrame:
    parts = []
    for p in sorted(TAGGED.glob("*.parquet")):
        try:
            parts.append(pd.read_parquet(p))
        except Exception as e:  # noqa: BLE001
            log(f"读取失败 {p.name}: {e}")
    df = pd.concat(parts, ignore_index=True)
    df["ts_code"] = df["代码"].map(to_ts_code)
    df["t"] = pd.to_datetime(df["公告时间"], errors="coerce")
    df = df.dropna(subset=["t"])
    df["norm_title"] = df["公告标题"].astype(str).str.replace(STRIP, "", regex=True)
    return df[["ts_code", "t", "category", "valence", "norm_title"]]


def build_panels(df: pd.DataFrame, anchors: pd.DatetimeIndex,
                 ref_codes: list) -> dict:
    """→ {因子名: (锚点 × 股票) 面板}。锚点取 2019-01 起与 wide31 交集。"""
    anchors = pd.DatetimeIndex([a for a in anchors if a >= pd.Timestamp("2019-01-01")])
    codes = sorted(set(ref_codes))
    code_ix = {c: i for i, c in enumerate(codes)}
    wins = {"text_n1m": 30, "text_n3m": 91, "text_n6m": 182}
    fam = {k: np.full((len(anchors), len(codes)), np.nan) for k in
           list(wins) + ["text_netflow3m", "text_earn3m", "text_novel3m", "text_gap"]}
    gap_vals = {c: [] for c in codes}

    by_stock = {c: g.sort_values("t") for c, g in df.groupby("ts_code") if c in code_ix}
    log(f"有公告的股票 {len(by_stock)} / 宇宙 {len(codes)} | 公告 {len(df):,} 条")
    for r, a in enumerate(anchors):
        a64 = np.int64(a.value)
        for c, g in by_stock.items():
            j = code_ix[c]
            times = g["t"].values.astype("datetime64[ns]").astype("int64")
            lo = a64 - 182 * 86400 * 10**9
            i0, i1 = np.searchsorted(times, lo), np.searchsorted(times, a64 + 86400 * 10**9)
            if i1 <= i0:
                # 稀缺性: 距上一条公告天数（窗口外也统计）
                if i0 > 0:
                    fam["text_gap"][r, j] = (a64 - times[i0 - 1]) / 86400 / 10**9
                continue
            w = g.iloc[i0:i1]
            in3 = (w["t"] >= a - pd.Timedelta(days=91)).to_numpy()
            in1 = (w["t"] >= a - pd.Timedelta(days=30)).to_numpy()
            fam["text_n6m"][r, j] = len(w)
            fam["text_n3m"][r, j] = int(in3.sum())
            fam["text_n1m"][r, j] = int(in1.sum())
            w3 = w[in3]
            if len(w3):
                pos = int((w3["valence"] == "pos").sum())
                neg = int((w3["valence"] == "neg").sum())
                fam["text_netflow3m"][r, j] = pos - neg
                ew = w3["公告标题"].astype(str) if "公告标题" in w3.columns else w3["norm_title"]
                fam["text_earn3m"][r, j] = (int(ew.str.contains(EARN_POS).sum())
                                            - int(ew.str.contains(EARN_NEG).sum()))
                nt = w3["norm_title"]
                fam["text_novel3m"][r, j] = 1.0 - nt.nunique() / len(nt)
            if i0 > 0:
                fam["text_gap"][r, j] = (a64 - times[i0 - 1]) / 86400 / 10**9
    return {k: pd.DataFrame(v, index=anchors, columns=codes) for k, v in fam.items()}


def main() -> int:
    t0 = time.time()
    wide31 = T._load_factors_wide()
    ref_index = pd.to_datetime(wide31.index)
    ref_codes = sorted(set(wide31.columns.get_level_values("ts_code")))
    log(f"参考宽表 {wide31.shape}, 锚点 {len(ref_index)}")

    df = load_tagged()
    log(f"打标语料 {len(df):,} 条")
    panels = build_panels(df, ref_index, ref_codes)

    # 中性化（生产写入口径）+ 稀疏守卫
    from app import db
    db.init_db()
    with db.conn_ctx() as conn:
        industry = fe.industry_map(conn)
    final, audit = {}, []
    for name, p in panels.items():
        p = p.reindex(index=ref_index, columns=ref_codes)
        sr = float(fe.sparse_ratio(p))
        mode = "sparse" if sr < fe.SPARSE_NONZERO_RATIO else "dense"
        out = (fe.neutralize_sparse(p, industry) if mode == "sparse"
               else fe.neutralize(p, industry))
        final[name] = out
        audit.append({"factor": name, "sparse_ratio": round(sr, 4),
                      "neutralize_mode": mode,
                      "nonnull": int(out.notna().to_numpy().sum())})
        log(f"{name}: sparse_ratio {sr:.3f} → {mode}")

    # 留档长表
    long_rows = []
    for name, p in final.items():
        s = p.stack().rename("value").reset_index()
        s.columns = ["trade_date", "ts_code", "value"]
        s["factor"] = name
        long_rows.append(s)
    pd.concat(long_rows, ignore_index=True).to_parquet(PANEL_OUT, index=False)

    # ---------- 门禁读数 ----------
    m = fe.load_market()
    md_all = fe.month_end_dates(pd.DatetimeIndex(m["close"].index))
    md_eval = pd.DatetimeIndex(md_all[md_all >= pd.Timestamp("2019-01-01")])
    zones = gate.zone_of(md_eval)
    fwd = gate.forward_return(m, gate.HORIZON)
    tradable = PITEngine(m=m).tradable_matrix(md_eval)

    def judge(panel: pd.DataFrame, name: str) -> dict:
        pm = panel.reindex(index=sorted(set(md_eval) & set(panel.index)),
                           columns=panel.columns)
        pm = pm.where(tradable.reindex(index=md_eval, columns=pm.columns).fillna(False))
        det = gate.evaluate(pm, fwd, md_eval, zones)
        if det.empty:
            return {"factor": name, "note": "无有效截面"}
        dead, act = det[det.zone == "DEAD"], det[det.zone == "ACTIVE"]
        h_d, h_a = float(dead.head_excess.mean()), float(act.head_excess.mean())
        t_d = float(h_d / (dead.head_excess.std(ddof=1) / np.sqrt(len(dead))))
        v = gate.verdict_single(h_d, t_d, h_a, None)   # S4 之后单独算
        return {"factor": name, "dead_head": round(h_d, 4), "dead_t": round(t_d, 2),
                "active_head": round(h_a, 4), "same_sign": bool(h_d * h_a > 0),
                **v}

    readouts = {n: judge(p, n) for n, p in final.items()}
    for r in readouts.values():
        log(str(r))

    # S4: 与 31 在池 + 文本族内部去相关（秩缓存，隔 2 锚点抽样）
    sub_anchors = list(md_eval[::2])
    rank_pool = {}
    wide_r = wide31.reorder_levels(["factor", "ts_code"], axis=1).sort_index(axis=1)
    pool31 = sorted(set(wide31.columns.get_level_values("factor")))
    for f in pool31:
        sub = wide_r.xs(f, axis=1, level="factor")
        sub.index = pd.to_datetime(sub.index)
        rank_pool[f] = {dt: sub.loc[dt].rank() for dt in sub_anchors if dt in sub.index}
    rank_text = {n: {dt: p.loc[dt].rank() for dt in sub_anchors if dt in p.index}
                 for n, p in final.items()}

    def mean_abs_rho(a: str, b: str) -> float:
        vals = []
        for dt in sub_anchors:
            ra, rb = rank_text[a].get(dt), (rank_pool.get(b, {}) or {}).get(dt)
            if b in rank_text:
                rb = rank_text[b].get(dt)
            if ra is None or rb is None:
                continue
            j = pd.concat([ra.rename("a"), rb.rename("b")], axis=1).dropna()
            if len(j) < 200:
                continue
            vals.append(abs(j["a"].corr(j["b"])))
        return float(np.mean(vals)) if vals else float("nan")

    s4 = {}
    for n in final:
        rhos = {("pool", f): mean_abs_rho(n, f) for f in pool31}
        rhos.update({("text", g): mean_abs_rho(n, g) for g in final if g != n})
        rhos = {k: round(v, 3) for k, v in rhos.items() if v == v}
        s4[n] = {f"{k[0]}:{k[1]}": v for k, v in rhos.items()}
        rmax_pool = max((v for k, v in rhos.items() if k[0] == "pool"), default=0)
        rmax_all = max(rhos.values(), default=0)
        readouts[n]["s4_rho_max_pool"] = rmax_pool
        readouts[n]["s4_rho_max_any"] = rmax_all
        readouts[n]["s4_shadow"] = max(rhos, key=lambda k: rhos[k])[1] if rhos else None

    # 判决汇总（S4: 与 31 在池 ≤0.7 且族内去重代表——同族取 |dead_t| 最大者）
    fam_of = {n: ("事件强度" if "n" in n.split("_")[1][:2] or n.endswith(("n1m", "n3m", "n6m"))
                  else n.split("_")[1] if n.startswith("text_") else n) for n in final}
    fam_of = {n: {"text_n1m": "事件强度", "text_n3m": "事件强度", "text_n6m": "事件强度",
                  "text_netflow3m": "类别净流", "text_earn3m": "业绩预告",
                  "text_novel3m": "新颖度", "text_gap": "稀缺性"}[n] for n in final}
    verdicts = {}
    for n, r in readouts.items():
        if "dead_head" not in r:
            verdicts[n] = {"verdict": "无有效截面"}
            continue
        s4_ok = r["s4_rho_max_pool"] <= 0.7
        is_family_rep = True   # 族代表判定在汇总后统一做（同族全 PASS 时取 |t| 最大）
        verdicts[n] = {"S1": r.get("s1_head"), "S2": r.get("s2_t"), "S3": r.get("s3_same_sign"),
                       "S4_pool": s4_ok, "rho_max_pool": r["s4_rho_max_pool"],
                       "PASS_single_candidate": bool(r.get("PASS_single") and s4_ok),
                       "family": fam_of[n], "is_family_rep": is_family_rep}
    # 族代表：同族多个 PASS 候选取 |dead_t| 最大
    for famname in set(v["family"] for v in verdicts.values()):
        cands = [n for n, v in verdicts.items()
                 if v["family"] == famname and v.get("PASS_single_candidate")]
        if len(cands) > 1:
            keep = max(cands, key=lambda n: abs(readouts[n]["dead_t"]))
            for n in cands:
                if n != keep:
                    verdicts[n]["PASS_single_candidate"] = False
                    verdicts[n]["note"] = f"同族让位 {keep}"
    n_pass = sum(1 for v in verdicts.values() if v.get("PASS_single_candidate"))
    log(f"单因子档 PASS: {n_pass}/{len(final)}")
    for n, v in verdicts.items():
        log(f"  {n}: {v}")

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"), "prereg": PREREG,
           "runtime_sec": round(time.time() - t0, 1), "neutralize_audit": audit,
           "readouts": readouts, "rho_tables": s4, "verdicts": verdicts,
           "n_pass_single": n_pass,
           "closure": ("有 PASS_single ⇒ 升级路径（bank 登记 + 全量重训 + 组合层档，另立预注册）"
                       if n_pass else "0 PASS ⇒ 文本因子线按 §七 结清（N 预算 +5）")}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    log(f"闭环: {out['closure']}")
    log(f"产物: {OUT} + {PANEL_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
