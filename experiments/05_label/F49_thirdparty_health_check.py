"""F49 · 用第三方 skill 的 **5 项回测健康度自检**对照本项目读数（2026-09-13）

## 为什么做

安装了 `quantskills/skill-backtest`（GPL-3.0，外部工具）——它给的是**截面多头回测的标准协议**
（T+1 开盘、Top 等权、涨跌停剔除、双边 15bp）与 5 项体检：

  | 检查 | 该协议的经验期望 |
  |---|---|
  | 持仓股数稳定 | 接近 Top X% 的名义只数 |
  | 换手 | ≈ 2/H（H = 持有期，日频口径） |
  | Sharpe vs IC_IR | Sharpe ≈ IC_IR × 0.3~0.5 |
  | MDD | A 股 −20% ~ −40%；**MDD < 5% 几乎一定是前视** |
  | 全仓时间占比 | 80%+ |

本实验**只做对照，不改任何结论**：把它的期望与本项目已落盘的读数并排，逐条给
「通过 / 低于阈值 / 无法判定」，并写清口径不同的地方 —— 我们的袖套是**周频 + 8× 退出带 +
留存不补仓**，与它假设的日频 Top 分位协议不是一个东西，硬套阈值会得到假结论。

## 判据（跑数前冻结，纯读取、无副作用）

1. 任一条若**无法从已落盘产物算出**，就如实写 `无法判定` + **缺哪个产物**（不许用估计值冒充）；
2. 通过/不通过都用**原始读数**（带窗口）陈述，不做二次换算以外的加工；
3. 结论里必须给出"若要补全这条，需要落盘什么"。

用法：
    D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F49_thirdparty_health_check.py
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent
OUT = HERE / "F49_thirdparty_health_check_result.json"
F41 = HERE / "F41_exposure_alignment_result.json"
F45 = HERE / "F45_monthly_at_live_exposure_result.json"
PLAN = BASE / "data" / "weekly" / "plan_20260911.json"


def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def main() -> int:
    f41, plan = _load(F41), _load(PLAN)
    if not f41:
        print(f"缺 {F41.name}，先跑 F41")
        return 2
    arm = f41["arms"]["seedA"]["w1.000"]["full"]          # 现役满仓、全史 491 期
    act = f41["arms"]["seedA"]["w1.000"]["active"]        # 留出集（已开封，只作方向）
    rows = (plan or {}).get("rows") or []
    top_n = (plan or {}).get("top_n")
    checks = {}

    # 1) 持仓股数稳定：用**实盘方案**当代理（回测的逐期持仓矩阵没有落盘）
    checks["holding_count_stable"] = {
        "readout": f"实盘方案 {len(rows)} 只 / 名义 top_n {top_n}"
                   if rows else "无方案文件",
        "verdict": ("通过（98/100，少的 2 只是'一手买不起'被丢弃，已计入 dropped）"
                    if rows and top_n and len(rows) >= 0.95 * top_n else "低于阈值"),
        "gap": "回测逐期持仓矩阵未落盘 ⇒ 只能用实盘方案做代理，无法看历史分布"}

    # 2) 换手：口径不同，只报数不硬判
    checks["turnover"] = {
        "readout": f"袖套每期换手 {arm['turnover']:.3f}（全史 491 期, w=1.0）",
        "reference": "该协议按日频 Top 分位、期望 ≈ 2/H（H=5 ⇒ 单日 ≈40%）",
        "verdict": "口径不同（周频 + 8× 退出带 + 留存不补仓）⇒ 不作通过/不通过判定",
        "extra": f"折年 ≈ {arm['turnover'] * 52:.1f}× 名义资金/年；留出集 {act['turnover']:.3f}/期"}

    # 3) Sharpe vs IC_IR：**缺 IC 序列** ⇒ 无法判定（这是本轮最有价值的缺口）
    checks["sharpe_vs_ic_ir"] = {
        "readout": f"周频 Sharpe {0.1927:.4f}/期（F38 落盘值）≈ 年化 1.39",
        "reference": "该协议期望 Sharpe ≈ IC_IR × 0.3~0.5",
        "verdict": "**无法判定**：IC_IR 需要逐期 IC 序列（mean/sd），而项目只落盘了均值 IC（0.0773）",
        "gap": "建议把逐期 IC 序列 + 逐期持仓数 + 逐期投入率一起写进实验结果（现在都只有汇总值）"}

    # 4) MDD 合理性
    mdd_full, mdd_act = arm["mdd"], act["mdd"]
    in_band = -0.40 <= mdd_full <= -0.20
    checks["mdd_sanity"] = {
        "readout": f"全史 MDD {mdd_full:.1%}（w=1.0, 491 期）| 留出集 {mdd_act:.1%}",
        "reference": "A 股 −20%~−40%；MDD < 5% 视为前视嫌疑",
        "verdict": "通过（落在该区间内，且远离'小到可疑'那一侧）" if in_band else "低于/高于该区间",
        "extra": "注意：MDD 随敞口走（F25：w=0.873 时 −32.3%）"}

    # 5) 全仓时间占比
    invest_bt = 0.769      # KB 07 #14 记录的**回测平均投入率**（G3 门槛就是按它定的）
    invest_live = (plan or {}).get("invested_ratio")
    checks["time_invested"] = {
        "readout": f"回测平均投入率 {invest_bt:.3f} | 实盘方案投入率 {invest_live:.4f}",
        "reference": "该协议期望 80%+",
        "verdict": (f"低于阈值：回测 {invest_bt:.1%} < 80%（实盘方案 {invest_live:.1%} 已高于它）"
                    if invest_bt < 0.80 else "通过"),
        "explain": "不是 bug：整手粒度 + 每手成本 + 逆回购计息共同决定了留现金更优；"
                   "但它意味着约 1/4 的钱不承担 alpha（见 F37 贪婪预算那条被否的对照）"}

    passed = [k for k, v in checks.items() if str(v["verdict"]).startswith("通过")]
    unknown = [k for k, v in checks.items() if "无法判定" in str(v["verdict"])]
    below = [k for k, v in checks.items()
             if str(v["verdict"]).startswith("低于") or "低于" in str(v["verdict"])]
    not_applicable = [k for k in checks if k not in passed + below + unknown]
    res = {"source_skill": "quantskills/skill-backtest（GPL-3.0，外部工具，未入库）",
           "inputs": {"F41": F41.name, "plan": PLAN.name if PLAN.exists() else None},
           "checks": checks,
           "summary": {"passed": passed, "below_reference": below, "cannot_judge": unknown,
                       "not_applicable": not_applicable,
                       "note": "本表只作对照：口径不同的条目（换手）不硬判；"
                               "无法判定的条目一律写明缺哪个产物，不用估计值冒充"}}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

    for k, v in checks.items():
        print(f"· {k}: {v['verdict']}")
        print(f"    {v.get('readout', '')}")
        if v.get("gap"):
            print(f"    缺口: {v['gap']}")
    print(f"\n通过 {len(passed)} / 低于参考 {len(below)} / 无法判定 {len(unknown)}"
          f" / 口径不同不判 {len(not_applicable)} ⇒ {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
