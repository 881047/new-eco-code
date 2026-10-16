"""LLM 提案第一轮 · 门禁执行(2026-09-17, 计划见 plans/llm-factor-round-20260917.md)

做什么: 把 DeepSeek 提案(data/llm/factor_proposals_r1.json, 预算冻结 50 条)中的
**A 组(id60_* 日内因子, 23 条)**经 LLMProposer 装载进 Alpha Factory 预注册门禁:
declare() 冻结分母=50(用户拍板, 全轮预算含 B/C/D) → 死区评估 → alpha_bank 登记。

A 组为什么能先跑: id60_* 三条基列已有现成日频面板(data/min60_derived/daily.parquet,
771 万行, 2020-01~) —— 本脚本把它重采样到月末锚点, 组成"表达式+基列面板"一起进场
(工厂求值器原生支持 Candidate.panel 直通, flow 因子同路径)。表达式里 Ref/Mean 等
滚动算子工厂会先 eval 基列表达式再算吗? 不 —— A 组提案是**基列的组合**, 组合算子在
基列**日频面板**上滚动即可, 故这里把每条提案翻译成"panel=日频面板.eval(表达式)"。

B/C/D 组(27 条)需先物化研报/金股/股东表 → 月度截面列, **本轮不跑**(过闸驱动,
计划第 3 步)。

⚠️ 预注册: intent/分母/判据在跑数前冻结; LLM 输出不进任何生产链路。
用法: .venv/Scripts/python -X utf8 scripts/llm_factor_gate_r1.py
"""
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd  # noqa: E402

from app.alpha import expr as expr_mod  # noqa: E402  (DSL 引擎, 供 panel.eval)
from app.alpha.factory import Factory, PreRegistration  # noqa: E402
from app.logger import setup_logger  # noqa: E402

setup_logger()

PROPOSALS = BASE / "data" / "llm" / "factor_proposals_r1.json"
MIN60 = BASE / "data" / "min60_derived" / "daily.parquet"
A_NAMES = ("x1", "x2", "x3")
# 列语义(对照 F75_intraday_minute_kgd.py:78-80 的聚合映射; parquet 是逐日值):
#   x1 = 当日尾盘bar成交额占比(late_vol_share 的日值)
#   x2 = 当日尾盘bar收益(late_ret 的日值)
#   x3 = 当日收盘/日VWAP-1(close_vs_dayvwap 的日值)
# 提案表达式里的 $id60_* 引用统一改写到这三个物理列名(下面的 RENAME)。


def load_a_group() -> list[dict]:
    """A 组提案(id60_* 基列组合), 逐条校验表达式只引用三个基列(按 RENAME 改写)。"""
    d = json.loads(PROPOSALS.read_text(encoding="utf-8"))
    rename = {"id60_late_vol_share": "x1", "id60_late_ret": "x2",
              "id60_close_vs_dayvwap": "x3"}
    out = []
    for p in d["proposals"]:
        if p.get("mine") != "A":
            continue
        expr = p["expr"]
        # 校验: 引用的 $ 列必须在基列内(防 LLM 幻觉列名让 eval 在求值时才炸)
        import re
        cols = set(re.findall(r"\$(\w+)", expr))
        bad = cols - set(rename)
        if bad:
            print(f"  跳过(引用未物化列 {bad}): {p['name']}")
            continue
        for k, v in rename.items():
            expr = expr.replace("$" + k, "$" + v)
        p = dict(p, expr=expr)
        out.append(p)
    return out


def main() -> int:
    a_group = load_a_group()
    print(f"A 组可求值提案: {len(a_group)} 条 (其余 B/C/D 物化后另轮)")
    if not a_group:
        return 2
    if not MIN60.exists():
        print(f"✗ 缺基列面板 {MIN60}")
        return 2

    # ① 日频基列面板 → 逐条 eval 成组合日频面板
    t0 = time.time()
    raw = pd.read_parquet(MIN60)
    wide = {c: raw.pivot(index="date", columns="ts_code", values=c) for c in A_NAMES}
    print(f"基列面板: {len(raw):,} 行 → 3 宽表 ({time.time()-t0:.0f}s)")
    # DSL 解析器的变量白名单是模块级 VARS(列名映射)—— 引擎不扩展它 ⇒ 脚本局部
    # 扩充 x1/x2/x3(用完还原), 这是 VARS 作为"列名映射"的设计内扩展点。
    _saved_vars = dict(expr_mod.VARS)
    expr_mod.VARS.update({c: c for c in A_NAMES})
    try:
        eng = expr_mod.Engine(wide)          # 面板 = {变量名: date×code 宽表}
        panel_items = {}
        for p in a_group:
            try:
                df = eng.eval(p["expr"])
                if isinstance(df, pd.Series):
                    df = df.to_frame()
                df.index = pd.to_datetime(df.index)
                panel_items[p["name"]] = df
            except Exception as e:
                print(f"  ✗ {p['name']}: {type(e).__name__}: {str(e)[:90]}")
    finally:
        expr_mod.VARS.clear()
        expr_mod.VARS.update(_saved_vars)
    print(f"组合面板就绪: {len(panel_items)}/{len(a_group)} 条")

    # ② 面板直通工厂(ZooProposer.panels, flow 因子同路径)
    from app.alpha.factory import ZooProposer
    zoo = ZooProposer(panels=panel_items, panel_meta={
        n: {"info_source": "llm_r1", "source": "baostock 60min 物化面板",
            "name": n} for n in panel_items}, label="llm_r1_A")

    prereg = PreRegistration(
        intent="LLM 提案第一轮 A 组(id60_* 日内因子组合): 死区头部超额是否≥门禁; "
               "提案=DeepSeek(新矿优先), 预算=用户拍板 50 条(Bonferroni 分母=50, 含 B/C/D); "
               "基列=baostock 60min 物化面板(id60_late_vol_share/late_ret/close_vs_dayvwap)。"
               "基列 2020-01 起才有值 ⇒ 预热段为 NaN, 死区评估自动剔除 — 已在 intent 冻结")
    f = Factory(prereg)   # 面板全历史加载; id60 列 2020 前为 NaN, 评估自动剔除
    f.add(zoo)
    denom = f.declare()
    print(f"分母冻结: {denom}")
    f.run(save=True)
    print("门禁完成 → alpha_bank 登记(过闸=POOLED, 未过=CANDIDATE, 撞车=DUPLICATE)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
