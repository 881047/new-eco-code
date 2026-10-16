# -*- coding: utf-8 -*-
"""公告分类 Rubric 设计（LLM）—— `plans/text-factor-llm-labeling-prereg-20260927.md` 的执行体。

采样 200 条已回填真实标题 → LLM 设计公告分类体系（固定枚举 + 正则提示 + valence）。
LLM 输出强制 JSON；产物 data/text_corpus/rubric_v1.json + experiments 留痕。
"""
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd
import requests

from app import config as _cfg  # noqa: F401  触发 load_dotenv —— LLM_* 环境变量依赖此链
from app.web.llm import _llm_cfg, ASK_TIMEOUT

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "data" / "text_corpus" / "cninfo"
RUBRIC = ROOT / "data" / "text_corpus" / "rubric_v1.json"
OUT = ROOT / "experiments" / "llm_text_rubric_20260927.json"
N_SAMPLE = 200

PROMPT = """你是 A 股公告文本分类专家。下面是 {n} 条真实上市公司公告标题（巨潮资讯，2019 至今）。

任务：为"月频选股因子构造"设计一个**公告分类体系（rubric）**，要求：

1. categories：8~14 个互斥类别。每类给出：
   - name：英文 snake_case 枚举名（如 buyback）
   - cn：中文名
   - description：一句话判定标准
   - regex_hints：3~6 条 Python 正则提示（re.search 用，针对标题文本）
   - valence：pos / neg / neutral（对该股票未来收益的典型方向含义，如回购=pos、减持=neg）
   - expected_share：预计占全部公告的比例（如 "5-10%"）
2. title_conventions：公告标题的模板规律 3~6 条（供规则层利用）。
3. extraction_notes：标题里可直接抽取的结构化信息（业绩预告数字、增减持股数、
   中标金额等）各 1 条说明与示例正则。
4. ambiguous_guidance：什么样的标题应归 other / 需要第二层 LLM 判定。

只输出一个 JSON 对象（不要 markdown 代码块包裹），结构：
{{"categories": [...], "title_conventions": [...], "extraction_notes": [...], "ambiguous_guidance": "..."}}

标题样本：
{titles}
"""


def sample_titles() -> list:
    files = sorted(CORPUS.glob("*.parquet"))
    rng = random.Random(20260927)
    picks = rng.sample(files, min(50, len(files)))
    titles = []
    for p in picks:
        try:
            df = pd.read_parquet(p)
        except Exception:  # noqa: BLE001
            continue
        col = next((c for c in df.columns if "标题" in str(c)), None)
        if col is None:
            continue
        titles += [str(x).strip() for x in df[col].dropna().tolist() if str(x).strip()]
    rng.shuffle(titles)
    return titles[:N_SAMPLE]


def call_llm(prompt: str) -> str:
    c = _llm_cfg()
    text = ""
    for attempt in (1, 2, 3):
        try:
            r = requests.post(c["base_url"] + "/chat/completions",
                              headers={"Authorization": f"Bearer {c['api_key']}"},
                              json={"model": c["model"],
                                    "messages": [{"role": "user", "content": prompt}],
                                    "max_tokens": 32000, "temperature": 0.2},
                              timeout=max(300, ASK_TIMEOUT))
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"  [LLM] 连接/读失败(第 {attempt}/3 次): {str(e)[:100]}", flush=True)
            time.sleep(20)
            continue
        j = r.json()
        msg = j["choices"][0]["message"]
        text = (msg.get("content") or "").strip()
        if text:
            break
        print(f"  [LLM] content 空(finish={j['choices'][0].get('finish_reason')}), 重试 {attempt}/3",
              flush=True)
    return text


def parse_json(text: str):
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        if t.startswith("json"):
            t = t[4:]
    return json.loads(t.strip())


def main() -> int:
    titles = sample_titles()
    log = print
    log(f"样本标题 {len(titles)} 条（来自 {CORPUS.name} 现有 parquet）")
    prompt = PROMPT.format(n=len(titles),
                           titles="\n".join(f"{i+1}. {t}" for i, t in enumerate(titles)))
    t0 = time.time()
    text = call_llm(prompt)
    if not text:
        print("LLM 三次重试后仍无 content，中止", flush=True)
        return 1
    try:
        rubric = parse_json(text)
    except json.JSONDecodeError as e:
        log(f"JSON 解析失败: {e}；原文前 500 字：{text[:500]}")
        return 1

    cats = rubric.get("categories", [])
    log(f"rubric v1: {len(cats)} 类 | 耗时 {time.time()-t0:.0f}s")
    for c in cats:
        log(f"  {c.get('name'):24s} {c.get('cn')}  valence={c.get('valence')}  "
            f"regex×{len(c.get('regex_hints', []))}")

    RUBRIC.parent.mkdir(parents=True, exist_ok=True)
    RUBRIC.write_text(json.dumps(rubric, ensure_ascii=False, indent=2), encoding="utf-8")
    payload = {"ran_at": datetime.now().isoformat(timespec="seconds"),
               "prereg": "plans/text-factor-llm-labeling-prereg-20260927.md",
               "n_sample": len(titles), "sample_titles": titles,
               "llm_raw_head": text[:400],
               "rubric": rubric}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"产物: {RUBRIC} + {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
