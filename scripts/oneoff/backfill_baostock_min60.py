"""baostock 60 分钟线全市场补库（只采分钟线，不重复拉已有数据）

## 为什么只采分钟线（用户纪律 2026-09-15）

日线 / 估值 / 财务**已经在 Tushare**（本项目主源）：日线重叠字段 2026-09-13 已抽样核对
（`scripts/oneoff/crosscheck_baostock.py`，52 只票：日收益 **0 不一致**、停牌标 **0 不一致**、
PE/PB 同日截面排序 **1.000 / 0.997**、ST 4 只票 47 天差异=我们更保守）⇒ **不重复拉**。
分钟线是本项目唯一没试过的**信息类别**（09 号：AkShare 东财分钟线被本机代理挡在 ProxyError；
baostock 走自己的协议，实测可达）。

## 预注册（写于拉取之前）

  · 范围：`daily_basic` 里 2020 年起出现过的 **沪深** 票；北交所（920xxx）baostock 不支持（已知缺口）
  · 频率：**60min**（先证前提；5min 只在门禁通过后才采，成本 12×）
  · 窗口：2020-01-01 ~ 今天（baostock 分钟线实测起点：2019-11 无、2020-01-02 有）
  · 字段：date,time,open,high,low,close,volume,amount,adjustflag（adjustflag=3 不复权）
  · 产物：`data/baostock_min60/<ts_code>.parquet` + 状态 `data/.baostock_min60_backfill.json`
    + 日志 `logs/backfill_baostock_min60.log`。**只写这三处，不碰任何生产表。**

## 规矩（逐条对应，不许放宽）

  1. **严格串行**：一次请求一只票，绝不并发（用户给的 baostock 限制）；
  2. **每日请求 ≤ 50000**（厂商硬限，超了进黑名单）⇒ 本脚本自设更紧的 `--daily-cap`（默认 20000），
     并把当日计数写进状态文件；撞上限就睡到第二天第一个窗口；
  3. **时窗**：默认 `--windows 6-9,12-14,18-24`（= Codex 自己的助手工作窗，见仓库 AGENTS.md §三）；
     用户 2026-09-15 明确允许**采集进程**在 09:00–12:00 / 14:00–18:00 也跑（原话："允许，但 codex
     依旧不许工作"）⇒ 正式运行传 `--windows 6-24`。**硬底线：00:00–06:00 断网窗永不允许**（脚本拒绝）；
  4. **断点续跑**：每只票落盘后立即写检查点，随时可停可续（Ctrl-C 安全）；
  5. **错误分类**（09 号）：`每分钟最多/频率` = 限流 ⇒ 退避重试；`权限/积分/抱歉` = 永久失败 ⇒ 记 failed 不再试。

**本脚本不产出任何结论**。结论只能在门禁（阶段 0c）里出，且因子必须先预注册 + 查 `alpha_bank` 撞车。

用法：
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\backfill_baostock_min60.py --limit 3   # 冒烟
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\backfill_baostock_min60.py             # 正式
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

START = "2020-01-01"
FIELDS = "date,time,open,high,low,close,volume,amount,adjustflag"
# 默认时窗 = Codex 自己的助手工作窗。用户 2026-09-15 允许**这个采集进程**放宽到 6-24，
# 但 Codex 本人的工作窗没有放开（两边是两件事，别混）。
DEFAULT_WINDOWS = "6-9,12-14,18-24"


def parse_windows(spec: str) -> tuple:
    """`'6-9,12-14,18-24'` → `((6,0,9,0), …)`。硬底线：不许含 00:00–06:00（断网窗）。"""
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
    """下一个窗口起点（窗外等它）。"""
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
        self.d.setdefault("started", datetime.now().isoformat(timespec="seconds"))

    def save(self) -> None:
        self.d["updated"] = datetime.now().isoformat(timespec="seconds")
        self.path.write_text(json.dumps(self.d, ensure_ascii=False, indent=1), encoding="utf-8")

    def req_bump(self) -> None:
        today = date.today().isoformat()
        if self.d["req_day"] != today:
            self.d["req_day"], self.d["req_today"] = today, 0
        self.d["req_today"] += 1


def log_line(logf: Path, msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with logf.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def universe(conn) -> list:
    """2020 起在 daily_basic 出现过的沪深票（含期间退市的 —— 不许有幸存者偏差）。"""
    q = ("SELECT DISTINCT ts_code FROM daily_basic WHERE trade_date >= '20200101' "
         "AND (ts_code LIKE '%.SH' OR ts_code LIKE '%.SZ') ORDER BY ts_code")
    return [r[0] for r in conn.execute(q)]


def bs_code(ts_code: str) -> str:
    c, suf = ts_code.split(".")
    return ("sh." if suf == "SH" else "sz.") + c


def fetch60(code: str, end: str) -> pd.DataFrame:
    import baostock as bs
    rs = bs.query_history_k_data_plus(code, FIELDS, start_date=START, end_date=end,
                                      frequency="60", adjustflag="3")
    if rs.error_code != "0":
        raise RuntimeError(f"{rs.error_code} {rs.error_msg}")
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    df = pd.DataFrame(rows, columns=rs.fields)
    if df.empty:
        return df
    for c in ("open", "high", "low", "close", "volume", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["dt"] = pd.to_datetime(df["time"].str[:14], format="%Y%m%d%H%M%S", errors="coerce")
    return df


def relogin(bs, logf: Path) -> None:
    """换一条会话；**这里绝不能抛**。

    2026-09-16 19:13 第二次事故：一次 `10002007 网络接收错误` 走进重试分支后，`bs.logout()`
    在坏 socket 上**抛了异常**（正是当天加的 60s 超时把"永久阻塞"变成了"抛异常"），
    而它在 `except` 块里 ⇒ 没人接 ⇒ 采集进程当场死掉；隐藏启动又没重定向输出 ⇒
    **连 traceback 都没留下**。恢复路径必须是哑的。
    """
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
    ap = argparse.ArgumentParser(description="baostock 60min 全市场补库（严格串行）")
    ap.add_argument("--outdir", default=str(BASE / "data" / "baostock_min60"))
    ap.add_argument("--state", default=str(BASE / "data" / ".baostock_min60_backfill.json"))
    ap.add_argument("--log", default=str(BASE / "logs" / "backfill_baostock_min60.log"))
    ap.add_argument("--daily-cap", type=int, default=20000, help="自设每日请求上限（厂商硬限 50000）")
    ap.add_argument("--sleep", type=float, default=0.3, help="两只票之间的间隔秒数")
    ap.add_argument("--limit", type=int, default=0, help="本轮只拉 N 只（冒烟用）")
    ap.add_argument("--windows", default=DEFAULT_WINDOWS,
                    help=f"允许跑的时窗（默认 Codex 工作窗 {DEFAULT_WINDOWS}；"
                         "用户 2026-09-15 允许采集进程用 6-24）")
    ap.add_argument("--no-schedule", action="store_true",
                    help="完全忽略时窗（**不要用**：会撞 00:00–06:00 断网窗空转）")
    args = ap.parse_args()

    windows = parse_windows(args.windows)
    outdir, state_path, logf = Path(args.outdir), Path(args.state), Path(args.log)
    outdir.mkdir(parents=True, exist_ok=True)
    logf.parent.mkdir(parents=True, exist_ok=True)
    st = State(state_path)
    conn = db.get_conn()
    codes = universe(conn)
    conn.close()
    pending = [c for c in codes if c not in st.d["done"] and c not in st.d["failed"]]
    log_line(logf, f"启动：时窗 {args.windows}；全市场 {len(codes)} 只，已完成 {len(st.d['done'])}，待拉 {len(pending)}，"
                   f"今日已用请求 {st.d['req_today']}"
                   f"{'，冒烟 limit=' + str(args.limit) if args.limit else ''}")
    end = datetime.now().strftime("%Y-%m-%d")

    import baostock as bs
    # 2026-09-16 事故：00:00–06:00 断网窗把 socket 打断后，baostock 的 recv 会**永久阻塞**
    # （vendor 的 socketutil 不设超时）⇒ 昨晚 23:59 起静默卡死 8 小时。两处一起修：
    #   ① 全局 socket 默认超时 ⇒ 卡住的读变成 socket.timeout 异常，走既有重试/重登录路径；
    #   ② 每轮开始时看心跳，超过 10 分钟没成功就换一条会话（防僵尸连接）。
    socket.setdefaulttimeout(60)
    last_ok = time.time()
    relogin(bs, logf)                               # 首次登录（同样不许抛）

    n_run = 0
    while pending:
        if not args.no_schedule and not _in_window(datetime.now(), windows):
            nxt = _next_window(datetime.now(), windows)
            log_line(logf, f"窗外，睡到 {nxt:%m-%d %H:%M}")
            time.sleep(max(60.0, (nxt - datetime.now()).total_seconds()))
            continue
        today = date.today().isoformat()
        if st.d["req_day"] == today and st.d["req_today"] >= args.daily_cap:
            nxt = _next_window(datetime.now() + timedelta(minutes=1), windows)
            log_line(logf, f"撞自设每日上限 {args.daily_cap}，睡到 {nxt:%m-%d %H:%M}")
            time.sleep(max(300.0, (nxt - datetime.now()).total_seconds()))
            continue
        if args.limit and n_run >= args.limit:
            log_line(logf, f"冒烟完成（limit={args.limit}），退出")
            break

        code = pending[0]
        if time.time() - last_ok > 600:             # 会话可能已死 ⇒ 换一条（注销+重登）
            log_line(logf, "  心跳超时 ⇒ 重登会话")
            relogin(bs, logf)
            last_ok = time.time()
        try:
            t0 = time.time()
            df = fetch60(bs_code(code), end)
            el = time.time() - t0
            last_ok = time.time()
            st.req_bump()
            n_run += 1
            if df.empty:
                st.d["empty"][code] = datetime.now().isoformat(timespec="seconds")
                st.d["done"][code] = {"rows": 0}
                log_line(logf, f"  {code} 空页（baostock 无该票数据）")
            else:
                df.to_parquet(outdir / f"{code}.parquet", index=False)
                st.d["done"][code] = {"rows": int(len(df)),
                                      "first": str(df["date"].iloc[0]),
                                      "last": str(df["date"].iloc[-1]),
                                      "secs": round(el, 1)}
            pending.pop(0)
            st.save()
            if n_run % 20 == 0 or args.limit:
                log_line(logf, f"  … 本轮 {n_run} 只 / 累计 {len(st.d['done'])} / 共 {len(codes)}"
                               f"（今日请求 {st.d['req_today']}，单票 {el:.1f}s）")
        except Exception as e:                                   # noqa: BLE001
            msg = str(e)[:120]
            tries = st.d.setdefault("tries", {})
            tries[code] = tries.get(code, 0) + 1
            if any(k in msg for k in ("权限", "积分", "抱歉")):        # 永久失败（09 号分类）
                st.d["failed"][code] = {"error": msg}
                pending.pop(0)
                log_line(logf, f"  {code} 永久失败，跳过：{msg}")
            elif tries[code] >= 3:                   # 三振出局：别让一只票把整个任务卡死
                st.d["failed"][code] = {"error": f"{msg}（连续 {tries[code]} 次）"}
                pending.pop(0)
                log_line(logf, f"  {code} 连续 {tries[code]} 次失败 ⇒ 记 failed 跳过：{msg}")
            else:
                log_line(logf, f"  {code} 失败（第 {tries[code]} 次，将重试）：{msg}")
                time.sleep(30)
                relogin(bs, logf)                   # 恢复路径抛异常 = 死进程（见 docstring）
            st.save()
            continue
        if args.sleep:
            time.sleep(args.sleep)
    bs.logout()
    if not pending and not args.limit:          # 全量跑完才置 finished（供下游任务排队用）
        st.d["finished"] = True
    st.save()
    log_line(logf, f"退出：done={len(st.d['done'])} failed={len(st.d['failed'])} "
                   f"empty={len(st.d['empty'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
