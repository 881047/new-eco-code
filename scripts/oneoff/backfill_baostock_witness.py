"""baostock 每日全量证券列表 —— **可交易域独立见证**（2015 起有历史）

## 为什么做（不是找 alpha，是治病）

KB 09 号开头：本项目全部行情/财务来自 Tushare(+AkShare 兜底) = **单一来源、零外部见证**，
而**最贵的事故恰好是 PIT 口径类**（买过生产买不到的 ST/退市/停牌股）。baostock 的
`query_all_stock(day=…)` 给出**当日全量证券 + tradeStatus(1=交易,0=停牌)**，且实测
**2015-06-30 就有 3,341 行**（有历史）⇒ 用它当"某日某票在册/可交易"的**独立第二源**。

它**不产出任何因子、不进模型**；产物只用来核对我们的可交易域与退市判定。

## 预注册（写于拉取之前）

  · 范围：2015-01-01 ~ 今天，逐年逐交易日 1 次调用（`query_all_stock(day)`）
  · 产物：`data/baostock_witness/all_stock_<YYYYMMDD>.parquet`（列 code/tradeStatus/code_name）
    + 状态 `data/.baostock_witness_backfill.json` + 日志 `logs/backfill_baostock_witness.log`
    **只写这三处，不碰任何生产表**
  · 排队：`--after <上游状态文件>`（默认等 `data/.baostock_min60_backfill.json` 的 `finished=true`）
    —— 因为**用户规矩是 baostock 不许并发**，所以本任务必须排在分钟线任务之后

## 规矩（与分钟线任务同一套，不许放宽）

  1. **严格串行**；2. 自设每日 ≤2 万请求（厂商硬限 5 万/天，超了进黑名单）；
  3. 时窗 `--windows 6-24`（用户 2026-09-15 允许采集进程跑；Codex 本人工作窗不放开），
     00:00–06:00 断网窗是**硬底线**（脚本拒绝）；4. 断点续跑；5. 错误分类同 09 号。

用法：
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\backfill_baostock_witness.py --limit 2   # 冒烟
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\backfill_baostock_witness.py             # 正式（自动排队）
"""
import argparse
import json
import socket
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import db  # noqa: E402

START, END = "2015-01-01", None            # END=None ⇒ 今天
DEFAULT_WINDOWS = "6-24"
#: 必须**绝对路径**：2026-09-17 实测踩坑 —— 相对路径按 cwd 解析，而守护（supervise.py）
#: 启动子进程时不设 cwd（继承启动器的目录）⇒ `data/...` 解析到别处、`exists()` 永远 False，
#: 见证任务就这样"排队"空等（日志一直打"排队中"，而上游其实早就 finished 了）。
DEFAULT_AFTER = str(Path(__file__).resolve().parent.parent.parent
                    / "data" / ".baostock_min60_backfill.json")


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
        self.d.setdefault("req_day", "")
        self.d.setdefault("req_today", 0)

    def save(self) -> None:
        self.d["updated"] = datetime.now().isoformat(timespec="seconds")
        self.path.write_text(json.dumps(self.d, ensure_ascii=False, indent=1), encoding="utf-8")

    def bump(self) -> None:
        today = date.today().isoformat()
        if self.d["req_day"] != today:
            self.d["req_day"], self.d["req_today"] = today, 0
        self.d["req_today"] += 1


def log_line(logf: Path, msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with logf.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def wait_for(upstream: Path, logf: Path) -> None:
    """排队：等上游状态文件出现 finished=true（baostock 不许并发）。"""
    while True:
        try:
            if upstream.exists() and json.loads(upstream.read_text(encoding="utf-8")).get("finished"):
                log_line(logf, f"上游 {upstream.name} 已完成 ⇒ 本任务开工")
                return
        except Exception as e:                                   # noqa: BLE001
            log_line(logf, f"读上游状态失败（忽略）：{str(e)[:80]}")
        log_line(logf, f"排队中：等 {upstream.name} 的 finished=true，5 分钟后再看")
        time.sleep(300)


def trading_days(conn, start: str, end: str) -> list:
    q = ("SELECT cal_date FROM trade_cal WHERE is_open=1 "
         "AND cal_date BETWEEN ? AND ? ORDER BY cal_date")
    return [r[0] for r in conn.execute(q, (start.replace("-", ""), end.replace("-", "")))]


def _bs_day(d: str) -> str:
    """trade_cal 是 YYYYMMDD，baostock `day` 要 YYYY-MM-DD（2026-09-15 实测：格式错报 10004010）。"""
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def relogin(bs, logf: Path) -> None:
    """换一条会话；**这里绝不能抛**（与 min60 同款事故：恢复路径抛异常 = 采集进程当场死掉，
    隐藏启动又没重定向 ⇒ 连 traceback 都没留下。见 07 号 #56/#57）。"""
    try:
        bs.logout()
    except Exception as e:                                       # noqa: BLE001
        log_line(logf, f"  logout 失败（忽略）：{str(e)[:80]}")
    time.sleep(2)
    try:
        lg = bs.login()
    except Exception as e:                                       # noqa: BLE001
        log_line(logf, f"  login 抛异常（等 5 分钟再试）：{str(e)[:80]}")
        time.sleep(300)
        return
    if lg.error_code != "0":
        log_line(logf, f"  登录失败 {lg.error_code} {lg.error_msg}（等 5 分钟再试）")
        time.sleep(300)


def main() -> int:
    ap = argparse.ArgumentParser(description="baostock 每日全量证券列表（可交易域见证）")
    ap.add_argument("--outdir", default=str(BASE / "data" / "baostock_witness"))
    ap.add_argument("--state", default=str(BASE / "data" / ".baostock_witness_backfill.json"))
    ap.add_argument("--log", default=str(BASE / "logs" / "backfill_baostock_witness.log"))
    ap.add_argument("--after", default=DEFAULT_AFTER, help="上游状态文件（finished=true 才开工）")
    ap.add_argument("--daily-cap", type=int, default=20000)
    ap.add_argument("--sleep", type=float, default=0.3)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--windows", default=DEFAULT_WINDOWS)
    args = ap.parse_args()

    outdir, state_path, logf = Path(args.outdir), Path(args.state), Path(args.log)
    outdir.mkdir(parents=True, exist_ok=True)
    logf.parent.mkdir(parents=True, exist_ok=True)
    windows = parse_windows(args.windows)
    st = State(state_path)
    conn = db.get_conn()
    end = END or datetime.now().strftime("%Y-%m-%d")
    days = trading_days(conn, START, end)
    conn.close()
    pending = [d for d in days if d not in st.d["done"] and d not in st.d["failed"]]
    log_line(logf, f"启动：时窗 {args.windows}；交易日 {len(days)} 天，已完成 {len(st.d['done'])}，待拉 {len(pending)}")
    if args.after:
        wait_for(Path(args.after), logf)

    import baostock as bs
    # 同分钟线任务：断网窗会打断 socket 而 vendor 不设超时 ⇒ 默认超时 + 心跳换会话
    # （2026-09-16 事故：min60 在断网窗后静默卡死 8 小时）
    socket.setdefaulttimeout(60)
    last_ok = time.time()
    relogin(bs, logf)                               # 首次登录（不许抛）

    n_run = 0
    while pending:
        if not _in_window(datetime.now(), windows):
            nxt = _next_window(datetime.now(), windows)
            log_line(logf, f"窗外，睡到 {nxt:%m-%d %H:%M}")
            time.sleep(max(60.0, (nxt - datetime.now()).total_seconds()))
            continue
        today = date.today().isoformat()
        if st.d["req_day"] == today and st.d["req_today"] >= args.daily_cap:
            nxt = _next_window(datetime.now() + timedelta(minutes=1), windows)
            log_line(logf, f"撞自设每日上限，睡到 {nxt:%m-%d %H:%M}")
            time.sleep(max(300.0, (nxt - datetime.now()).total_seconds()))
            continue
        if args.limit and n_run >= args.limit:
            log_line(logf, f"冒烟完成（limit={args.limit}），退出")
            break
        d = pending[0]
        if time.time() - last_ok > 600:
            log_line(logf, "  心跳超时 ⇒ 重登会话")
            relogin(bs, logf)
            last_ok = time.time()
        try:
            rs = bs.query_all_stock(day=_bs_day(d))
            if rs.error_code != "0":
                raise RuntimeError(f"{rs.error_code} {rs.error_msg}")
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            last_ok = time.time()
            st.bump()
            n_run += 1
            if rows:
                pd.DataFrame(rows, columns=rs.fields).to_parquet(
                    outdir / f"all_stock_{d.replace('-', '')}.parquet", index=False)
                st.d["done"][d] = {"rows": len(rows)}
            else:
                st.d["empty"][d] = datetime.now().isoformat(timespec="seconds")
                st.d["done"][d] = {"rows": 0}
            pending.pop(0)
            st.save()
            if n_run % 50 == 0 or args.limit:
                log_line(logf, f"  … 本轮 {n_run} 天 / 累计 {len(st.d['done'])} / 共 {len(days)}"
                               f"（今日请求 {st.d['req_today']}）")
        except Exception as e:                                   # noqa: BLE001
            msg = str(e)[:120]
            tries = st.d.setdefault("tries", {})
            tries[d] = tries.get(d, 0) + 1
            if any(k in msg for k in ("权限", "积分", "抱歉")):
                st.d["failed"][d] = {"error": msg}
                pending.pop(0)
                log_line(logf, f"  {d} 永久失败，跳过：{msg}")
            elif tries[d] >= 3:                     # 三振出局：别让一天把整个任务卡死
                st.d["failed"][d] = {"error": f"{msg}（连续 {tries[d]} 次）"}
                pending.pop(0)
                log_line(logf, f"  {d} 连续 {tries[d]} 次失败 ⇒ 记 failed 跳过：{msg}")
            else:
                log_line(logf, f"  {d} 失败（第 {tries[d]} 次，将重试）：{msg}")
                time.sleep(30)
                relogin(bs, logf)
            st.save()
            continue
        if args.sleep:
            time.sleep(args.sleep)
    bs.logout()
    if not pending and not args.limit:
        st.d["finished"] = True
    st.save()
    log_line(logf, f"退出：done={len(st.d['done'])} failed={len(st.d['failed'])} empty={len(st.d['empty'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
