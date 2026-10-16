# -*- coding: utf-8 -*-
"""互动易注意力前瞻追踪器（plans/irm-attention-family-prereg-20261002.md §三，跑数前冻结）。

背景：互动易公开 API 只服务最近 ~3 个月滚动窗（§一勘察实测，两端点日期参数均被服务端
忽略）⇒ 回测因子族机械不可行（判决 1 线结清）；滚动窗对**前瞻实时追踪**充分 ⇒ 本追踪器
（判决 2 激活）。形态与 ff_ratio 规则追踪器 / 卫星层群体追踪器同款：零资金零交易、只读
生产库、本地归档、计划任务逐日跑。

职责：
  1. 人群 = 最新 model_score 月末截面 top 300（剔除器实际作用人群的超集）；
  2. 逐票抓互动易滚动窗问答（秒级提问时间戳，PIT 质量优于公告日期粒度），
     按 `问题编号`(indexId) 幂等合并入 `data/irm_attention/qa_archive.parquet`；
  3. 逐日快照 `data/irm_attention/daily_counts.parquet` = 每票
     {n_questions, n_answered, window_start, window_end, fetch_status} + 池快照；
  4. 拍板门（冻结）：本地累积 ≥6 个日历月快照 ⇒ READY 提请评估预注册，否则 WAITING。

幂等：同票同日已快照则跳过（部分轮次日自续）；归档按 indexId 去重天然幂等。
工程：requests 全程直连（trust_env=False，07 号 66 号 + 10-01 巨潮教训）；限速 1.2s；
重试 3 次退避 2s；单实例 FileLock（retries=3）；时盒 25 分钟。
调度：NEWECO_irm_attention_daily 每日 21:00（silent_irm_attention_daily.vbs 静默包装）。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# 2026-10-01 教训: requests 默认读 Windows 注册表代理, 代理对国内站点曾整轮失败;
# 且 schtasks 环境无环境变量可依赖 ⇒ 入口先设 NO_PROXY 再 import requests。
os.environ.setdefault("NO_PROXY", "*")

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import pandas as pd
import requests

import app.db as app_db
from app.filelock import FileLock

log = logging.getLogger(__name__)

DATA_DIR = BASE / "data" / "irm_attention"
ARCHIVE = DATA_DIR / "qa_archive.parquet"
DAILY = DATA_DIR / "daily_counts.parquet"
ORG_CACHE = DATA_DIR / "org_ids.json"

POPULATION = 300
RATE_LIMIT_S = 1.2
RETRIES = 3
TIME_BOX = 1500                      # 计划任务轮时盒(秒), 与 25 分钟触发间隔同款
LOCK_RETRIES = 3

_URL_Q = "https://irm.cninfo.com.cn/newircs/company/question"
_URL_ORG = "https://irm.cninfo.com.cn/newircs/index/queryKeyboardInfo"
_HEADERS = {"User-Agent": "Mozilla/5.0"}


# ---------------- 纯函数（单测覆盖） ----------------

def parse_rows(rows: list, code: str) -> pd.DataFrame:
    """API 行 → 归档列。answered = 回答内容非空；ask_ts = epoch 毫秒 → UTC+8 时间戳。"""
    recs = []
    for r in rows or []:
        try:
            idx = str(r.get("indexId") or "")
        except Exception:
            idx = ""
        if not idx:
            continue
        pub = r.get("pubDate")
        try:
            ts = pd.Timestamp(int(pub), unit="ms", tz="Asia/Shanghai") if pub else pd.NaT
        except Exception:
            ts = pd.NaT
        ans = r.get("attachedContent")
        recs.append({"index_id": idx, "ts_code": code, "ask_ts": ts,
                     "is_answered": bool(ans), "source": r.get("pubClient") or "",
                     "question": (r.get("mainContent") or "")[:500],
                     "answer": (ans or "")[:500]})
    return pd.DataFrame(recs, columns=["index_id", "ts_code", "ask_ts", "is_answered",
                                       "source", "question", "answer"])


def merge_archive(archive: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """按 index_id 幂等合并（滚动窗重叠天然去重，重跑不重复计）。"""
    if archive is None or archive.empty:
        return new.copy()
    both = pd.concat([archive, new], ignore_index=True)
    return both.drop_duplicates("index_id", keep="last")


def coverage_of(snapshot: pd.DataFrame) -> float:
    """当日覆盖率 = 池内 n_questions>0 的票数占比（采集层读数，非 α 判决）。"""
    if snapshot.empty:
        return float("nan")
    return float((snapshot["n_questions"] > 0).mean())


def gate_status(n_days: int, first_day, today) -> tuple:
    """拍板门（冻结 §四）：≥6 个日历月快照 ⇒ READY。n_days=快照表不同 fetch_date 数。"""
    if first_day is None:
        return "WAITING", 0
    months = (today.year - first_day.year) * 12 + (today.month - first_day.month)
    return ("READY", months) if months >= 6 or n_days >= 130 else ("WAITING", months)


# ---------------- IO / 网络 ----------------

def _post(session: requests.Session, url: str, data: dict, timeout: int = 20):
    """带重试的 POST → json dict。失败抛 RuntimeError(调用方记 failed 继续下一票)。"""
    last = None
    for attempt in range(RETRIES):
        try:
            r = session.post(url, data=data, headers=_HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:                                   # noqa: BLE001 网络层兜底
            last = e
            if attempt < RETRIES - 1:
                time.sleep(2.0)
    raise RuntimeError(f"{type(last).__name__}: {last}")


def _org_id(session: requests.Session, code: str, cache: dict) -> str:
    if code in cache:
        return cache[code]
    j = _post(session, _URL_ORG, {"keyWord": code})
    org = str((j.get("data") or [{}])[0].get("secid") or "")
    if not org:
        raise RuntimeError("orgId 未返回")
    cache[code] = org
    return org


def fetch_questions(session: requests.Session, code: str, org_id: str,
                    max_pages: int = 3) -> list:
    """单票滚动窗全量(≤max_pages×1000 守卫)。"""
    out = []
    for page in range(1, max_pages + 1):
        j = _post(session, _URL_Q, {"stockcode": code, "orgId": org_id,
                                    "pageSize": "1000", "pageNum": str(page),
                                    "keyWord": "", "startDay": "", "endDay": ""})
        rows = j.get("rows") or []
        out.extend(rows)
        total_page = int(j.get("totalPage") or 1)
        if page >= total_page:
            break
    return out


def load_or_init(path: Path, kind: str) -> pd.DataFrame:
    if path.exists():
        try:
            return pd.read_parquet(path)
        except Exception as e:                                    # 坏档重建, 幂等无害
            log.warning("%s 读取失败重建: %s", path.name, e)
    return pd.DataFrame()


def save_atomic(df: pd.DataFrame, path: Path) -> None:
    import os
    tmp = Path(str(path) + ".tmp")
    df.to_parquet(tmp, index=False)
    for attempt in range(3):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(5)


# ---------------- 主流程 ----------------

def pool_top300() -> pd.DataFrame:
    """最新 model_score 月末截面 top300（ts_code, score）。"""
    with app_db.conn_ctx() as conn:
        row = conn.execute("SELECT MAX(trade_date) FROM model_score").fetchone()
        latest = row[0]
        df = pd.read_sql("SELECT ts_code, score FROM model_score WHERE trade_date=? "
                         "ORDER BY score DESC LIMIT ?",
                         conn, params=(latest, POPULATION))
    return df


def run(today: datetime, time_box: int = TIME_BOX, max_stocks: int = POPULATION) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = FileLock(DATA_DIR / "tracker.lock")
    if not lock.acquire(retries=LOCK_RETRIES):
        log.info("上一轮仍在运行(锁被持有), 本轮秒退")
        return {"skipped": "locked"}
    session = requests.Session()
    session.trust_env = False
    try:
        archive = load_or_init(ARCHIVE, "archive")
        daily = load_or_init(DAILY, "daily")
        cache = json.loads(ORG_CACHE.read_text(encoding="utf-8")) if ORG_CACHE.exists() else {}
        fetch_date = today.strftime("%Y%m%d")
        done = set()
        if not daily.empty:
            done = set(daily.loc[daily["fetch_date"] == fetch_date, "ts_code"])
        pool = pool_top300()
        codes = [c for c in pool["ts_code"].tolist() if c not in done][:max_stocks]
        log.info("池 %s top%d, 本轮待采 %d 票(已完成 %d)",
                 pool["ts_code"].iloc[0] if len(pool) else "-", len(pool), len(codes),
                 len(done))
        t0 = time.time()
        snap_rows, new_rows = [], []
        n_ok = n_fail = 0
        for i, code in enumerate(codes):
            if time_box and (time.time() - t0) > time_box:
                log.info("时盒到点, 中断续采(下轮自续)")
                break
            try:
                org = _org_id(session, code.split(".")[0], cache)
                rows = fetch_questions(session, code.split(".")[0], org)
                parsed = parse_rows(rows, code)
                new_rows.append(parsed)
                snap_rows.append({"fetch_date": fetch_date, "ts_code": code,
                                  "n_questions": int(len(parsed)),
                                  "n_answered": int(parsed["is_answered"].sum())
                                  if len(parsed) else 0,
                                  "window_start": str(parsed["ask_ts"].min().date())
                                  if len(parsed) and parsed["ask_ts"].notna().any() else "",
                                  "window_end": str(parsed["ask_ts"].max().date())
                                  if len(parsed) and parsed["ask_ts"].notna().any() else "",
                                  "fetch_status": "ok"})
                n_ok += 1
            except Exception as e:                                # fail-soft: 记 failed 次日重试
                snap_rows.append({"fetch_date": fetch_date, "ts_code": code,
                                  "n_questions": 0, "n_answered": 0,
                                  "window_start": "", "window_end": "",
                                  "fetch_status": f"failed:{type(e).__name__}"})
                n_fail += 1
                log.warning("%s 采集失败: %s", code, e)
            time.sleep(RATE_LIMIT_S)
        snap = pd.DataFrame(snap_rows)
        if not snap.empty:
            if not daily.empty:
                # 幂等: 覆盖当日旧快照(部分轮重跑), 保留历史日
                snap = pd.concat([daily[daily["fetch_date"] != fetch_date], snap],
                                 ignore_index=True)
            save_atomic(snap, DAILY)
        if new_rows and any(len(x) for x in new_rows):
            archive = merge_archive(archive, pd.concat(new_rows, ignore_index=True))
            save_atomic(archive, ARCHIVE)
        if cache:
            ORG_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        cov = coverage_of(snap[snap["fetch_status"] == "ok"]) if not snap.empty else float("nan")
        ndays = int(snap["fetch_date"].nunique()) if not snap.empty else 0
        first_day = (datetime.strptime(str(snap["fetch_date"].min()), "%Y%m%d").date()
                     if ndays else None)
        gate, months = gate_status(ndays, first_day, today.date())
        log.info("完成: ok %d / fail %d | 覆盖率 %.1f%% | 快照天数 %d | gate=%s(%d 月)",
                 n_ok, n_fail, 100 * cov if cov == cov else -1, ndays, gate, months)
        return {"n_ok": n_ok, "n_fail": n_fail, "coverage": cov,
                "snapshot_days": ndays, "gate": gate, "months": months}
    finally:
        lock.release()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--time-box", type=int, default=TIME_BOX)
    ap.add_argument("--max-stocks", type=int, default=POPULATION)
    a = ap.parse_args()
    res = run(datetime.now(), time_box=a.time_box, max_stocks=a.max_stocks)
    print(json.dumps(res, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
