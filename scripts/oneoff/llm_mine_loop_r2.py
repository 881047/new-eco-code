# -*- coding: utf-8 -*-
"""LLM 自主挖因子循环 r2 + KG 降噪读数 —— `plans/llm-mine-loop-r2-prereg-20260926.md` 的执行体。

机制（预注册 §二/§三）:
  迭代 1: LLM 读【31 因子 + DSL + KG 25 列字典 + 失败知识库 + KG 拥挤目录】→ ≤10 条提案
          (kind=expr 标准列 DSL / kind=kg KG 列机械变换; 每条带 noise_hypothesis)
  判决  : factory 标准六判据(PIT 域, 撞车硬闸) + **KG 降噪域平行读数**
          (死区横截面剔除 kg_resonance+kg_pagerank_ind 拥挤前十分之一后的 head/t;
           报告用不参与判据, 分母保守计双域)
  迭代 2: LLM 读迭代 1 全部读数 → ≤10 条(禁重复已被否方向) → 同款判决
出口: PASS=POOLED=资格非可用; 0 PASS 为基准预期; 成功定义 = 闭环机制首跑 + 降噪读数入册。
只读生产表; LLM 密钥只在 .env 不落日志; 产物 experiments/llm_mine_loop_r2_20260926.json。
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "oneoff"))

import numpy as np
import pandas as pd
import requests

import llm_factor_propose as lp                    # 复用 PANEL_COLS/DSL 单一来源
from app import config as cfg_mod
from app.alpha import bank
from app.alpha import expr as E
from app.alpha.factory import Candidate, Factory, PreRegistration
from app.alpha.factory import _masked_panel        # 与门禁同一裁域实现
from app.alpha import gate
from app.factors import factor_engine as fe
from app.factors.factor_engine import load_market, month_end_dates, industry_map
from app.logger import setup_logger
from app.web.llm import _llm_cfg, ASK_TIMEOUT

ROOT = Path(__file__).resolve().parents[2]
KG = ROOT / "data" / "kg" / "kg_features.parquet"
OUT = ROOT / "experiments" / "llm_mine_loop_r2_20260926.json"
PROMPTS_OUT = ROOT / "data" / "llm" / "mine_loop_r2_prompts.json"
N_PER_ITER = 10
CROWD_TOP = 0.10                                   # 降噪: 剔 KG 拥挤前十分之一

KG_COV_TEXT = ""  # build_context 时填充

KG_DICT = """KG 月度面板可用列(data/kg/kg_features.parquet, 每锚点一股一行, 提案 kind=kg 时引用):
kg_rating_up_60d 评级上调次数 | kg_rating_cov_60d 评级覆盖家数 | kg_broker_rec_3m 金股推荐数
kg_broker_rec_chg 金股推荐变化 | kg_holder_add_60d/dec_60d/net 股东增持/减持/净 | kg_holder_new_entry 新进股东
kg_holder_conc_chg 集中度变化 | kg_catalyst_60d 催化剂数 | kg_sentiment_60d 情绪分 | kg_dividend_90d 分红事件
kg_resonance 多源共振分(热门代理) | kg_pagerank_ind 行业内关联度(pagerank, 热门代理)
kg_eps_revision_mom EPS 修正动量 | kg_target_upside 目标价隐含空间 | kg_rating_dispersion 评级分歧
kg_coverage_chg 覆盖变化 | kg_unlock_90d_ratio/cnt 解禁压力 | kg_insider_buy_60d/sell_60d/net 增减持
KG 机械变换(kind=kg 只许): op ∈ {z, rank_pct, chg3, mul, ratio}; mul/ratio 的 cols 给 2 列(对每列先截面 z)。"""

LESSONS = """失败知识库(必须避开的方向与本仓已实证的事实):
- 量价 158 / flow 12 / KG 事件 21 / min60 12 条变换 + 日值 3, 全部 CANDIDATE 未过闸; 全史唯一历史 PASS(margin_net_share)组合层无效;
- LLM r1 一次性提案 0/18(17 条撞车 = 重新发明已知因子) ⇒ 本轮禁止提动量/波动/反转/换手/价值的普通变体;
- KG/事件/尾盘族是**反向定价**信号(死区 head t −4~−5): 它们的价值在排除/风控, 不在取正当 alpha(反号臂 head 全塌);
- gp_000(GP PASS) = size 的非线性影子(ρ=0.951)被组合层否 ⇒ 单因子 PASS 后第一问永远是"增量 vs rev_5/size";
- 已采纳的 chase6 证明: 本仓正路 = 计划时**排除性信息**(追高剔除 +2.07pp/年)。你的 noise_hypothesis 请围绕
  "这条因子躲开了什么噪声(借 KG 判断)"来写 —— 但门禁只判正向因子, 判决不由你。"""

PROMPT = """你是量化因子研究员, 在一个月频 A 股多因子系统(LightGBM, 等权 top6, 微盘段)里
提出 **{n} 条**可证伪的候选因子表达式。门禁会客观判决(死区超额/t/Bonferroni), 你不是法官。

{panel_cols}

{kg_dict}

{lessons}

{feedback}

硬性要求:
1. 每条 = {{"kind": "expr"|"kg", "name": "小写下划线名(llm_ 前缀)", "expr"/"op"+"cols": ..., "why": "一句话经济含义", "noise_hypothesis": "它借 KG 判断躲开了什么噪声"}};
2. kind=expr: expr 只用上面 $ 列与 DSL 算子; kind=kg: op 只用 {{z, rank_pct, chg3, mul, ratio}}, cols 从 KG 列表选;
3. 禁止与现役 31 因子及 KG 已登记 21 列近似的东西; 优先微盘段行为金融逻辑;
4. 与上一轮重复或已被否方向的重新包装 = 浪费;
5. 只输出 JSON 数组, 不要其它文字。"""


def ask_llm(prompt: str) -> list:
    c = _llm_cfg()
    # deepseek-flash 是推理型(reasoning_content): max_tokens 太小会被思考耗尽 ⇒ content 为空
    text = ""
    for attempt in (1, 2, 3):
        try:
            # 推理型模型生成 12k token 需数分钟 ⇒ 读超时 300s(ASK_TIMEOUT 是问答档, 太短)
            r = requests.post(c["base_url"] + "/chat/completions",
                              headers={"Authorization": f"Bearer {c['api_key']}"},
                              json={"model": c["model"],
                                    "messages": [{"role": "user", "content": prompt}],
                                    "max_tokens": 32000, "temperature": 0.6},
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
        print(f"  [LLM] content 空(finish={j['choices'][0].get('finish_reason')}), "
              f"重试 {attempt}/3", flush=True)
    if not text:
        raise RuntimeError("LLM content 为空(推理耗尽 token 预算)")
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    arr = json.loads(text)
    assert isinstance(arr, list), "输出不是 JSON 数组"
    return [it for it in arr if isinstance(it, dict) and it.get("name")]


def kg_wides():
    kg = pd.read_parquet(KG)
    feats = [c for c in kg.columns if c not in ("ts_code", "anchor")]
    wide = {}
    for f in feats:
        w = kg.pivot_table(index="anchor", columns="ts_code", values=f, aggfunc="last")
        w.index = pd.to_datetime(w.index)
        wide[f] = w.sort_index()
    return wide


def kg_transform(op: str, cols: list, wide: dict) -> pd.DataFrame:
    def z(w):
        return (w - w.mean(axis=1).to_numpy()[:, None]) / \
               w.std(axis=1, ddof=1).to_numpy()[:, None].clip(min=1e-12)
    if op == "z":
        return z(wide[cols[0]])
    if op == "rank_pct":
        return wide[cols[0]].rank(axis=1, pct=True)
    if op == "chg3":
        zw = z(wide[cols[0]])
        return zw - zw.shift(3)
    if op == "mul":
        return z(wide[cols[0]]) * z(wide[cols[1]])
    if op == "ratio":
        return z(wide[cols[0]]) / (z(wide[cols[1]]).abs() + 0.5)
    raise ValueError(f"未知 KG 变换 {op}")


def kg_coverage(wide: dict) -> str:
    lines = []
    for f, w in wide.items():
        recent = w.loc[w.index >= "2021-01-01"]
        cov = float(recent.notna().mean().mean())
        lines.append(f"{f} 覆盖{cov * 100:.0f}%{'(稀疏!慎用)' if cov < 0.30 else ''}")
    return "\n".join(lines)


def build_context() -> tuple:
    m = load_market()
    md_all = month_end_dates(pd.DatetimeIndex(m["close"].index))
    wide = kg_wides()
    global KG_COV_TEXT
    KG_COV_TEXT = "各列覆盖率(2021+ 锚点非空率; 低覆盖列进厂会被稀疏中性化整行清零, 勿选):\n" + kg_coverage(wide)
    res, cov = wide["kg_resonance"], wide["kg_pagerank_ind"]
    common = res.index.intersection(cov.index)
    crowd = (res.loc[common] - res.loc[common].mean(axis=1).to_numpy()[:, None]) / \
            res.loc[common].std(axis=1, ddof=1).to_numpy()[:, None].clip(min=1e-12) + \
            (cov.loc[common] - cov.loc[common].mean(axis=1).to_numpy()[:, None]) / \
            cov.loc[common].std(axis=1, ddof=1).to_numpy()[:, None].clip(min=1e-12)
    return m, md_all, wide, crowd.sort_index()


def to_candidates(props: list, wide: dict) -> tuple:
    out, raws = [], {}
    for it in props:
        name = str(it["name"])
        meta = {"info_source": "llm_r2", "source": "llm_mine_loop_r2", "name": name,
                "note": f"why: {it.get('why', '')} | noise: {it.get('noise_hypothesis', '')}"}
        if it.get("kind") == "kg":
            try:
                panel = kg_transform(it["op"], list(it["cols"]), wide)
                out.append(Candidate(name=name, panel=panel, meta=meta))
                raws[name] = panel
            except Exception as e:                        # noqa: BLE001
                print(f"  [kg 物化失败] {name}: {e}", flush=True)
        else:
            out.append(Candidate(name=name, expr=it.get("expr"), meta=meta))
            raws[name] = ("expr", it.get("expr"))
    return out, raws


def denoised_readout(name: str, raw: pd.DataFrame, eng, m, md, zones, fwd,
                     ind, tradable, crowd) -> dict:
    try:
        if raw is None or (hasattr(raw, "empty") and raw.empty):
            return {}
        neu = fe.neutralize(raw, ind)
        panel_md = _masked_panel(neu, tradable, md)
        det = gate.evaluate(panel_md, fwd, md, zones)
        # 降噪域: 每锚点剔除拥挤合成前 10%
        keep_rows = {}
        for dt in md:
            if dt in crowd.index:
                row = crowd.loc[dt].dropna()
                thr = row.quantile(1 - CROWD_TOP)
                keep_rows[dt] = set(row[row <= thr].index)
        keep = pd.DataFrame([[c in keep_rows.get(dt, set()) for c in panel_md.columns]
                             for dt in panel_md.index],
                            index=panel_md.index, columns=panel_md.columns)
        det_d = gate.evaluate(panel_md.where(keep), fwd, md, zones)
        dead = det[det["zone"] == "DEAD"] if len(det) else det
        dead_d = det_d[det_d["zone"] == "DEAD"] if len(det_d) else det_d
        return {"std_dead_head": round(float(dead["head_excess"].mean()), 4) if len(dead) else None,
                "den_dead_head": round(float(dead_d["head_excess"].mean()), 4) if len(dead_d) else None,
                "delta_head_denoised": round(float(dead_d["head_excess"].mean() -
                                                    dead["head_excess"].mean()), 4)
                if len(dead) and len(dead_d) else None}
    except Exception as e:                                # noqa: BLE001
        return {"denoise_error": str(e)[:80]}


def run_iteration(idx: int, feedback: str, m, md_all, wide, crowd) -> tuple:
    prompt = PROMPT.format(n=N_PER_ITER, panel_cols=lp.PANEL_COLS,
                           kg_dict=KG_DICT + "\n" + KG_COV_TEXT,
                           lessons=LESSONS, feedback=feedback or "(首轮, 无上一轮读数)")
    props = ask_llm(prompt)[:N_PER_ITER]
    print(f"[迭代 {idx}] LLM 提案 {len(props)} 条", flush=True)
    n_prior = bank.cumulative_tests()
    denom = 2 * len(props)                               # 双域保守计
    f = Factory(PreRegistration(intent=(
        f"LLM 自主挖因子循环 r2 · 迭代 {idx} (plans/llm-mine-loop-r2-prereg-20260926.md): "
        f"{len(props)} 条, 双域保守分母; 标准域判据, 降噪域平行读数")), market_since=None)
    # 静态预过滤(r1 同款守卫): 非法表达式不进厂烧预算, 失败原因进迭代 2 反馈
    dropped = []
    kept = []
    for it in props:
        if it.get("kind") == "expr":
            try:
                lp._static_check(it.get("expr") or "")
            except Exception as e:                        # noqa: BLE001
                dropped.append(f"- {it.get('name')}: 静态检查失败 {str(e)[:80]}")
                continue
        kept.append(it)
    if dropped:
        print(f"[静态守卫] 非法表达式 {len(dropped)} 条剔除(原因进迭代 2 反馈)", flush=True)
    return_scope = {"dropped": dropped}
    cands, raws = to_candidates(kept, wide)
    if not cands:
        return props, [], {"denominator": 0, "n_prior": n_prior, "n_used": n_prior}, return_scope["dropped"]
    f.add(_FixedProposer(cands))
    f.declare()
    records = f.run(save=True)
    # KG 降噪域平行读数(预注册 §三): 同一面板在"剔拥挤前十分之一"横截面上的死区 head
    eng = E.Engine(E.build_panel(m))
    from app import db as _db
    ind = industry_map(_db.get_conn())
    tradable = f._tradable("eval", m, md_all[md_all >= pd.Timestamp("2019-01-01")])
    md_eval = md_all[md_all >= pd.Timestamp("2019-01-01")]
    zones = gate.zone_of(md_eval)
    fwd = gate.forward_return(m, gate.HORIZON)
    for c in cands:
        raw = raws.get(c.name)
        if raw is None:
            continue
        raw_df = raw if isinstance(raw, pd.DataFrame) else eng.eval(raw[1])
        dn = denoised_readout(c.name, raw_df, eng, m, md_eval, zones, fwd, ind, tradable, crowd)
        if records is not None and len(records):
            for k, v in dn.items():
                records.loc[records["alpha_id"] == c.name, k] = v
    recs = records.replace({np.nan: None}).to_dict("records") if len(records) else []
    n_used = max(denom, n_prior + denom)
    print(f"[迭代 {idx}] 判决 {len(recs)} 条 | 分母 {denom} | N_used={n_used}", flush=True)
    for r in recs:
        print(f"  {r['alpha_id']:16s} DEAD {r.get('DEAD_head', 0):+.4f} (t {r.get('DEAD_head_t', 0):+.2f}) "
              f"p_adj {r.get('p_adj', 1):.4f} {'PASS' if r.get('PASS') else '否'}", flush=True)
    return props, recs, {"denominator": denom, "n_prior": n_prior, "n_used": n_used}, return_scope["dropped"]


class _FixedProposer:
    """固定候选集的提议器(工厂契约: declare 前给出可数分母)。"""
    label = "llm_r2"

    def __init__(self, cands: list):
        self._cands = cands
        self.n_evaluated = len(cands)

    def declared(self) -> int:
        return len(self._cands)

    def propose(self) -> list:
        return self._cands

    def bind_factory(self, factory) -> None:
        return None


def main() -> int:
    setup_logger()
    if not _llm_cfg()["api_key"]:
        print("✗ 未配置 LLM_API_KEY")
        return 2
    m, md_all, wide, crowd = build_context()
    print(f"KG 拥挤目录: {crowd.shape[0]} 锚点 × {crowd.shape[1]} 只", flush=True)

    feedback, all_data = "", {"iterations": []}
    for idx in (1, 2):
        props, recs, meta, dropped = run_iteration(idx, feedback, m, md_all, wide, crowd)
        it_data = {"iteration": idx, "n_proposals": len(props), "proposals": props,
                   "records": recs, **meta}
        all_data["iterations"].append(it_data)
        if idx == 1:
            lines = list(dropped)
            for r in recs:
                lines.append(f"- {r['alpha_id']}: DEAD head {r.get('DEAD_head')}, "
                             f"t {r.get('DEAD_head_t')}, p_adj {r.get('p_adj')}, "
                             f"{'PASS' if r.get('PASS') else '未过'}"
                             + (f"(撞车←{r.get('note', '')[:60]})" if "撞车" in str(r.get('note', '')) else ""))
            feedback = "上一轮(迭代 1)的门禁读数, 请据此修正方向(禁重复已否方向):\n" + "\n".join(lines)
            all_data["feedback_iter2"] = feedback
        time.sleep(2)
    all_data["ran_at"] = datetime.now().isoformat(timespec="seconds")
    n_all = sum(len(i["records"]) for i in all_data["iterations"])
    all_data["n_pass"] = sum(1 for i in all_data["iterations"]
                             for r in i["records"] if r.get("PASS"))
    OUT.write_text(json.dumps(all_data, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    PROMPTS_OUT.write_text(json.dumps({"context_seed": LESSONS}, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print(f"\n产物: {OUT}\n[总读数] 判决 {n_all} | PASS {all_data['n_pass']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
