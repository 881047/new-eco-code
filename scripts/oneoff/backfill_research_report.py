"""东财研报采集（全市场按日期区间）→ 表 `research_report_em`（2026-09-13）

**为什么是它**（两条限额逼出来的）：
  · Tushare `report_rc` 实测限额 **10 次/天**（文案依次出现过 1 次/分钟、10 次/小时、10 次/天）
    ⇒ 全史 74 个月约 83 次调用要 **8 天多**，做不了研究；
  · AkShare 的 `stock_research_report_em` 是**逐只**封装（4.6 秒/只 × 5900 只 ≈ 8 小时）。
直接打东财 `reportapi.eastmoney.com/report/list` 可以 **`code=*` + 日期区间** 全市场取数，
分页上限 100/页：2026-08 一个月 1882 篇 = 19 页 ≈ 45 秒 ⇒ **全史约 1 小时**。

**拿到的信息比 Tushare 还全**：东财评级 + **上次评级 + 评级变动**、三年 EPS/PE 预测、
**目标价（高/低）**、机构、行业、作者、`infoCode`（唯一 ID，直接做主键）、PDF 链接。

**PIT**：一律按 `publishDate`（研报发布日）对齐 —— 与 `report_rc` 同一条规矩。
**年份标签**：EPS/PE 的列是"今年/明年/后年"，跨年含义会变 ⇒ 按**位置**存 `fy1..fy3`，
并把 `currentYear` 写进 `fy_years`（否则明年重跑会对着错的年份读）。

**可断点续跑**：检查点 `data/.research_report_backfill.json` 记每月的 `next_page` 与 `done_months`；
中断/重启后从未完成的页继续。**只写新表**，不碰任何既有表。

运行（后台，建议 detached）：
    set ONEOFF_CONFIRM=backfill_research_report.py
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\backfill_research_report.py
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
import requests                                                     # noqa: E402

from app import db                                                  # noqa: E402
from app import config as cfg_mod                                   # noqa: E402

URL = "https://reportapi.eastmoney.com/report/list"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Referer": "https://data.eastmoney.com/report/"}
STATE = cfg_mod.DATA_DIR / ".research_report_backfill.json"
START_MONTH = "2020-08"
PAGE_SIZE = 100                 # 上游硬上限（要 500/1000 也只回 100）
COLS = ["ts_code", "name", "report_date", "report_title", "org_name", "rating",
        "rating_prev", "rating_change", "industry", "fy_years",
        "fy1_eps", "fy1_pe", "fy2_eps", "fy2_pe", "fy3_eps", "fy3_pe",
        "aim_price_t", "aim_price_l", "report_type", "author", "info_code", "encode_url"]
# 数值列**显式列出**：不许用"以 fy/aim 开头就转数值"这种规则 —— 那会把 `fy_years`（年份标签，
# 值形如 "2026,2027,2028"）静默清成 NaN（2026-09-13 测试当场抓到）。
NUM_COLS = ["fy1_eps", "fy1_pe", "fy2_eps", "fy2_pe", "fy3_eps", "fy3_pe",
            "aim_price_t", "aim_price_l"]
DF_TEXT_COLS = ["ts_code", "name", "report_date", "report_title", "org_name", "rating",
                "rating_prev", "rating_change", "industry", "fy_years", "report_type",
                "author", "info_code", "encode_url"]


def _to_ts_code(code6) -> str:
    c = str(code6).zfill(6)
    if c.startswith(("60", "68", "90")):
        return f"{c}.SH"
    if c.startswith(("00", "30", "20")):
        return f"{c}.SZ"
    if c.startswith(("83", "87", "43", "92")):
        return f"{c}.BJ"
    return f"{c}.SH"


def map_records(data: list, current_year) -> pd.DataFrame:
    """东财 API 记录 → 表列。**EPS/PE 按位置映射**（列名含义随年份漂），年份标签存 `fy_years`。"""
    if not data:
        return pd.DataFrame(columns=COLS)
    years = ([str(current_year), str(int(current_year) + 1), str(int(current_year) + 2)]
             if current_year else [])
    rows = []
    for r in data:
        d = pd.to_datetime(r.get("publishDate"), errors="coerce")
        if pd.isna(d) or not r.get("infoCode"):
            continue
        rows.append({
            "ts_code": _to_ts_code(r.get("stockCode")), "name": r.get("stockName"),
            "report_date": d.strftime("%Y%m%d"), "report_title": r.get("title"),
            "org_name": r.get("orgSName") or r.get("orgName"),
            "rating": r.get("emRatingName"), "rating_prev": r.get("lastEmRatingName"),
            "rating_change": r.get("ratingChange"), "industry": r.get("industryName"),
            "fy_years": ",".join(years),
            "fy1_eps": r.get("predictThisYearEps"), "fy1_pe": r.get("predictThisYearPe"),
            "fy2_eps": r.get("predictNextYearEps"), "fy2_pe": r.get("predictNextYearPe"),
            "fy3_eps": r.get("predictNextTwoYearEps"), "fy3_pe": r.get("predictNextTwoYearPe"),
            "aim_price_t": r.get("indvAimPriceT"), "aim_price_l": r.get("indvAimPriceL"),
            "report_type": r.get("reportType"), "author": r.get("author"),
            "info_code": r.get("infoCode"), "encode_url": r.get("encodeUrl"),
        })
    df = pd.DataFrame(rows, columns=COLS)
    for c in NUM_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in DF_TEXT_COLS:
        df[c] = df[c].map(_flat)
    return df


def _flat(v):
    """把非标量拍平成字符串。

    2026-09-13 实测：东财的 `author` 有时直接返回**列表**（多作者）⇒ sqlite3 绑不了、
    整个进程崩在第一页。这里统一处理（列表用顿号连接、字典转 JSON、其余原样），
    并配合插入前的 `_sqlite_safe` 兜底：上游再变字段类型也不会让整夜任务白跑。
    """
    if isinstance(v, (list, tuple)):
        return "、".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return v


def _sqlite_safe(df: pd.DataFrame) -> pd.DataFrame:
    """插入前最后一道：任何 sqlite3 绑不了的 Python 对象（list/dict/Timestamp…）都转字符串。"""
    out = df.copy()
    for c in out.columns:
        out[c] = out[c].map(
            lambda v: (_flat(v).strftime("%Y-%m-%d %H:%M:%S")
                       if hasattr(v, "strftime") else _flat(v)))
    return out.where(out.notna(), None)


def _months(start: str, end: str) -> list:
    return [str(p) for p in pd.period_range(pd.Timestamp(start + "-01"),
                                            pd.Timestamp(end + "-01"), freq="M")]


def _month_bounds(m: str) -> tuple:
    per = pd.Period(m, freq="M")
    return per.start_time.strftime("%Y-%m-%d"), per.end_time.strftime("%Y-%m-%d")


def _fetch_month(m: str, session: requests.Session, state: dict, max_pages: int = 200) -> tuple:
    """抓一个月（翻页到没有数据为止）。返回 (DataFrame, 页数)。"""
    lo, hi = _month_bounds(m)
    pages, frames = 0, []
    page = int((state.get("next_page") or {}).get(m, 1))
    while page <= max_pages:
        params = {"industryCode": "*", "pageSize": PAGE_SIZE, "industry": "*", "rating": "*",
                  "ratingChange": "*", "beginTime": lo, "endTime": hi, "pageNo": page,
                  "fields": "", "qType": "0", "orgCode": "", "code": "*", "rcode": "",
                  "p": page, "pageNum": page}
        r = session.get(URL, params=params, timeout=30, headers=HEADERS)
        r.raise_for_status()
        d = r.json()
        data = d.get("data") or []
        pages += 1
        if not data:
            break
        frames.append(map_records(data, d.get("currentYear")))
        if page >= int(d.get("TotalPage") or 1):
            break
        page += 1
        time.sleep(0.15)                      # 公开接口: 礼貌间隔
    df = (pd.concat(frames, ignore_index=True) if frames
          else pd.DataFrame(columns=COLS))
    return df, pages


def main() -> int:
    ap = argparse.ArgumentParser(description="东财研报全市场采集（断点续跑）")
    ap.add_argument("--sleep", type=float, default=0.3, help="每页之间的间隔秒数")
    ap.add_argument("--start", default=START_MONTH)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_oneoff_guard", Path(__file__).resolve().parent / "_guard.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    guard.require_confirm(__file__, "写入新表 research_report_em(东财研报; 不碰既有表; 断点续跑)")

    end = args.end or datetime.date.today().strftime("%Y-%m")
    months = _months(args.start, end)
    st = {"done_months": [], "next_page": {}}
    if STATE.exists():
        try:
            st = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            print(f"[warn] 检查点损坏, 重建: {STATE}", flush=True)
    done = set(st.get("done_months") or [])
    todo = [m for m in months if m not in done]
    print(f"[start] 月份 {months[0]}~{months[-1]} 共 {len(months)} | 已完成 {len(done)} | "
          f"本轮 {len(todo)}", flush=True)
    conn = db.get_conn()
    session = requests.Session()
    total, t0 = 0, time.time()
    try:
        for i, m in enumerate(todo, 1):
            # 断点: 该月已抓过的页数从 next_page 起
            ls = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else st
            st.update(ls if isinstance(ls, dict) else {})
            df, pages = _fetch_month(m, session, st)
            if len(df):
                total += db.upsert_df(conn, _sqlite_safe(df), "research_report_em")
            done.add(m)
            (st.get("next_page") or {}).pop(m, None)
            st["done_months"] = sorted(done)
            st["rows"] = int(st.get("rows") or 0) + len(df)
            STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"[ok] {i}/{len(todo)} {m}: {len(df)} 行 / {pages} 页 | 累计 {total} 行 | "
                  f"{time.time() - t0:.0f}s", flush=True)
            time.sleep(args.sleep)
    finally:
        conn.close()
    with db.conn_ctx() as c2:
        n = c2.execute("SELECT COUNT(*) FROM research_report_em").fetchone()[0]
    print(f"[done] 表内合计 {n} 行 | 用时 {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
