# -*- coding: utf-8 -*-
"""公告正文回填管线 —— tagged 语料的 PDF 下载 + pypdf 全文抽取。

plans/fulltext-backfill-pilot-prereg-20260929.md（判据跑前冻结）：
- 范围：6 个高价值类别 × 公告时间 [2024-09-01, 2026-09-29] × 每类等距 500 条
- 管线：静态地址 http://static.cninfo.com.cn/finalpage/{YYYY-MM-DD}/{announcementId}.PDF
  串行限速 1.2s/req（礼貌口径，cninfo 未公布频率上限）、重试 3 次；
  pypdf 抽全文（≤30 页）；断点续采（manifest 按 announcementId 记 done/failed）；
- 不存 PDF 本体（只存抽取文本，丢失可按 announcementId 重下）；
- 产物：data/text_corpus/fulltext/fulltext_{类别}.parquet + manifest_fulltext.json。

用法:
    .venv/Scripts/python.exe scripts/collect_text_fulltext.py               # 跑试点
    .venv/Scripts/python.exe scripts/collect_text_fulltext.py --dry-run     # 只列出选样
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import requests
from pypdf import PdfReader

log = logging.getLogger(__name__)

BASE = Path(__file__).resolve().parent.parent
TAGGED_DIR = BASE / "data" / "text_corpus" / "tagged"
OUT_DIR = BASE / "data" / "text_corpus" / "fulltext"
MANIFEST = OUT_DIR / "manifest_fulltext.json"

# 冻结范围（试点 §范围 + 全量模式 20260930 拍板）
CATEGORIES = ("earnings_forecast", "operating_contract_data", "ma_restructuring",
              "litigation_regulatory", "guarantee_financial_assistance", "buyback")
PILOT_WINDOW = ("2024-09-01", "2026-09-29")
ALL_WINDOW_START = "2019-01-01"            # tagged 语料窗口起点（与标题回填同源）
PER_CATEGORY = 500
MAX_PAGES = 30
RATE_LIMIT_S = 1.2
RETRIES = 3
DEFAULT_TIMEBOX = 1500                     # 全量计划任务的单轮时盒(秒), 到点保存退出

_STATIC = "http://static.cninfo.com.cn/finalpage/{date}/{aid}.PDF"
_AID_RE = re.compile(r"announcementId=(\d+)")
_UA = {"User-Agent": "Mozilla/5.0"}
_MONEY_RE = re.compile(r"\d+(?:\.\d+)?\s*[亿万万元]")

# 限流熔断(2026-09-30, 提速扩分片前加的安全垫): cninfo 未公布频率上限, robots 只禁
# /data/ 与 /user/(/finalpage/ 允许), 社区经验是高频会 403、静态 CDN 比查询接口宽容。
# 连续 BLOCK_LIMIT 次 403/429 ⇒ 本轮立即保存退出(计划任务下轮再续), 宁可慢不可被封。
_BLOCK_LIMIT = 5
_consecutive_block = 0


class _RateLimited(Exception):
    """连续 403/429 触顶 —— 上层捕获后保存退出本轮。"""


def _backoff_reset():
    global _consecutive_block
    _consecutive_block = 0


def load_manifest() -> dict:
    if MANIFEST.exists():
        try:
            return json.loads(MANIFEST.read_text(encoding="utf-8"))
        except Exception:
            log.warning("manifest 损坏, 重建(已采条目会重下, 幂等无害)")
    return {}


def save_manifest(m: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(MANIFEST) + ".tmp")
    tmp.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    import os
    os.replace(tmp, MANIFEST)


def select_samples(all_mode: bool = False, shard: int = 0, nshards: int = 1) -> dict[str, pd.DataFrame]:
    """选样：试点=6 类×2024-09 窗×每类等距 500；全量=6 类×2019-01 起全部条目。
    分片 = 全量模式下行序取模（i % nshards == shard），分片间零重叠。"""
    files = sorted(TAGGED_DIR.glob("*.parquet"))
    out: dict[str, list] = {c: [] for c in CATEGORIES}
    for f in files:
        code = f.stem
        try:
            df = pd.read_parquet(f)
        except Exception as e:
            log.warning("%s 读取失败: %s", f.name, e)
            continue
        if df.empty or "category" not in df.columns:
            continue
        dt = pd.to_datetime(df["公告时间"], errors="coerce")
        win = PILOT_WINDOW if not all_mode else (ALL_WINDOW_START, "2099-12-31")
        mask = (dt >= win[0]) & (dt <= win[1]) & dt.notna()
        sub = df[mask]
        if sub.empty:
            continue
        sub = sub.copy()
        sub["_dt"] = dt[mask]
        for cat in CATEGORIES:
            cdf = sub[sub["category"] == cat]
            if cdf.empty:
                continue
            cdf = cdf.sort_values("_dt").reset_index(drop=True)
            if all_mode:
                picked = cdf.iloc[shard::nshards]          # 行序取模分片, 零重叠
            elif len(cdf) <= PER_CATEGORY:
                picked = cdf
            else:                      # 等距抽样（覆盖全窗，不偏头尾）
                idx = pd.Series(range(len(cdf))).sample(
                    PER_CATEGORY, random_state=42).sort_values().to_numpy()
                picked = cdf.loc[idx]
            for _, r in picked.iterrows():
                m = _AID_RE.search(str(r.get("公告链接") or ""))
                if not m:
                    continue
                out[cat].append({
                    "代码": r["代码"], "简称": r.get("简称"),
                    "公告标题": r["公告标题"], "公告时间": r["公告时间"],
                    "announcementId": m.group(1), "category": cat,
                    "valence": r.get("valence"),
                    "_date": r["_dt"].strftime("%Y-%m-%d"),
                })
    res = {}
    for cat, rows in out.items():
        if all_mode:
            res[cat] = pd.DataFrame(rows).drop_duplicates("announcementId")
        else:
            res[cat] = pd.DataFrame(rows[:PER_CATEGORY * 2]).drop_duplicates(
                "announcementId").head(PER_CATEGORY)
    return res


def fetch_one(session: requests.Session, date: str, aid: str) -> tuple[str | None, str | None]:
    """下载并抽取 → (text, fail_reason)。text=None 表示失败。"""
    url = _STATIC.format(date=date, aid=aid)
    global _consecutive_block
    for attempt in range(RETRIES):
        try:
            r = session.get(url, timeout=20, headers=_UA)
            if r.status_code == 404:
                return None, "http_404"
            if r.status_code in (403, 429):
                # 限流信号: 退避 30s 再试; 连续 _BLOCK_LIMIT 次 ⇒ 熔断本轮(保存退出)
                _consecutive_block += 1
                log.warning("%s 返回 %d (连续 %d 次)", url, r.status_code,
                            _consecutive_block)
                if _consecutive_block >= _BLOCK_LIMIT:
                    raise _RateLimited(f"HTTP {r.status_code} ×{_consecutive_block}")
                time.sleep(30.0)
                continue
            r.raise_for_status()
            _backoff_reset()
            reader = PdfReader(io.BytesIO(r.content))
            text = "".join((p.extract_text() or "")
                           for p in reader.pages[:MAX_PAGES])
            # 2026-09-30: PDF 抽出的文本偶含未配对 UTF-16 代理字符(如 \udb40),
            # pyarrow 写 parquet 直接 UnicodeEncodeError 崩掉整轮(s1 实测)——
            # 入库前剥掉 UTF-8 非法字符。
            text = text.encode("utf-8", "ignore").decode("utf-8").strip()
            return (text if len(text) >= 100 else None), \
                (None if len(text) >= 100 else "empty_text")
        except requests.RequestException as e:
            if attempt < RETRIES - 1:
                time.sleep(2.0)
                continue
            return None, f"http_other:{type(e).__name__}"
        except _RateLimited:
            raise                              # 熔断不许被下面的兜底 except 吞掉
        except Exception as e:                       # pypdf 解析类
            return None, f"parse_fail:{type(e).__name__}"
    return None, "http_other:exhausted"


def main(all_mode: bool = False, shard: int = 0, nshards: int = 1,
         time_box: int = 0, dry_run: bool = False) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    global OUT_DIR, MANIFEST
    if all_mode:
        OUT_DIR = OUT_DIR.parent / "fulltext_all" / f"shard{shard}"
        MANIFEST = OUT_DIR / f"manifest_all_shard{shard}.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    samples = select_samples(all_mode=all_mode, shard=shard, nshards=nshards)
    total_pending = 0
    for cat, df in samples.items():
        done_n = sum(1 for _, r in df.iterrows()
                     if manifest.get(f"{cat}:{r['announcementId']}", {}).get("status") == "done")
        log.info("选样 %s: %d 条(已完成 %d)", cat, len(df), done_n)
        total_pending += len(df) - done_n
    if total_pending == 0:
        log.info("全部分片任务已完成, 秒退(计划任务可保留, 无副作用)")
        return 0
    # 计划任务防重叠: 上一轮 25 分钟时盒 < 30 分钟触发间隔, 但机器慢时可能撞车
    # ⇒ 分片级文件锁(非阻塞), 拿不到=上一轮还在跑, 秒退(下一轮再续)
    from app.filelock import FileLock
    _lock = FileLock(OUT_DIR / f"shard{shard}.lock")
    # retries=3 而非 1(2026-09-30, 与 extract_fulltext_fields_llm.py 同坑): 崩溃残留的
    # 陈旧锁接管需两次尝试 —— 第一次搬走死锁、第二次才建上自己的锁; retries=1 会在
    # 搬走后直接秒退, 白缺一轮(s1 崩溃后实测)。
    if not _lock.acquire(retries=3):
        log.info("分片 %d 上一轮仍在运行(锁被持有), 本轮秒退", shard)
        return 0
    if dry_run:
        for cat, df in samples.items():
            print(cat, len(df))
        return 0
    session = requests.Session()
    # 2026-10-01: 绕过系统代理直连 —— requests 默认读 Windows 注册表代理, 代理出口
    # 对 static.cninfo.com.cn 返回 400(疑似巨潮风控拉黑了 28 万次下载的代理出口 IP,
    # 或代理规则更新), 曾致 8 分片连续 4,183 条 http_other:HTTPError(13:00-14:07)。
    # 实测直连 200 完整 PDF; 巨潮是国内法定披露站点, 直连本就是正确路由。
    session.trust_env = False
    t0 = time.time()
    n_done = n_fail = 0
    summary = {}
    timebox_hit = False

    def merge_write(cat: str, new_rows: list) -> None:
        """**合并写**：本轮新增行与已有 parquet 合并去重后落盘——多轮时盒模式下
        每轮只拿到增量行, 直接覆写会丢历史轮数据(首版缺陷, 2026-09-30)。
        2026-09-30 二修: 改**原子落盘**(tmp + os.replace) —— 直写时并发读者
        (LLM 全量抽取轮启动装载输入)可能读到半截文件; replace 撞上正被打开
        读取的窗口会 PermissionError, 退避重试。"""
        if not new_rows:
            return
        path = OUT_DIR / f"fulltext_{cat}.parquet"
        new = pd.DataFrame(new_rows)
        if path.exists():
            old = pd.read_parquet(path)
            new = pd.concat([old, new], ignore_index=True)
        new = new.drop_duplicates("announcementId", keep="last")
        tmp = Path(str(path) + ".tmp")
        new.to_parquet(tmp, index=False)
        import os as _os
        for attempt in range(3):
            try:
                _os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(5)

    for cat, df in samples.items():
        rows = []
        for _, r in df.iterrows():
            aid = r["announcementId"]
            key = f"{cat}:{aid}"
            # 跳过一切有终态记录的条目(2026-09-30): 原先只跳 done, http_404/empty_text
            # 这类**永久性**失败每轮被重试 —— empty_text 每条要完整下载+解析 3 次才知道
            # 是扫描件, 纯浪费。暂时性失败(网络)重试价值有限且下轮 manifest 未记 done
            # 自然会重抓; 若未来要捞回终态失败, 单独清 manifest 对应键即可。
            if manifest.get(key, {}).get("status"):
                continue
            if time_box and (time.time() - t0) > time_box:
                timebox_hit = True
                break
            try:
                text, fail = fetch_one(session, r["_date"], aid)
            except _RateLimited as e:
                # 限流熔断: 复用时盒退出路径(保存 manifest + merge_write + 跳出),
                # 计划任务下一轮再续 —— 宁可慢, 不可被封 IP 后全分片停摆。
                log.error("限流熔断(%s): 本轮保存退出, 等下一轮计划任务", e)
                timebox_hit = True
                break
            if text is not None:
                rows.append({**{k: r[k] for k in
                                ("代码", "简称", "公告标题", "公告时间",
                                 "announcementId", "category", "valence")},
                             "n_chars": len(text), "text": text,
                             "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                manifest[key] = {"status": "done"}
                n_done += 1
            else:
                manifest[key] = {"status": "failed", "reason": fail}
                n_fail += 1
            time.sleep(RATE_LIMIT_S)
            if (n_done + n_fail) % 50 == 0:
                log.info("进度 %d done / %d fail (%.0fs)", n_done, n_fail, time.time() - t0)
                # 2026-09-30 换序: 先写 parquet 再存 manifest —— 原序在 parquet 写失败
                # (如代理字符崩 pyarrow)时 manifest 已标 done, 断点续采跳过 ⇒ 正文
                # 永久丢失(s1 实测丢 50 条)。先正文后 manifest: 失败时下轮重抓, 幂等。
                merge_write(cat, rows)
                save_manifest(manifest)
        if timebox_hit:
            merge_write(cat, rows)
            break
        merge_write(cat, rows)
        if rows:
            rdf = pd.read_parquet(OUT_DIR / f"fulltext_{cat}.parquet")
            hit = rdf["text"].str.contains(_MONEY_RE).mean()
            summary[cat] = {"n": int(len(rdf)),
                            "money_hit_rate": round(float(hit), 4),
                            "median_chars": int(rdf["n_chars"].median())}
            log.info("落库 %s: 累计 %d 条, 金额字段命中率 %.1f%%",
                     cat, len(rdf), 100 * hit)
    save_manifest(manifest)
    fails = {}
    for v in manifest.values():
        if v.get("status") == "failed":
            k = (v.get("reason") or "?").split(":")[0]
            fails[k] = fails.get(k, 0) + 1
    n_selected = sum(len(df) for df in samples.values())
    rate = round(n_done / max(1, n_done + n_fail), 4)
    out = {"asof": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "mode": "all" if all_mode else "pilot",
           "shard": shard, "nshards": nshards,
           "n_selected": n_selected, "n_done_this_round": n_done,
           "n_fail_this_round": n_fail, "valid_rate_this_round": rate,
           "fail_kinds": fails, "timebox_hit": timebox_hit,
           "category_summary": summary,
           "throughput_per_min": round((n_done + n_fail) / max(1e-9, (time.time() - t0) / 60), 1)}
    suffix = f"all_shard{shard}" if all_mode else "pilot"
    (OUT_DIR / f"result_{suffix}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("本轮完成: %s | done %d / fail %d | %.1f 条/分钟%s",
             "全量模式" if all_mode else "试点", n_done, n_fail,
             out["throughput_per_min"],
             " | 时盒到点保存退出(下轮续采)" if timebox_hit else "")
    _lock.release()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="公告正文回填管线(试点/全量分片)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true",
                    help="全量模式: 6 类×2019-01 起全部条目(分片取模), 写 fulltext_all/shardN/")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--nshards", type=int, default=1)
    ap.add_argument("--time-box", type=int, default=0,
                    help="单轮时盒秒数(计划任务模式用), 到点保存 manifest 退出")
    a = ap.parse_args()
    sys.exit(main(all_mode=a.all, shard=a.shard, nshards=a.nshards,
                  time_box=a.time_box, dry_run=a.dry_run))
