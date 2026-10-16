# -*- coding: utf-8 -*-
"""月频「近期被问询」指示因子 —— `plans/inq-monthly-factor-prereg-20261003.md` 执行体。

构造: 锚点前 21/63 交易日内收到 ≥1 封清洁问询函(冻结正则, import 勘察模块单一来源)
⇒ 1.0 否则 0.0; 求值链 = 门禁家族同款(fe.neutralize → PIT 可交易域 → gate.evaluate);
判决 = gate.verdict_single(S1|head|/S2|t|/S3同号/S4 去相关), S5 不适用(构造内嵌时窗)。
只读零写库, 不计 alpha_bank 分母(家族声明试验数 = 2)。
机械悬崖预告(预注册 §一): 21d 窗死区 k<100 预期被 winsorize 整行归零 = 构造性失败。
产物 experiments/inq_monthly_factor_20261003.json。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

from probe_inquiry_family import (RE_EXCLUDE, RE_INTER, RE_LETTER, RE_REPLY,
                                  to_ts_code)
from app import db
from app.alpha import gate
from app.alpha.factory import _masked_panel, tradable_mask
from app.factors import factor_engine as fe
from app.config import load_config

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "text_corpus" / "cninfo"
OUT = ROOT / "experiments" / "inq_monthly_factor_20261003.json"
PREREG = "plans/inq-monthly-factor-prereg-20261003.md"
WINDOWS = (21, 63)
EVAL_SINCE = "2019-01-01"


def scan_letters():
    files = sorted(CORPUS.glob("*.parquet"))
    parts = []
    for fp in files:
        df = pd.read_parquet(fp, columns=["代码", "公告标题", "公告时间"])
        t = df["公告标题"].fillna("").astype(str)
        m = (t.str.contains(RE_LETTER) & ~t.str.contains(RE_REPLY)
             & ~t.str.contains(RE_INTER) & ~t.str.contains(RE_EXCLUDE))
        if m.any():
            sub = df[m][["代码", "公告时间"]].copy()
            sub["dt"] = pd.to_datetime(sub["公告时间"], format="mixed", errors="coerce")
            parts.append(sub[sub["dt"].notna()])
    L = pd.concat(parts, ignore_index=True)
    L["ts_code"] = L["代码"].astype(str).str.zfill(6).map(to_ts_code)
    L["date"] = L["dt"].dt.normalize()
    return L.dropna(subset=["ts_code"]).groupby(["ts_code", "date"]).size().reset_index(name="n")


def build_dummy(letters_by_code: dict, dates: pd.DatetimeIndex, md: pd.DatetimeIndex,
                all_cols, window: int) -> pd.DataFrame:
    """锚点前 window 个交易日 (cutoff, anchor] 内有函 ⇒ 1.0 否则 0.0。"""
    pos_of = {d: i for i, d in enumerate(dates)}
    out = pd.DataFrame(0.0, index=md, columns=sorted(all_cols))
    k_stats = []
    for a in md:
        i = pos_of[a]
        if i - window < 0:
            continue
        cutoff = dates[i - window]
        for code, ldates in letters_by_code.items():
            if code not in out.columns:
                continue
            j = np.searchsorted(ldates, cutoff, side="right")
            jj = np.searchsorted(ldates, a, side="right")
            if jj > j:
                out.at[a, code] = 1.0
        k_stats.append({"anchor": str(a.date()), "k": int(out.loc[a].sum())})
    return out, k_stats


def pooled_spearman(cand_vals: pd.Series, conn) -> tuple:
    """候选(md×code 掩码面板堆叠) vs 现役池 26 因子 + model_score 的合并 Spearman。"""
    cfg = load_config()
    excl = set(cfg.get("factor_exclusions") or [])
    names = [r[0] for r in conn.execute(
        "SELECT DISTINCT factor FROM factor ORDER BY factor").fetchall()]
    pool = [n for n in names if n not in excl]
    rhos = {}
    for nm in pool + ["model_score"]:
        if nm == "model_score":
            df = pd.read_sql("SELECT trade_date, ts_code, score AS value FROM model_score "
                             "WHERE trade_date>='20190101'", conn)
        else:
            df = pd.read_sql("SELECT trade_date, ts_code, value FROM factor WHERE factor=?",
                             conn, params=(nm,))
        if not len(df):
            continue
        s = df.assign(d=pd.to_datetime(df["trade_date"], format="%Y%m%d")
                      ).set_index(["d", "ts_code"])["value"]
        mm = pd.concat([cand_vals.rename("c"),
                        s.reindex(cand_vals.index).rename("f")], axis=1).dropna()
        if len(mm) < 1000:
            rhos[nm] = None
            continue
        rhos[nm] = float(mm["c"].rank().corr(mm["f"].rank()))
    vals = [abs(v) for v in rhos.values() if v is not None]
    return (max(vals) if vals else None), rhos


def main() -> int:
    t0 = time.time()
    db.init_db()
    print("[1/4] 扫描清洁问询函…", flush=True)
    L = scan_letters()
    letters_by_code = {c: np.sort(g["date"].unique())
                       for c, g in L.groupby("ts_code")}
    print(f"    函 {len(L)} (去重) | {len(letters_by_code)} 只", flush=True)

    print("[2/4] 加载行情面板+月末锚+PIT 域…", flush=True)
    warm = (pd.Timestamp(EVAL_SINCE) - pd.Timedelta(days=400)).strftime("%Y%m%d")
    m = fe.load_market(since=warm)
    dates_all = pd.to_datetime(m["close"].index)
    md_all = fe.month_end_dates(dates_all)
    md = md_all[md_all >= pd.Timestamp(EVAL_SINCE)]
    zones = gate.zone_of(md)
    fwd = gate.forward_return(m, gate.HORIZON)
    tradable = tradable_mask(m, md)
    conn = db.get_conn()
    ind = fe.industry_map(conn)
    print(f"    面板 {m['close'].shape[0]} 天 × {m['close'].shape[1]} 只 | 锚 {len(md)} "
          f"| 死区 {int((zones == 'DEAD').sum())} ({time.time()-t0:.0f}s)", flush=True)

    readings, verdicts = {}, {}
    for W in WINDOWS:
        print(f"[3/4] 构造 inq_hit_{W} …", flush=True)
        panel, k_stats = build_dummy(letters_by_code, dates_all, md,
                                     m["close"].columns, W)
        ks = pd.DataFrame(k_stats)
        dead_ks = ks[ks["anchor"] < "2024-01-01"]["k"]
        print(f"    非零 k: 全程 min {int(ks['k'].min())} / 中位 {float(ks['k'].median())}"
              f" | 死区中位 {float(dead_ks.median())}", flush=True)
        neu = fe.neutralize(panel, ind)
        panel_md = _masked_panel(neu, tradable, md)
        det = gate.evaluate(panel_md, fwd, md, zones)
        rec = gate.summarize(f"inq_hit_{W}", det, n_tests=1,
                             extra={"expr": f"inq_hit_{W}(dummy, trailing {W} td)",
                                    "note": PREREG})
        cand = panel_md.stack()
        rho_max, rho_detail = pooled_spearman(cand, conn)
        v = gate.verdict_single(rec["DEAD_head"], rec["DEAD_head_t"],
                                rec["ACTIVE_head"], shadow_abs_rho=rho_max)
        readings[f"inq_hit_{W}"] = {
            "DEAD_head": rec["DEAD_head"], "DEAD_head_t": rec["DEAD_head_t"],
            "DEAD_top6": rec["DEAD_top6"], "ACTIVE_head": rec["ACTIVE_head"],
            "ACTIVE_head_t": rec["ACTIVE_head_t"], "avg_names": rec.get("avg_names"),
            "k_stats": {"min": int(ks["k"].min()),
                        "median": float(ks["k"].median()),
                        "dead_median": float(dead_ks.median()),
                        "series": k_stats},
            "rho_max": rho_max, "rho_detail": rho_detail,
            "verdict_single": v}
        verdicts[f"inq_hit_{W}"] = v["PASS_single"]
        print(f"    inq_hit_{W}: DEAD head {rec['DEAD_head']:+.4f} (t={rec['DEAD_head_t']:+.2f}) "
              f"| ACTIVE {rec['ACTIVE_head']:+.4f} | rho_max {rho_max} | "
              f"{v}", flush=True)

    conn.close()
    n_pass = sum(1 for v in verdicts.values() if v)
    if n_pass == 0:
        verdict = "线结清(0/2 过单因子档)"
    else:
        verdict = f"{n_pass}/2 过单因子档 ⇒ 剔除器 A/B 候选(另立预注册+拍板)"
    payload = {
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "prereg": PREREG,
        "letters_deduped": int(len(L)),
        "readings": readings,
        "verdicts": verdicts,
        "family_verdict": verdict,
        "declared_tests": len(WINDOWS),
        "note": "只读零写库; S5 不适用(构造内嵌时窗); 21d 机械悬崖预告见预注册 §一",
        "elapsed_sec": round(time.time() - t0, 1),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"\n判决: {verdict}", flush=True)
    print(f"产物: {OUT} ({time.time()-t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
