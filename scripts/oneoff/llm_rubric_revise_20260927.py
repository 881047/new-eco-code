# -*- coding: utf-8 -*-
"""Rubric 修订（LLM 辅助，预注册 §一 迭代条款）：v1 + 错配样本 → v2（枚举/valence 不动，
只修 regex_hints）。产物 data/text_corpus/rubric_v2.json + experiments 留痕。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import config as _cfg  # noqa: F401  触发 load_dotenv
import requests

from app.web.llm import _llm_cfg, ASK_TIMEOUT

ROOT = Path(__file__).resolve().parents[2]
V1 = ROOT / "data" / "text_corpus" / "rubric_v1.json"
MIS = ROOT / "experiments" / "text_label_validate_s777_20260927.json"
OUT = ROOT / "data" / "text_corpus" / "rubric_v3.json"

PROMPT = """你是 A 股公告文本分类专家。这是一套公告分类 rubric（v1），300 条双通道验证
一致率 77.3% 未达标。错配样本显示：governance_meeting / audit_internal_esg /
financing_capital / periodic_report 等类的 regex_hints 太窄，大量模板化标题漏归 other。

错配样本（tier1→tier2，tier2 是正确方向的参考）：
{mismatches}

请**只修订各类别的 regex_hints**（枚举 name、valence、description 不许改），要求：
1. 覆盖上述错配样本的全部标题形态；
2. 每类 regex_hints 扩到 6~10 条，注意：决议公告/独立意见/续聘会计师事务所/变更董监高/
   财务报表/债券发行/ None 等高频模板必须命中；
3. 类别间仍须互斥优先级合理（rubric 数组顺序 = 优先级，可调顺序）；
4. 只输出 **categories 数组**（其余字段沿用 v1，由本脚本合并），不要 markdown 包裹。

v1 rubric 的 categories：
{v1cats}
"""


def main() -> int:
    v1 = json.loads(V1.read_text(encoding="utf-8"))
    mis = json.loads(MIS.read_text(encoding="utf-8"))
    mism = "\n".join(f"[{m['tier1']} → {m['tier2']}] {m['title']}"
                     for m in mis.get("mismatch_samples", []))
    prompt = PROMPT.format(mismatches=mism,
                           v1cats=json.dumps(v1["categories"], ensure_ascii=False, indent=1))
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
            text = (r.json()["choices"][0]["message"].get("content") or "").strip()
            if text:
                break
        except requests.RequestException as e:
            print(f"[LLM] 第 {attempt}/3 次失败: {str(e)[:120]}", flush=True)
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    new_cats = json.loads(text.strip())
    if not isinstance(new_cats, list) or not new_cats:
        print(f"LLM 未返回 categories 数组（head: {text[:200]}）")
        return 1
    v2 = {**v1, "categories": new_cats}   # 其余字段沿用 v1

    # valence 由 v1 按名权威合并（LLM 不重发该字段）；名字集合守卫
    v1_val = {c["name"]: c.get("valence") for c in v1["categories"]}
    v1_names = set(v1_val)
    for c in new_cats:
        if c.get("name") not in v1_names:
            print(f"守卫拦截：出现 v1 没有的类别 {c.get('name')}，拒绝写入")
            return 1
        c["valence"] = v1_val[c["name"]]
    v2 = {**v1, "categories": new_cats}   # 其余字段沿用 v1

    # 守卫：枚举名集合不许变（顺序 = 优先级，预注册允许调整）
    v2_names = {c["name"] for c in v2["categories"]}
    if v1_names != v2_names:
        print(f"守卫拦截：类别集合被改动（新增 {sorted(v2_names - v1_names)}，"
              f"删除 {sorted(v1_names - v2_names)}），拒绝写入")
        return 1
    n_h1 = sum(len(c.get("regex_hints", [])) for c in v1["categories"])
    n_h2 = sum(len(c.get("regex_hints", [])) for c in v2["categories"])
    OUT.write_text(json.dumps(v2, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"rubric v2 写入: {OUT} | regex_hints {n_h1} → {n_h2} 条 | 类别 {len(v2_names)} 个")
    (ROOT / "experiments" / "llm_rubric_revise_20260927.json").write_text(
        json.dumps({"ran_at": datetime.now().isoformat(timespec="seconds"),
                    "v3": v2, "llm_raw_head": text[:300]}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
