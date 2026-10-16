# -*- coding: utf-8 -*-
"""要约收购报告书正文采集（plans/tender-offer-phase2-prereg-20261002.md §一，冻结）。

事件列表与 tender_offer_event_study_20261002.load_events 逐字同源（importlib 载入同目录
模块，禁裸名 import）；PDF 静态地址/直连/限速/重试/断点续采语义与 collect_text_fulltext
同款。产物 data/text_corpus/tender_offers/tender_offers.parquet（gitignore）。
"""
import json
import io
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import importlib.util
import pandas as pd
import requests
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "data" / "text_corpus" / "tender_offers"
OUT = OUT_DIR / "tender_offers.parquet"
MANIFEST = OUT_DIR / "manifest.json"

_STATIC = "http://static.cninfo.com.cn/finalpage/{date}/{aid}.PDF"
_AID_RE = re.compile(r"announcementId=(\d+)")
_UA = {"User-Agent": "Mozilla/5.0"}
RATE = 1.2
RETRIES = 3
MAX_PAGES = 40


def _load_study():
    spec = importlib.util.spec_from_file_location(
        "tender_study", Path(__file__).resolve().parent / "tender_offer_event_study_20261002.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_anchors() -> pd.DataFrame:
    """与事件研究逐字同源的事件列表 + 各自首份报告书的 announcementId/date。"""
    study = _load_study()
    files = sorted((ROOT / "data" / "text_corpus" / "cninfo").glob("*.parquet"))
    hits = []
    for f in files:
        df = pd.read_parquet(f, columns=["代码", "公告标题", "公告时间", "公告链接"])
        m = df[df["公告标题"].str.contains("要约收购", na=False)
               & df["公告标题"].str.contains("报告书", na=False)]
        if len(m):
            hits.append(m)
    allh = pd.concat(hits, ignore_index=True)
    allh["dt"] = pd.to_datetime(allh["公告时间"], format="mixed")
    allh = allh.sort_values("dt")
    rows, last = [], {}
    for _, r in allh.iterrows():
        k = r["代码"]
        if k not in last or (r["dt"] - last[k]).days > 60:
            rows.append(r)
        last[k] = r["dt"]
    ev = pd.DataFrame(rows)
    ev = ev[~ev["代码"].str.startswith(("4", "8", "9"))].reset_index(drop=True)
    ev["aid"] = ev["公告链接"].map(lambda u: (_AID_RE.search(str(u)) or [None, None])[1])
    ev["date"] = ev["dt"].dt.strftime("%Y-%m-%d")
    return ev[ev["aid"].notna()][["代码", "dt", "公告标题", "aid", "date"]]


def fetch_one(session, date, aid):
    url = _STATIC.format(date=date, aid=aid)
    for attempt in range(RETRIES):
        try:
            r = session.get(url, timeout=25, headers=_UA)
            if r.status_code == 404:
                return None, "http_404"
            r.raise_for_status()
            reader = PdfReader(io.BytesIO(r.content))
            text = "".join((p.extract_text() or "") for p in reader.pages[:MAX_PAGES])
            text = text.encode("utf-8", "ignore").decode("utf-8").strip()
            return (text if len(text) >= 100 else None), \
                (None if len(text) >= 100 else "empty_text")
        except requests.RequestException as e:
            if attempt < RETRIES - 1:
                time.sleep(2.0)
                continue
            return None, f"http_other:{type(e).__name__}"
        except Exception as e:
            return None, f"parse_fail:{type(e).__name__}"
    return None, "http_other:exhausted"


def main() -> int:
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}
    anchors = load_anchors()
    print(f"锚点 {len(anchors)} 份报告书", flush=True)
    session = requests.Session()
    session.trust_env = False
    rows = []
    n_done = n_fail = 0
    for _, a in anchors.iterrows():
        key = f"{a['代码']}:{a['aid']}"
        if manifest.get(key, {}).get("status"):
            if manifest[key]["status"] == "done":
                cached = manifest[key]
                rows.append({**cached["row"], "text": cached["text"]})
                n_done += 1
            else:
                n_fail += 1
            continue
        text, fail = fetch_one(session, a["date"], a["aid"])
        row = {"code": a["代码"], "t0": a["dt"].strftime("%Y%m%d"), "aid": a["aid"],
               "title": a["公告标题"], "n_chars": len(text) if text else 0}
        if text is not None:
            manifest[key] = {"status": "done", "row": row, "text": text}
            rows.append({**row, "text": text})
            n_done += 1
        else:
            manifest[key] = {"status": "failed", "reason": fail}
            n_fail += 1
        time.sleep(RATE)
    df = pd.DataFrame(rows)
    if len(df):
        tmp = Path(str(OUT) + ".tmp")
        df.to_parquet(tmp, index=False)
        import os
        os.replace(tmp, OUT)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    print(f"完成: done {n_done} / fail {n_fail} → {OUT.name}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
