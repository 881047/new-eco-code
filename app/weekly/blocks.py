"""因子**六块分组**（2026-09-22，D 案）—— 只服务**监控与归因**，不改输入层。

## 它是什么

把 31 个生产因子切成六块，用于把"模型整体健康"拆成"六块各自的健康"（健康档见
`health.block_ic`）与"六块各自的贡献"（归因见 `attrib` 的 `blocks` 字段）。

## 为什么可以现在就定死

C 案（块级子模型 + 融合，05 号台账 2026-09-22）已判 **effect_indistinguishable**：
输入层任何重组都只是在平台内移动。⇒ 分组的用途只剩监控与归因，**不接任何交易决策**
（那是换 pin 级行为变更，需另立预注册）。

## 映射来源

与 C 案（`experiments/05_label/C_block_fusion.py`）**同一份映射**，依据
`experiments/factor_corr_36m.csv`（36 个月末截面 Spearman 均值）+ F77 边际贡献 +
F67/F52 归因。块间跨块 |ρ|≤0.4 ⇒ 块级读数有独立信息；块内"冗余"由模型消化
（F77 教训：线性冗余 ≠ 模型冗余）。

## 不变量（改这里 = 改监控口径 ⇒ 单测会红，见 `tests/test_weekly_blocks.py`）

1. 六块**恰好覆盖** 31 个生产因子（与 `app.models.daily_score.ALL_FACTORS` 同集合）、无重叠；
2. 块的**成员与优先级**与 C 案一致；
3. `STYLE_BLOCK` 只把 `attrib.STYLES` 里**真实存在**的风格列映射到块（B6 无对应风格列，
   归因里恒为 0 并如实标注 —— 事件类不在 Fama-MacBeth 风格集内，不代表它没贡献）。
"""
from __future__ import annotations

# (块键, 优先级, 角色, 成员) —— 顺序即展示顺序
BLOCKS: tuple = (
    ("B1", "P0", "流动性/规模", ("size", "amount_log", "illiq")),
    ("B2", "P0", "波动/彩票/β",
     ("vol_20", "vol_60", "ret_max20", "turnover_20", "skew_20", "beta_250", "turnover_chg")),
    ("B3", "P0", "动量/趋势",
     ("mom_20", "mom_60", "mom_120", "mom_250", "high_52w", "low_52w", "bias_60")),
    ("B4", "P0", "短窗反转", ("rev_5", "bias_20")),
    ("B5", "P1", "估值/质量/杠杆",
     ("ep", "bp", "dv_ttm", "roe", "netprofit_yoy", "or_yoy", "grossprofit_margin",
      "eps_yoy", "debt_to_assets")),
    ("B6", "P2", "龙虎榜事件", ("lhb_net20", "lhb_org_net20", "lhb_cnt20")),
)

BLOCK_KEYS: tuple = tuple(b[0] for b in BLOCKS)
BLOCK_MEMBERS: dict = {b[0]: tuple(b[3]) for b in BLOCKS}
BLOCK_PRIORITY: dict = {b[0]: b[1] for b in BLOCKS}
BLOCK_ROLE: dict = {b[0]: b[2] for b in BLOCKS}
BLOCK_FACTORS: tuple = tuple(f for b in BLOCKS for f in b[3])

#: 需要**上报**（运维邮件）的块；P1/P2 只记录不上报（D 案 §三③-1）
ALERT_PRIORITIES: tuple = ("P0",)
#: 进 doctor 的块（D 案 §四：P0 四块每块 ≥1 个读数）
DOCTOR_BLOCKS: tuple = tuple(k for k in BLOCK_KEYS if BLOCK_PRIORITY[k] == "P0")

_BLOCK_OF: dict = {f: b for b, mem in BLOCK_MEMBERS.items() for f in mem}

#: `attrib.STYLES` 的风格列 → 块。注意风格名是**归因口径**的名字（turn_20 / dv），
#: 与因子表名字（turnover_20 / dv_ttm）不同，别混。
STYLE_BLOCK: dict = {
    "size": "B1",
    "vol_20": "B2", "turn_20": "B2",
    "mom_20": "B3", "mom_60": "B3",
    "rev_5": "B4",
    "ep": "B5", "bp": "B5", "dv": "B5",
}

#: 归因里"不属于任何块"的那部分（行业 + 缺失指示 + 选股残差）的桶名。
#: 恒等式是 `Σ(B1..B6) + 未归类 = 持仓超额` —— 把它藏进残差就是本模块要防的那类自欺。
UNATTRIBUTED = "未归类"


def block_of(factor: str) -> str:
    """因子 → 块键；不在六块里（新因子/研究因子）返回空串。"""
    return _BLOCK_OF.get(str(factor), "")


def members(block: str) -> tuple:
    return BLOCK_MEMBERS.get(str(block), ())


def validate(factor_universe=None) -> dict:
    """自检：覆盖性 / 重叠 / 与生产因子全集的一致性。纯函数，供单测与 CLI 用。"""
    from collections import Counter
    cnt = Counter(BLOCK_FACTORS)
    dup = sorted(k for k, v in cnt.items() if v > 1)
    out = {"n_factors": len(BLOCK_FACTORS), "duplicates": dup, "n_blocks": len(BLOCKS)}
    if factor_universe is not None:
        uni = set(factor_universe)
        out["missing"] = sorted(uni - set(BLOCK_FACTORS))
        out["extra"] = sorted(set(BLOCK_FACTORS) - uni)
        out["covers_production"] = (not out["missing"]) and (not out["extra"])
    return out
