"""D06/D07 归因残差诊断 —— 把 `l1_l2` 判为「无法归因」的格子拉出来看清是什么。

2026-09-12。背景: `D06_D07_limit_fill.py` 的 L1/L2 逐格比对把每一条分歧按 6 个桶归因,
归不进去的判 `ATTRIB_UNKNOWN` ⇒ 按预注册 P1 直接 FAIL。四轮修复后残差从 322,546 格
降到 1,416 格, 本脚本回答「这 1,416 格是什么」。

**这是纯诊断, 不改判据** —— 预注册的 P1 一字未动。诊断只做两件事:
  1. 它们是**同一种**东西, 还是若干种的混合?
  2. 每一种的**真相长什么样**(引擎档位 vs 交易所档位的比值、涉及哪些股票/年份/板块)

用法: ./.venv/Scripts/python.exe experiments/01_data/diag_d06_unknown.py
输出: D:/claude-home/tmp/d06_unknown_diag.log  +  experiments/01_data/D06_unknown_cells.csv
"""
from __future__ import annotations

import io
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_ORIG_STDOUT = sys.stdout          # 留住引用: TextIOWrapper 被 GC 时会关掉底层 buffer
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

from app import db
from app.models.names import NameLookup
from app.portfolio.board import board_of, limit_ratio, no_limit_trading_days, round2

LOG = Path("D:/claude-home/tmp/d06_unknown_diag.log")
CSV = Path(__file__).resolve().parent / "D06_unknown_cells.csv"


def main():
    lines = []

    def say(s=""):
        print(s)
        lines.append(s)

    nl = NameLookup()
    conn = db.get_conn()
    ld = dict(conn.execute("SELECT ts_code, list_date FROM stock_basic"))
    allday = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily ORDER BY trade_date")]
    dayidx = {d: i for i, d in enumerate(allday)}
    years = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(trade_date,1,4) y FROM stk_limit ORDER BY y")]

    say("=" * 78)
    say("D06/D07 归因残差诊断 —— 年 %s" % ",".join(years))
    say("=" * 78)

    unk_rows = []
    for y in years:
        df = pd.read_sql(
            """SELECT l.trade_date AS d, l.ts_code AS c, l.up_limit AS lu,
                      l.down_limit AS ld, d0.pre_close AS pc, d0.close AS cl,
                      d0.vol AS vol
               FROM stk_limit l
               JOIN daily d0 ON d0.trade_date = l.trade_date AND d0.ts_code = l.ts_code
               WHERE substr(l.trade_date,1,4) = ? AND d0.pre_close > 0""",
            conn, params=(y,))
        if df.empty:
            continue
        codes = df["c"].to_numpy()
        ds = df["d"].to_numpy()
        pc = df["pc"].to_numpy(dtype="float64")
        cl = df["cl"].to_numpy(dtype="float64")
        lu_t = df["lu"].to_numpy(dtype="float64")
        ld_t = df["ld"].to_numpy(dtype="float64")

        ratio = np.empty(len(df), dtype="float64")
        isst = np.empty(len(df), dtype=bool)
        for i in range(len(df)):
            st = nl.is_st_only(codes[i], ds[i])
            isst[i] = st
            # 与 l1_l2 同一复算路径: 传当日名称 + name_of(创业板退市整理期新老划断)
            ratio[i] = limit_ratio(codes[i], st, ds[i],
                                   nl.name_at(codes[i], ds[i]), nl.name_at)
        lu_e = np.array([round2(v) for v in pc * (1.0 + ratio)], dtype="float64")
        ld_e = np.array([round2(v) for v in pc * (1.0 - ratio)], dtype="float64")

        bad = (~np.isclose(lu_e, lu_t, rtol=0, atol=0.005)) | \
              (~np.isclose(ld_e, ld_t, rtol=0, atol=0.005))
        if not bad.any():
            continue
        bidx = np.where(bad)[0]
        lst = np.array([ld.get(c, "") or "" for c in codes[bidx]], dtype=object)

        for j, i in enumerate(bidx):
            d, c = ds[i], codes[i]
            # 与 l1_l2 逐条同序的排除(前 5 桶) —— 归得进去的不是本脚本的对象
            if lu_t[i] >= 99999 or ld_t[i] <= 0.02:
                continue
            if board_of(c) == "bse":
                continue
            lidx, didx = dayidx.get(lst[j], -1), dayidx.get(d, -1)
            nld = no_limit_trading_days(c)
            if lidx >= 0 and didx >= 0 and nld > 0 and (didx - lidx) < nld:
                continue
            prev = allday[didx - 1] if didx > 0 else None
            if prev is not None:
                pv = conn.execute("SELECT vol FROM daily WHERE trade_date=? AND ts_code=?",
                                  (prev, c)).fetchone()
                if pv is None or pv[0] is None or pv[0] <= 0:
                    continue
            n0 = nl.name_at(c, d)
            nprev = nl.name_at(c, allday[didx - 1]) if didx > 0 else ""
            nnext = nl.name_at(c, allday[didx + 1]) if didx + 1 < len(allday) else ""
            if ("ST" in nprev) != ("ST" in n0) or ("ST" in nnext) != ("ST" in n0):
                continue
            # ---- 到这里 = ATTRIB_UNKNOWN ----
            unk_rows.append(dict(
                year=y, trade_date=d, ts_code=c, name=n0, board=board_of(c),
                is_st=bool(isst[i]), list_date=lst[j],
                days_since_list=(didx - lidx) if (lidx >= 0 and didx >= 0) else -1,
                pre_close=pc[i], close=cl[i], vol=df["vol"].iloc[i],
                engine_ratio=ratio[i],
                engine_up=lu_e[i], truth_up=lu_t[i],
                engine_dn=ld_e[i], truth_dn=ld_t[i],
                truth_up_ratio=(lu_t[i] / pc[i] - 1.0) if pc[i] > 0 else np.nan,
                truth_dn_ratio=(ld_t[i] / pc[i] - 1.0) if pc[i] > 0 else np.nan,
            ))

    conn.close()
    u = pd.DataFrame(unk_rows)
    say("\n无法归因合计: %d 格" % len(u))
    if u.empty:
        say("(空) —— P1 可判 PASS")
        _dump(lines)
        return

    say("\n--- 按年 ---")
    for y, g in u.groupby("year"):
        say("  %s  %5d 格   板块 %s" % (y, len(g), dict(Counter(g["board"]))))
    say("\n--- 按板块×是否ST ---")
    for (b, s), g in u.groupby(["board", "is_st"]):
        say("  %-8s ST=%-5s %5d 格" % (b, s, len(g)))

    say("\n--- 分歧方向 ---")
    bad_u = ~np.isclose(u["engine_up"], u["truth_up"], rtol=0, atol=0.005)
    bad_d = ~np.isclose(u["engine_dn"], u["truth_dn"], rtol=0, atol=0.005)
    say("  仅涨停价分歧 %d / 仅跌停价分歧 %d / 两者 %d"
        % (int((bad_u & ~bad_d).sum()), int((~bad_u & bad_d).sum()),
           int((bad_u & bad_d).sum())))

    say("\n--- 交易所那一档到底是什么(truth_up_ratio 分布) ---")
    r = u["truth_up_ratio"].round(4)
    for v, n in Counter(r).most_common(12):
        say("    上轨 %-9s %5d 格" % (v, n))
    r2 = u["truth_dn_ratio"].round(4)
    say("  ")
    for v, n in Counter(r2).most_common(12):
        say("    下轨 %-9s %5d 格" % (v, n))

    say("\n--- 出现次数最多的股票(前 15) ---")
    for c, n in Counter(u["ts_code"]).most_common(15):
        g = u[u["ts_code"] == c]
        say("  %-12s %-10s %5d 格  %s~%s  板块=%s ST=%s 上市日=%s"
            % (c, g["name"].iloc[0], n, g["trade_date"].min(), g["trade_date"].max(),
               g["board"].iloc[0], g["is_st"].iloc[0], g["list_date"].iloc[0]))

    say("\n--- 逐格样例(前 40) ---")
    cols = ["trade_date", "ts_code", "name", "board", "is_st", "days_since_list",
            "pre_close", "close", "engine_up", "truth_up", "engine_dn", "truth_dn"]
    say(u[cols].head(40).to_string(index=False))

    outlier_evidence(say, nl, u, allday, dayidx)

    u.to_csv(CSV, index=False, encoding="utf-8-sig")
    say("\n逐格明细 → %s" % CSV)
    _dump(lines)


def _band(conn, code, d):
    """该股当日在交易所文件里的档位(上轨/前收−1), 无行返回 None。"""
    r = conn.execute(
        "SELECT l.up_limit, d0.pre_close FROM stk_limit l JOIN daily d0 "
        "ON d0.ts_code=l.ts_code AND d0.trade_date=l.trade_date "
        "WHERE l.ts_code=? AND l.trade_date=?", (code, d)).fetchone()
    if not r or not r[1] or r[0] is None or r[0] >= 99999:
        return None
    return round(r[0] / r[1] - 1.0, 4)


def outlier_evidence(say, nl, u, allday, dayidx):
    """§7 —— 残留格的定性: 三项**独立于引擎**的检验。判据一字不改, 只出证据。

    2026-09-12 第 7 轮。三项检验的共同点: **只用 `stk_limit`/`daily` 的原始值与同日
    邻股**, 不调用 `limit_ratio`, 因此它们的结论不能被"引擎怎么算的"污染。

    ① 同日同类邻股一致性 —— 该格当天, 同板同档(pc 末位=5)邻股跟 A 还是跟 B
    ② ST 格的前后档位序列 —— 单日孤立还是整段时期
    ③ 当日 high/low 是否触及任一候选限价 —— 分歧是否改变成交
    """
    say("\n" + "=" * 78)
    say("§7 残留格定性（三项独立于引擎的检验；判据未改，仅出证据）")
    say("=" * 78)
    if u.empty:
        say("(无残留格)")
        return
    # 调用方(main)在这之前已 close 连接; §7 自开一条, 不依赖调用方生命周期
    conn = db.get_conn()

    # ---- ① 同日同类邻股 ----
    say("\n① 同日同板同档(pre_close 末位=5)邻股用的是哪种取整")
    say("   A=半进位 round2(pc*(1-r))   B=先舍幅度 pc-round2(pc*r)")
    for _, r in u.iterrows():
        if r["is_st"]:
            continue
        d, me = r["trade_date"], r["ts_code"]
        na = nb = 0
        for (c2,) in conn.execute("SELECT ts_code FROM daily WHERE trade_date=?", (d,)):
            if c2 == me or board_of(c2) != "main":
                continue
            x = conn.execute(
                "SELECT l.up_limit,l.down_limit,d0.pre_close FROM stk_limit l JOIN daily d0 "
                "ON d0.ts_code=l.ts_code AND d0.trade_date=l.trade_date "
                "WHERE l.ts_code=? AND l.trade_date=?", (c2, d)).fetchone()
            if not x:
                continue
            lu, ld, pc = x
            if not pc or pc <= 0 or lu is None or lu >= 99999 or ld is None or ld <= 0.02:
                continue
            if abs(round(pc * 100) % 10) != 5:      # 只看同型: 落在 A/B 分歧价位上的
                continue
            st2 = nl.is_st_only(c2, d)
            rr = limit_ratio(c2, st2, d, nl.name_at(c2, d), nl.name_at)
            dA, dB = round2(pc * (1 - rr)), pc - round2(pc * rr)
            if abs(dA - dB) < 1e-9:
                continue
            if abs(dA - ld) < 0.005:
                na += 1
            elif abs(dB - ld) < 0.005:
                nb += 1
        say("   %s %-10s  同日同型邻股: A=%d  B=%d" % (d, me, na, nb))

    # ---- ② ST 格的档位序列 ----
    say("\n② ST 残留格: 前后各 6 个交易日的**交易所档位**(单日孤立 = 发布异常)")
    for _, r in u.iterrows():
        if not r["is_st"]:
            continue
        d, me = r["trade_date"], r["ts_code"]
        i = dayidx.get(d)
        if i is None:
            continue
        seq = []
        for j in range(max(0, i - 6), min(len(allday), i + 7)):
            b = _band(conn, me, allday[j])
            seq.append("%s:%s%s" % (allday[j][4:], "None" if b is None else b,
                                    "*" if allday[j] == d else ""))
        say("   %s %-10s %s" % (d, me, " ".join(seq)))

    # ---- ③ 当日高低价是否触及任一候选限价 ----
    say("\n③ 当日 high/low 是否触及引擎值或交易所值(* = 本格)")
    n_hit = 0
    for _, r in u.iterrows():
        d, me = r["trade_date"], r["ts_code"]
        x = conn.execute("SELECT high,low FROM daily WHERE trade_date=? AND ts_code=?",
                         (d, me)).fetchone()
        if not x:
            continue
        hi, lo = x
        hits = []
        for tag, px in (("high==涨_引擎", r["engine_up"]), ("high==涨_交易所", r["truth_up"])):
            if hi is not None and abs(hi - px) < 0.005:
                hits.append(tag)
        for tag, px in (("low==跌_引擎", r["engine_dn"]), ("low==跌_交易所", r["truth_dn"])):
            if lo is not None and abs(lo - px) < 0.005:
                hits.append(tag)
        if hits:
            n_hit += 1
        say("   %s %-10s high=%-8s low=%-8s 引擎 %.2f/%.2f 交易所 %.2f/%.2f  %s"
            % (d, me, hi, lo, r["engine_up"], r["engine_dn"], r["truth_up"], r["truth_dn"],
               ";".join(hits) if hits else "-"))
    say("   ⇒ 触及任一候选限价的格数: %d / %d" % (n_hit, len(u)))
    say("\n[§7 结论] 三项证据若同时成立(邻股全跟 A / ST 格单日孤立 / 0 格触及限价),")
    say("          则残留 = **交易所公布文件自身的单日异常行**, 非引擎缺陷。")
    say("          ⚠️ 但这**不构成归因**, P1 仍判 FAIL(残留格数)。不得据此新增归因桶。")
    conn.close()


def _dump(lines):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(lines), encoding="utf-8")
    print("\n[日志] %s" % LOG)


def _evidence_only():
    """只跑 §7: 直接读已落地的 `D06_unknown_cells.csv`, 不重跑 10M 行的归因推导。
    用法: ./.venv/Scripts/python.exe experiments/01_data/diag_d06_unknown.py --evidence-only
    (归因推导那段是纯 Python 逐行循环, 要跑十几分钟; 而 §7 只关心那 24 格。)"""
    lines = []

    def say(s=""):
        print(s)
        lines.append(s)

    if not CSV.exists():
        raise SystemExit("缺 %s —— 先跑一次不带参数的本脚本生成它。" % CSV)
    nl = NameLookup()
    conn = db.get_conn()
    allday = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily ORDER BY trade_date")]
    dayidx = {d: i for i, d in enumerate(allday)}
    u = pd.read_csv(CSV, dtype={"trade_date": str, "ts_code": str, "name": str})
    conn.close()
    outlier_evidence(say, nl, u, allday, dayidx)
    _dump(lines)


if __name__ == "__main__":
    if "--evidence-only" in sys.argv:
        _evidence_only()
    else:
        main()
