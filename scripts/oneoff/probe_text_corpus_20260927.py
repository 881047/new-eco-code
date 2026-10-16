# -*- coding: utf-8 -*-
"""文本语料源可行性探针 —— `plans/text-corpus-probe-prereg-20260927.md` 的执行体。

四问：可及性/覆盖度/对齐性/体量。逐源 try/except，失败记因不中断。
只读生产（不碰任何库表），样本数据截断内联进 experiments/text_probe_20260927.json。
联网探针：tushare pro（token 走 .env）+ akshare（东财）。限频友好：每源 ≤2 次调用。
"""
import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "text_probe_20260927.json"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def clip(obj, n=3, width=120):
    """样本截断内联"""
    if isinstance(obj, list):
        return [clip(x, n, width) for x in obj[:n]]
    if isinstance(obj, dict):
        return {k: (str(v)[:width] if isinstance(v, str) else v) for k, v in list(obj.items())[:8]}
    return obj


def df_summary(df):
    if df is None or len(df) == 0:
        return {"rows": 0}
    s = {"rows": int(len(df)), "columns": list(df.columns)[:10]}
    for c in df.columns:
        if any(k in str(c).lower() for k in ("time", "date", "日期", "时间", "pub", "publish")):
            s["time_col"] = str(c)
            try:
                t = df[c].astype(str)
                s["min"] = t.min()[:19]
                s["max"] = t.max()[:19]
            except Exception:  # noqa: BLE001
                pass
            break
    return s


def probe_akshare(res):
    try:
        import akshare as ak
        res["versions"]["akshare"] = ak.__version__
    except Exception as e:  # noqa: BLE001
        res["versions"]["akshare"] = f"IMPORT_FAIL: {e}"
        return
    # ① 东财个股新闻（per-stock）
    try:
        t0 = time.time()
        df = ak.stock_news_em(symbol="000001")
        res["stock_news_em"] = {**df_summary(df),
                                "latency_s": round(time.time() - t0, 1),
                                "samples": clip(df.to_dict(orient="records"))}
        log(f"stock_news_em: {len(df)} 行")
    except Exception as e:  # noqa: BLE001
        res["stock_news_em"] = {"error": f"{type(e).__name__}: {e}"[:300]}
        log(f"stock_news_em 失败: {e}")
    # ② 全市场公告（按日，测今天与 3 年前两个日期的深度）
    try:
        t0 = time.time()
        today = datetime.now().strftime("%Y%m%d")
        df_now = ak.stock_notice_report(symbol="全部", date=today)
        r_now = df_summary(df_now)
        old = (datetime.now() - timedelta(days=1100)).strftime("%Y%m%d")
        df_old = ak.stock_notice_report(symbol="全部", date=old)
        r_old = df_summary(df_old)
        res["stock_notice_report"] = {"recent": r_now, "three_years_ago": r_old,
                                      "latency_s": round(time.time() - t0, 1),
                                      "samples": clip(df_now.to_dict(orient="records"), 2)}
        log(f"stock_notice_report: 今日 {r_now.get('rows')} 行, 3 年前 {r_old.get('rows')} 行")
    except Exception as e:  # noqa: BLE001
        res["stock_notice_report"] = {"error": f"{type(e).__name__}: {e}"[:300]}
        log(f"stock_notice_report 失败: {e}")
    # ②b 财联社电报（全市场快讯 feed，带时间戳）
    try:
        t0 = time.time()
        df = ak.stock_telegraph_cls(date=datetime.now().strftime("%Y%m%d"))
        r_now = df_summary(df)
        old = (datetime.now() - timedelta(days=1100)).strftime("%Y%m%d")
        try:
            df_old = ak.stock_telegraph_cls(date=old)
            r_old = df_summary(df_old)
        except Exception as e:  # noqa: BLE001
            r_old = {"error": f"{type(e).__name__}: {e}"[:200]}
        res["stock_telegraph_cls"] = {"recent": r_now, "three_years_ago": r_old,
                                      "latency_s": round(time.time() - t0, 1),
                                      "samples": clip(df.to_dict(orient="records"), 2)}
        log(f"stock_telegraph_cls: 今日 {r_now.get('rows')} 行")
    except Exception as e:  # noqa: BLE001
        res["stock_telegraph_cls"] = {"error": f"{type(e).__name__}: {e}"[:300]}
        log(f"stock_telegraph_cls 失败: {e}")
    # ②c 巨潮个股公告（per-stock，深历史）
    try:
        t0 = time.time()
        start = (datetime.now() - timedelta(days=1100)).strftime("%Y%m%d")
        end = datetime.now().strftime("%Y%m%d")
        df = ak.stock_zh_a_disclosure_report_cninfo(symbol="000001", market="沪深京",
                                                    start_date=start, end_date=end)
        res["stock_zh_a_disclosure_cninfo"] = {**df_summary(df),
                                               "window_days": 1100,
                                               "latency_s": round(time.time() - t0, 1),
                                               "samples": clip(df.to_dict(orient="records"), 2)}
        log(f"disclosure_cninfo: 3 年 {len(df)} 条(000001)")
    except Exception as e:  # noqa: BLE001
        res["stock_zh_a_disclosure_cninfo"] = {"error": f"{type(e).__name__}: {e}"[:300]}
        log(f"disclosure_cninfo 失败: {e}")
    # ②d 巨潮深度测试（2019-01 起 —— bank 面板窗对齐的关键读数）
    try:
        t0 = time.time()
        df = ak.stock_zh_a_disclosure_report_cninfo(symbol="000001", market="沪深京",
                                                    start_date="20190101",
                                                    end_date=datetime.now().strftime("%Y%m%d"))
        res["stock_zh_a_disclosure_cninfo_deep"] = {
            "rows_since_2019": int(len(df)),
            "latency_s": round(time.time() - t0, 1),
            "time_col": df_summary(df).get("time_col"),
            "min": df_summary(df).get("min"), "max": df_summary(df).get("max")}
        log(f"disclosure_cninfo deep: 2019 起 {len(df)} 条")
    except Exception as e:  # noqa: BLE001
        res["stock_zh_a_disclosure_cninfo_deep"] = {"error": f"{type(e).__name__}: {e}"[:300]}
        log(f"disclosure_cninfo deep 失败: {e}")


def probe_tushare(res):
    load_dotenv(ROOT / ".env")
    token = os.environ.get("TUSHARE_TOKEN", "")
    try:
        import tushare as ts
        pro = ts.pro_api(token)
        res["versions"]["tushare"] = ts.__version__
    except Exception as e:  # noqa: BLE001
        res["versions"]["tushare"] = f"INIT_FAIL: {e}"[:200]
        return
    end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    start = (datetime.now() - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
    # 快讯 feed（非个股，但带时间戳与正文）—— 体量/天 的关键读数
    try:
        t0 = time.time()
        df = pro.news(src="sina", start_date=start, end_date=end)
        res["ts_news_sina"] = {**df_summary(df),
                               "latency_s": round(time.time() - t0, 1),
                               "rows_per_6h": int(len(df)),
                               "samples": clip(df.to_dict(orient="records"), 2)}
        log(f"ts.news(sina): 6h {len(df)} 行")
    except Exception as e:  # noqa: BLE001
        res["ts_news_sina"] = {"error": f"{type(e).__name__}: {e}"[:300]}
        log(f"ts.news(sina) 失败: {e}")
    # 公告类接口存在性探测（名字不确定，如实记录）
    found = {}
    for attr in ("notice", "anns", "report_rc", "major_news", "news_notice"):
        found[attr] = hasattr(pro, attr)
    res["ts_announce_iface_exists"] = found
    if found.get("major_news"):
        try:
            df = pro.major_news(src="")
            res["ts_major_news"] = {**df_summary(df),
                                    "samples": clip(df.to_dict(orient="records"), 2)}
        except Exception as e:  # noqa: BLE001
            res["ts_major_news"] = {"error": f"{type(e).__name__}: {e}"[:300]}
    if found.get("notice"):
        try:
            df = pro.notice(ts_code="000001.SZ", start_date="20230101", end_date="20231231")
            res["ts_notice"] = {**df_summary(df),
                                "samples": clip(df.to_dict(orient="records"), 2)}
            log(f"ts.notice: {len(df)} 行(2023 全年 000001.SZ)")
        except Exception as e:  # noqa: BLE001
            res["ts_notice"] = {"error": f"{type(e).__name__}: {e}"[:300]}
            log(f"ts.notice 失败: {e}")


def main() -> int:
    res = {"ran_at": datetime.now().isoformat(timespec="seconds"),
           "prereg": "plans/text-corpus-probe-prereg-20260927.md",
           "versions": {}, "sources": {}}
    t0 = time.time()
    try:
        probe_akshare(res)
    except Exception:  # noqa: BLE001
        res["sources"]["akshare_fatal"] = traceback.format_exc()[-500:]
    try:
        probe_tushare(res)
    except Exception:  # noqa: BLE001
        res["sources"]["tushare_fatal"] = traceback.format_exc()[-500:]
    res["runtime_sec"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    log(f"产物: {OUT} ({res['runtime_sec']}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
