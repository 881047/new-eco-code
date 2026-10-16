# -*- coding: utf-8 -*-
"""v2 LLM 辅助字段抽取（plans/fulltext-field-factors-prereg-20260930.md §六，判据冻结）。

定位：正则通道的补集覆盖（措辞变体）+ 主抽取器；正则通道降级为交叉校验通道。
- 模型：.env LLM_*（deepseek-flash），temperature=0；
- 输入：text 前 3,000 字符 + 每类字段 schema；输出：严格 JSON（金额统一万元）；
- 验证判据：C1 开发者金标准 60 条一致率 ≥90% / C2 格式合规 ≥98% /
  C3 与正则通道交集数值一致率 ≥95%。

用法:
    .venv/Scripts/python.exe scripts/extract_fulltext_fields_llm.py --gold      # 60 条金标准验证
    .venv/Scripts/python.exe scripts/extract_fulltext_fields_llm.py --pilot     # 试点 2,900 条全跑
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import requests

log = logging.getLogger(__name__)

BASE = Path(__file__).resolve().parent.parent
FULLTEXT_DIR = BASE / "data" / "text_corpus" / "fulltext"
FIELDS_DIR = BASE / "data" / "text_corpus" / "fields"
OUT_DIR = BASE / "data" / "text_corpus" / "fields_llm"

TEXT_CAP = 3000
GOLD_PER_CAT = 10

# 字段 schema（与预注册 §一 同义；LLM 端允许自由表述匹配）
SCHEMA = {
    "buyback": {
        "bb_amount_max_wan": "回购资金总额上限（万元）",
        "bb_amount_min_wan": "回购资金总额下限（万元）",
        "bb_shares_wan": "回购股份数量上限（万股）",
    },
    "guarantee_financial_assistance": {
        "gr_amount_wan": "本次担保金额（万元）",
        "gr_is_related": "被担保人是否为关联方/子公司（true/false）",
    },
    "operating_contract_data": {
        "oc_amount_wan": "合同金额（万元）",
    },
    "ma_restructuring": {
        "ma_price_wan": "交易作价/对价（万元）",
    },
    "litigation_regulatory": {
        "lit_amount_wan": "涉案金额（万元）",
    },
    "earnings_forecast": {
        "ef_np_min_wan": "预计净利润下限（万元，亏损为负数）",
        "ef_np_max_wan": "预计净利润上限（万元，亏损为负数）",
        "ef_yoy_pct": "净利润同比变动幅度（%，增长为正）",
    },
}


# 真实 token 用量累计(线程安全): 试点跑完直接换算 65 万条全量的成本报价(预注册 §七)。
# 牌价按 DeepSeek 官方(2026-09): 输入缓存命中 0.02 元/M、未命中 1 元/M、输出 2 元/M。
_USAGE = {"n": 0, "prompt_tokens": 0, "completion_tokens": 0,
          "prompt_cache_hit_tokens": 0, "reasoning_tokens": 0}
_USAGE_LOCK = threading.Lock()


def _acc_usage(body: dict) -> None:
    u = body.get("usage") or {}
    det = u.get("completion_tokens_details") or {}
    with _USAGE_LOCK:
        _USAGE["n"] += 1
        _USAGE["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        _USAGE["completion_tokens"] += int(u.get("completion_tokens") or 0)
        _USAGE["prompt_cache_hit_tokens"] += int(u.get("prompt_cache_hit_tokens") or 0)
        _USAGE["reasoning_tokens"] += int(det.get("reasoning_tokens") or 0)


def _cfg() -> dict:
    import os
    from app import config  # noqa: F401  触发 load_dotenv（llm.py 同款坑：只 import os 拿不到 key）
    return {"key": os.getenv("LLM_API_KEY", "").strip(),
            "base": (os.getenv("LLM_BASE_URL") or "https://api.deepseek.com").rstrip("/"),
            "model": os.getenv("LLM_MODEL") or "deepseek-chat"}


def _check_balance(cfg: dict) -> tuple[bool, float]:
    """GET /user/balance(官方端点) → (可用, 人民币余额)。
    **直连**(绕过系统代理): 2026-10-01 教训——代理对 api.deepseek.com SSL 抽风曾令
    余额查询失败, 保守继续(-1)使护栏失明, 3.40 元余额下轮次照常空跑。
    查询失败仍返回 (True,-1) 不阻塞(交由 API 402 兜底), 但直连后查询成功率恢复。"""
    try:
        r = requests.get(cfg["base"] + "/user/balance",
                         headers={"Authorization": f"Bearer {cfg['key']}"},
                         timeout=30, proxies={"http": None, "https": None})
        if r.status_code != 200:
            return True, -1.0
        b = r.json()
        if not b.get("is_available", True):
            return False, 0.0
        for info in b.get("balance_infos", []):
            if info.get("currency") == "CNY":
                return True, float(info.get("total_balance") or 0.0)
        return True, -1.0
    except Exception:
        return True, -1.0


def _post_chat(cfg: dict, prompt: str, timeout: int = 90) -> tuple[str | None, str | None]:
    """底层单次调用 → (content, 失败原因)。单篇与链式共用。"""
    for attempt in range(3):
        try:
            r = requests.post(
                cfg["base"] + "/chat/completions",
                headers={"Authorization": f"Bearer {cfg['key']}"},
                json={"model": cfg["model"], "temperature": 0,
                      "max_tokens": 800,
                      # DeepSeek 官方适配(2026-09-30, api-docs.deepseek.com):
                      # ① thinking 默认 enabled 且思考 token 计入 max_tokens 预算 —— 字段
                      #   抽取这类简单任务遇长表格会触发长思考吃光预算(content 空转直至
                      #   截断, 官方 JSON 指南原文"生成空白直到 token 限制…显得'卡住'",
                      #   即本次试点业绩预告类挂起的根因)。显式关闭: 实测同任务
                      #   0.7s/24 tokens(思考开启时 1.9s/297 tokens)。
                      # ② JSON Output(response_format)强制结构化, prompt 已含"JSON"指示
                      #   (官方要求), 抓不到的字段填 null 也符合 JSON 模式约束。
                      "thinking": {"type": "disabled"},
                      "response_format": {"type": "json_object"},
                      "messages": [{"role": "user", "content": prompt}]},
                # 直连(绕过系统代理, 2026-10-01 同巨潮教训: DeepSeek 为国内服务,
                # 代理 SSL 抽风曾致整轮请求失败标 failed)
                timeout=timeout, proxies={"http": None, "https": None})
            r.raise_for_status()
            body = r.json()
            _acc_usage(body)
            choice = body["choices"][0]
            if choice.get("finish_reason") == "length":
                return None, "max_tokens 截断(finish_reason=length)"
            return choice["message"].get("content"), None
        except requests.HTTPError as e:
            # 402 = 余额耗尽(官方欠费码): 不重试, 立即失败 —— 下一轮开工前的
            # 余额检查会停轮, 充值后自动恢复。
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code == 402:
                return None, "insufficient_balance"
            if attempt < 2:
                time.sleep(3)
                continue
            return None, f"HTTPError:{code}: {e}"
        except requests.RequestException as e:
            if attempt < 2:
                time.sleep(3)
                continue
            return None, f"{type(e).__name__}: {e}"
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"
    return None, "exhausted"


def ask_llm(cfg: dict, cat: str, text: str, header: str = "") -> tuple[str | None, str | None]:
    """抽取一次 → (原始响应文本, 失败原因)。header = few-shot 大头部(恒定共享前缀)。"""
    schema_txt = "\n".join(f'  "{k}": {v}' for k, v in SCHEMA[cat].items())
    prompt = (
        "从下面的 A 股公告正文中抽取字段。只输出一个 JSON 对象（不要解释、不要 markdown 代码块），"
        "键如下，正文里找不到的字段填 null；金额统一换算成万元（纯数字）；布尔用 true/false。\n"
        f"字段定义：\n{schema_txt}\n\n{header}公告正文：\n{text[:TEXT_CAP]}")
    return _post_chat(cfg, prompt, timeout=60)


def _chain_header(cat: str) -> str:
    """链式模式常量头 —— **必须逐字恒定**: 命中要求新请求完全匹配上一请求的
    完整输入单元(官方 kv_cache Example 1), 因此"抽最后一篇"的指令只能放头部,
    正文按【公告N】逐篇**追加在末尾**, 任何尾部指令都会断链。"""
    schema_txt = "\n".join(f'  "{k}": {v}' for k, v in SCHEMA[cat].items())
    return ("从下面按时间顺序给出的多篇 A 股公告正文中，抽取**最后一篇**公告的字段。"
            "只输出一个 JSON 对象（不要解释、不要 markdown 代码块），"
            "键如下，正文里找不到的字段填 null；金额统一换算成万元（纯数字）；布尔用 true/false。\n"
            f"字段定义：\n{schema_txt}\n\n")


_FEWSHOT_HDR: dict = {}


def _fewshot_header(cat: str, n: int, gold_aids: set, target_tokens: float = 0.0) -> str:
    """few-shot 大头部(2026-09-30, 用户目标"命中率≥90%"): 共享前缀必须足够大才会
    被持久化为缓存单元(实测 ~250 token 的指令头几乎不命中; 大头部自请求 1 起整头
    命中, hit 恒等于头部长度——生产池诊断 3,968 恒定)。示例取自试点 manifest,
    **排除金标准 60 条**防泄漏。聚合命中率 = H/(H+池均正文长) ⇒ 头部按
    9×池均文本 tokens 定容(target_tokens 由调用方按实际待抽池计算): 先按 1200 字
    补齐 N 例(已过 C1 门禁的配置), 再追加 2400 字长例直至达标 —— 前例逐字不变
    以锚定抽取行为。头部按命中价 0.02 元/M 重读(成本代价 ≈ +15-20%)。"""
    if cat in _FEWSHOT_HDR:
        return _FEWSHOT_HDR[cat]
    import pandas as pd
    pilot_path = OUT_DIR.parent / "fields_llm" / "manifest_llm_pilot.json"
    if not pilot_path.exists():
        _FEWSHOT_HDR[cat] = ""
        return ""
    pilot = json.loads(pilot_path.read_text(encoding="utf-8"))
    done = [(aid, v) for aid, v in pilot.items()
            if v.get("status") == "done" and v.get("category") == cat
            and str(aid) not in gold_aids]
    done.sort(key=lambda kv: (-sum(1 for x in kv[1]["fields"].values() if x is not None),
                              str(kv[0])))
    ftf = pd.read_parquet(FULLTEXT_DIR / f"fulltext_{cat}.parquet").set_index("announcementId")
    # 配比贴近该类真实 null 率(2026-09-30 迭代2): 首版"字段最全优先"教会模型过度给值
    # (拿调剂额度/估值冒充字段值, C1 93.6→89.1)。改为 满字段示例 + 按真实 null 率的
    # 缺字段示例(教拒答) + 头部硬规则。
    n_null_fields = sum(1 for _, v in done
                        if sum(1 for x in v["fields"].values() if x is not None) == 0)
    null_share = min(0.5, n_null_fields / max(1, len(done)))
    n_null = int(round(n * null_share))
    picked, used = [], set()

    def _take(aid, v):
        if len(picked) < n and aid in ftf.index and aid not in used:
            picked.append((aid, v))
            used.add(aid)
            return True
        return False

    nulls = [(aid, v) for aid, v in reversed(done)
             if sum(1 for x in v["fields"].values() if x is not None) == 0]
    for aid, v in nulls[:n_null]:            # 拒答示例按真实 null 率配比
        _take(aid, v)
    for aid, v in done:                      # 其余给字段最全的
        if len(picked) >= n:
            break
        _take(aid, v)
    for aid, v in done:                      # 凑不满就随便补
        if len(picked) >= n:
            break
        _take(aid, v)
    parts = []
    for i, (aid, v) in enumerate(picked):
        t = str(ftf.loc[aid, "text"])[:1200]
        fj = json.dumps({k: v["fields"][k] for k in SCHEMA[cat]}, ensure_ascii=False)
        parts.append(f"示例{i+1}正文：\n{t}\n示例{i+1}输出：{fj}")
    # 追加 2400 字长例至目标头部长度(2026-09-30): 聚合命中率 = H/(H+池均文本长),
    # 生产池实测(轮6)均文本 ~1,507 tok, 7 例头(3,968 tok)只能到 80.2% —— 按
    # target_tokens=9×池均 扩容。前 N 例逐字不变。
    if target_tokens > 0:
        est = sum(len(p) for p in parts) * 0.579
        ext_aids = [aid for aid, v in done if aid in ftf.index and aid not in used]
        j = 0
        while est < target_tokens and j < len(ext_aids) and len(picked) < 40:
            aid = ext_aids[j]
            j += 1
            v = pilot[aid]
            t = str(ftf.loc[aid, "text"])[:2400]
            fj = json.dumps({k: v["fields"][k] for k in SCHEMA[cat]}, ensure_ascii=False)
            parts.append(f"示例{len(parts)+1}正文：\n{t}\n示例{len(parts)+1}输出：{fj}")
            picked.append((aid, v))
            est = sum(len(p) for p in parts) * 0.579
    hdr = ("以下是从真实公告中抽取字段的示例（正文→正确输出）。硬规则：正文没有明确的"
           "定价/金额/数量表述时必须返回 null；严禁拿授权额度、估值、调剂额、融资余额、"
           "赎回款等其它数字冒充字段值。\n\n"
           + "\n\n".join(parts) + "\n\n")
    _FEWSHOT_HDR[cat] = hdr
    return hdr


def _chain_run_segment(cfg: dict, cat: str, seg: list, stop: threading.Event) -> list:
    """链式段: seg = [(cat, rep_row, todo), ...](≤chain 条, 与单篇模式同 item 形状,
    入账复用 _absorb)。串行累计请求 —— 每轮 prompt = 上一轮 prompt + 追加一篇,
    上轮完整输入成为本轮可完全命中的前缀单元(实测命中 95-98%)。
    返回 [(item, raw, err), ...] 交主线程入账。stop 置位时在请求间干净退出。"""
    prompt = _chain_header(cat)
    out = []
    for i, item in enumerate(seg):
        if stop.is_set():
            break
        _, rep, _todo = item
        prompt += f"【公告{i+1}】\n{str(rep['text'])[:TEXT_CAP]}\n\n"
        raw, err = _post_chat(cfg, prompt)
        out.append((item, raw, err))
    return out


def parse_json(raw: str | None) -> dict | None:
    if not raw:
        return None
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.S)
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, flags=re.S)
        if m:
            try:
                obj = json.loads(m.group())
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def pick_gold(df: pd.DataFrame, n: int = GOLD_PER_CAT) -> pd.DataFrame:
    return df.iloc[:: max(1, len(df) // n)][:n]


def _save_manifest(path: Path, manifest: dict) -> None:
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    import os
    os.replace(tmp, path)


def _coerce(field: str, v):
    """落盘前类型强制(2026-09-30): thinking 关闭后模型偶把区间原样吐成字符串
    ('-97.70~-96.63'), 混入 value 列令 parquet 双精度转换崩溃(exit 1)。数值字段
    只收数字(字符串取首个数值, 带千分位容错), 布尔字段归一化 —— 落盘列恒为数值型。"""
    if v is None:
        return None
    if field == "gr_is_related":
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        return str(v).strip().lower() in ("true", "1", "是", "yes")
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+(?:\.\d+)?", v.replace(",", ""))
        return float(m.group()) if m else None
    return None


def _flush_parquet(out_dir: Path, mode: str, manifest: dict) -> None:
    """从 manifest 重建长表落盘(幂等, 每次全量重写——2,900 条规模秒级)。
    2026-10-02 审查改**原子写**(tmp + os.replace, 与回填 merge_write 同款):
    原直写终路径, 并发读者(字段价值判断器读 llm_all.parquet)可能读到半截文件;
    replace 撞上正被打开的读取窗口会 PermissionError, 退避重试。"""
    rows = []
    for aid, v in manifest.items():
        if v.get("status") != "done":
            continue
        for k, val in v["fields"].items():
            rows.append({"代码": v["代码"], "公告时间": v["公告时间"],
                         "announcementId": aid, "category": v["category"],
                         "field": k, "value": _coerce(k, val),
                         "missing_key": k in (v.get("missing_key") or [])})
    if rows:
        import os
        path = out_dir / f"llm_{mode}.parquet"
        tmp = Path(str(path) + ".tmp")
        pd.DataFrame(rows).to_parquet(tmp, index=False)
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(5)


def _input_frames(all_mode: bool) -> dict[str, pd.DataFrame]:
    """输入语料: 试点/gold = fulltext/{cat}.parquet; 全量 = fulltext_all/shard*/ 拼接去重。
    读失败重试 3 次(回填 merge_write 落盘窗口碰撞), 仍失败跳过该分片文件 —— 文档不丢,
    下一轮自然补上(断点续采)。"""
    res = {}
    for cat in SCHEMA:
        if not all_mode:
            res[cat] = pd.read_parquet(FULLTEXT_DIR / f"fulltext_{cat}.parquet")
            continue
        dfs = []
        for f in sorted((BASE / "data" / "text_corpus" / "fulltext_all").glob(
                f"shard*/fulltext_{cat}.parquet")):
            for attempt in range(3):
                try:
                    dfs.append(pd.read_parquet(f))
                    break
                except Exception as e:
                    if attempt == 2:
                        log.warning("读 %s 失败, 本轮跳过该分片(下轮补): %s", f.name, e)
                    else:
                        time.sleep(10)
        df = pd.concat(dfs, ignore_index=True).drop_duplicates("announcementId") \
            if dfs else pd.DataFrame()
        res[cat] = df
    return res


def main(gold: bool = False, pilot: bool = False, all_mode: bool = False,
         workers: int = 1, time_box: int = 0, chain: int = 0, fewshot: int = 0) -> int:
    global OUT_DIR
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    mode = "gold" if gold else ("pilot" if pilot else "all")
    if all_mode:
        # 全量模式(2026-09-30 拍板启动): 输入 fulltext_all 8 分片, 产物独立目录,
        # 断点续采 + 计划任务时盒轮转(与回填同款弹性模式)
        OUT_DIR = OUT_DIR.parent / "fields_llm_all"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cfg = _cfg()
    if not cfg["key"]:
        log.error("LLM_API_KEY 未配置")
        return 2
    # 断点续采(2026-09-30): 首跑 640/2900 时进程被杀, 无中间产物全丢——
    # manifest 按 announcementId 记结果, 周期原子落盘; 单实例 FileLock 防重复。
    from app.filelock import FileLock
    manifest_path = OUT_DIR / f"manifest_llm_{mode}.json"
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
    _lock = FileLock(OUT_DIR / f"llm_{mode}.lock")
    # retries=3 而非 1: 陈旧锁接管(上轮崩溃残留)需要两次尝试 —— 第一次搬走死锁、
    # 第二次才建上自己的锁; retries=1 会在搬走后直接认输(2026-09-30 实测)。
    if not _lock.acquire(retries=3):
        log.error("另一个 %s 实例在跑(锁被持有), 退出", mode)
        return 2
    # 余额护栏(2026-09-30, 用户指令"没有余额就停止"): 每轮开工前查官方
    # /user/balance, 余额 < LLM_MIN_BALANCE(默认 10 元, 约两轮成本)即干净停轮 ——
    # 计划任务下轮再查, 充值后自动恢复。402 中途断油由 _post_chat 兜底。
    import os as _os
    _min_bal = float(_os.getenv("LLM_MIN_BALANCE", "10"))
    bal_ok, balance = _check_balance(cfg)
    if not bal_ok or (balance >= 0 and balance < _min_bal):
        log.error("余额不足停轮: balance=%.2f 元 (阈值 %.2f) —— 充值后下轮自动恢复",
                  balance, _min_bal)
        _save_manifest(manifest_path, manifest)
        _flush_parquet(OUT_DIR, mode, manifest)
        _lock.release()
        (OUT_DIR / f"result_llm_{mode}.json").write_text(json.dumps(
            {"asof": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "mode": mode,
             "stopped": "insufficient_balance", "balance": balance,
             "n_docs_total": sum(1 for v in manifest.values()
                                 if v.get("status") in ("done", "failed"))},
            ensure_ascii=False, indent=1), encoding="utf-8")
        return 0
    log.info("余额检查通过: %.2f 元", balance if balance >= 0 else -1)
    input_frames = _input_frames(all_mode)
    headers: dict = {}
    if fewshot != 0:
        import math
        for cat in SCHEMA:
            df_c = input_frames[cat]
            if df_c.empty:
                headers[cat] = ""
                continue
            # 示例数自动定容: 命中率 ≥90% 要求头部 ≥ 9×正文 tokens
            # (H/(H+T)≥0.9); 例均 ~960 tok(1200 字正文+JSON)。金标准文档排除防泄漏。
            gold_aids = set(pick_gold(df_c)["announcementId"].astype(str))
            # 定容必须用**截断后**的文本长(请求实际只发 text[:3000]): 原用未截断均值,
            # ma 类均 8,763 字 → 目标虚胖 3 倍, 头部顶到 40 例上限白烧重读费
            med = float(df_c["n_chars"].clip(upper=TEXT_CAP).median()) \
                if "n_chars" in df_c.columns else 1500.0
            mean_chars = float(df_c["n_chars"].clip(upper=TEXT_CAP).mean()) \
                if "n_chars" in df_c.columns else 1500.0
            n_ex = max(4, min(24, math.ceil(9 * med * 0.75 / 960))) if fewshot < 0 else fewshot
            # 目标头部长度 = 9×池均正文 tokens(聚合命中率≥90% ⇔ H≥9×mean_T);
            # 轮6 实测池均 1,507 tok(右偏, 中位仅 664) ⇒ 定容必须用均值
            target = 9 * mean_chars * 0.75
            headers[cat] = _fewshot_header(cat, n_ex, gold_aids, target_tokens=target)
            log.info("few-shot 头部[%s]: %d 例, %d 字符 (median=%.0f mean=%.0f 目标=%.0f tok)",
                     cat, len(headers[cat]) and headers[cat].count("示例正文：") or 0,
                     len(headers[cat]), med, mean_chars, target)
    n_ok = n_bad = n_fmt = 0
    box_hit = False
    t0 = time.time()
    # 落盘节奏按规模分级: 全量 manifest 最终 ~130MB/65 万条, 每 20 条全量重建是
    # O(n²) 磁盘消耗 —— 试点 20 条/次, 全量 2000 条/次(时盒轮内 ~6 次)。
    FLUSH_EVERY = 2000 if all_mode else 20
    for cat, schema in SCHEMA.items():
        df = input_frames[cat]
        if gold:
            df = pick_gold(df)
        # 文本去重(2026-09-30): 公式化公告整文字节级雷同(ef 实测 20%), 相同文本只调
        # 一次 API、字段复制给同文全部文档 —— temp=0 同文同输出, 语义无损; rep 已抽过
        # (done)时组员直接免费复制。pending 元素 = (cat, 代表行, [(aid, 行), ...])。
        import hashlib
        groups: dict = {}
        gorder = []
        for _, r in df.iterrows():
            h = hashlib.md5(str(r["text"]).encode("utf-8", "ignore")).hexdigest()
            g = groups.get(h)
            if g is None:
                g = {"rep": r, "members": []}
                groups[h] = g
                gorder.append(h)
            g["members"].append((r["announcementId"], r))
        pending = []
        n_copy = 0
        for h in gorder:
            g = groups[h]
            todo = [(a, r) for a, r in g["members"]
                    if not (a in manifest and manifest[a].get("status") == "done")]
            if not todo:
                continue
            rep_aid = g["rep"]["announcementId"]
            if (rep_aid in manifest and manifest[rep_aid].get("status") == "done"):
                src = manifest[rep_aid]
                for a, r in todo:            # rep 已有字段: 免费复制(带各自行信息)
                    manifest[a] = {"status": "done", "category": cat,
                                   "代码": r["代码"], "公告时间": r["公告时间"],
                                   "fields": src["fields"],
                                   "missing_key": src.get("missing_key", [])}
                    n_ok += 1
                    n_copy += 1
                continue
            pending.append((cat, g["rep"], todo))
        # 同文/同模板相邻(按正文前 300 字排序) ⇒ 前缀缓存命中率最大化 ——
        # 12 并发曾把命中打到 0.7%(请求散到不同缓存节点), 串行探测 88%。
        pending.sort(key=lambda e: str(e[1]["text"])[:300])
        n_pending_docs = sum(len(t) for _, _, t in pending)
        log.info("%s: %s 待抽取 %d 组 / %d 条 (同文复制 %d, workers=%d, chain=%s)",
                 mode, cat, len(pending), n_pending_docs, n_copy, workers, chain)

        def _call(item):
            c, r, todo = item
            raw, err = ask_llm(cfg, c, r["text"], headers.get(c, ""))
            return item, raw, err

        def _absorb(item, raw, err):
            """结果入账(manifest + 计数) —— 单篇/预热/链式三条路共用。"""
            nonlocal n_ok, n_bad, n_fmt
            c, r, todo = item
            if raw is None:
                n_bad += len(todo)
                for a, _ in todo:
                    manifest[a] = {"status": "failed", "category": c, "error": err}
                return
            obj = parse_json(raw)
            if obj is None:
                n_bad += len(todo)
                n_fmt += len(todo)
                for a, _ in todo:
                    manifest[a] = {"status": "failed", "category": c,
                                   "error": "json_parse_fail", "raw": raw[:300]}
                return
            missing = [k for k in schema if k not in obj]
            if missing:
                n_fmt += len(todo)
            for a, r2 in todo:   # 同文组: 每个成员记自己的行信息
                n_ok += 1
                manifest[a] = {"status": "done", "category": c,
                               "代码": r2["代码"], "公告时间": r2["公告时间"],
                               "fields": {k: obj.get(k) for k in schema},
                               "missing_key": missing}

        from concurrent.futures import ThreadPoolExecutor, as_completed
        if chain:
            # 链式累计模式(2026-09-30, 用户目标"命中率≥90%"; 官方 kv_cache Example 1):
            # 每段 ≤chain 条, 段内串行、prompt = 上一轮 + 追加一篇 —— 上轮完整输入成为
            # 本轮可完全命中的前缀单元(金标准滚动实测 95.7%/97.7%)。链间相互独立,
            # workers = 并行链数; 命中率与链间并发无关(单元各自独立)。
            stop = threading.Event()
            segs = [pending[i:i + chain] for i in range(0, len(pending), chain)]
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(_chain_run_segment, cfg, cat, sg, stop) for sg in segs]
                box_hit = False
                for fut in as_completed(futs):
                    try:
                        results = fut.result()
                    except Exception:
                        continue
                    for item, raw, err in results:
                        _absorb(item, raw, err)
                    if time_box and not box_hit and (time.time() - t0) > time_box:
                        n_cancel = sum(1 for f in futs if f.cancel())
                        box_hit = True
                        stop.set()
                        log.info("时盒到点: 取消未开始段 %d (下轮续), 在途链收尾", n_cancel)
                    if (n_ok + n_bad) % FLUSH_EVERY == 0:
                        _save_manifest(manifest_path, manifest)
                        _flush_parquet(OUT_DIR, mode, manifest)
                        log.info("%s 进度 %d ok / %d bad (%.0fs)%s",
                                 mode, n_ok, n_bad, time.time() - t0,
                                 " [时盒到点, 收尾中]" if box_hit else "")
            continue
        # 缓存预热(官方 kv_cache 机制, 2026-09-30): 命中要求"完全匹配已持久化的前缀
        # 单元"; 公共前缀 A(指令块)靠跨请求检测落盘 —— 若批量请求先于检测到达则全部
        # miss(12 并发 0.68% 的根因)。先串行消化 2 条让 A 持久化, 批量请求此后恒定
        # 命中 A(ef 稳态 ≈ 250/948 ≈ 26%)。
        for it in pending[:2]:
            _, raw, err = _call(it)
            _absorb(it, raw, err)
        rest = pending[2:]

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_call, it) for it in rest]
            box_hit = False
            for fut in as_completed(futs):
                try:
                    item, raw, err = fut.result()
                except Exception:        # CancelledError(时盒取消)等 —— 未跑的直接丢弃
                    continue
                _absorb(item, raw, err)
                # 时盒(计划任务轮): 到点后取消**未开始**的请求(未付费, 无浪费),
                # 继续收完在途/已完成的 —— 不 break, 否则已付费结果会被丢弃。
                if time_box and not box_hit and (time.time() - t0) > time_box:
                    n_cancel = sum(1 for f in futs if f.cancel())
                    box_hit = True
                    log.info("时盒到点: 取消未开始 %d 条(下轮续), 收尾在途请求", n_cancel)
                if (n_ok + n_bad) % FLUSH_EVERY == 0:
                    _save_manifest(manifest_path, manifest)
                    _flush_parquet(OUT_DIR, mode, manifest)
                    log.info("%s 进度 %d ok / %d bad (%.0fs)%s",
                             mode, n_ok, n_bad, time.time() - t0,
                             " [时盒到点, 收尾中]" if box_hit else "")
    _save_manifest(manifest_path, manifest)
    _flush_parquet(OUT_DIR, mode, manifest)
    _lock.release()
    # 2026-09-30 修: 原先此处再用本轮 rows_all 覆盖写 parquet —— 断点续采轮次里
    # rows_all 只含本轮新抽条目, 会把 _flush_parquet 刚重建的全量长表又裁剪回本轮,
    # 旧条目整段丢失。落盘只认 manifest 全量重建这一个出口。
    path = OUT_DIR / f"llm_{mode}.parquet"
    fmt_rate = round(1 - n_fmt / max(1, n_ok + n_bad), 4)
    n_total = sum(1 for v in manifest.values() if v.get("status") in ("done", "failed"))
    u = dict(_USAGE)
    est_cost = (u["prompt_cache_hit_tokens"] * 0.02
                + (u["prompt_tokens"] - u["prompt_cache_hit_tokens"]) * 1.0
                + u["completion_tokens"] * 2.0) / 1e6
    result = {"asof": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "mode": mode,
              "n_docs_this_round": int(n_ok + n_bad), "n_ok": n_ok, "n_bad": n_bad,
              "n_docs_total": n_total,
              "timebox_hit": bool(box_hit),
              "format_ok_rate": fmt_rate,
              "seconds": round(time.time() - t0, 1),
              "usage_this_round": {**u, "est_cost_yuan": round(est_cost, 4)},
              "note": "C1 金标准一致率/C3 双通道一致性 = 人工与脚本后验（见回填）"}
    (OUT_DIR / f"result_llm_{mode}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("完成: %d 文档, 格式合规率 %.1f%% → %s", n_ok + n_bad, 100 * fmt_rate, path.name)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", action="store_true", help="每类 10 条金标准验证")
    ap.add_argument("--pilot", action="store_true", help="试点 2,900 条全跑")
    ap.add_argument("--all", action="store_true",
                    help="全量模式: fulltext_all 8 分片输入, 产物 fields_llm_all/(时盒轮转)")
    ap.add_argument("--time-box", type=int, default=0,
                    help="单轮时盒秒数(计划任务模式用), 到点取消未开始请求并保存退出")
    ap.add_argument("--chain", type=int, default=0,
                    help="链式累计模式: 每段最多 N 篇, 段内串行累计 prompt 抽最后一篇"
                         "(前缀缓存命中率 90%+; 0=关闭走单篇模式; 金标准已否决, 保留仅作研究)")
    ap.add_argument("--fewshot", type=int, default=0,
                    help="few-shot 大头部: 每类 N 个示例做共享前缀(命中 90%+ 的正确性"
                         "保持方案); -1=按类均文本长度自动定容; 0=关闭")
    ap.add_argument("--workers", type=int, default=1,
                    help="并发线程数(单篇模式=并发请求数; 链式模式=并行链数, 默认 1=串行)")
    a = ap.parse_args()
    sys.exit(main(gold=a.gold, pilot=a.pilot, all_mode=a.all,
                  workers=a.workers, time_box=a.time_box, chain=a.chain,
                  fewshot=a.fewshot))
