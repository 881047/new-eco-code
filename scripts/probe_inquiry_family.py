# -*- coding: utf-8 -*-
"""问询函回复延迟族 —— 语料勘察(只读, 跑数前天花板标定, acceptance-bar 纪律)。

目的: 在冻结预注册判据之前, 先标定该族的真实天花板——
  ①语料里问询函/回复各有多少、可配对多少(配对率);
  ②月频截面的覆盖天花板(月末锚前溯 90 交易日窗内有可算延迟的股票数)——
    覆盖决定该族能不能过门禁 MIN_NAMES=200 这一道闸;
  ③PIT 语义核对(公告时间戳粒度)。
勘察读数不进任何因子判决; 产物 experiments/inquiry_probe_20261003.json。
"""
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "text_corpus" / "cninfo"
OUT = ROOT / "experiments" / "inquiry_probe_20261003.json"

RE_LETTER = re.compile(r"问询函")
RE_REPLY = re.compile(r"回复")
RE_EXCLUDE = re.compile(r"延期|取消|进展|撤回|终止|催告|更正")
# 中介机构随回复包同日发布的文件(说明/意见/核查)既非问询函也非公司回复 —— 2026-10-03
# 勘察 v1 实证: 不过滤会把配对延迟中位数压到 0(同日"说明"被当成"函")。
RE_INTER = re.compile(
    r"会计师|律师事务所|资产评估|评估公司|独立董事|独立意见|证券公司|保荐|财务顾问"
    r"|核查意见|核查说明|法律意见|专项意见|审核意见|说明(?!会)|回复之专项")
RE_REG = re.compile(r"监管函|关注函")   # 记录但不在册(本族=问询函)


def to_ts_code(code6: str) -> str:
    if code6.startswith("6"):
        return f"{code6}.SH"
    if code6.startswith(("0", "3")):
        return f"{code6}.SZ"
    if code6.startswith(("4", "8")):
        return f"{code6}.BJ"
    return f"{code6}.SZ"


def main() -> int:
    t0 = time.time()
    files = sorted(CORPUS.glob("*.parquet"))
    letters, replies, regs = [], [], []
    n_files, n_rows = 0, 0
    for fp in files:
        df = pd.read_parquet(fp, columns=["代码", "公告标题", "公告时间"])
        n_files += 1
        n_rows += len(df)
        t = df["公告标题"].fillna("").astype(str)
        dt = pd.to_datetime(df["公告时间"], errors="coerce")
        m_reg = t.str.contains(RE_REG)
        if m_reg.any():
            regs.append(df[m_reg][["代码", "公告标题", "公告时间"]])
        m_l = t.str.contains(RE_LETTER) & ~t.str.contains(RE_REPLY) \
            & ~t.str.contains(RE_INTER)
        m_r = t.str.contains(RE_LETTER) & t.str.contains(RE_REPLY) \
            & ~t.str.contains(RE_INTER)
        if m_l.any():
            sub = df[m_l][["代码", "公告标题", "公告时间"]].copy()
            sub["is_excluded"] = t[m_l].str.contains(RE_EXCLUDE)
            letters.append(sub)
        if m_r.any():
            sub = df[m_r][["代码", "公告标题", "公告时间"]].copy()
            sub["is_excluded"] = t[m_r].str.contains(RE_EXCLUDE)
            replies.append(sub)

    L = pd.concat(letters, ignore_index=True)
    R = pd.concat(replies, ignore_index=True)
    G = pd.concat(regs, ignore_index=True) if regs else pd.DataFrame(
        columns=["代码", "公告标题", "公告时间"])
    L["dt"] = pd.to_datetime(L["公告时间"], format="mixed", errors="coerce")
    R["dt"] = pd.to_datetime(R["公告时间"], format="mixed", errors="coerce")
    L["ts_code"] = L["代码"].astype(str).str.zfill(6).map(to_ts_code)
    R["ts_code"] = R["代码"].astype(str).str.zfill(6).map(to_ts_code)

    n_l_raw, n_r_raw = len(L), len(R)
    Lx, Rx = L[~L["is_excluded"]], R[~R["is_excluded"]]

    # ---- 配对(函锚定): 每封函 ← 同票其后第一封回复(≤120 自然日); 每条回复至多归属一封 ----
    pairs = []
    Rx_s = {c: g.sort_values("dt") for c, g in Rx.groupby("ts_code")}
    for c, g in Lx.sort_values("dt").groupby("ts_code"):
        cand = Rx_s.get(c)
        if cand is None:
            continue
        used = set()
        for _, lrow in g.iterrows():
            if pd.isna(lrow["dt"]):
                continue
            fol = cand[(cand["dt"] >= lrow["dt"])
                       & (cand["dt"] - lrow["dt"] <= pd.Timedelta(days=120))]
            fol = fol[~fol.index.isin(used)]
            if len(fol) == 0:
                continue
            best = fol.iloc[0]
            used.add(best.name)
            pairs.append({"ts_code": c, "letter_dt": lrow["dt"],
                          "reply_dt": best["dt"],
                          "latency_days": (best["dt"] - lrow["dt"]).days})
    P = pd.DataFrame(pairs)

    # ---- 月频覆盖天花板: 月末锚, 前溯 90 自然日窗内有配对的股票数 ----
    if len(P):
        anchors = pd.date_range("2019-08-31", "2026-09-30", freq="ME")
        cover = []
        for a in anchors:
            win = P[(P["reply_dt"] > a - pd.Timedelta(days=90)) & (P["reply_dt"] <= a)]
            cover.append({"anchor": str(a.date()),
                          "n_stocks_with_latency": int(win["ts_code"].nunique()),
                          "n_pairs": int(len(win))})
        C = pd.DataFrame(cover)
        cstats = {"min": int(C["n_stocks_with_latency"].min()),
                  "median": float(C["n_stocks_with_latency"].median()),
                  "max": int(C["n_stocks_with_latency"].max()),
                  "p10": float(C["n_stocks_with_latency"].quantile(0.10)),
                  "n_anchors_ge_200": int((C["n_stocks_with_latency"] >= 200).sum()),
                  "n_anchors": int(len(C))}
        lat_stats = {"mean": float(P["latency_days"].mean()),
                     "median": float(P["latency_days"].median()),
                     "p90": float(P["latency_days"].quantile(0.9)),
                     "share_within_30d": float((P["latency_days"] <= 30).mean())}
        yearly = P.groupby(P["reply_dt"].dt.year)["ts_code"].nunique().to_dict()
    else:
        cstats, lat_stats, yearly, C = {}, {}, {}, pd.DataFrame()

    payload = {
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": "问询函延迟族语料勘察(只读, 判据冻结前的天花板标定)",
        "corpus": {"files": n_files, "rows": n_rows},
        "counts": {"letters_raw": n_l_raw, "letters_excluded": int(L["is_excluded"].sum()),
                   "letters_clean": len(Lx),
                   "replies_raw": n_r_raw, "replies_excluded": int(R["is_excluded"].sum()),
                   "replies_clean": len(Rx),
                   "regulatory_watch_out_of_scope": int(len(G)),
                   "pairs": int(len(P)),
                   "pair_rate_vs_replies": round(len(P) / max(1, len(Rx)), 3)},
        "latency_days_stats": lat_stats,
        "stocks_with_any_pair": int(P["ts_code"].nunique()) if len(P) else 0,
        "yearly_stocks_with_pair": yearly,
        "monthly_coverage_ceiling": cstats,
        "coverage_sample": C.to_dict("records") if len(C) else [],
        "letter_title_samples": Lx["公告标题"].drop_duplicates().head(5).tolist(),
        "reply_title_samples": Rx["公告标题"].drop_duplicates().head(5).tolist(),
        "excluded_title_samples": L[L["is_excluded"]]["公告标题"].head(5).tolist(),
        "elapsed_sec": round(time.time() - t0, 1),
        "note": "勘察只标定天花板与可行性; 判据在预注册冻结, 本读数不进任何因子判决",
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(json.dumps({k: v for k, v in payload.items()
                      if k not in ("coverage_sample", "letter_title_samples",
                                   "reply_title_samples", "excluded_title_samples",
                                   "yearly_stocks_with_pair")},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
