# -*- coding: utf-8 -*-
"""LLM 自主挖因子循环 r3 —— `plans/llm-mine-loop-r3-prereg-20260926.md` 的执行体。

相对 r2 的三处增量（依据全球检索综述 plans/llm-mining-survey-20260926.md:
Hubble 2604.09601 / AlphaAgent 2502.16789）:
  ① 生成端约束: prompt 给受控词汇表 + 合法/非法样例（Hubble 式）;
  ② AST 三层校验器（LLM 后、工厂前）: 变量白名单 / 算子白名单+arity / 复杂度上限,
     错误**分类**进迭代 2 反馈（Hubble 式错误摘要）;
  ③ 去相关预筛（AlphaAgent 式）: 与 31 现役因子 |ρ|max > 0.7 ⇒ 标记"影子"。
主判据 = **静态失败率 ≤ 40%**（r2 = 85%）——机制级可测; 因子门禁六判据照旧。
KG 降噪域平行读数沿用 r2（报告用）。LLM 输出不进任何生产链路。
产物 experiments/llm_mine_loop_r3_20260926.json。
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

import llm_factor_propose as lp                    # PANEL_COLS/DSL 单一来源
from app import db
from app.alpha import bank
from app.alpha import expr as E
from app.alpha.factory import Candidate, Factory, PreRegistration
from app.alpha.factory import _masked_panel
from app.alpha import gate
from app.factors import factor_engine as fe
from app.factors.factor_engine import load_market, month_end_dates, industry_map
from app.logger import setup_logger
from app.web.llm import _llm_cfg, ASK_TIMEOUT

ROOT = Path(__file__).resolve().parents[2]
KG = ROOT / "data" / "kg" / "kg_features.parquet"
OUT = ROOT / "experiments" / "llm_mine_loop_r3_20260926.json"
N_PER_ITER = 10
CROWD_TOP = 0.10
MAX_NODES, MAX_DEPTH = 24, 4                       # AST 复杂度上限（冻结）
SHADOW_RHO = 0.7                                   # 去相关预筛（冻结）

# ---- 受控词汇表（Hubble 式; = 工厂求值器真实可执行的集合） ----
ALLOWED_VARS = {"open", "high", "low", "close", "pre_close", "vol", "amount",
                "turnover", "total_mv", "circ_mv", "pb", "pe_ttm", "vwap"}
OP_ARITY = {}
for _n in E.ELEM_UNARY:
    OP_ARITY[_n] = 1
for _n in E.ELEM_BINARY:
    OP_ARITY[_n] = 2
for _n in E.ROLLING_OPS:
    OP_ARITY[_n] = 3 if _n == "Quantile" else 2
for _n in E.PAIR_ROLLING_OPS:
    OP_ARITY[_n] = 3
ALLOWED_OPS = set(OP_ARITY)

KG_DICT = """KG 月度面板可用列(data/kg/kg_features.parquet, kind=kg 时引用, 附 2021+ 覆盖率):
{kg_cov}
KG 机械变换(kind=kg 只许): op ∈ {{z, rank_pct, chg3, mul, ratio}}; mul/ratio 的 cols 给 2 列(先截面 z)。"""

LESSONS = """失败知识库(必须避开):
- 量价 158/flow 12/KG 事件 21/min60 15/LLM r1 18 全部 CANDIDATE 未过闸; 历史 PASS margin_net_share 与
  gp_000(GP PASS, ρ(size)=0.951)组合层均无效 ⇒ 与现有因子高相关的候选 = 浪费;
- KG/事件/尾盘族是反向定价信号(价值在排除/风控, 不在取正当 alpha);
- r2 教训: 20 条提案 17 条静态非法(幻觉算子/变量) —— **输出前逐算子、逐变量核对白名单**。"""

PROMPT = """你是量化因子研究员, 在一个月频 A 股多因子系统(LightGBM, 等权 top6, 微盘段)里
提出 **{n} 条**可证伪的候选因子表达式。

**受控词汇表(逐字核对后输出, 白名单外的任何算子/变量都会被沙箱拒绝)**:
$ 变量: {vars}
算子(名称=arity): {ops}

合法样例:
  {{"kind":"expr","name":"llm_x1","expr":"Div(Std($close,20), Mean($close,20))","why":"20日变异系数...","noise_hypothesis":"..."}}
  {{"kind":"expr","name":"llm_x2","expr":"Corr($close, $amount, 10)","why":"量价相关...","noise_hypothesis":"..."}}
非法样例(都会被拒):
  "TS_MEAN($close,5)" → 算子未注册(应写 Mean) ; "$roe" → 变量不在面板(求值器不认财务列)

{kg_dict}

{lessons}

{feedback}

硬性要求:
1. 每条 = {{"kind": "expr"|"kg", "name": "llm_ 前缀小写", "expr"(expr 类) 或 "op"+"cols"(kg 类),
   "why": "一句话经济含义", "noise_hypothesis": "它借 KG 判断躲开了什么噪声"}};
2. 禁止与现役 31 因子近似; 优先微盘段行为金融逻辑;
3. 与上一轮重复或已被否方向的重新包装 = 浪费;
4. 只输出 JSON 数组, 不要其它文字。"""


def ask_llm(prompt: str) -> list:
    c = _llm_cfg()
    text = ""
    for attempt in (1, 2, 3):
        try:
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
        print(f"  [LLM] content 空(finish={j['choices'][0].get('finish_reason')}), 重试 {attempt}/3",
              flush=True)
    if not text:
        raise RuntimeError("LLM content 为空")
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    arr = json.loads(text)
    assert isinstance(arr, list)
    return [it for it in arr if isinstance(it, dict) and it.get("name")]


def validate_ast(expr: str) -> list:
    """AST 三层校验（Hubble 式）: 语法/前视(引擎既有) + 变量白名单 + 算子白名单/arity + 复杂度。"""
    errs = []
    try:
        ast = E.parse(expr)
        wins = E.ref_windows(ast)
        if any(w < 0 for w in wins):
            errs.append("前视(负窗口)")
    except Exception as e:                                # noqa: BLE001
        return [f"语法: {str(e)[:80]}"]
    stats = {"nodes": 0, "depth": 0}

    def walk(n, d):
        stats["nodes"] += 1
        stats["depth"] = max(stats["depth"], d)
        if stats["nodes"] > MAX_NODES:
            errs.append(f"节点数超 {MAX_NODES}")
            return
        if stats["depth"] > MAX_DEPTH:
            errs.append(f"树深超 {MAX_DEPTH}")
            return
        if n[0] == "var":
            if n[1] not in ALLOWED_VARS:
                errs.append(f"未知变量 ${n[1]}")
        elif n[0] == "call":
            name, args = n[1], n[2]
            if name not in ALLOWED_OPS:
                errs.append(f"未知算子 {name}")
            elif len(args) != OP_ARITY[name]:
                errs.append(f"{name} arity {len(args)}≠{OP_ARITY[name]}")
            else:
                # 窗口必须为正整数常量(引擎 _win 的规则; 首跑实证 LLM 会写 0.0/动态窗)
                wi = 2 if name in E.PAIR_ROLLING_OPS else 1
                if name in E.ROLLING_OPS or name in E.PAIR_ROLLING_OPS:
                    w = args[wi] if len(args) > wi else None
                    ok = w and w[0] == "num" and float(w[1]) >= 1                         and float(w[1]) == int(float(w[1]))
                    if not ok:
                        errs.append(f"{name} 窗口须为正整数常量")
                    elif name == "Quantile" and len(args) > 2:
                        q = args[2]
                        if not (q and q[0] == "num" and 0 < float(q[1]) < 1):
                            errs.append("Quantile 分位数须在 (0,1)")
            for a in args:
                walk(a, d + 1)
    walk(ast, 1)
    return list(dict.fromkeys(errs))[:3]


def kg_wides():
    kg = pd.read_parquet(KG)
    feats = [c for c in kg.columns if c not in ("ts_code", "anchor")]
    wide = {}
    for f in feats:
        w = kg.pivot_table(index="anchor", columns="ts_code", values=f, aggfunc="last")
        w.index = pd.to_datetime(w.index)
        wide[f] = w.sort_index()
    return wide


def kg_coverage(wide: dict) -> str:
    out = []
    for f, w in wide.items():
        recent = w.loc[w.index >= "2021-01-01"]
        cov = float(recent.notna().mean().mean())
        out.append(f"{f} 覆盖{cov * 100:.0f}%" + ("(稀疏!慎用)" if cov < 0.30 else ""))
    return NL.join(out)


NL = chr(92) + "n"


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


def to_candidates(props: list, wide: dict) -> tuple:
    out, raws = [], {}
    for it in props:
        name = str(it["name"])
        meta = {"info_source": "llm_r3", "source": "llm_mine_loop_r3", "name": name,
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


class _FixedProposer:
    label = "llm_r3"

    def __init__(self, cands: list):
        self._cands = cands
        self.n_evaluated = len(cands)

    def declared(self) -> int:
        return len(self._cands)

    def propose(self) -> list:
        return self._cands

    def bind_factory(self, factory) -> None:
        return None


def denoised_readout(raw: pd.DataFrame, eng, m, md, zones, fwd, ind,
                     tradable, crowd) -> dict:
    try:
        if raw is None or raw.empty:
            return {}
        neu = fe.neutralize(raw, ind)
        panel_md = _masked_panel(neu, tradable, md)
        det = gate.evaluate(panel_md, fwd, md, zones)
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
        if not len(dead) or not len(dead_d):
            return {}
        return {"std_dead_head": round(float(dead["head_excess"].mean()), 4),
                "den_dead_head": round(float(dead_d["head_excess"].mean()), 4),
                "delta_head_denoised": round(float(dead_d["head_excess"].mean()
                                                    - dead["head_excess"].mean()), 4)}
    except Exception as e:                                # noqa: BLE001
        return {"denoise_error": str(e)[:80]}


def shadow_flag(raw: pd.DataFrame, eng, m, md, ind, wide31r) -> dict:
    """去相关预筛（AlphaAgent 式）: 与 31 现役因子的逐锚点截面 |ρ|max。"""
    try:
        neu = fe.neutralize(raw, ind)
        neu.index = pd.to_datetime(neu.index)
        neu = neu.loc[neu.index.isin(wide31r.index)]
        if neu.empty:
            return {}
        best = ("?", 0.0)
        for f in wide31r.columns.get_level_values("factor").unique():
            sub = wide31r.xs(f, axis=1, level="factor")
            sub.index = pd.to_datetime(sub.index)
            common = sub.index.intersection(neu.index)
            if len(common) < 24:
                continue
            a = neu.loc[common].rank(axis=1)
            b = sub.loc[common].rank(axis=1)
            rho = a.corrwith(b, axis=1).dropna()
            if len(rho) and abs(rho.mean()) > abs(best[1]):
                best = (f, round(float(rho.mean()), 3))
        return {"shadow_factor": best[0], "shadow_abs_rho": abs(best[1]),
                "is_shadow": bool(abs(best[1]) > SHADOW_RHO)}
    except Exception as e:                                # noqa: BLE001
        return {"shadow_error": str(e)[:80]}


def build_all():
    m = load_market()
    md_all = month_end_dates(pd.DatetimeIndex(m["close"].index))
    wide = kg_wides()
    res, cov = wide["kg_resonance"], wide["kg_pagerank_ind"]
    common = res.index.intersection(cov.index)
    crowd = ((res.loc[common] - res.loc[common].mean(axis=1).to_numpy()[:, None])
             / res.loc[common].std(axis=1, ddof=1).to_numpy()[:, None].clip(min=1e-12)
             + (cov.loc[common] - cov.loc[common].mean(axis=1).to_numpy()[:, None])
             / cov.loc[common].std(axis=1, ddof=1).to_numpy()[:, None].clip(min=1e-12))
    from app.models import trainer as T
    wide31 = T._load_factors_wide()
    wide31r = wide31.reorder_levels(["factor", "ts_code"], axis=1).sort_index(axis=1)
    return m, md_all, wide, crowd.sort_index(), wide31r


def run_iteration(idx: int, feedback: str, m, md_all, wide, crowd, wide31r) -> tuple:
    kg_cov = kg_coverage(wide)
    prompt = PROMPT.format(n=N_PER_ITER, vars=", ".join(sorted(f"${v}" for v in ALLOWED_VARS)),
                           ops=", ".join(f"{k}({v})" for k, v in sorted(OP_ARITY.items())),
                           kg_dict=KG_DICT.replace("{kg_cov}", kg_cov),
                           lessons=LESSONS, feedback=feedback or "(首轮, 无上一轮读数)")
    props = ask_llm(prompt)[:N_PER_ITER]

    # AST 三层校验（生成端约束的第二半: 错误分类进反馈, 不进厂烧预算）
    legal, dropped = [], []
    for it in props:
        if it.get("kind") == "expr":
            errs = validate_ast(it.get("expr") or "")
            if errs:
                dropped.append(f"- {it.get('name')} [kind=expr]: {'; '.join(errs)}")
                continue
        legal.append(it)
    print(f"[迭代 {idx}] 提案 {len(props)} | 静态合法 {len(legal)} "
          f"({len(props) - len(legal)} 非法)", flush=True)

    cands, raws = to_candidates(legal, wide)
    if not cands:
        return props, [], {"denominator": 0, "n_prior": bank.cumulative_tests(),
                           "n_used": bank.cumulative_tests()}, dropped, legal
    n_prior = bank.cumulative_tests()
    f = Factory(PreRegistration(intent=(
        f"LLM 自主挖因子循环 r3 · 迭代 {idx} (plans/llm-mine-loop-r3-prereg-20260926.md): "
        f"{len(cands)} 条进厂(静态合法), 双域保守分母; 主判据=静态失败率(预注册 §三)")), market_since=None)
    f.add(_FixedProposer(cands))
    f.declare()
    records = f.run(save=True)

    eng = E.Engine(E.build_panel(m))
    ind = industry_map(db.get_conn())
    md_eval = md_all[md_all >= pd.Timestamp("2019-01-01")]
    zones = gate.zone_of(md_eval)
    fwd = gate.forward_return(m, gate.HORIZON)
    tradable = f._tradable("eval", m, md_eval)
    recs = records.replace({np.nan: None}).to_dict("records") if len(records) else []
    for c in cands:
        raw = raws.get(c.name)
        if raw is None:
            continue
        try:
            raw_df = raw if isinstance(raw, pd.DataFrame) else eng.eval(raw[1])
        except Exception as e:                            # noqa: BLE001
            rec = next((r for r in recs if r["alpha_id"] == c.name), None)
            if rec is not None:
                rec["denoise_error"] = f"expr eval: {str(e)[:60]}"
            continue
        rec = next((r for r in recs if r["alpha_id"] == c.name), None)
        if rec is None:
            continue
        for k, v in denoised_readout(raw_df, eng, m, md_eval, zones, fwd, ind, tradable, crowd).items():
            rec[k] = v
        for k, v in shadow_flag(raw_df, eng, m, md_eval, ind, wide31r).items():
            rec[k] = v
    n_used = max(2 * len(cands), n_prior + 2 * len(cands))
    print(f"[迭代 {idx}] 判决 {len(recs)} 条 | N_used≈{n_used}", flush=True)
    for r in recs:
        print(f"  {r['alpha_id']:32s} DEAD {r.get('DEAD_head', 0):+.4f} "
              f"{'PASS' if r.get('PASS') else '否'} | 影子: {r.get('shadow_factor', '-')} "
              f"|ρ|={r.get('shadow_abs_rho', '-')}", flush=True)
    return props, recs, {"denominator": 2 * len(cands), "n_prior": n_prior,
                         "n_used": n_used}, dropped, legal


def main() -> int:
    setup_logger()
    if not _llm_cfg()["api_key"]:
        print("✗ 未配置 LLM_API_KEY")
        return 2
    t0 = time.time()
    m, md_all, wide, crowd, wide31r = build_all()
    print(f"KG 拥挤目录 {crowd.shape} | 31 因子宽表 {wide31r.shape}", flush=True)

    static_fail_total, static_total = 0, 0
    feedback, iters = "", []
    for idx in (1, 2):
        props, recs, meta, dropped, legal = run_iteration(
            idx, feedback, m, md_all, wide, crowd, wide31r)
        static_total += len(props)
        static_fail_total += len(dropped) + (len(props) - len(legal) - len(dropped))
        iters.append({"iteration": idx, "n_proposals": len(props), "proposals": props,
                      "legal_examples": [it.get("expr") or f"{it.get('op')}{it.get('cols')}"
                                          for it in legal][:3],
                      "static_dropped": dropped, "records": recs, **meta})
        if idx == 1:
            err_cls = {}
            for dline in dropped:
                for cls in ("未知算子", "未知变量", "arity", "树深", "节点数", "语法", "前视"):
                    if cls in dline:
                        err_cls[cls] = err_cls.get(cls, 0) + 1
                        break
            feedback = ("上一轮(迭代 1)结果——静态错误分类分布: " + json.dumps(err_cls, ensure_ascii=False)
                        + "。被拒样例:\n" + "\n".join(dropped[:4])
                        + "\n门禁读数:\n" + "\n".join(
                            f"- {r['alpha_id']}: DEAD head {r.get('DEAD_head')}, t {r.get('DEAD_head_t')}, "
                            f"{'PASS' if r.get('PASS') else '未过'}"
                            for r in recs)
                        + "\n请据此修正(合法样例: "
                        + " | ".join(iters[-1]["legal_examples"]) + ")")
    static_rate = round(static_fail_total / max(1, static_total), 3)
    verdict = {"static_fail_rate": static_rate, "n_proposals": static_total,
               "mech_verdict": ("生成端约束有效(≤40%)" if static_rate <= 0.40
                                else "LLM 直写 DSL 判'不可行'(≥r2 水平, 换载体)"),
               "n_pass": sum(1 for it in iters for r in it["records"] if r.get("PASS"))}
    print(f"\n主判据: 静态失败率 {static_rate * 100:.0f}% ({static_fail_total}/{static_total}) "
          f"⇒ {verdict['mech_verdict']}", flush=True)

    OUT.write_text(json.dumps({
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "prereg": "plans/llm-mine-loop-r3-prereg-20260926.md",
        "criteria": {"static_fail_rate_max": 0.40, "shadow_rho": SHADOW_RHO},
        "iterations": iters, "verdict": verdict,
        "runtime_sec": round(time.time() - t0, 1)},
        ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
