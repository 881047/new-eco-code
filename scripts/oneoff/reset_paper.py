"""模拟盘全量初始化: 清空持仓/挂单/成交/净值/计息, 资金重置为初始资金, 并重新应用当前月度方案

用法: python scripts/oneoff/reset_paper.py [--capital 20000]
注意: 只重建挂单不执行成交(成交由服务在下一交易日按开盘价撮合)。

## 2026-09-14 改造(留痕) —— 起因是 09-10 那次重置"查不到重置前是什么"

1. **重置前强制归档**: 先调 `app.paper.archive` 把 6 张表导成小 JSON 并**读回自检**;
   自检不过就**中止**(宁可不动, 也不要一次没留痕的重置)。09-10 12:03 那次没留痕,
   事后只剩 4.6 GB 的全库备份可翻(KB 02/14 记着这笔)。
2. **默认资金改为读 config 的 `capital`**(与 `ensure_account` 同源), 不再硬编码 10000 ——
   硬编码会让"没带参数的一次重置"悄悄把账户规模改成第三个值。
3. 重置后在 `pipeline_run` 写一行 `stage='paper_reset'`(谁在何时重置、重置前多少行)。
4. 重置后按最新数据日补一个**净值基准点**(= 现金, 仓位 0)。不补的话, 看板/KPI 要等到
   次日 18:00 链才有第一个净值点; 重置前**同一天**的读数仍在该日归档文件里, 两者不互相覆盖。
"""
import argparse
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.logger import setup_logger
from app import db
from app import config as cfg_mod
from app import timeutil
from app.paper.engine import apply_plan, trading_paused, update_nav


def main():
    from _guard import require_confirm
    require_confirm(__file__, "清空模拟盘各表并重建 paper_account（重置纸面账户，不可逆）")
    from app.paper import archive as arc

    cfg = cfg_mod.load_config()
    parser = argparse.ArgumentParser()
    parser.add_argument("--capital", type=float, default=None,
                        help="初始资金(缺省读 config 的 capital, 兜底 10000)")
    args = parser.parse_args()
    capital = float(args.capital) if args.capital else float(cfg.get("capital", 10000))
    setup_logger()
    db.init_db()

    # 1) 重置前归档(只读打开, 写完读回自检; 失败即中止)
    payload = arc.dump(db.DB_PATH)
    arch_path = arc.write(arc.default_path(db.DB_PATH), payload)
    ok, detail = arc.verify(arch_path)
    if not ok:
        raise SystemExit("[重置中止] 归档自检失败, 没有动任何表: %s" % detail)
    print("[重置前归档] %s" % arch_path)
    print("  %s" % arc.summary_line(payload))
    print("  逐表行数: %s" % payload["counts"])

    conn = db.get_conn()
    # 2) 清空模拟盘全部业务表
    for t in ("paper_position", "paper_order", "paper_trade", "paper_nav", "paper_interest"):
        conn.execute("DELETE FROM %s" % t)
    # 重置账户: 现金=初始资金, 清空已应用方案标记
    now = timeutil.stamp()
    conn.execute(
        "INSERT INTO paper_account (id, cash, created_at, last_applied_plan, initial_capital) "
        "VALUES (1, ?, ?, NULL, ?) "
        "ON CONFLICT(id) DO UPDATE SET cash=excluded.cash, created_at=excluded.created_at, "
        "last_applied_plan=NULL, initial_capital=excluded.initial_capital",
        (capital, now, capital))
    # 当前方案锚定日(与 builder 默认口径一致: 已完结自然月的最新评分)
    plan_date = conn.execute(
        "SELECT MAX(trade_date) FROM model_score WHERE trade_date<?",
        (timeutil.date8()[:6] + "01",)).fetchone()[0]
    latest = conn.execute("SELECT MAX(trade_date) FROM daily").fetchone()[0]
    conn.commit()
    conn.close()

    # 3) 按当前方案重建挂单 + 补净值基准点
    if trading_paused():
        print("[注意] paper.paused=true ⇒ 交易总闸关着, 不会重建挂单(恢复后由 18:00 链按最新方案建)")
    n = apply_plan(plan_date)
    if latest:
        update_nav(latest)

    # 4) 库内留痕(重置前多少行 / 什么时候重置)
    conn = db.get_conn()
    sid = db.stage_start(conn, "paper_reset", latest or timeutil.date8())
    db.stage_finish(conn, sid, "ok", rows=sum(payload["counts"].values()))
    orders = conn.execute(
        "SELECT ts_code, side, shares, reason FROM paper_order WHERE status='pending'").fetchall()
    pos = conn.execute("SELECT COUNT(*) FROM paper_position").fetchone()[0]
    acct = conn.execute("SELECT cash, initial_capital FROM paper_account WHERE id=1").fetchone()
    nav = conn.execute("SELECT trade_date, nav, cash, market_value FROM paper_nav "
                       "ORDER BY trade_date DESC LIMIT 1").fetchone()
    conn.close()
    print(f"初始化完成: 方案 {plan_date}, 资金 {acct[0]:.2f}(初始 {acct[1]:.2f}), "
          f"持仓 {pos} 只, 重建挂单 {n} 笔:")
    for o in orders:
        print(" ", o)
    print("  净值基准点: %s" % (nav,))
    print("(挂单待下一交易日开盘执行, 不手动成交)")
    print("[留痕] pipeline_run 已写 stage='paper_reset' run_date=%s rows=%d"
          % (latest or timeutil.date8(), sum(payload["counts"].values())))


if __name__ == "__main__":
    main()
