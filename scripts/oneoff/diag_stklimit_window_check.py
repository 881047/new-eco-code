"""stk_limit 回填窗口「引擎推导限价 vs 交易所公布值」逐格验证（诊断，不改任何判据/不写库）。

背景(2026-10-05): stk_limit 此前由 oneoff/pull_stk_limit.py 于 2026-09-12 一次性回填至
20260911 后从未接入日链（接线缺口，已修 e3e5c755），本次经新接线路径补齐 0912~0930。
E5_float32_limit.py 的「推导 vs 公布位级一致」判决（P2, 2026-09-12, pin
20260912_limitquant）只覆盖到**当时的表尾 0911** —— 回填出的新窗口是从未被验证过的数据。

本脚本对指定窗口逐格重跑同一比对：
    推导 = limit_price(pre_close, ±limit_ratio(code, is_st, dstr, name, name_of),
                       board_of(code))   ← 2026-10-05 起与引擎同走板块化取整
                          （BSE=上轨 floor/下轨 ceil，其余 round2 半进位）
    公布 = stk_limit.up_limit / down_limit
pre_close 取 daily 表 float64 标量，不经过 float32 市场矩阵（E5 PRE_CLOSE_QUANTIZE
语义）。本脚本自 2026-10-05 起是 BSE 取整接线的常设验证器。

哨兵/缺口处理：stk_limit 的**无限制日哨兵有两族** —— 沪深退市整理期首日 100000、
BSE 上市首日 up=99999.99/dn=0.0（2026-10-05 探针实测）；NULL = 无公布值（如上市初期
无涨跌幅窗口）。三类**单独计数、不参与比对**，与真值臂的"回退"语义一致。
残差归因：非 BSE 的残余分歧格附查「pre_close ≠ 前一日 close」（除权除息参考价签名
—— 交易所限价以除权参考价为基, daily.pre_close 该日是原始前收, 属数据层已知差异）。

用法: ./.venv/Scripts/python.exe scripts/oneoff/diag_stklimit_window_check.py [--start 20260912]
输出: 控制台 + D:/claude-home/tmp/diag_stklimit_window_check.log
"""
from __future__ import annotations

import argparse
import io
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
_ORIG_STDOUT = sys.stdout
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(_ORIG_STDOUT.buffer, encoding="utf-8", errors="replace")

from app import db                                             # noqa: E402
from app.logger import setup_logger                            # noqa: E402
from app.models.names import NameLookup                        # noqa: E402
from app.portfolio.board import board_of, limit_price, limit_ratio  # noqa: E402

LOG = Path("D:/claude-home/tmp/diag_stklimit_window_check.log")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20260912", help="比对窗口起点 YYYYMMDD")
    args = ap.parse_args()

    lines = []

    def say(s=""):
        print(s)
        lines.append(s)

    setup_logger()
    db.init_db()
    nl = NameLookup()
    conn = db.get_conn()

    say("=" * 96)
    say("stk_limit 窗口 %s~ 「引擎推导 vs 交易所公布」逐格验证（诊断；E5 口径，float64）" % args.start)
    say("=" * 96)

    limit_rows = conn.execute(
        "SELECT ts_code, trade_date, up_limit, down_limit FROM stk_limit "
        "WHERE trade_date>=? ORDER BY trade_date", (args.start,)).fetchall()
    pre = {(c, d): float(p) for c, d, p in conn.execute(
        "SELECT ts_code, trade_date, pre_close FROM daily WHERE trade_date>=? "
        "AND pre_close IS NOT NULL", (args.start,))}

    n_cmp = n_sent = n_null = n_nodaily = 0
    bad = []            # (day, code, side, derived, published)
    boards = Counter()
    for ts_code, d, up, dn in limit_rows:
        pc = pre.get((ts_code, d))
        if pc is None:
            n_nodaily += 1
            continue
        up = None if up is None else float(up)
        dn = None if dn is None else float(dn)
        # 无限制日哨兵两族: 沪深 100000 / BSE 上市首日 up=99999.99 & dn=0.0
        if (up is not None and up >= 99999.0) or (dn is not None and dn <= 0.01):
            n_sent += 1
            continue
        if up is None and dn is None:
            n_null += 1
            continue
        st = nl.is_st_only(ts_code, d)
        nm = nl.name_at(ts_code, d)
        rr = limit_ratio(ts_code, st, d, nm, nl.name_at)
        bd = board_of(ts_code)
        du = limit_price(pc, rr, bd)
        dd = limit_price(pc, -rr, bd)
        n_cmp += 1
        for side, derived, published in (("up", du, up), ("dn", dd, dn)):
            if published is None:
                continue
            if abs(derived - published) > 1e-9:
                bad.append((d, ts_code, side, derived, published, pc))
                boards[(bd, st, "退" in str(nm), side)] += 1

    say("stk_limit 行            : %d" % len(limit_rows))
    say("参与比对格 (股票×日)     : %d" % n_cmp)
    say("无限制哨兵跳过           : %d" % n_sent)
    say("公布值全空跳过           : %d" % n_null)
    say("daily 无行/无 pre_close  : %d" % n_nodaily)
    say("分歧格 (up+dn 合计)      : %d" % len(bad))
    if bad:
        say("\n分歧按 (板块, ST, 名含退, 方向):")
        for k, n in sorted(boards.items(), key=lambda x: -x[1]):
            say("  %-8s ST=%-5s 退=%-5s %s  %5d" % (k[0], k[1], k[2], k[3], n))
        ex = 0
        for d, code, side, dv, pv, pc in bad:
            prev = conn.execute(
                "SELECT close FROM daily WHERE ts_code=? AND trade_date<? "
                "ORDER BY trade_date DESC LIMIT 1", (code, d)).fetchone()
            if prev and abs(float(prev[0]) - pc) > 1e-9:
                ex += 1
        say("其中 pre_close ≠ 前一日 close(除权参考价签名): %d" % ex)
        say("\n分歧样例(前 20):")
        say("%-9s %-11s %-3s %10s %10s %8s" % ("day", "code", "side", "derived", "published", "pc"))
        for d, code, side, dv, pv, pc in bad[:20]:
            say("%-9s %-11s %-3s %10.2f %10.2f %8.2f" % (d, code, side, dv, pv, pc))
    else:
        say("\n✅ 零分歧 —— 推导限价与交易所公布值在窗口内逐格位级一致"
            "（与 E5 P2 判决在旧表尾 0911 前的结论衔接）。")
    conn.close()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(lines), encoding="utf-8")
    print("\n[日志] %s" % LOG)


if __name__ == "__main__":
    main()
