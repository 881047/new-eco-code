"""十大流通股东采集(Tushare `top10_floatholders`) → 表 `top10_holder`(2026-09-13)

**为什么是这一族**：前 12 族里已有 = 量价/财务/资金流/龙虎榜/股东户数/事件族/研报/金股；
唯一还没试过的是**机构持仓结构**（谁是前十大流通股东、持股比例怎么变）。而且它便宜：
单次 6000 行、`offset` 翻页、**无明显限速**（2026-09-13 实测 3 次/9 秒全通）
⇒ 全史(2020-09 起约 24 个报告期 × 约 9 页)≈ 220 次调用 ≈ 8 分钟。

**PIT 硬口径**：按 `ann_date`（公告日）对齐，**绝不许用 `end_date`**（报告期）——
季报披露有 1~4 个月滞后，用报告期就是提前知道还没公告的持仓。数据落在表里两列都存，
因子层只按 `ann_date` 取。

**可断点续跑**：检查点 `data/.top10_holder_backfill.json` 记每个报告期的 `next_offset` 与
已完成期；中断后重跑即续。**只写新表**，不碰既有表。

运行：
    set ONEOFF_CONFIRM=backfill_top10_holder.py
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\backfill_top10_holder.py
"""
import argparse
import datetime
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd                                                 # noqa: E402

from app import db                                                  # noqa: E402
from app import config as cfg_mod                                   # noqa: E402

STATE = cfg_mod.DATA_DIR / ".top10_holder_backfill.json"
START_PERIOD = "20200930"          # 死区起点所在报告期
PAGE = 6000                        # 上游单次上限（撞到它就说明还有下一页）
COLS = ["ts_code", "ann_date", "end_date", "holder_name", "hold_amount",
        "hold_ratio", "hold_float_ratio", "hold_change", "holder_type"]


def _quarters(start: str, end: str) -> list:
    per = pd.period_range(pd.Timestamp(start), pd.Timestamp(end), freq="Q")
    return [p.end_time.strftime("%Y%m%d") for p in per]


def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            print(f"[warn] 检查点损坏, 重建: {STATE}", flush=True)
    return {"done": [], "next_offset": {}, "rows": 0}


def _save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="十大流通股东全史补库（断点续跑）")
    ap.add_argument("--sleep", type=float, default=0.5, help="每次调用之间的间隔秒数")
    ap.add_argument("--start", default=START_PERIOD)
    args = ap.parse_args()

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_oneoff_guard", Path(__file__).resolve().parent / "_guard.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    guard.require_confirm(__file__, "写入新表 top10_holder(十大流通股东; 不碰既有表; 断点续跑)")

    from app.datasource.tushare_client import TushareClient
    ts = TushareClient()
    today = datetime.date.today().strftime("%Y%m%d")
    periods = _quarters(args.start, today)
    st = _load_state()
    done = set(st.get("done") or [])
    # 最近 2 个报告期**无条件重拉**（与 sync_holder_num 的"最近 4 期强制重拉"同一思路）：
    # 季报会补披露/更正，只按"这个 end_date 有没有行"判增量会把更正永久漏掉。
    todo = sorted(set([p for p in periods if p not in done] + periods[-2:]))
    print(f"[start] 报告期 {periods[0]}~{periods[-1]} 共 {len(periods)} | 已完成 {len(done)} | "
          f"本轮 {len(todo)}", flush=True)
    conn = db.get_conn()
    total, t0 = 0, time.time()
    try:
        for i, p in enumerate(todo, 1):
            off = int((st.get("next_offset") or {}).get(p, 0))
            n_period = 0
            while True:
                df = ts.call("top10_floatholders", period=p, offset=off, limit=PAGE)
                if ts.last_status == "perm":
                    print(f"[err] {p} 权限不足, 终止本轮", flush=True)
                    return 3
                if df is None or df.empty:
                    break
                miss = [c for c in COLS if c not in df.columns]
                if miss:
                    print(f"[err] {p} 缺列 {miss} —— 先修客户端", flush=True)
                    return 4
                n = db.upsert_df(conn, df[COLS].where(df[COLS].notna(), None), "top10_holder")
                n_period += n
                total += n
                if len(df) < PAGE:
                    break
                off += PAGE
                st.setdefault("next_offset", {})[p] = off
                _save_state(st)
                time.sleep(args.sleep)
            done.add(p)
            (st.get("next_offset") or {}).pop(p, None)
            st["done"] = sorted(done)
            st["rows"] = int(st.get("rows") or 0) + n_period
            _save_state(st)
            print(f"[ok] {i}/{len(todo)} {p}: {n_period} 行 | 累计 {total} | "
                  f"{time.time() - t0:.0f}s", flush=True)
            time.sleep(args.sleep)
    finally:
        conn.close()
    with db.conn_ctx() as c2:
        rows, per = c2.execute("SELECT COUNT(*), COUNT(DISTINCT end_date) FROM top10_holder").fetchone()
    print(f"[done] 表内 {rows} 行 / {per} 个报告期 | 用时 {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
