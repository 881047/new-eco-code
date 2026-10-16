"""Tushare 分红送转（`dividend`）全史补库 —— 新事件族，本账号实测可用

## 为什么走 Tushare 而不是 baostock

baostock 也有 `query_dividend_data`，但它**必须给 code** ⇒ 全史 = 5,400 票 × 12 年 ≈ **6.5 万次调用**
（还要吃 baostock 每日 5 万配额、且与分钟线任务抢串行时隙）。**Tushare `dividend` 支持按
`ann_date` 一次拿全市场**（实测 `ann_date=20230901` 返回 5 行）⇒ 全史 ≈ 4,300 次调用、半小时级。
⇒ 按用户纪律"不要重复拉取"的反面同样成立：**能在主源拿到的，就不去第二个源抢配额**。

## 预注册（写于拉取之前）

  · 源：Tushare `dividend`（主源），按**公告日**逐日遍历 2015-01-01 ~ 今天（含非交易日，公告可落在周末）
  · 字段：ts_code / end_date / ann_date / div_proc（预案·股东大会通过·实施）/ stk_div / stk_bo_rate /
    stk_co_rate / cash_div / cash_div_tax / record_date / ex_date / pay_date / div_listann_date …
  · 产物：`data/dividend_ts/div_<YYYYMMDD>.parquet` + 状态 `data/.dividend_ts_backfill.json`
    + 日志 `logs/backfill_dividend_ts.log`。**只写这三处，不碰任何生产表**（不进库，等它过门禁再说）
  · `alpha_bank` 撞车预检（2026-09-15 实查）：171 条里 **0 条**分红/送转/质押类 ⇒ 无撞车

## 规矩

  1. 串行（Tushare 客户端自带账号级限速 0.32s/次 + 限流退避）；
  2. 时窗 `--windows 6-24`（用户 2026-09-15 允许采集进程跑；Codex 本人工作窗不放开），
     00:00–06:00 断网窗是**硬底线**；
  3. 断点续跑；4. `last_status` 分类：perm ⇒ 永久失败跳过；empty ⇒ 记空页；error ⇒ 下次续跑。

用法：
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\backfill_dividend_ts.py --limit 2   # 冒烟
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\backfill_dividend_ts.py             # 正式
"""
import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app.datasource.tushare_client import TushareClient  # noqa: E402

START = "2015-01-01"
DEFAULT_WINDOWS = "6-24"


def parse_windows(spec: str) -> tuple:
    out = []
    for part in spec.split(","):
        a, b = part.split("-")
        lo, hi = int(a), int(b)
        if lo < 6:
            raise SystemExit(f"时窗 {part} 撞到 00:00–06:00 断网窗 —— 硬底线，不许放宽")
        out.append((lo, 0, min(hi, 24), 0))
    return tuple(out)


def _in_window(now: datetime, windows: tuple) -> bool:
    m = now.hour * 60 + now.minute
    return any(sh * 60 + sm <= m < eh * 60 + em for sh, sm, eh, em in windows)


def _next_window(now: datetime, windows: tuple) -> datetime:
    for add in range(0, 2):
        day = (now + timedelta(days=add)).replace(second=0, microsecond=0)
        for sh, sm, _, _ in windows:
            cand = day.replace(hour=sh, minute=sm)
            if cand > now:
                return cand
    return now + timedelta(hours=6)


class State:
    def __init__(self, path: Path):
        self.path = path
        self.d = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.d.setdefault("done", {})
        self.d.setdefault("failed", {})
        self.d.setdefault("empty", {})

    def save(self) -> None:
        self.d["updated"] = datetime.now().isoformat(timespec="seconds")
        self.path.write_text(json.dumps(self.d, ensure_ascii=False, indent=1), encoding="utf-8")


def log_line(logf: Path, msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with logf.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def all_days(start: str, end: str) -> list:
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    return [(a + timedelta(days=i)).strftime("%Y%m%d") for i in range((b - a).days + 1)]


def main() -> int:
    ap = argparse.ArgumentParser(description="Tushare dividend 全史补库（按公告日逐日）")
    ap.add_argument("--outdir", default=str(BASE / "data" / "dividend_ts"))
    ap.add_argument("--state", default=str(BASE / "data" / ".dividend_ts_backfill.json"))
    ap.add_argument("--log", default=str(BASE / "logs" / "backfill_dividend_ts.log"))
    ap.add_argument("--sleep", type=float, default=0.0)
    ap.add_argument("--interval", type=float, default=2.0,
                    help="Tushare 账号级限速的**本任务配额**（秒/次）。默认 2.0s=30 次/分 = 账号 200/分"
                         "预算的 ~15% —— 刻意留余量给生产档（18:00/18:05/19:30/19:45 那几个）"
                         "与其它进程；**不要**调到 0.32（那会吃掉 93% 的账号预算，production 会被挤掉）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--windows", default=DEFAULT_WINDOWS)
    args = ap.parse_args()

    outdir, logf = Path(args.outdir), Path(args.log)
    outdir.mkdir(parents=True, exist_ok=True)
    logf.parent.mkdir(parents=True, exist_ok=True)
    windows = parse_windows(args.windows)
    st = State(Path(args.state))
    days = all_days(START, datetime.now().strftime("%Y-%m-%d"))
    pending = [d for d in days if d not in st.d["done"] and d not in st.d["failed"]]
    log_line(logf, f"启动：时窗 {args.windows}；日数 {len(days)}，已完成 {len(st.d['done'])}，待拉 {len(pending)}")
    cli = TushareClient()
    cli.min_interval = max(args.interval, 0.32)      # 只许更慢，不许比生产默认更激进
    n_run = 0
    while pending:
        if not _in_window(datetime.now(), windows):
            nxt = _next_window(datetime.now(), windows)
            log_line(logf, f"窗外，睡到 {nxt:%m-%d %H:%M}")
            time.sleep(max(60.0, (nxt - datetime.now()).total_seconds()))
            continue
        if args.limit and n_run >= args.limit:
            log_line(logf, f"冒烟完成（limit={args.limit}），退出")
            break
        d = pending[0]
        df = cli.call("dividend", ann_date=d)
        status = cli.last_status
        n_run += 1
        if status == "perm":
            st.d["failed"][d] = {"error": "perm"}
            pending.pop(0)
            log_line(logf, f"  {d} 权限不足 ⇒ 永久失败，跳过")
        elif status == "error":
            log_line(logf, f"  {d} 网络/服务异常 ⇒ 下次续跑")
            time.sleep(20)
        else:
            if df is not None and len(df):
                df.to_parquet(outdir / f"div_{d}.parquet", index=False)
                st.d["done"][d] = {"rows": int(len(df))}
            else:
                st.d["empty"][d] = datetime.now().isoformat(timespec="seconds")
                st.d["done"][d] = {"rows": 0}
            pending.pop(0)
        st.save()
        if n_run % 200 == 0 or args.limit:
            log_line(logf, f"  … 本轮 {n_run} 天 / 累计 {len(st.d['done'])} / 共 {len(days)}"
                           f"（有数据的天 {len(st.d['done']) - len(st.d['empty'])}）")
        if args.sleep:
            time.sleep(args.sleep)
    if not pending and not args.limit:
        st.d["finished"] = True
    st.save()
    log_line(logf, f"退出：done={len(st.d['done'])} failed={len(st.d['failed'])} empty={len(st.d['empty'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
