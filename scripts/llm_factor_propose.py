"""LLM 因子提案第一轮（2026-09-17, 计划见 plans/llm-factor-round-20260917.md）

做什么: DeepSeek 读【新矿数据字典 + 31 现役因子清单 + 工厂 DSL 算子表】,
提出 **50 条**候选表达式(预算已由用户拍板冻结, Bonferroni 分母 = 50),
产出 LLMProposer 可装载的 JSON → 下一步进 Alpha Factory 门禁。

矿区(用户拍板"新矿优先"): 60 分钟线衍生面板(id60_*, 771 万行, 零因子覆盖) +
第 11~13 族(broker_recommend 1.6 万/研报 10.5 万/十大股东 126 万, 全部零因子覆盖)。
**关键约束 —— 表达式只能引用 PIT 月度面板已有列**(工厂求值器只认 factor 表字段),
新矿字段需先物化进面板; 本脚本让 LLM 在"面板列 + 明确的数据字典"内提案,
字段物化由门禁结果驱动(过闸的字段才值得建管线)。

⚠️ LLM 输出不进任何生产链路; 判决归工厂门禁(预注册/死区/IC/KGD/组合层)。

用法: .venv/Scripts/python -X utf8 scripts/llm_factor_propose.py
产出: data/llm/factor_proposals_r1.json (50 条)
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.web.llm import _llm_cfg, ASK_TIMEOUT  # noqa: E402
import requests  # noqa: E402

BUDGET = 50                    # 用户 2026-09-17 拍板(3 批分批请求, 每批去重累计)
BATCH = 17
OUT = BASE / "data" / "llm" / "factor_proposals_r1.json"

PANEL_COLS = """月频面板可用列(factor 表字段, 表达式只能引用这些):
$open $high $low $close $pre_close $vol $amount $adj (行情, 复权后)
$turnover_rate $volume_ratio $pe $pe_ttm $pb $dv_ratio $dv_ttm $total_mv $circ_mv (daily_basic)
$roe $netprofit_yoy $grossprofit_margin $debt_to_assets $eps_yoy $or_yoy (财务, 报告期)

现役 31 因子(新提案必须与其显著差异化, 下列名字的近似变体会被 DUPLICATE 闸杀):
amount_log beta_250 bias_20 bias_60 bp debt_to_assets dv_ttm ep eps_yoy
grossprofit_margin high_52w illiq lhb_cnt20 lhb_net20 lhb_org_net20 low_52w
mom_120 mom_20 mom_250 mom_60 netprofit_yoy or_yoy ret_max20 rev_5 roe size
skew_20 turnover_20 turnover_chg vol_20 vol_60"""

DSL = """工厂 DSL 算子(与 qlib Alpha158 同族):
Ref(y,n)=n日前 | Mean/Sum/Std/Var/Max/Min(y,n)=滚动窗 | Quantile(y,n,q) | Rank(y,n)
Slope/Rsquare/Resi(y,n)=滚动 OLS | Corr(a,b,n) | IdxMax/IdxMin(y,n)=1..n
Greater/Less(a,b)=逐元素取大/小 | > < == 返回 0/1 | Abs/Log/Sign/Add/Sub/Mul/Div
窗口 n 只许正整数; 不许引用未来(Ref 负数直接报错)。"""

MINES = """新矿区(全部零因子覆盖, 优先在此提案):
【A. 60 分钟线日内面板】已物化为月度列(在锚点日 T 取 T-4..T 五个交易日):
  id60_late_vol_share  = mean(尾盘bar成交额/当日成交额)  —— 尾盘成交占比
  id60_late_ret        = sum(尾盘bar收益 close/open-1)   —— 尾盘动量
  id60_close_vs_dayvwap= mean(收盘/当日VWAP-1)          —— 收盘偏离均价
  (提案可在表达式里引用这三个列名, 或用标准列组合出"日内信息"的替代式)
【B. 券商金股】broker_recommend(month, broker, ts_code, name; 1.6 万行)
  → 可供构造: 近 N 月被 N 家券商共同推荐的股票计数/热度变化(需物化计数列)
【C. 研报】research_report_em(report_date, rating, rating_prev, rating_change,
  fy1_eps, fy1_pe, aim_price_t/l, 10.5 万行)
  → 可供构造: 评级上调密度/EPS 预测修正动量/目标价隐含空间(需物化)
【D. 十大流通股东】top10_holder(ann_date, holder_name, hold_amount, hold_ratio,
  hold_change; 126 万行, PIT 键=ann_date)
  → 可供构造: 股东集中度变化/新进退出计数/知名席位出现(需物化)
⚠️ B/C/D 需要先物化进月度面板才能被工厂求值 —— 提案时请同时给出物化口径
  (一句话: 用什么窗口/聚合把该表变成锚点日的横截面列)。"""

PROMPT = """你是量化因子研究员。任务: 在给定的"新矿区"提出 **{n} 条**有明确经济含义的
选股因子表达式, 供预注册门禁(IC/死区超额/多重比较)做客观判决——你不是法官, 提案
质量以门禁结果计, 但重复已知因子或无经济含义的表达式是浪费。

{panel_cols}

{dsl}

{mines}

硬性要求:
1. 每条 = {{"name": "小写下划线名", "expr": "工厂DSL表达式", "why": "一句话经济含义",
   "mine": "A/B/C/D", "materialize": "物化口径(仅 B/C/D 需要, 一句话; A 与标准列可省)"}}
2. 严禁提出与现役 31 因子近似的东西(动量/波动/反转/换手/价值的普通变体=浪费名额);
3. 优先押注微盘段行为金融逻辑(系统收益来源在微盘段的错定价);
4. expr 必须语法合法、只用上面列出的 $ 列与算子;
5. 只输出 JSON 数组, 不要其它文字。
"""

_SEEN_NAMES = set()


def ask_batch(n: int, batch_no: int, total_batches: int) -> list:
    c = _llm_cfg()
    r = requests.post(
        c["base_url"] + "/chat/completions",
        headers={"Authorization": f"Bearer {c['api_key']}"},
        json={"model": c["model"],
              "messages": [{"role": "user", "content": PROMPT.format(
                  n=n, panel_cols=PANEL_COLS, dsl=DSL, mines=MINES)}],
              "max_tokens": 4000, "temperature": 0.6},
        timeout=ASK_TIMEOUT)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"].strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    arr = json.loads(text)
    assert isinstance(arr, list), "输出不是 JSON 数组"
    keep = []
    for it in arr:
        if not isinstance(it, dict) or not it.get("name") or not it.get("expr"):
            continue
        if it["name"] in _SEEN_NAMES:
            continue
        _SEEN_NAMES.add(it["name"])
        it["batch"] = f"{batch_no}/{total_batches}"
        keep.append(it)
    return keep


def main() -> int:
    if not _llm_cfg()["api_key"]:
        print("✗ 未配置 LLM_API_KEY")
        return 2
    proposals = []
    need = BUDGET
    batch = 0
    while need > 0 and batch < 6:            # 最多 6 批防死循环
        batch += 1
        n_ask = min(BATCH, need)
        try:
            got = ask_batch(n_ask, batch, 6)
        except Exception as e:
            print(f"批 {batch} 失败: {e}")
            break
        proposals.extend(got)
        need = BUDGET - len(proposals)
        print(f"批 {batch}: +{len(got)} 条 (累计 {len(proposals)}/{BUDGET})")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "round": "r1", "date": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "budget_frozen": BUDGET, "n": len(proposals),
        "mines_priority": "新矿优先(用户拍板)",
        "note": "LLM 提案, 判决归 Alpha Factory 门禁; 不进任何生产链路",
        "proposals": proposals,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✓ {len(proposals)} 条提案 → {OUT}")
    return 0 if len(proposals) >= 30 else 1


if __name__ == "__main__":
    sys.exit(main())
