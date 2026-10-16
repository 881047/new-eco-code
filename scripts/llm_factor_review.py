"""LLM 因子审读（离线研究辅助，2026-09-16 前端重构新增）

用法（先在 .env 配 LLM_API_KEY，可选 LLM_BASE_URL / LLM_MODEL）：

    .venv\\Scripts\\python -X utf8 scripts/llm_factor_review.py            # 默认审 CANDIDATE 前 40 条
    .venv\\Scripts\\python -X utf8 scripts/llm_factor_review.py --status POOLED --limit 20

做什么：读 alpha_bank 里候选因子的**表达式与读数**，让 LLM 逐条输出：
  · 经济含义（这个表达式在赌什么现象）
  · 风险红旗（前视/泄漏/拥挤/高换手/口径陷阱）
  · 建议关注度（high / mid / low）
结果写 data/llm/factor_review.json，看板「模型与数据」页只读展示。

**纪律**（与 app/alpha/factory.py 的 LLMProposer 同一口径）：
- LLM 的输出**不进任何门禁/打分/组合链路**，只作人读的批注；
- 判决仍归 IC/Bonferroni 门禁与组合层回测（"LLM 说好"不构成任何入池依据）；
- 每条输出都带模型名与时间戳，可追溯、可再生（重跑即覆盖）。

这是 STATUS: active 脚本（可重复运行，产物幂等覆盖）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.web import llm as llm_mod  # noqa: E402

BATCH = 8          # 一次喂给 LLM 的因子条数（省调用次数；太大输出质量下降）
RETRY = 1

_PROMPT = """你是量化因子研究审读员。下面是 A股多因子系统的 alpha_bank 候选因子（表达式 DSL 与
qlib Alpha158 同族：Ref/Mean/Std/Rank/Corr/Slope 等窗口算子，$close/$volume 等为日线字段）。

对每一条输出：
- meaning: 一句话经济含义（这个表达式在捕捉什么现象，赌的是什么错定价或行为偏差）
- flags: 风险红旗列表（从这几个里选或留空：["前视风险","存活偏差","高换手","拥挤/已知因子",
  "口径陷阱(复权/停牌/涨跌停)","过拟合迹象(表达式过于复杂)"]）
- verdict: high / mid / low —— **研究优先级**（不是入池判决，判决归门禁与组合层回测）
- note: 一句话备注（可空）

输入字段：name=因子名, expr=表达式, PASS=是否过门禁, p_adj=校正后p值, FULL_head=全史头部超额。
只输出 JSON 数组，不要其它文字：
[{{"name":"...","meaning":"...","flags":[...],"verdict":"high|mid|low","note":"..."}}, ...]

因子列表：
{rows}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 审读 alpha_bank 候选因子（离线研究辅助）")
    ap.add_argument("--status", default="CANDIDATE", help="alpha_bank 状态过滤（默认 CANDIDATE）")
    ap.add_argument("--limit", type=int, default=40, help="最多审读条数（默认 40）")
    args = ap.parse_args()

    if not llm_mod._llm_cfg()["api_key"]:
        print("✗ 未配置 LLM_API_KEY（.env；可选 LLM_BASE_URL/LLM_MODEL）。这是研究辅助工具，不配就不跑。")
        return 2

    rows = llm_mod.review_rows_from_bank(args.status, args.limit)
    if not rows:
        print(f"alpha_bank 里没有 status={args.status} 的行，无事可做。")
        return 0

    reviews = []
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        payload = "\n".join(
            f"- name={r['name']} | expr={r['expr']} | PASS={r['PASS']} | "
            f"p_adj={r['p_adj']} | FULL_head={r['FULL_head']} | alpha_id={r['alpha_id']}"
            for r in chunk)
        text = None
        for attempt in range(1 + RETRY):
            try:
                text = llm_mod._chat(
                    [{"role": "user", "content": _PROMPT.format(rows=payload)}],
                    max_tokens=2000)
                break
            except Exception as e:
                print(f"  批 {i // BATCH + 1} 第 {attempt + 1} 次调用失败: {e}")
        if not text:
            print(f"  批 {i // BATCH + 1} 放弃（保留此前结果）")
            continue
        try:
            parsed = json.loads(text.strip().removeprefix("```json").removesuffix("```").strip())
        except json.JSONDecodeError:
            print(f"  批 {i // BATCH + 1} 输出不是合法 JSON，跳过")
            continue
        by_name = {r["name"]: r for r in chunk}
        for item in parsed:
            base = by_name.get(item.get("name"))
            if base is None:
                # LLM 回显的 name 对不上库内任何一条 ⇒ 丢弃，不让幻觉因子名混进看板
                print(f"  ⚠ 丢弃未匹配项: {item.get('name')!r}")
                continue
            reviews.append({
                "alpha_id": base.get("alpha_id"),
                "name": item.get("name"),
                "expr": base.get("expr"),
                "meaning": item.get("meaning"),
                "flags": item.get("flags") or [],
                "verdict": item.get("verdict"),
                "note": item.get("note"),
                "gate": {"PASS": base.get("PASS"), "p_adj": base.get("p_adj"),
                         "FULL_head": base.get("FULL_head")},
            })
        print(f"  批 {i // BATCH + 1}/{(len(rows) + BATCH - 1) // BATCH} ✓ {len(parsed)} 条")

    if not reviews:
        print("✗ 没有任何成功批次，不写文件。")
        return 1
    llm_mod.DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "model": llm_mod._llm_cfg()["model"],
           "scope": f"status={args.status} 前 {args.limit} 条",
           "n_bank_rows": len(rows),
           "disclaimer": "LLM 批注仅为研究辅助，不构成入池依据；判决归门禁与组合层回测。",
           "reviews": reviews}
    llm_mod.REVIEW_FILE.write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✓ {len(reviews)} 条审读写入 {llm_mod.REVIEW_FILE}（看板「模型与数据」页可见）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
