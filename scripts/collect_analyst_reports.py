# -*- coding: utf-8 -*-
r"""分析师研报采集器 —— plans/analyst-data-pilot-prereg-20260928.md 的执行体。

数据源：ak.stock_research_report_em（东财研报，逐条报告带日期/机构/评级/分财年 EPS 预测）。
模式沿用 cninfo 语料采集器（collect_text_corpus.py）：串行、断点续采 manifest、
--shard/--nshards 分片。落 data/analyst/（gitignore 运行态）。

判据与验收见预注册（A1-A4）。**只采集，不做任何因子计算。**

⚠️ 冗余警告（2026-09-29）：research_report_em 表已在库（105,304 行/4,166 股/
2020-08~2026-09-13，09 号第 12 族），本脚本 pilot 判 VOIDED——仅当该表需增量刷新时
改造使用（含两个实测坑：无研报股空载荷 KeyError infoCode ⇒ 返回空表；
~20 分钟 150+ 请求触发东财 IP 封禁窗 ⇒ SLEEP≥3.5s）。

用法（pilot）：
    .venv\\Scripts\\python.exe -X utf8 scripts\\collect_analyst_reports.py --limit 300
全量（待拍板）：
    .venv\\Scripts\\python.exe -X utf8 scripts\\collect_analyst_reports.py --shard 0 --nshards 2
"""
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT_DIR = BASE / "data" / "analyst"
MANIFEST = OUT_DIR / "manifest.json"
RETRY = 2
SLEEP = 3.5          # v2：0.8s 触发东财 IP 封禁窗（pilot 首跑 152 只后 100% SSL EOF 约 2h）


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {}


def save_manifest(m: dict) -> None:
    MANIFEST.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")


def universe(limit: int | None) -> list[str]:
    import sqlite3
    con = sqlite3.connect(f"file:{BASE / 'data' / 'neweco.db'}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT ts_code FROM stock_basic WHERE ts_code NOT LIKE '%.BJ' "
            "ORDER BY ts_code").fetchall()
    finally:
        con.close()
    codes = [r[0] for r in rows]
    if limit:
        codes = codes[:limit]
    return codes


def fetch_one(sym6: str) -> pd.DataFrame | None:
    import akshare as ak
    try:
        df = ak.stock_research_report_em(symbol=sym6)
    except KeyError as e:
        if "infoCode" in str(e):
            return pd.DataFrame()          # 无研报股：API 空载荷在 akshare 内部炸键 ⇒ 空表非失败
        raise
    if df is None or len(df) == 0:
        return pd.DataFrame()
    keep = {"日期": "report_date", "机构": "org", "东财评级": "rating",
            "报告名称": "title", "行业": "industry"}
    out = df.rename(columns=keep)[[c for c in keep.values() if c in df.columns]].copy()
    out["ts_code"] = sym6
    for fy in ("2026", "2027", "2028"):
        col = f"{fy}-盈利预测-收益"
        out[f"eps_{fy}"] = pd.to_numeric(df[col], errors="coerce") if col in df.columns else None
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--nshards", type=int, default=1)
    ap.add_argument("--limit", type=int, default=None, help="pilot 用：只采前 N 只")
    a = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    codes = universe(a.limit)
    mine = [c for i, c in enumerate(codes) if i % a.nshards == a.shard]
    log(f"宇宙 {len(codes)} | 本片 {len(mine)} | manifest 已有 {sum(1 for v in manifest.values() if v.get('status') == 'done')}")
    parts, done, fail = [], 0, 0
    for i, code in enumerate(mine):
        sym6 = code.split(".")[0]
        if manifest.get(code, {}).get("status") == "done":
            done += 1
            continue
        df = None
        for attempt in range(RETRY + 1):
            try:
                df = fetch_one(sym6)
                break
            except Exception as e:  # noqa: BLE001
                wait = 5 * (attempt + 1)
                log(f"{code} 第{attempt + 1}次失败 {type(e).__name__}: {str(e)[:60]}，{wait}s 后重试")
                time.sleep(wait)
                df = None
        status = "done" if df is not None else "fail"
        if df is not None:
            parts.append(df)
            done += 1
        else:
            fail += 1
        manifest[code] = {"status": status, "rows": 0 if df is None else int(len(df)),
                          "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        if (i + 1) % 10 == 0:
            save_manifest(manifest)
            log(f"进度 {i + 1}/{len(mine)} | done {done} fail {fail}")
        time.sleep(SLEEP)
    save_manifest(manifest)
    if parts:
        shard_df = pd.concat(parts, ignore_index=True)
        out = OUT_DIR / f"reports_shard{a.shard}of{a.nshards}.parquet"
        shard_df.to_parquet(out, index=False)
        log(f"落盘 {out.name}: {len(shard_df):,} 行 / {shard_df['ts_code'].nunique()} 股")
    log(f"完成：done {done} / fail {fail}")
    return 0 if fail <= 0.05 * max(1, done + fail) else 1


if __name__ == "__main__":
    raise SystemExit(main())
