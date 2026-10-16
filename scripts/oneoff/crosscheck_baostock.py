"""baostock 交叉校验（只读，2026-09-13）

**为什么做**：本项目所有行情/财务都来自 Tushare(+AkShare 兜底) —— **单一来源**，没有任何
独立见证。而本项目最贵的一次事故恰恰是 PIT 口径类（买过生产买不到的 ST/退市/停牌股）。
baostock 是另一个免费源，自带 `tradestatus`(停牌)、`isST`、`peTTM/pbMRQ`、后复权价，
正好用来做**独立复核**。

⚠️ **不同平台口径本来就不一样**（用户提醒 + 本项目教训）：所以本脚本**只比口径可比量**，
不把"数值不同"当错误：
  · 价格 —— 比**日收益率**（`close/preclose−1` 与我们的 `close/pre_close−1`），不比价位
    （各自的复权基准不同）；分红除权日的微小差异属正常，只统计分布与极端值；
  · 估值 —— 比**同日截面排序一致率**（Spearman），不比绝对数值（PE/PB 口径各家不同）；
  · 交易状态/ST —— 比**布尔标记**（停牌、ST）并列出不一致样本，因为这两项直接决定
    可交易域，弄错就是真金白银。

**规矩（用户给的 baostock 限制）**：每日 ≤ 5 万次请求；**不许并发**。本脚本严格串行，
默认只抽 ~70 只票（≈70 次请求），远低于上限。

运行：
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\crosscheck_baostock.py [--n-random 30]
产物：`logs/crosscheck_baostock.json`（读数）+ 控制台摘要。**只读，不写任何生产表。**
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import db                                                   # noqa: E402
from app import config as cfg_mod                                    # noqa: E402
from app.models.names import NameLookup                              # noqa: E402

log = logging.getLogger("bscheck")
START, END = "2020-08-01", "2026-09-11"
RET_TOL = 0.002          # 日收益差 > 0.2pp 记为"明显不一致"


def _bs_daily(code: str, start: str = START, end: str = END) -> pd.DataFrame:
    """baostock 日线（后复权口径，但只用其中的**日收益/状态**字段）。"""
    import baostock as bs
    rs = bs.query_history_k_data_plus(
        code, "date,close,preclose,volume,tradestatus,pctChg,peTTM,pbMRQ,isST",
        start_date=start, end_date=end, frequency="d", adjustflag="1")
    if rs.error_code != "0":
        raise RuntimeError(f"{code}: {rs.error_code} {rs.error_msg}")
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    df = pd.DataFrame(rows, columns=rs.fields)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    for c in ("close", "preclose", "volume", "pctChg", "peTTM", "pbMRQ"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ret_bs"] = df["close"] / df["preclose"] - 1.0
    return df.set_index("date")


def _bs_code(ts_code: str) -> str:
    c, suf = ts_code.split(".")
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(suf, "sh") + "." + c


def _ours(conn, ts_code: str) -> pd.DataFrame:
    q = ("SELECT d.trade_date AS date, d.close, d.pre_close, d.vol, b.pe_ttm, b.pb, "
         "       b.turnover_rate "
         "FROM daily d LEFT JOIN daily_basic b "
         "  ON b.ts_code = d.ts_code AND b.trade_date = d.trade_date "
         "WHERE d.ts_code = ? AND d.trade_date BETWEEN ? AND ? ORDER BY d.trade_date")
    df = pd.read_sql(q, conn, params=(ts_code, START.replace("-", ""), END.replace("-", "")))
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")   # SQL 里已 AS date
    df["ret_ours"] = df["close"] / df["pre_close"] - 1.0
    return df.set_index("date")


def pick_sample(conn, n_random: int, seed: int = 20260913) -> list:
    """抽样：随机票 + 历史上出现过 ST/退 名字的票 + 停牌天数最多的票（后两类才检验得出问题）。"""
    rng = np.random.default_rng(seed)
    all_codes = [r[0] for r in conn.execute(
        "SELECT ts_code FROM stock_basic ORDER BY ts_code")]
    st_codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT ts_code FROM namechange WHERE name LIKE '%ST%' OR name LIKE '%退%'")]
    susp = [r[0] for r in conn.execute(
        "SELECT ts_code, SUM(CASE WHEN vol <= 0 THEN 1 ELSE 0 END) s FROM daily "
        "WHERE trade_date BETWEEN ? AND ? GROUP BY ts_code ORDER BY s DESC LIMIT ?",
        (START.replace("-", ""), END.replace("-", ""), n_random))]
    random_pick = list(rng.choice(all_codes, size=min(n_random, len(all_codes)), replace=False))
    out = []
    for c in random_pick + st_codes[:n_random] + susp:
        if c not in out:
            out.append(c)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="baostock 交叉校验（只读）")
    ap.add_argument("--n-random", type=int, default=30)
    ap.add_argument("--out", default=str(BASE / "logs" / "crosscheck_baostock.json"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        log.error("baostock 登录失败: %s %s", lg.error_code, lg.error_msg)
        return 2
    res = {"window": [START, END], "ret_tol": RET_TOL, "n_tested": 0,
           "ret_disagree": [], "susp_disagree": [], "st_disagree": [], "pe_rank": [], "pb_rank": []}
    t0 = time.time()
    try:
        conn = db.get_conn()
        nl = NameLookup(conn)
        codes = pick_sample(conn, args.n_random)
        log.info("样本 %d 只（随机 %d + 曾 ST %d + 停牌最多 %d）",
                 len(codes), args.n_random, args.n_random, args.n_random)
        for i, code in enumerate(codes, 1):
            try:
                b = _bs_daily(_bs_code(code))
            except Exception as e:
                log.warning("%s baostock 取数失败: %s", code, str(e)[:80])
                continue
            o = _ours(conn, code)
            if b.empty or o.empty:
                continue
            j = o.join(b[["ret_bs", "tradestatus", "isST", "peTTM", "pbMRQ"]],
                       how="inner", rsuffix="_bs")
            j = j[j["ret_ours"].notna() & j["ret_bs"].notna()]
            if j.empty:
                continue
            res["n_tested"] += 1
            # ① 日收益（只比收益，不比价位）
            d = (j["ret_ours"] - j["ret_bs"]).abs()
            bad = d[d > RET_TOL]
            if len(bad):
                res["ret_disagree"].append({
                    "code": code, "n_days": int(len(d)),
                    "n_bad": int(len(bad)), "max_pp": round(float(bad.max()) * 100, 3),
                    "median_pp": round(float(d.median()) * 100, 4)})
            # ② 停牌：我们 vol<=0 vs baostock tradestatus!='1'
            ours_susp = j["vol"] <= 0
            bs_susp = j["tradestatus"] != "1"
            n_diff = int((ours_susp != bs_susp).sum())
            if n_diff:
                res["susp_disagree"].append({"code": code, "n_diff": n_diff,
                                             "days": [str(x.date()) for x in
                                                      j.index[ours_susp != bs_susp][:5]]})
            # ③ ST：我们的**时点名称** vs baostock isST
            names = pd.Series({d: nl.name_at(code, d.strftime("%Y%m%d")) for d in j.index})
            ours_st = names.str.contains("ST|退", na=False)
            bs_st = j["isST"] == "1"
            n_st = int((ours_st != bs_st).sum())
            if n_st:
                res["st_disagree"].append({"code": code, "n_diff": n_st,
                                           "days": [str(x.date()) for x in
                                                    j.index[ours_st != bs_st][:5]]})
            # ④ 估值：只比同日截面排序（口径差异允许，排序该一致）
            for col, bscol, key in (("pe_ttm", "peTTM", "pe_rank"), ("pb", "pbMRQ", "pb_rank")):
                x = pd.to_numeric(j[col], errors="coerce")
                y = pd.to_numeric(j[bscol], errors="coerce")
                ok = x.notna() & y.notna() & (x > 0) & (y > 0)
                if ok.sum() >= 30:
                    rho = float(x[ok].corr(y[ok], method="spearman"))
                    res[key].append(round(rho, 3))
            if i % 10 == 0:
                log.info("  %d/%d 只（%.0fs）", i, len(codes), time.time() - t0)
        conn.close()
    finally:
        bs.logout()

    def _agg(key, name):
        v = res[key]
        if not v:
            return f"{name}: 无不一致"
        n = sum(x.get("n_diff", x.get("n_bad", 0)) for x in v)
        return f"{name}: {len(v)} 只票有差异，累计 {n} 天（样本里最大 {max((x.get('max_pp', 0) for x in v), default=0)}pp）"

    res["summary"] = {
        "ret": _agg("ret_disagree", f"日收益差>{RET_TOL*100:.1f}pp"),
        "susp": _agg("susp_disagree", "停牌标记不一致"),
        "st": _agg("st_disagree", "ST 标记不一致"),
        "pe_rank_median": round(float(np.median(res["pe_rank"])), 3) if res["pe_rank"] else None,
        "pb_rank_median": round(float(np.median(res["pb_rank"])), 3) if res["pb_rank"] else None,
        "elapsed_s": round(time.time() - t0, 1)}
    Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str),
                              encoding="utf-8")
    print("\n".join([f"样本 {res['n_tested']} 只 | 窗口 {START}~{END} | 用时 {res['summary']['elapsed_s']}s",
                     "  日收益:", res["summary"]["ret"],
                     "  停牌  :", res["summary"]["susp"],
                     "  ST    :", res["summary"]["st"],
                     f"  估值排序一致率(中位): PE {res['summary']['pe_rank_median']} / "
                     f"PB {res['summary']['pb_rank_median']}",
                     f"明细 → {args.out}"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
