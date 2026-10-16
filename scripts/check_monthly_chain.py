r"""老盘月链跑成核验（只读；替代 Agent 自动化 24f5cb96，W2-F64）。

背景：monthly 档 18:33，退避重排是 APScheduler date job（服务重启即丢），
故需要一个独立的事实核验入口。
本脚本只报事实（查 pipeline_run + model_score 月末锚点覆盖），不做判断、
不发邮件、不修复；结论由人读日志拍板。2026-10-01 19:00 由计划任务拉起一次。

⚠️ 2026-10-13 修两处**过时假设** —— 该脚本曾因此报出**假警报**
「月链未跑（静默失败先例在册）」（2026-10-01 19:00 那条）：
  ① **stage 名**：月链实际落在 `monthly_sync`（app/scheduler.py:803）与
     `train`（同文件 L379 那条）两个 stage；`'monthly'` 只在 `_record_skip`
     分支出现（L485/L497）⇒ 查 `stage='monthly'` 必然为空。
     原 docstring 据此宣称的「pipeline_run stage=monthly 自 2026-09-12 后为零 =
     历史静默失败先例」**正是这次误判的产物**；实测 20261001 两个 stage 都是 ok。
  ② **状态词**：pipeline_run 的 status 取值是 `ok` / `skipped` / `failed`，
     **没有 `done`** ⇒ 原 `r[3] == "done"` 恒 False，`any_done` 永远报 False。
假警报与漏报同害（audit skill §7.9）：会让后续所有人对真故障脱敏。

跑法：`python -X utf8 scripts/check_monthly_chain.py [--expect 20260930]`
输出：stdout 一行结论 + 明细行（由 bat 落 data/logs/monthly_chain_check.log）。
全程 mode=ro，零写入。
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # 不硬编码盘符(换机/迁盘可跑)
DB = str(ROOT / "data" / "neweco.db")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect", default="20260930", help="本次月链应覆盖的月末锚点")
    ap.add_argument("--stage", default="monthly_sync,train",
                    help="逗号分隔的 stage 名。2026-10-13 前默认 'monthly' —— 已过时，"
                         "月链实际落在 monthly_sync + train")
    args = ap.parse_args()
    stages = [s.strip() for s in args.stage.split(",") if s.strip()]

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        ph = ",".join("?" * len(stages))
        rows = cur.execute(
            "select stage, started_at, ended_at, run_date, status, rows, error "
            f"from pipeline_run where stage in ({ph}) and run_date>=? order by id",
            (*stages, args.expect)).fetchall()
        ms = cur.execute(
            "select max(trade_date) from model_score").fetchone()[0]
        print(f"[monthly-chain-check] expect_anchor={args.expect} "
              f"stages={stages} model_score_max_trade_date={ms}")
        if not rows:
            print(f"[monthly-chain-check] RESULT: pipeline_run 无 stage in {stages} "
                  f"run_date>={args.expect} 的记录 —— 月链未跑，需人工排查")
        else:
            for r in rows:
                print(f"  pipeline_run: stage={r[0]} started={r[1]} ended={r[2]} "
                      f"run_date={r[3]} status={r[4]} rows={r[5]} error={r[6]}")
            # 2026-10-13：状态词是 ok/skipped/failed（无 'done'），原判据恒 False
            ok = any(r[4] == "ok" for r in rows)
            cover = str(ms) >= str(args.expect)
            print(f"[monthly-chain-check] RESULT: any_ok={ok} "
                  f"model_score_covers_anchor={cover} —— 事实如上，是否处置由人拍板")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
