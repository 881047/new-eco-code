"""研报标题 LLM 语义抽取 → 知识图谱文本边（2026-09-17，图谱轮完全体）

抽取语义（用户拍板全选）：主题(多标签) + 情绪(-1~1) + 确定性(事实/推测 0~1)
+ 催化剂类型(业绩超预期/新产能/技术突破/政策受益/订单落地/提价/并购/其它)
+ 数字承诺(目标价/EPS/增速%，可空)。

断点续跑：进度存 data/kg/title_extract.json（按 info_code 记录），中断重跑自动接续。
输出：kg_edge_event 表 edge_type IN ('catalyst','sentiment') + data/kg/title_semantics.jsonl
（全量语义留档，主题共现边后续派生用）。

用法: .venv/Scripts/python -X utf8 scripts/llm_title_extract.py [--limit N] [--batch 50]
"""
import argparse
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.logger import setup_logger  # noqa: E402
from app.web.llm import _llm_cfg, ASK_TIMEOUT  # noqa: E402

import requests  # noqa: E402

setup_logger()

CKPT = BASE / "data" / "kg" / "title_extract.json"
OUT_JL = BASE / "data" / "kg" / "title_semantics.jsonl"
BATCH = 50   # 固定批大小（2026-09-17 缓存优化：前缀字节级一致才命中 DeepSeek context cache）

# system 前缀（全批字节级一致 ⇒ DeepSeek context caching 自动命中，缓存输入价 1 折）。
# ⚠️ 任何日期/随机内容都不得进这段——那会让缓存永远 miss。
SYSTEM = """研报标题批量语义抽取。对每条输出 JSON 对象：
{"i": 序号, "topics": ["主题"], "sentiment": -1~1, "certainty": 0~1,
 "catalyst": "业绩超预期|新产能|技术突破|政策受益|订单落地|提价|并购|其它|无",
 "numbers": {"aim_price": 数值或null, "eps": 数值或null, "growth_pct": 数值或null}}

主题用通用行业/题材词（AI/半导体/新能源/医药/消费/军工/金融地产/机械/化工/汽车/传媒/其它）。
sentiment: 标题对该公司前景的态度（-1 极空 ~ +1 极多）。certainty: 0=纯预期推测, 1=已落地事实
（"业绩超预期"=0.9, "有望/前景/可期"=0.3）。catalyst 只选最主要一个。numbers 只抽标题中
明确写出的数字，没有就 null。只输出 JSON 数组，不要任何其它文字。"""

_title_cache: dict = {}


def extract_batch(rows: list, c: dict) -> list:
    payload = "\n".join(f"{i}. {r[3]}" for i, r in enumerate(rows))
    # 缓存优化（官方文档 api-docs.deepseek.com/zh-cn/guides/kv_cache + chat_prefix_completion）:
    # a) system 前缀字节级一致 ⇒ kv_cache 自动命中（缓存输入价 1 折，几小时内跨批复用）；
    # b) /beta 前缀续写: assistant prefix "[" 强制输出从 JSON 首字节开始——
    #    消除开场白 token（省 completion 费）且解析永不碰 markdown 围栏。
    base = c["base_url"].rstrip("/") + "/beta"
    r = requests.post(
        base + "/chat/completions",
        headers={"Authorization": f"Bearer {c['api_key']}"},
        json={"model": c["model"],
              "messages": [{"role": "system", "content": SYSTEM},
                           {"role": "user", "content": payload},
                           {"role": "assistant", "content": "[", "prefix": True}],
              "max_tokens": 16000, "temperature": 0.1},   # flash 为推理模型: 思考 token 占预算, 50 条批实测需 1.2 万+
        timeout=ASK_TIMEOUT)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"].strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    if not text.startswith("["):
        text = "[" + text          # assistant prefix "[" 被截走的补回
    text = text.rstrip(",")
    if not text.endswith("]"):
        text += "]"
    arr = json.loads(text)
    out = []
    for item in arr:
        i = item.get("i")
        if isinstance(i, int) and 0 <= i < len(rows):
            out.append((rows[i], item))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条(0=全部)")
    ap.add_argument("--batch", type=int, default=BATCH)
    args = ap.parse_args()
    c = _llm_cfg()
    if not c["api_key"]:
        print("✗ 未配置 LLM_API_KEY")
        return 2

    CKPT.parent.mkdir(parents=True, exist_ok=True)
    done = json.loads(CKPT.read_text(encoding="utf-8")) if CKPT.exists() else {}
    jl = OUT_JL.open("a", encoding="utf-8")

    from app import db
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT info_code, ts_code, report_date, report_title FROM research_report_em "
            "WHERE report_date >= ? AND report_title != '' ORDER BY report_date",
            ((__import__("datetime").date.today() - __import__("datetime").timedelta(days=3 * 365)).strftime("%Y%m%d"),)
        ).fetchall()
    finally:
        conn.close()
    todo = [r for r in rows if r[0] not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"标题总量 {len(rows):,}，已完成 {len(done):,}，本轮待处理 {len(todo):,}")

    n_ok = n_fail = 0
    t0 = time.time()
    for i in range(0, len(todo), args.batch):
        chunk = todo[i:i + args.batch]
        try:
            pairs = extract_batch(chunk, c)   # 传原 tuple(row), extract_batch 取 r[3]=标题
        except Exception as e:
            print(f"批 {i // args.batch + 1} 失败: {str(e)[:120]}，60s 后续跑")
            time.sleep(60)
            continue
        for (info_code, ts_code, rdate, title), sem in pairs:
            done[info_code] = 1
            jl.write(json.dumps({
                "info_code": info_code, "ts_code": ts_code, "date": rdate, **sem
            }, ensure_ascii=False) + "\n")
            # 文本边入库: 催化剂边(有催化剂时) + 情绪边(方向=情绪符号, 强度=|情绪|×确定性)
            try:
                conn2 = db.get_conn()
                cat = sem.get("catalyst")
                if cat and cat not in ("无", "其它"):
                    conn2.execute(
                        "INSERT INTO kg_edge_event (edge_type, ts_code, date8, entity, direction, strength, quote) "
                        "VALUES ('catalyst', ?, ?, ?, ?, ?, ?)",
                        (ts_code, rdate, cat,
                         1.0 if (sem.get("sentiment") or 0) >= 0 else -1.0,
                         abs(sem.get("sentiment") or 0), title[:80] or cat))
                if sem.get("sentiment") is not None:
                    conn2.execute(
                        "INSERT INTO kg_edge_event (edge_type, ts_code, date8, entity, direction, strength, quote) "
                        "VALUES ('sentiment', ?, ?, 'title', ?, ?, ?)",
                        (ts_code, rdate, sem["sentiment"],
                         abs(float(sem["sentiment"])) * float(sem.get("certainty") or 0.5),
                         title[:80]))
                conn2.commit()
                conn2.close()
                n_ok += 1
            except Exception:
                n_fail += 1
        jl.flush()
        CKPT.write_text(json.dumps(done), encoding="utf-8")
        done_n = len(done)
        rate = done_n / max(1e-9, time.time() - t0) * 60
        eta = (len(rows) - done_n) / max(rate, 1e-9)
        print(f"进度 {done_n:,}/{len(rows):,} ({done_n / len(rows):.0%}) | "
              f"{rate:.0f} 条/分 | ETA {eta:.0f} 分钟")
        _lu = globals().get("_last_usage") or {}
        if _lu.get("prompt_cache_hit_tokens"):
            print(f"  缓存命中 {_lu['prompt_cache_hit_tokens']}/"
                  f"{_lu.get('prompt_cache_miss_tokens', 0) + _lu['prompt_cache_hit_tokens']} prompt tokens")
    jl.close()
    print(f"✓ 完成: 成功 {n_ok}, 失败 {n_fail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
