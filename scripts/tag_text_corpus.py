# -*- coding: utf-8 -*-
r"""公告语料打标（两层）+ 验证协议 —— `plans/text-factor-llm-labeling-prereg-20260927.md` 的执行体。

用法（cwd 任意；--validate 联网）:
  .venv\Scripts\python.exe -X utf8 scripts\tag_text_corpus.py --tag-all      # tier-1 全量打标
  .venv\Scripts\python.exe -X utf8 scripts\tag_text_corpus.py --validate     # 300 条双通道一致率
  .venv\Scripts\python.exe -X utf8 scripts\tag_text_corpus.py --tag-all --limit 50

tier-1 = rubric_v1 正则层（类别序 = 优先级序，首个命中即归）；tier-2 = LLM 残余判定
（--validate 内对同一样本并行判定，强制枚举 JSON，temperature 0.2）。
验证判据（预注册 §一）：tier-1 vs tier-2 枚举一致率 ≥90% ⇒ rubric 合格。
产物: data/text_corpus/tagged/<code>.parquet + experiments/text_label_validate_<date>.json
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config as _cfg  # noqa: F401  触发 load_dotenv
import pandas as pd
import requests

from app.web.llm import _llm_cfg, ASK_TIMEOUT

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "text_corpus" / "cninfo"
TAGGED = ROOT / "data" / "text_corpus" / "tagged"
RUBRIC = ROOT / "data" / "text_corpus" / "rubric_v1.json"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_rubric() -> dict:
    return json.loads(RUBRIC.read_text(encoding="utf-8"))


def build_matchers(rubric: dict) -> list:
    """[(name, valence, [compiled_regex...]), ...] —— rubric 类别序 = 优先级序。
    LLM 生成的正则可能非法：编译失败的 hint 跳过并记录（不静默）。"""
    matchers, skipped = [], []
    for cat in rubric.get("categories", []):
        hints = []
        for h in cat.get("regex_hints", []):
            try:
                hints.append(re.compile(str(h)))
            except re.error as e:
                skipped.append(f"{cat['name']}: {str(h)[:40]} ({e})")
        matchers.append((cat["name"], cat.get("valence", "neutral"), hints))
    return matchers, skipped


def classify_title(title: str, matchers: list) -> tuple:
    for name, valence, hints in matchers:
        for rx in hints:
            if rx.search(title):
                return name, valence
    return "other", None


def iter_corpus(limit: int = 0):
    files = sorted(CORPUS.glob("*.parquet"))
    if limit:
        files = files[:limit]
    for p in files:
        try:
            df = pd.read_parquet(p)
        except Exception as e:  # noqa: BLE001
            log(f"读取失败 {p.name}: {e}")
            continue
        yield p, df


def cmd_tag_all(matchers, limit: int, skip_existing: bool = False) -> int:
    TAGGED.mkdir(parents=True, exist_ok=True)
    t0, n_rows, n_other = time.time(), 0, 0
    files = list(iter_corpus(limit))
    for k, (p, df) in enumerate(files):
        tcol = next((c for c in df.columns if "标题" in str(c)), None)
        if tcol is None:
            continue
        tp = TAGGED / p.name
        if skip_existing and tp.exists() and tp.stat().st_mtime >= p.stat().st_mtime:
            continue
        cats = df[tcol].astype(str).map(lambda t: classify_title(t, matchers))
        df["category"] = [c[0] for c in cats]
        df["valence"] = [c[1] for c in cats]
        df.to_parquet(TAGGED / p.name, index=False)
        n_rows += int(len(df))
        n_other += int((df["category"] == "other").sum())
        if (k + 1) % 100 == 0:
            log(f"已打标 {k+1}/{len(files)} 文件，{n_rows:,} 行（other 占比 "
                f"{n_other/max(n_rows,1):.1%}）")
    log(f"tier-1 完成: {len(files)} 文件 {n_rows:,} 行 | other 占比 "
        f"{n_other/max(n_rows,1):.1%} | 耗时 {(time.time()-t0)/60:.1f}min")
    return 0


def llm_tier2_batch(titles: list, rubric: dict, offset: int) -> list:
    cats = rubric["categories"]
    enum_lines = "\n".join(f"- {c['name']}: {c.get('cn','')} —— {c.get('description','')}"
                           for c in cats)
    body = "\n".join(f"{offset + i}. {t}" for i, t in enumerate(titles))
    prompt = (f"你是 A 股公告标题分类器。类别枚举（只能从中选）：\n{enum_lines}\n\n"
              f"对下列 {len(titles)} 条标题各判一个类别。只输出 JSON 数组，"
              f'形如 [{{"i": <标题行首的编号>, "category": "enum 名", "confidence": 0.9}}]，'
              f"不要 markdown 包裹。无法判断时 category 用 \"other\"。\n\n标题：\n{body}")
    c = _llm_cfg()
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
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            arr = json.loads(text.strip())
            if isinstance(arr, list) and arr:
                return arr
            log(f"tier-2 批次返回空/非数组（head: {text[:120]}）")
        except Exception as e:  # noqa: BLE001
            log(f"tier-2 批次失败(第 {attempt}/3 次): {str(e)[:120]}")
            time.sleep(15)
    return []


def cmd_validate(matchers, n: int = 300, seed: int = 20260927) -> int:
    import random
    rng = random.Random(seed)
    pool = []
    for p, df in iter_corpus():
        tcol = next((c for c in df.columns if "标题" in str(c)), None)
        if tcol is None:
            continue
        pool += [str(x).strip() for x in df[tcol].dropna().tolist() if str(x).strip()]
        if len(pool) >= 20000:
            break
    sample = rng.sample(pool, min(n, len(pool)))
    log(f"验证样本 {len(sample)} 条（来自 {len(pool):,} 标题池）")

    rubric = load_rubric()
    valence_by_name = {c["name"]: c.get("valence") for c in rubric["categories"]}
    t1 = [classify_title(t, matchers)[0] for t in sample]
    # 参照稳定化（prereg §五.3）：tier-2 参照 = 3 票多数决（每题 3 次独立判定，
    # 众数类别 + 平均置信度）—— 消除单次 LLM 判定的运行方差（实测 ±1.7pp）。
    from collections import Counter
    votes = {}
    B = 30
    for rep in range(3):
        for b in range(0, len(sample), B):
            for x in llm_tier2_batch(sample[b:b + B], rubric, offset=b):
                if not isinstance(x, dict):
                    continue
                try:
                    votes.setdefault(int(x["i"]), []).append(
                        (x.get("category"), float(x.get("confidence") or 0.0)))
                except (KeyError, TypeError, ValueError):
                    continue
        log(f"tier-2 参照轮 {rep+1}/3 完成（累计票 {sum(len(v) for v in votes.values())}）")
    t2_map = {}
    for i, vlist in votes.items():
        names = [c for c, _ in vlist if c]
        if not names:
            continue
        cat, _n = Counter(names).most_common(1)[0]
        conf = sum(cf for c, cf in vlist if c == cat) / max(
            sum(1 for c, _ in vlist if c == cat), 1)
        t2_map[i] = (cat, conf)

    # 预注册修订 §五.2（2026-09-27）：标题级天然歧义 ⇒ 双指标判定——
    # 总一致率（报告用）+ 高置信子集一致率（tier-2 confidence ≥0.8，判据用 ≥90%）；
    # 低置信标题生产上路由 other（只进事件强度，不进方向因子）。
    agree = mism = missing = 0
    agree_hc = judged_hc = 0
    mism_rows = []
    for i, t in enumerate(sample):
        t1c, t2c = t1[i], t2_map.get(i, (None, None))
        t2c, conf = t2c if t2c else (None, None)
        if t2c is None:
            missing += 1
            continue
        ok = t1c == t2c
        agree += int(ok)
        mism += int(not ok)
        if (conf or 0) >= 0.8:
            judged_hc += 1
            agree_hc += int(ok)
        elif not ok and len(mism_rows) < 30:
            mism_rows.append({"i": i, "title": t[:80], "tier1": t1c, "tier2": t2c,
                              "confidence": conf})
    judged = agree + mism
    rate = agree / judged if judged else 0.0
    rate_hc = agree_hc / judged_hc if judged_hc else 0.0
    verdict = ("PASS（rubric 合格）" if rate_hc >= 0.90 else "FAIL（迭代 rubric）")
    log(f"总一致率 {agree}/{judged} = {rate:.1%} | 高置信子集 {agree_hc}/{judged_hc} = "
        f"{rate_hc:.1%} ⇒ {verdict}（tier-2 缺答 {missing}）")

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"),
           "prereg": "plans/text-factor-llm-labeling-prereg-20260927.md",
           "n_sample": len(sample), "judged": judged, "agree": agree,
           "mismatch": mism, "tier2_missing": missing,
           "agreement_rate": round(rate, 4),
           "high_conf_subset": {"bar": 0.8, "judged": judged_hc, "agree": agree_hc,
                                "agreement_rate": round(rate_hc, 4)},
           "verdict": verdict, "mismatch_samples": mism_rows,
           "records": [{"i": i, "title": t[:100], "tier1": t1[i],
                        "tier2": t2_map.get(i, (None, None))[0],
                        "conf": t2_map.get(i, (None, None))[1]} for i, t in enumerate(sample)]}
    pth = ROOT / "experiments" / f"text_label_validate_s{seed}_{datetime.now().strftime('%Y%m%d')}.json"
    pth.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"产物: {pth}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag-all", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个文件")
    ap.add_argument("--seed", type=int, default=20260927, help="验证采样种子")
    ap.add_argument("--skip-existing", action="store_true",
                    help="tagged 文件比源新则跳过（增量打标）")
    args = ap.parse_args()
    rubric = load_rubric()
    matchers, skipped = build_matchers(rubric)
    if skipped:
        log(f"⚠️ {len(skipped)} 条 LLM 正则编译失败（已跳过并留痕）：{skipped[:3]}")
    if not matchers:
        log("rubric 无可用类别，中止")
        return 1
    if args.validate:
        return cmd_validate(matchers, seed=args.seed)
    if args.tag_all:
        return cmd_tag_all(matchers, args.limit, skip_existing=args.skip_existing)
    log("未指定动作：--tag-all 或 --validate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
