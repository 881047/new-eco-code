# -*- coding: utf-8 -*-
r"""公告语料全史回填管线 —— `plans/text-corpus-backfill-prereg-20260927.md` 的执行体。

源 = akshare 巨潮个股公告（标题级）。串行 + 断点续采（manifest per-stock 状态机）。
红线：股票间隔 ≥0.3s、单票重试 1 次、连续 20 败熔断、单次运行 ≤14h、生产库只读。

用法（cwd 任意，联网）:
  .venv/Scripts/python.exe -X utf8 scripts/collect_text_corpus.py            # 全量断点续跑
  .venv/Scripts/python.exe -X utf8 scripts/collect_text_corpus.py --limit 3  # 冒烟
  .venv/Scripts/python.exe -X utf8 scripts/collect_text_corpus.py --force --ts-codes 000001  # 重抓
产物: data/text_corpus/cninfo/<code>.parquet + data/text_corpus/manifest.json
"""
import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "neweco.db"
OUT_DIR = ROOT / "data" / "text_corpus" / "cninfo"
MANIFEST = ROOT / "data" / "text_corpus" / "manifest.json"
START_DATE = "20190101"
SLEEP_S = 0.3
MAX_RUNTIME_H = 14.0
BLOWUP_CONSEC_FAILS = 20


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_universe() -> list:
    con = sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT ts_code FROM stock_basic WHERE ts_code LIKE '%.SZ' "
            "OR ts_code LIKE '%.SH' OR ts_code LIKE '%.BJ' ORDER BY ts_code").fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def fetch_one(ak, ts_code: str):
    """→ DataFrame(公告标题级) 或抛异常。窗口 2019-01-01 → 今天。"""
    symbol = ts_code.split(".")[0]
    market = "沪深京" if ts_code.endswith((".SZ", ".SH")) else "京市"
    end = datetime.now().strftime("%Y%m%d")
    return ak.stock_zh_a_disclosure_report_cninfo(
        symbol=symbol, market=market, start_date=START_DATE, end_date=end)


def _worker_fetch(ts_code: str):
    """子进程 worker（spawn 安全：akshare 惰性导入）。抓取 + 落盘都在本进程完成，
    返回 (ok, rows, err) —— 主进程 get(timeout) 超时即 terminate 重建，防请求挂死。"""
    try:
        import akshare as ak
        df = fetch_one(ak, ts_code)
        if df is None or len(df) == 0:
            return True, 0, None
        df = df.rename(columns={c: str(c) for c in df.columns})
        df.to_parquet(OUT_DIR / f"{ts_code.split('.')[0]}.parquet", index=False)
        return True, int(len(df)), None
    except Exception as e:  # noqa: BLE001
        return False, 0, f"{type(e).__name__}: {e}"[:200]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 票（冒烟）")
    ap.add_argument("--force", action="store_true", help="重抓 done 票")
    ap.add_argument("--ts-codes", default=None, help="逗号分隔指定票（调试）")
    ap.add_argument("--fetch-timeout", type=float, default=240.0,
                    help="单票硬超时秒（子进程池 get 超时 → terminate 重建，标 failed）")
    ap.add_argument("--shard", type=int, default=0, help="分片序号 0..n-1")
    ap.add_argument("--nshards", type=int, default=1, help="总分片数（>1 时 manifest 按分片拆分）")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    mf_path = (MANIFEST.parent / f"manifest.shard{args.shard}of{args.nshards}.json"
               if args.nshards > 1 else MANIFEST)
    manifest = json.loads(mf_path.read_text(encoding="utf-8")) if mf_path.exists() else {}

    universe = load_universe()
    if args.nshards > 1:
        universe = universe[args.shard::args.nshards]
    if args.ts_codes:
        want = {c.strip() for c in args.ts_codes.split(",")}
        universe = [c for c in universe if c in want or c.split(".")[0] in want]
    if args.limit:
        universe = universe[:args.limit]

    # 启动对账: 磁盘 parquet = 单票完整抓取的证据(抓完才落盘), manifest 缺记的补 done
    import pandas as pd
    reconciled = 0
    for ts_code in universe:
        p = OUT_DIR / f"{ts_code.split('.')[0]}.parquet"
        if p.exists() and manifest.get(ts_code, {}).get("status") != "done":
            try:
                rows = int(len(pd.read_parquet(p)))
                manifest[ts_code] = {"status": "done", "rows": rows, "error": None,
                                     "updated_at": datetime.now().isoformat(timespec="seconds"),
                                     "note": "reconciled_from_disk"}
                reconciled += 1
            except Exception as e:  # noqa: BLE001
                log(f"对账读失败 {ts_code}: {e}")
    if reconciled:
        mf_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    log(f"宇宙 {len(universe)} 票 | manifest done {sum(1 for v in manifest.values() if v.get('status') == 'done')}"
        f"（对账补记 {reconciled}）")

    from multiprocessing import Pool
    import multiprocessing

    def _pool():
        return Pool(processes=1)

    t0 = time.time()
    done_n = fail_n = skip_n = 0
    consec_fails = 0
    pool = _pool()
    for i, ts_code in enumerate(universe):
        if time.time() - t0 > MAX_RUNTIME_H * 3600:
            log(f"达单次运行上限 {MAX_RUNTIME_H}h，保存 manifest 退出")
            break
        st = manifest.get(ts_code, {})
        if st.get("status") == "done" and not args.force:
            skip_n += 1
            continue
        ok, err, rows = False, None, 0
        async_res = pool.apply_async(_worker_fetch, (ts_code,))
        try:
            ok, rows, err = async_res.get(timeout=args.fetch_timeout)
            ok = bool(ok)
        except multiprocessing.TimeoutError:
            pool.terminate()
            pool.join()
            pool = _pool()
            ok, rows, err = False, 0, f"timeout>{args.fetch_timeout:.0f}s(已换新 worker)"
        except Exception as e:  # noqa: BLE001
            ok, rows, err = False, 0, f"{type(e).__name__}: {e}"[:200]
        if not ok and err and "timeout" not in str(err):
            # 网络类失败就地重试一次（超时型失败不再原地重试，防连挂）
            try:
                async_res = pool.apply_async(_worker_fetch, (ts_code,))
                ok, rows, err = async_res.get(timeout=args.fetch_timeout)
                ok = bool(ok)
            except Exception as e:  # noqa: BLE001
                ok, rows, err = False, 0, f"retry {type(e).__name__}: {e}"[:200]
        manifest[ts_code] = {"status": "done" if ok else "failed", "rows": rows,
                             "error": None if ok else err,
                             "updated_at": datetime.now().isoformat(timespec="seconds")}
        if ok:
            done_n += 1
            consec_fails = 0
        else:
            fail_n += 1
            consec_fails += 1
            log(f"失败 {ts_code}: {err}")
            if consec_fails >= BLOWUP_CONSEC_FAILS:
                log(f"连续 {consec_fails} 票失败 ⇒ 熔断（系统性故障），保存 manifest 退出")
                break
        log(f"[{i+1}/{len(universe)}] {ts_code} {'ok' if ok else 'FAIL'} rows={rows}"
            f" 累计done {sum(1 for v in manifest.values() if v.get('status') == 'done')}"
            f" 耗时{(time.time()-t0)/3600:.2f}h")
        if (i + 1) % 50 == 0:
            mf_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                               encoding="utf-8")
            elapsed_h = (time.time() - t0) / 3600
            log(f"进度 {i+1}/{len(universe)} | 本次 done {done_n} fail {fail_n} skip {skip_n}"
                f" | {elapsed_h:.2f}h")
        time.sleep(SLEEP_S)

    mf_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    total_done = sum(1 for v in manifest.values() if v.get("status") == "done")
    total_rows = sum(int(v.get("rows") or 0) for v in manifest.values())
    log(f"结束 | 本次 done {done_n} fail {fail_n} skip {skip_n} | "
        f"累计 done {total_done}/{len(universe)} 行 {total_rows} | "
        f"耗时 {(time.time()-t0)/3600:.2f}h")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
