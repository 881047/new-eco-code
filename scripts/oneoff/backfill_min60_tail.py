# -*- coding: utf-8 -*-
"""min60 尾部增量刷新 —— 09 号数据族登记的待办执行体（2026-09-26）。

为什么单独一个脚本：`backfill_baostock_min60.py` 是 ★ 冻结脚本（内容与位置都不许动），
且其 checkpoint 的 `done` 按**股**终结（done.last 是数据新鲜度, 不是"永远不再拉"）⇒
存量股的尾部重拉（09-17~09-24 共 6 个交易日）需要新鲜度门控。本 runner **import 其
原语**（fetch60/relogin/log_line/State/universe/bs_code/时窗函数——同一套串行+限流+
断网窗守卫, 零口径分叉），只加两件事：
  ① pending = 全市场 − `done.last` 已达最新交易日的股（新鲜度门控, 不再按股终结）;
  ② 账户级请求预算：启动时把原 checkpoint 的 `req_today` 种进自己的计数器
    （baostock 日额度是**账户级**的, 两边计数器必须合并看; 自设上限仍 20,000 < 5 万硬限）。
每股仍全量重拉（2020-01-01 起, 覆盖写 parquet——与冻结脚本逐字同款, 不做本地 merge）。

跑完后 `min60_derived/daily.parquet` **不自动重建**：其生成脚本（work/build_min60_daily.py）
在仓库外、口径未知 ⇒ 重建须先用重叠窗逐位比对验证重实现（待办, 见 09 号）。

用法:  .venv 无 baostock ⇒ 用系统解释器:
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\backfill_min60_tail.py [--limit N]
只写 data/baostock_min60/*.parquet + 自有 checkpoint + 日志; 不碰生产表。
"""
import argparse
import json
import socket
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "oneoff"))

import backfill_baostock_min60 as bf   # ★ 冻结脚本: 只 import 原语, 不改内容
from app import db

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "data" / ".baostock_min60_tail.json"
LOGF = ROOT / "logs" / "backfill_min60_tail.log"
WINDOWS = bf.parse_windows("6-24")     # 与正式轮同款时窗（00:00–06:00 硬底线另行拒绝）


def last_trading_day() -> str:
    conn = db.get_conn()
    try:
        d = conn.execute("SELECT MAX(trade_date) FROM daily").fetchone()[0]
    finally:
        conn.close()
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"        # "2026-09-24"（与 done.last 同格式）


def main() -> int:
    global WINDOWS
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="本轮只拉 N 只（冒烟）")
    ap.add_argument("--sleep", type=float, default=0.3)
    ap.add_argument("--overnight", action="store_true",
                    help="用户明示当晚不断网时使用: 时窗放宽为 0-24")
    ap.add_argument("--daily-cap", type=int, default=20000)
    args = ap.parse_args()
    if args.overnight:
        # 仅限用户明示"今晚不断网"的当晚(2026-09-26 授权): 时窗放宽为 0-24
        WINDOWS = ((0, 0, 24, 0),)
        bf.log_line(LOGF, "用户授权今晚不断网 ⇒ 时窗放宽 0-24(本次运行)")

    outdir = ROOT / "data" / "baostock_min60"
    st = bf.State(STATE)                       # 自有断点（与冻结脚本 checkpoint 分离）
    conn = db.get_conn()
    codes = bf.universe(conn)
    conn.close()
    end = last_trading_day()

    # 账户级预算: 原 checkpoint 的今日计数种进来（同一 baostock 账号）
    orig = json.loads((ROOT / "data" / ".baostock_min60_backfill.json").read_text(encoding="utf-8"))
    today = date.today().isoformat()
    if orig.get("req_day") == today:
        st.d["req_day"] = today
        st.d["req_today"] = max(int(st.d.get("req_today", 0)), int(orig.get("req_today", 0)))

    pending = [c for c in codes
               if c not in st.d["done"] or str(st.d["done"][c].get("last", "")) < end]
    bf.log_line(LOGF, f"尾部刷新启动: 最新交易日 {end} | 全市场 {len(codes)} 只 | "
                      f"待刷 {len(pending)} | 账户级今日请求 {st.d['req_today']}")
    if not pending:
        bf.log_line(LOGF, "全部已新鲜, 无事可做")
        return 0

    import baostock as bs
    socket.setdefaulttimeout(60)
    last_ok = time.time()
    bf.relogin(bs, LOGF)

    n_run = 0
    while pending:
        if not bf._in_window(datetime.now(), WINDOWS):
            nxt = bf._next_window(datetime.now(), WINDOWS)
            bf.log_line(LOGF, f"窗外，睡到 {nxt:%m-%d %H:%M}")
            time.sleep(max(60.0, (nxt - datetime.now()).total_seconds()))
            continue
        if st.d["req_day"] == date.today().isoformat() and st.d["req_today"] >= args.daily_cap:
            bf.log_line(LOGF, f"撞自设每日上限 {args.daily_cap}，退出（明日续跑）")
            break
        if args.limit and n_run >= args.limit:
            bf.log_line(LOGF, f"冒烟完成（limit={args.limit}）")
            break
        code = pending[0]
        if time.time() - last_ok > 600:
            bf.log_line(LOGF, "  心跳超时 ⇒ 重登会话")
            bf.relogin(bs, LOGF)
            last_ok = time.time()
        try:
            t0 = time.time()
            df = bf.fetch60(bf.bs_code(code), end)
            el = time.time() - t0
            last_ok = time.time()
            st.req_bump()
            n_run += 1
            if df.empty:
                st.d["empty"][code] = datetime.now().isoformat(timespec="seconds")
                st.d["done"][code] = {"rows": 0}
                bf.log_line(LOGF, f"  {code} 空页")
            else:
                df.to_parquet(outdir / f"{code}.parquet", index=False)
                st.d["done"][code] = {"rows": int(len(df)),
                                      "first": str(df["date"].iloc[0]),
                                      "last": str(df["date"].iloc[-1]),
                                      "secs": round(el, 1)}
            pending.pop(0)
            st.save()
            if n_run % 25 == 0:
                bf.log_line(LOGF, f"  … 本轮 {n_run} / 待刷余 {len(pending)}"
                                  f"（账户级请求 {st.d['req_today']}，单票 {el:.1f}s）")
        except Exception as e:                               # noqa: BLE001
            msg = str(e)[:120]
            tries = st.d.setdefault("tries", {})
            tries[code] = tries.get(code, 0) + 1
            if any(k in msg for k in ("权限", "积分", "抱歉")):
                st.d["failed"][code] = {"error": msg}
                pending.pop(0)
                bf.log_line(LOGF, f"  {code} 永久失败，跳过：{msg}")
            elif tries[code] >= 3:
                st.d["failed"][code] = {"error": f"{msg}（连续 {tries[code]} 次）"}
                pending.pop(0)
                bf.log_line(LOGF, f"  {code} 三振出局：{msg}")
            else:
                bf.log_line(LOGF, f"  {code} 失败（第 {tries[code]} 次，重试）：{msg}")
                time.sleep(30)
                bf.relogin(bs, LOGF)
            st.save()
            continue
        if args.sleep:
            time.sleep(args.sleep)
    bs.logout()
    st.save()
    bf.log_line(LOGF, f"退出: 本轮 {n_run} | done {len(st.d['done'])} | "
                      f"failed {len(st.d['failed'])} | 未完待续 {len(pending)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
