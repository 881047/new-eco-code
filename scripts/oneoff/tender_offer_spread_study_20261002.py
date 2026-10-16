# -*- coding: utf-8 -*-
"""要约收购事件族 Phase 2 价差研究（plans/tender-offer-phase2-prereg-20261002.md §二-四
判据跑前冻结）。只读零写库。

D1 = 正则命中率 ≥ 0.8×锚点共现天花板 且 (价格+类型)齐备率 ≥ 60%;
D2 = 价差型(部分要约且要约溢价>3%) n≥40 且净收益中位数 ≥ +1.0%;
D3 = 价差型净收益 p5 ≥ −10%。经济口径 = 部分要约按比例接受、余券 t0+21 收盘退出
(冻结代理), 全面要约全额接受, 扣双边 0.6%。
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app.factors.factor_engine import load_market

ROOT = Path(__file__).resolve().parents[2]
TXT = ROOT / "data" / "text_corpus" / "tender_offers" / "tender_offers.parquet"
OUT = ROOT / "experiments" / "tender_offer_spread_study_20261002.json"

COST = 0.006
SPREAD_MIN = 0.03
LOTTERY_CAR21 = 0.30
D1_RATE = 0.8
D1_READY = 0.60
D2_MIN_N = 40
D2_MED = 0.010
D3_P5 = -0.10
PRICE_WIN = 200
HEAD = 6000

_RE_PRICE = re.compile(r"([0-9]+(?:\.[0-9]+)?)元/?股")
_RE_RATIO = re.compile(r"预定收购[^%]{0,80}?([0-9]+(?:\.[0-9]+)?)%")
_RE_RATIO2 = re.compile(r"占[^%]{0,40}总股本[^%]{0,30}?([0-9]+(?:\.[0-9]+)?)%")
_RE_TERM = re.compile(r"要约期限[^0-9]{0,15}([0-9]+)\s*天")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _norm(text: str) -> str:
    """pypdf 换行劈词('收购报告
书')⇒ 中文正则前必须剥全部空白(首跑 18.7% 命中根因)。"""
    return re.sub(r"\s+", "", text)


_RE_TBL = re.compile(r"要约价格（元/股）[^0-9]{0,80}?\d{6}([0-9]+\.[0-9]{2})")
_RE_PROSE = re.compile(r"要约(?:收购)?价格[为是:：]?([0-9]+(?:\.[0-9]+)?)元/?股")
_RE_PER_SHARE = re.compile(r"每股要约收购价格[^0-9]{0,10}([0-9]+(?:\.[0-9]+)?)元")
_RE_RATIO_TBL = re.compile(r"股[^%]{0,50}?([0-9]+(?:\.[0-9]+)?)%")


def _find_price(head: str):
    """价格抽取: 表格压平行(要约价格（元/股）…00080315.08…)优先, 再散文形态;
    逐次出现迭代 —— 首个'要约价格'常落在释义节(无数值, 首跑 31.4% 命中根因)。"""
    tm = _RE_TBL.search(head)
    if tm:
        return float(tm.group(1))
    for m in re.finditer(r"要约价格|要约收购价格", head):
        pm = _RE_PROSE.search(head[m.start():m.start() + PRICE_WIN])
        if pm:
            return float(pm.group(1))
    pm = _RE_PER_SHARE.search(head)
    if pm:
        return float(pm.group(1))
    return None


def extract(text: str) -> dict:
    head = _norm(text)[:HEAD]
    price = _find_price(head)
    price = price if price and 0.5 < price < 1000 else None
    is_part = "部分要约" in head
    is_full = "全面要约" in head
    typ = "partial" if is_part else ("full" if is_full else "unknown")
    ratio = None
    if typ == "partial":
        rm = _RE_RATIO.search(head) or _RE_RATIO2.search(head)
        if rm:
            ratio = float(rm.group(1))
        elif price:
            tpos = head.find(str(price))
            if tpos >= 0:
                rm = _RE_RATIO_TBL.search(head[tpos:tpos + 80])
                if rm:
                    ratio = float(rm.group(1))
    tm = _RE_TERM.search(head)
    return {"price": price, "type": typ, "ratio": ratio,
            "term_days": int(tm.group(1)) if tm else None}


def is_real_offer(text: str) -> bool:
    """真要约子样本(样本污染修复): 首跑 51.2% 锚点混入'豁免要约收购申请/财务顾问报告'
    —— 那些文档没有真实要约条款。真报告书 = 归一化正文同时陈述 要约价格 与 要约期限。"""
    n = _norm(text)
    return ("要约价格" in n) and ("要约期限" in n) and ("元" in n)


def main() -> int:
    txt = pd.read_parquet(TXT)
    log(f"报告书正文 {len(txt)} 份")
    txt = txt[txt["text"].map(is_real_offer)].reset_index(drop=True)
    log(f"真要约子样本(含要约价格+要约期限条款) {len(txt)} 份")
    # D1 天花板标定: 锚点 = 子样本正文含"要约价格"关键词
    has_anchor = txt["text"].map(lambda t: "要约价格" in _norm(t)).mean()
    ext = [extract(t) for t in txt["text"]]
    edf = pd.DataFrame(ext)
    edf["code"] = txt["code"].to_numpy()
    edf["t0"] = txt["t0"].to_numpy()
    hit_rate = float(edf["price"].notna().mean())
    ready = float(((edf["price"].notna()) & (edf["type"] != "unknown")).mean())
    d1 = bool(hit_rate >= D1_RATE * has_anchor and ready >= D1_READY)
    log(f"D1: 锚点共现 {has_anchor:.1%} → 0.8× = {0.8*has_anchor:.1%} | "
        f"价格命中 {hit_rate:.1%} | 价格+类型齐备 {ready:.1%} | D1={d1}")

    # 买入价/退出价(与 Phase 1 同一 defer 口径)
    m = load_market()
    ao = (m["open"] * m["adj"]).astype(float)
    ac = (m["close"] * m["adj"]).astype(float)
    td = pd.DatetimeIndex(m["close"].index)
    pos = {d: i for i, d in enumerate(td)}
    rows = []
    for _, e in edf.iterrows():
        d = pd.Timestamp(datetime.strptime(e["t0"], "%Y%m%d"))
        i0 = pos.get(d)
        if i0 is None or i0 + 1 >= len(td):
            continue
        c = e["code"] + (".SH" if e["code"].startswith("6")
                         else ".BJ" if e["code"][0] in "489" else ".SZ")
        if c not in ao.columns or not e["price"] or e["price"] <= 0:
            continue
        ib = None
        for k in range(1, 6):
            if i0 + k < len(td) and pd.notna(ao.iloc[i0 + k][c]) and ao.iloc[i0 + k][c] > 0:
                ib = i0 + k
                break
        if ib is None or ib + 21 >= len(td):
            continue
        p_buy = float(ao.iloc[ib][c])
        p21 = float(ac.iloc[ib + 21][c])
        p42 = float(ac.iloc[ib + 42][c]) if ib + 42 < len(td) else np.nan
        gross_offer = e["price"] / p_buy - 1.0
        r = e["ratio"] / 100.0 if (e["type"] == "partial" and e["ratio"]) else None
        if e["type"] == "full":
            net = gross_offer - COST
            kind = "full"
        elif r:
            net = r * gross_offer + (1 - r) * (p21 / p_buy - 1.0) - COST
            kind = "partial"
        else:
            net = np.nan
            kind = "partial_ratio_missing"
        rows.append({"code": c, "t0": e["t0"], "p_buy": round(p_buy, 3),
                     "p_offer": e["price"], "type": e["type"], "ratio": e["ratio"],
                     "term_days": e["term_days"], "gross_offer": round(gross_offer, 4),
                     "p21_exit_ret": round(p21 / p_buy - 1.0, 4),
                     "p42_exit_ret": round(p42 / p_buy - 1.0, 4) if pd.notna(p42) else None,
                     "net": round(net, 4) if pd.notna(net) else None, "kind": kind})
    dfm = pd.DataFrame(rows)
    dfm["spread_type"] = (dfm["kind"] == "partial") & (dfm["gross_offer"] > SPREAD_MIN)
    dfm["lottery"] = dfm["p21_exit_ret"] >= LOTTERY_CAR21
    log(f"可定价事件 {len(dfm)} | 价差型 {int(dfm['spread_type'].sum())} | "
        f"彩票型(CAR21≥30%) {int(dfm['lottery'].sum())}")

    spread = dfm[dfm["spread_type"]]["net"].dropna()
    d2 = bool(len(spread) >= D2_MIN_N and spread.median() >= D2_MED)
    d3 = bool(len(spread) >= 5 and spread.quantile(0.05) >= D3_P5)
    verdict = "GO" if (d1 and d2 and d3) else "NO-GO"

    def stats(s):
        if len(s) < 5:
            return None
        return {"n": int(len(s)), "median": round(float(s.median()), 4),
                "mean": round(float(s.mean()), 4),
                "p5": round(float(s.quantile(0.05)), 4),
                "win_rate": round(float((s > 0).mean()), 4)}

    out = {"asof": datetime.now().isoformat(timespec="seconds"),
           "criteria_file": "plans/tender-offer-phase2-prereg-20261002.md",
           "n_texts": int(len(txt)), "anchor_rate": round(float(has_anchor), 4),
           "price_hit_rate": round(hit_rate, 4), "ready_rate": round(ready, 4),
           "D1": d1, "D2": d2, "D3": d3, "verdict": verdict,
           "spread_stats": stats(spread),
           "full_stats": stats(dfm[dfm["kind"] == "full"]["net"].dropna()),
           "lottery_stats": stats(dfm[dfm["lottery"]]["p21_exit_ret"].dropna()),
           "rows": dfm.replace({np.nan: None}).to_dict("records")}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    log(f"判决: {verdict} | D1={d1} D2={d2} D3={d3}")
    log(f"价差型: {out['spread_stats']}")
    log(f"全面要约: {out['full_stats']}")
    log(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
