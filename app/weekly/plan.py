"""周频方案: 打分 → (top-N + 排名退出带) → 整手/费用约束下的目标持仓

## 构型的来源(全部是 2026-09-12 实测, 不是拍脑袋)

| 账户 | 构型 | 实测全史超额 | 组合年化 |
|---|---|---|---|
| 2 万 | top6 + 持仓加成 0.2σ | −2.04pp | +6.44% |
| 20 万 | top50 + 出带 3× | +7.98pp | +16.47% |
| 50 万 | top100 + 出带 5× | +8.85pp | +17.34% |
| 100 万 | top100 + 出带 8× | +9.40 ~ +10.09pp | +17.9 ~ +18.6% |

⇒ 构型**随资金自适应**(`pick_shape`),这是实测出来的,不是配置审美。
独立实现(Claude Code 从零重写)复算 100 万构型: +20.59% / +12.11pp。

## 退出带为什么关键

买前 N、**卖要等排名掉出 N×band**。实测同一实现里把 band 从 1×(无带)提到 8×:
换手 0.511 → 0.180,超额 +8.82pp → +12.11pp。窄进宽出同时降成本与减少"擦边进出"。

## 边界

- 本模块**只产出方案**(JSON),不下单、不改 `plan` 表;接线到模拟盘是独立一步。
- 持有集合由调用方给出(默认空)⇒ 首次运行等价于全新建仓,不会误清月频持仓。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd

from app import config as cfg_mod
from app import db
from app.portfolio import board as board_mod
from app.weekly import scores as sc
from app.weekly.errors import ArchiveAnchorNotCovered

log = logging.getLogger(__name__)

PLAN_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "weekly"

# 资金上限 → (top_n, 排名退出带倍数, 持仓加成σ)。四个档位**各自对应一次实测**
# (2万 top6+加成 / 20万 top50+出带3× / 50万 top100+出带5× / 100万 top100+出带8×),
# 不是插值出来的漂亮曲线。改这张表 = 换构型, 必须重跑实验。
SHAPES = ((20_000, 6, 0.0, 0.20),
          (200_000, 50, 3.0, 0.0),
          (500_000, 100, 5.0, 0.0),
          (float("inf"), 100, 8.0, 0.0))

# 实测预期 —— 写进方案里, 让每次出方案都带着真实数字(而不是让人以为能赚 3%/周)。
#
# 当前默认构型 = **2×W5 + 1×pit 两个 die**(5 日标签 LightGBM + 申万一级 PIT 时点行业版同款模型),
# 数字取两套独立种子的均值, **同窗口 491 期(2017-02~2026-09)**:
#   seedA: 超额 +14.91pp MDD −32.3% IC +0.0773 | seedB: +14.30pp −32.2% IC +0.0773
# 对照(同窗口同种子): 单 die W5 +9.39/+8.85pp; 旧默认 W5+M +12.02/+11.52pp;
#   W5+pit(1:1) +13.16/+12.48pp; W5+head_vt 固定配对 +10.78/+11.31pp(不如现役)。
# ⇒ **F22 判决: 2×W5+pit 比旧默认高 +2.8~2.9pp, 回撤持平, IC 更高, 两套种子一致。**
#
# 为什么不是 606 期: 生产 `model_score` 2017 年才有数据, 含 M 的打分在更早锚点是 NaN。
# 606 期(含 2015-2016)的周分布见 F7 —— 那是**单 die 口径**, 不是当前默认。
#
# ⚠️ 2026-09-13 F28 **样本外一次性开封**: 上面那 +14.6pp 几乎全部来自 2020-09~2023-12;
# **留出集 2024-01~2026-08(24 个月) 只有 +7.34/+8.62pp, 且 t=0.78/0.91、多重性校正后 p=1.00**
# （与 0 不可区分）。而且构型本身是在**含该窗口**的 491 期上选的 ⇒ 那个 +7~9pp 还是上界。
# ⇒ 任何对外报数都必须同时给两行, 只报 +14.6pp 是误导(§6.2 的"报告必带窗口"就是这个意思)。
EXPECTATION = {
    "basis": "同窗口 491 期(2017-02~2026-09), 100万/top100/出带8×/2×W5+pit/两套种子均值",
    "ann_pct": None, "excess_pp": 14.6, "mdd_pct": -32.3, "turnover": 0.137,
    "oos_24m": {"window": "2024-01~2026-08", "excess_pp": [7.34, 8.62],
                "t_hac": [0.78, 0.91], "p_corrected": 1.00,
                "note": "样本外且构型是在含该窗的 491 期上选的 ⇒ 这是**上界**; 一次性开封已用掉"},
    # F38(2026-09-13): 构型是从 ~25 个组合臂里选出来的 ⇒ 必须做多重比较打折。
    # 现役信息比率(超额 Sharpe)年化 1.39/1.33, 而 7 个有记录的试验臂给出的"期望最大 SR"=0.89
    #  ⇒ DSR(N=7)=0.876/0.850, DSR(N=25)=0.597/0.544 —— **都低于 0.95 门槛**。
    # 注意与 Bonferroni 的区别: 同一份 t=4.28/4.11 在 N≤1000 下仍显著("超额>0"成立),
    # DSR 回答的是另一个问题("这个 IR 是不是搜索出来的最好那条")。
    "multiplicity": {"dsr_n7": [0.876, 0.850], "dsr_n25": [0.597, 0.544],
                     "pass_95": False,
                     "note": "多重比较后**不显著** ⇒ 全史 +14.6pp 不得当显著读数用"},
    "weeks_ge_3pct": None,          # 当前默认的周分布尚未实测(下一步: 对 W5+M 重跑 F7 型分布)
    "single_die_reference": {"ann_pct": 18.4, "excess_pp": 9.05, "mdd_pct": -31.6,
                             "mean_week_pct": 0.391, "weeks_ge_3pct": 0.180,
                             "basis": "606 期(含 2015-2016), 单 die W5"},
}

# F25 的**敞口刻度**（2026-09-13 接进来）：上面那份 EXPECTATION 是在 `w = 0.873` 这一个点上
# 实测的。袖套的权益权重一旦改（`weekly.equity_weight`），预期就必须跟着走 —— 否则方案里会
# 一边写着"投出 87%"，一边挂着 w=0.873 时代的 +14.6pp，那是拿旧标签描述新配置（配置漂移）。
# 三个点是 F25 实测（491 期、两套种子均值），点与点之间线性插值并**明确标注是插值**。
DIAL_POINTS = {
    # `by` = **这个数是谁测的**：必须逐点标注，否则会出现"数值来自 F52、却写着 F25"的误标
    # （2026-09-13 自查评审抓到 —— 当时 w=1.0 的 source 还写着 F25，而值已是 F52 的）。
    0.873: {"excess_pp": 14.61, "mdd_pct": -32.3,
            "by": "F25 实测点 w=0.873（研究构型 hyst=0.2）"},
    0.95: {"excess_pp": 16.32, "mdd_pct": -35.1,
           "by": "F25 实测点 w=0.95（研究构型 hyst=0.2）"},
    # ⚠️ 2026-09-13 F52 实测（两套种子）：**生产构型**（hold_bonus=0.0，即 pick_shape(1e6) 的真实值）
    # 全史 +17.78/+17.47pp、MDD −36.6%、换手 0.137；而 F25 那批研究读数用的是 **hyst=0.2**
    # （+18.00/+17.30pp、−36.9%、换手 0.118）。全史差 0.17~0.22pp（在 F52 预注册容差 1.0pp 内），
    # 但**留出集差 ~0.8pp**（生产 +9.21/+11.40 vs 研究 +10.38/+11.80）⇒ 这里以**生产构型**为准。
    1.00: {"excess_pp": 17.63, "mdd_pct": -36.6,     # F52: +17.78/+17.47（生产构型）
           "by": "F52 实测点 w=1.00（生产构型 hold_bonus=0.0；研究构型 hyst=0.2 为 "
                 "+18.00/+17.30、−36.9%）"},
}
DIAL_CONFIG_NOTE = ("w=0.873/0.95 两点是**研究构型**（hyst=0.2）读数；w=1.00 已按 F52 换成"
                    "**生产构型**（hold_bonus=0.0）读数。研究构型的 w=1.00 读数是 +18.00/+17.30pp、"
                    "MDD −36.9%、换手 0.118（F25/F41），保留在 F52 结果里备查。")
DIAL_REGRESSION = {"intercept_pp": -10.44, "slope_pp_per_w": 26.23, "r2": 0.955,
                   "note": "F25 实测关系: 超额 ≈ −10.44 + 26.23×w（用它交叉核对插值）"}

# **样本外(F28 那段留出集 2024-01~2026-08)逐敞口实测** —— 它只在被测过的那几个 w 上存在：
#   0.873 → F28 一次性开封: +7.34/+8.62pp, t=0.78/0.91, 校正后 p=1.00 ⇒ 与 0 不可区分;
#   1.00  → F41(2026-09-13 敞口对齐): +10.38/+11.80pp, **水平 t=1.15/1.28**, 增量 t≈1.44。
# ⚠️ 1.00 这一行**不是 C3 通过**：C3 的判据(F28)已经在 w=0.873 上一次性开封并用掉,
#    满仓这版是在**同一段已开封窗口**上再测 ⇒ 只能当方向性读数, 不产生新的显著性证据。
OOS_BY_WEIGHT = {
    0.873: {"excess_pp": [7.34, 8.62], "t_hac": [0.78, 0.91], "p_corrected": 1.00,
            "measured_by": "F28", "measured_at_weight": 0.873},
    # ⚠️ 2026-09-13 F52：生产构型（hold_bonus=0.0）实测 +9.21/+11.40pp ⇒ **两套种子的均值
    # 约 +10.3pp，其中 seedA 落在 +10pp 线下方**；研究构型（hyst=0.2, F41）是 +10.38/+11.80pp。
    # 口径仍是同一段**已开封**的留出集（F28）⇒ 只作方向性读数，不产生新的显著性证据。
    1.00: {"excess_pp": [9.21, 11.40], "t_hac": [1.15, 1.28], "p_corrected": None,
           "measured_by": "F52", "measured_at_weight": 1.00,
           "research_config_excess_pp": [10.38, 11.80],
           "increment_t": [1.44, 1.45],
           "note": "**生产构型**读数：均值 ≈+10.3pp、但 seedA(+9.21) 在 C3 的 +10pp 线下方"
                   "（研究构型是 +10.38/+11.80）⇒ 只能说'贴着线'，不能说'名义跨过'。"
                   "水平 t≈1.2 与 0 不可区分 ⇒ 不是 C3 通过，见 §2.46/F41/F52"},
}

# **月度口径**（用户的目标函数就是"月收益"）—— 逐敞口实测, 同样只在被测过的 w 上存在：
#   0.873 → F36: 留出集 月均 +0.55/+0.63pp, 命中 62%, 月σ 3.0pp
#   1.00  → F45: 全史 月均 +1.35/+1.30pp(命中 74%, 月σ 2.66)；
#                  留出集 月均 +0.84/+0.93pp(命中 72%/69%, 月σ 3.48, 年化 +10.33/+11.73pp)；
#                  死区 月均 +1.79/+1.75pp(命中 78%)
# 样本量算术（F45 一并给出）: 留出集 月σ 3.48pp ⇒ 月均 SE≈0.61pp ⇒ 要在 2×SE 下分辨
# "+10pp/年 ≈ +0.83pp/月"，需要 **约 70 个月（≈6 年）** —— 纸面 8~12 周在统计上什么都证明不了。
MONTHLY_BY_WEIGHT = {
    0.873: {"full": {"mean_pp": [1.08, 1.04], "hit": [0.76, 0.74], "sd_pp": 2.9},
            "oos": {"mean_pp": [0.55, 0.63], "hit": [0.62, 0.62], "sd_pp": 3.0},
            "measured_by": "F36", "measured_at_weight": 0.873},
    # ⚠️ 2026-09-13 F53：月度口径同样按**生产构型**重算（F52 的逐期序列 → F36.to_monthly 同一口径）。
    # 生产构型：全史 +1.34/+1.31pp、留出集 **+0.76/+0.91pp（均值 0.835）**、OOS 命中 65.6%/65.6%；
    # 研究构型（F45）是 +1.35/+1.30 与 +0.84/+0.93（命中 71.9%/68.8%）⇒ 留出集差 ~0.09pp/月，
    # 且生产构型**贴着** +10pp/年 ≈ +0.83pp/月 的线（一套种子在线上、一套在线下）。
    1.00: {"full": {"mean_pp": [1.34, 1.31], "hit": [0.74, 0.74], "sd_pp": 2.66,
                    "ann_excess_pp": [17.4, 16.9]},
           "oos": {"mean_pp": [0.76, 0.91], "hit": [0.656, 0.656], "sd_pp": 3.48,
                   "ann_excess_pp": [9.2, 11.2]},
           "research_config": {"full_mean_pp": [1.35, 1.30], "oos_mean_pp": [0.84, 0.93],
                               "oos_hit": [0.719, 0.688], "measured_by": "F45"},
           "months_for_2se_at_0_83pp": 70,
           "measured_by": "F53", "measured_at_weight": 1.00},
}


def expectation_at(cfg: dict = None) -> dict:
    """按**当前敞口**给出一份预期，并把"这个数是怎么来的"写在同一份字典里。

    - `w` 正好落在 F25 实测点上 ⇒ 直接给该点读数（`source="F25 实测点"`）；
    - 落在点与点之间 ⇒ 线性插值（`source="F25 两实测点线性插值"`）；
    - 落在实测区间外 ⇒ 照给但标注 `source="超出 F25 实测区间(仅线性外推)"`；
    - 样本外那段（F28）**只在 w=0.873 上测过** ⇒ 无论当前 w 是多少，都原样带着并注明，
      免得让人以为"满仓的样本外读数"存在。
    """
    w = float(equity_weight(cfg or {}))
    out = json.loads(json.dumps(EXPECTATION, ensure_ascii=False))     # 深拷贝, 不改原表
    pts = sorted(DIAL_POINTS)
    out["equity_weight"] = w
    out["calibrated_at_weight"] = 0.873
    if any(abs(w - p) < 1e-9 for p in pts):
        p = min(pts, key=lambda x: abs(w - x))
        out.update({"excess_pp": DIAL_POINTS[p]["excess_pp"],
                    "mdd_pct": DIAL_POINTS[p]["mdd_pct"],
                    "source": DIAL_POINTS[p]["by"],
                    "basis": DIAL_POINTS[p]["by"] + "｜" + EXPECTATION["basis"]})
    else:
        lo = max([p for p in pts if p <= w], default=None)
        hi = min([p for p in pts if p >= w], default=None)
        if lo is None or hi is None:
            lo = hi = pts[0] if lo is None and hi is None else (lo or hi)
            # 2026-09-28 审查(36补)文案如实化: 本分支**不外推** —— excess_pp/mdd_pct 保持
            # 基线 0.873 点的实测值原样展示(原文案写"仅线性外推"与数值不符, 误导读数)
            out["source"] = (f"超出实测区间 {pts[0]:g}~{pts[-1]:g}（不外推, 沿用基线 "
                             f"w=0.873 点的实测读数, 不可当当前敞口的读数用；"
                             f"端点由 F25/F52 测得）")
        elif lo == hi:
            out["source"] = DIAL_POINTS[lo]["by"]
        else:
            k = (w - lo) / (hi - lo)
            out["excess_pp"] = round(DIAL_POINTS[lo]["excess_pp"] * (1 - k)
                                     + DIAL_POINTS[hi]["excess_pp"] * k, 2)
            out["mdd_pct"] = round(DIAL_POINTS[lo]["mdd_pct"] * (1 - k)
                                   + DIAL_POINTS[hi]["mdd_pct"] * k, 2)
            out["source"] = (f"两实测点线性插值（w={lo:g} 与 w={hi:g} 之间；"
                             f"刻度关系 {DIAL_REGRESSION['note']}）")
        out["basis"] = f"w={w:g}（{out['source']}）: " + EXPECTATION["basis"]
    # 样本外：**只在被测过的敞口点上存在**。当前 w 有实测就用当前的；没有就退回最近测过的那一点,
    # 并把"这是哪个敞口上测的"写死在同一份字典里(否则会被读成"当前敞口也验过")。
    oos = json.loads(json.dumps(out.get("oos_24m") or {}, ensure_ascii=False))
    wsp = sorted(OOS_BY_WEIGHT)
    near = min(wsp, key=lambda x: abs(w - x))
    src = OOS_BY_WEIGHT[near] if abs(w - near) < 1e-9 else None
    if src is None:                      # 当前敞口没测过 ⇒ 显示最近测过那点, 并标明
        src = OOS_BY_WEIGHT[wsp[-1]] if w > wsp[-1] else OOS_BY_WEIGHT[near]
    oos.update({k: v for k, v in src.items() if k != "measured_at_weight"})
    oos["window"] = "2024-01~2026-08"
    oos["measured_at_weight"] = src["measured_at_weight"]
    if abs(w - src["measured_at_weight"]) > 1e-9:
        oos["note"] = ((src.get("note") or "") +
                       f"；⚠ 这段只在 w={src['measured_at_weight']:g} 上测过 —— "
                       f"当前 w={w:g} 的样本外读数**不存在**，不要用其它敞口点的全史数去替代它"
                       ).lstrip("；")
    out["oos_24m"] = oos
    # F38 的 DSR 是用 **w=0.873 那条臂**的 IR 算的；换敞口后没重算 ⇒ 明写出来。
    # 注意两个方向都不许越读: 不因为"旧敞口不显著"就断言新敞口不显著,
    # 也不因为"新敞口全史更高"就当作已经过了多重比较关。
    mul = out.get("multiplicity") or {}
    mul["computed_at_weight"] = 0.873
    mul["note"] = ((mul.get("note") or "") +
                   "；⚠ 该 DSR 用 w=0.873 的 IR 算出, 当前敞口未重算 ⇒ "
                   "既不能推断新敞口显著, 也不能当作它已过多重比较关（按未通过处理）")
    out["multiplicity"] = mul
    # 月度口径：用户的目标函数是"月收益"，所以**月度那一行必须跟着敞口走**，
    # 而且带上"多久才够判"—— 否则 2~3 个月的纸面样本很容易被读成"已经达标"。
    msp = sorted(MONTHLY_BY_WEIGHT)
    mnear = min(msp, key=lambda x: abs(w - x))
    msrc = MONTHLY_BY_WEIGHT[mnear]
    out["monthly"] = json.loads(json.dumps(msrc, ensure_ascii=False))
    if abs(w - msrc["measured_at_weight"]) > 1e-9:
        out["monthly"]["note"] = (f"⚠ 月度读数只在 w={msrc['measured_at_weight']:g} 上测过；"
                                  f"当前 w={w:g} 没有月度实测")
    return out


def pick_shape(capital: float, cfg: dict = None) -> tuple:
    """按账户规模选构型;config 的 `weekly.shape` 可显式覆盖(top_n/band/hold_bonus)。"""
    ov = ((cfg or {}).get("weekly") or {}).get("shape") or {}
    top_n, band, bonus = next((t, b, h) for lim, t, b, h in SHAPES if capital <= lim)
    return (int(ov.get("top_n", top_n)), float(ov.get("band", band)),
            float(ov.get("hold_bonus", bonus)))


def equity_weight(cfg: dict) -> float:
    """袖套的权益权重。

    ⚠️ 默认**继承** `regime_equity_weight.aggressive.attack`(0.873) —— 但那个键是**月频系统**的,
    改它会同时改月频。袖套要单独调敞口(实测 F25: 超额 ≈ −10.4 + 26.2×w, R²=0.955)
    必须写 `weekly.equity_weight`, 只影响袖套。
    """
    ov = (cfg.get("weekly") or {}).get("equity_weight")
    if ov is not None:
        return float(ov)
    return float((cfg.get("regime_equity_weight") or {}).get("aggressive", {})
                 .get("attack", 0.873))


def sleeve_capital(cfg: dict, override: float = None) -> float:
    """袖套资金: **优先 `weekly.capital`**, 缺省回退到顶层 `capital`。

    2026-09-12 审计发现(Claude 独立审计 #1): config.yaml 写了 `weekly.capital: 1000000`,
    但所有消费点都读顶层 `capital`(=2 万) ⇒ 袖套会按 2 万跑, 而 2 万在本构型下
    因整手粒度只买得起 3 只(plan.py 的阶梯注释与实测都写明"2 万级失效")。
    配置宣称的唯一事实源与运行行为不一致 —— 这类"静默按错规模跑"必须堵掉。
    """
    if override is not None:
        return float(override)
    w = (cfg or {}).get("weekly") or {}
    if w.get("capital"):
        return float(w["capital"])
    return float((cfg or {}).get("capital", 20000))


def _latest_prices(conn, anchor: str, codes: list) -> pd.DataFrame:
    """锚点日的收盘价/名称/行业(只读;用于整手估算,真实成交价由模拟盘按次日开盘定)。"""
    q = ",".join("?" * len(codes))
    px = pd.read_sql(
        f"SELECT ts_code, close FROM daily WHERE trade_date=? AND ts_code IN ({q})",
        conn, params=(anchor, *codes))
    info = pd.read_sql(
        f"SELECT ts_code, name, industry FROM stock_basic WHERE ts_code IN ({q})",
        conn, params=tuple(codes))
    return px.merge(info, on="ts_code", how="left").set_index("ts_code")


def select_targets(sc_vec: pd.Series, ok: np.ndarray, held: list, top_n: int,
                   band: float, hold_bonus: float) -> tuple:
    """纯函数(无 IO, 可单测): 按「宽出带 + 补满名额」选目标。

    - `ok` = 可交易且分数有限(与 sc_vec 同序);
    - 持仓名先按 `top_n × band` 的排名带保留(窄进宽出);
    - 其余名额按分数从高到低补满, 总数不超过 top_n;
    - `hold_bonus` > 0 时给持仓名加 σ 倍数加成(小资金构型用, 实测可降换手)。
    返回 (target 列表, 排名 Series)。
    """
    raw = sc_vec.where(ok)
    sd = float(np.nanstd(raw.to_numpy()))
    adj = raw.copy()
    if hold_bonus and held:
        present = [c for c in held if c in adj.index]
        if present:
            adj.loc[present] = adj.loc[present] + hold_bonus * sd
    rk = adj.rank(ascending=False, method="first")
    band_n = int(top_n * band) if band > 1 else top_n
    target = [c for c in held
              if c in rk.index and np.isfinite(rk[c]) and rk[c] <= band_n]
    for c in rk.dropna().sort_values().index:
        if len(target) >= top_n:
            break
        if c not in target:
            target.append(c)
    return target[:top_n], rk


def _code_version() -> str:
    """当前代码版本（git 短 sha）。拿不到就返回 `unknown` —— 版本号缺失不该让出方案失败。"""
    try:
        # 2026-09-13（07 号 #27 同类）：`git` 是控制台程序，而服务由看门狗**无控制台**拉起
        # （DETACHED_PROCESS）⇒ 不带 CREATE_NO_WINDOW 时 Windows 会给它新建一个控制台窗口，
        # 出方案时桌面上闪一下。加标志后无窗（不影响 capture_output）。
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           cwd=str(Path(__file__).resolve().parent.parent.parent),
                           capture_output=True, text=True, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (r.stdout or "").strip() or "unknown"
    except Exception:
        return "unknown"


def _sha16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def artifact_meta(cfg: dict, scores_path, eq_w: float, top_n: int, band: float,
                  bonus: float, lot_mult: float) -> dict:
    """产物的**输入版本指纹**（规划 §6.1-2：任一产物可反查其输入版本）。

    记录: 代码版本 / 打分存档(名 + 内容指纹) / 影响出方案的配置子集指纹 / 构型与权重。
    """
    p = Path(scores_path)
    try:
        scores_sha = _sha16(p.read_bytes()) if p.exists() else None
    except OSError:
        scores_sha = None
    subset = {"weekly": (cfg.get("weekly") or {}), "fees": cfg.get("fees"),
              "regime_equity_weight": cfg.get("regime_equity_weight"),
              "single_side_cost": cfg.get("single_side_cost"),
              "benchmark": cfg.get("benchmark")}
    return {"built_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "code": _code_version(),
            "scores_file": p.name, "scores_sha16": scores_sha,
            "config_sha16": _sha16(json.dumps(subset, sort_keys=True, default=str).encode()),
            "equity_weight": eq_w, "top_n": top_n, "band": band, "hold_bonus": bonus,
            "max_lot_cost_mult": lot_mult}


def build(asof: str = None, capital: float = None, held: list = None,
          cfg: dict = None, write: bool = True, ledger_path=None,
          max_lot_cost_mult: float = 1.5) -> dict:
    """生成周频方案。返回 dict(含 rows/summary/path)。

    **held 的默认值 = 袖套账本的当前持仓**(2026-09-13 实测修掉的真 bug):
    排名退出带(band=8×)只有在**传入当前持仓**时才生效 —— 回测里持仓由模拟器内部维护,
    而生产路径若默认 held=[](空), 目标就退化成"每周按分数取前 100 名",
    持有的一律被卖掉 ⇒ **实盘换手会比回测高数倍**(12 周生产路径回放实测: 每周 84~127 笔,
    约 85 个持仓换掉一半; 而 band 生效时回测换手是 9%)。费用假设因此全部失真。

    `max_lot_cost_mult > 0`: **选股阶段就要求"一手买得起"**(价格×100 ≤ 倍数×等权预算)。
    动机: 新账户在 100 万/top100 下, 每只预算 8,730 元 ⇒ 价格 >87 元的票买不起,
    实测锚点 20260911 的 100 个目标里有 15 只属于此类 ⇒ 只投出 64%。
    回测里加这个上限**几乎没有差别**(F23: 投入率 76.9%→77.1%、超额 ±0.1pp),
    但那只说明"回测的组合是早年低股价时建起来的、约束不再咬人" —— **测不到新账户的起点效应**。
    面向新账户时, 把价钱门槛前置可以让名额顺延给买得起的票。**默认 1.5**(可用 config 的
    `weekly.max_lot_cost_mult` 覆盖; 0 = 关)。实测(锚点 20260911, 100 万):
    关 → 85 只 / 投出 63.5%; 1.25× → 94 只 / 69.7%; 1.5× → 94 只 / 69.7%; 2.0× → 91 只 / 67.8%。
    ⚠️ 剩下的 ~17pp 缺口是**整手取整**(每只 9,287 元预算, 取整后平均只投出约 7,400 元)——
    那是结构性的, 只能靠"减少持仓只数"或"贪婪预算"解决, 两者都会改变已验证的构型, 未采纳。
    """
    cfg = cfg or cfg_mod.load_config()
    capital = sleeve_capital(cfg, capital)
    if max_lot_cost_mult == 1.5:            # 允许 config 覆盖(缺省 1.5)
        max_lot_cost_mult = float((cfg.get("weekly") or {}).get("max_lot_cost_mult", 1.5))
    held_shares = {}
    if held is None:
        from app.weekly.ledger import Ledger
        try:
            _pos = Ledger.load(capital, ledger_path).state.get("positions", {})
        except Exception as e:
            # 2026-09-14（07 号 #48 同族）：**文件存在但读不动** ⇒ 绝不许按空仓出方案。
            # 按空仓出方案 = 排名退出带整体失效 ⇒ 执行档会把所有不在新目标里的留存票卖掉
            # （docstring 自己写过："实盘换手会比回测高数倍"，费用假设随之全部失真）。
            # 文件**不存在**是正常首跑（`Ledger.load` 返回空账本、走不到这里），
            # 所以能触发本分支的只有真损坏 —— 那种时候正确动作是失败并让人看见。
            raise RuntimeError(
                f"袖套账本不可读（{ledger_path or Ledger.DEFAULT_PATH}）：{e} —— "
                f"拒绝按空持仓出方案（会静默丢掉排名退出带、把留存票全部卖掉）；"
                f"请先从 .bak 或 data/weekly/backup/ 恢复账本，或显式传 held=[] 走人工口径"
            ) from e
        held_shares = {c: int(v.get("shares", 0)) for c, v in _pos.items()}
        held = list(_pos)
        log.info("持有集合取自袖套账本: %d 只", len(held))
    top_n, band, bonus = pick_shape(capital, cfg)
    eq_w = equity_weight(cfg)
    cost = float(cfg.get("single_side_cost", 0.003))

    S, anchors, cols, names, path = sc.latest()
    if asof:                       # 允许指定锚点(必须存在于存档里)
        want = pd.Timestamp(asof)
        if want not in set(anchors):
            # 2026-09-18 审查 Q-04：带 ArchiveMiss 标记，**文案一字未改**；仍是 ValueError
            raise ArchiveAnchorNotCovered(
                f"锚点 {asof} 不在打分存档里({anchors[0].date()}~{anchors[-1].date()})")
        k = list(anchors).index(want)
    else:
        k = len(anchors) - 1
    anchor_d = anchors[k].strftime("%Y%m%d")

    conn = db.get_conn()
    try:
        mask = _tradable_at(conn, anchor_d, cols)
        sc_vec = pd.Series(S[k], index=cols, dtype=float)
        ok = mask & np.isfinite(sc_vec.to_numpy())
        if max_lot_cost_mult > 0:
            # 选股阶段就要求"一手买得起"(见 docstring): 把买不起的高价票从候选里剔除,
            # 名额自然顺延给下一名 ⇒ 新账户的投入率不再被少数高价股拖住。
            eq_budget = capital * eq_w / max(1, top_n)
            px_all = pd.read_sql("SELECT ts_code, close FROM daily WHERE trade_date=?",
                                 conn, params=(anchor_d,)).set_index("ts_code")["close"]
            px_all = px_all.reindex(cols).astype(float)
            ok &= (px_all.to_numpy() * 100.0) <= (max_lot_cost_mult * eq_budget)
        if ok.sum() < 200:
            raise RuntimeError(f"锚点 {anchor_d} 可交易且可打分的股票只有 {int(ok.sum())} 只, 拒绝出方案")
        held = [c for c in (held or []) if c in set(cols)]
        target, rk = select_targets(sc_vec, ok, held, top_n, band, bonus)
        info = _latest_prices(conn, anchor_d, target)
    finally:
        conn.close()

    budget = capital * eq_w / max(1, len(target))
    rows, cost_est = [], 0.0
    drop = {"no_price": 0, "unaffordable": 0}
    for c in target:
        if c not in info.index:
            drop["no_price"] += 1
            continue
        px = float(info.loc[c, "close"])
        if not np.isfinite(px) or px <= 0:
            drop["no_price"] += 1
            continue
        share = board_mod.lot_shares(c, int(np.floor(budget / (px * (1 + cost)) + 1e-9)))
        if share <= 0:
            # 预算买不起最小起买量(100 股 ⇒ 8,730 元预算时价格 >87 元即出局)。
            # **但已持有的票不该因此被剔除**(2026-09-13 回放实测修掉的分歧): 它不需要再花钱,
            # 回测里只要排名还在退出带内就保留。原实现把它一并丢掉 ⇒ 实盘卖掉了本该留的仓位,
            # 换手比回测高数倍。
            if c in held_shares and held_shares[c] > 0:
                share = held_shares[c]
            else:
                drop["unaffordable"] += 1
                continue
        elif c in held_shares and held_shares[c] > 0:
            share = held_shares[c]        # 留存持仓不动(不补仓), 与回测同口径
        amt = share * px
        cost_est += amt
        rows.append({"ts_code": c, "name": info.loc[c, "name"], "industry": info.loc[c, "industry"],
                     "score": round(float(sc_vec[c]), 6), "rank": int(rk[c]),
                     "close": round(px, 3), "shares": int(share),
                     "amount": round(amt, 2), "held": c in held})
    weights = {r["ts_code"]: round(r["amount"] / max(1e-9, cost_est), 4) for r in rows}
    for r in rows:
        r["weight"] = weights[r["ts_code"]]
    if not rows:
        # 2026-09-28 审查修复(P1): 锚点日行情整日缺失(周五晚链失败+周末宕机+补跑又失败)
        # 时 target 全部 no_price ⇒ 原会写出 n_target=0 的**空方案**且 ensure_plan 判 ok,
        # 下游 execute_plan 把"不在目标集"的**全部持仓**按当日价清仓(卖出不受
        # risk_halt/governor 拦截) —— 整个袖套"按规则"清零且台账全绿。
        # 空目标集 ≠ 决定清仓(与月盘 builder A7 同语义) ⇒ 拒绝出方案。
        raise RuntimeError(
            f"锚点 {anchor_d} 目标全部无价格(no_price={drop['no_price']}), 拒绝出空方案")

    out = {
        "anchor": anchor_d, "capital": capital, "top_n": top_n, "band": band,
        "hold_bonus": bonus, "equity_weight": eq_w,
        "n_target": len(rows), "n_held": sum(1 for r in rows if r["held"]),
        "invested": round(cost_est, 2),
        "cash_left": round(capital - cost_est, 2),
        "invested_ratio": round(cost_est / max(1e-9, capital), 4),
        "dropped": drop,
           # 预期**随敞口走**(见 expectation_at): 换 w 之后挂着旧点的数就是配置漂移
           "expectation": expectation_at(cfg),
        "scores_file": str(path), "rows": rows,
        "meta": artifact_meta(cfg, path, eq_w, top_n, band, bonus, max_lot_cost_mult),
    }
    if write:
        PLAN_DIR.mkdir(parents=True, exist_ok=True)
        p = PLAN_DIR / f"plan_{anchor_d}.json"
        # 2026-09-28: 改**原子写**(与 ledger.save 同手法) —— 原 write_text 直接覆写,
        # 写到一半被杀会留下半截 JSON, /api/weekly、reconcile、exec 在窗口内读到坏方案
        tmp = Path(str(p) + ".tmp")
        try:
            tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, p)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    log.warning("临时方案文件清理失败: %s", tmp)
        out["path"] = str(p)
    log.info("周频方案 %s: %d 只(保留 %d), 投出 %.0f/%.0f 元(%.0f%%), 构型 top%d/band%.1f/bonus%.2f"
             " | 丢弃: 买不起一手 %d / 无价格 %d",
             anchor_d, len(rows), out["n_held"], cost_est, capital,
             100 * out["invested_ratio"], top_n, band, bonus,
             drop["unaffordable"], drop["no_price"])
    return out


def _tradable_at(conn, anchor: str, cols: list) -> np.ndarray:
    """锚点日的 PIT 可交易掩码(复用 PITEngine, **窗口化加载**避免全历史读盘)。

    窗口只需覆盖锚点前 60 个交易日(上市满 60 日那层)+ 20 日成交额窗 ⇒ 取 250 个自然日。
    对窗口起点之前就上市的老股, `first_trade = max(窗口首日, list_date)` 仍 ≤ 60 日前的截点,
    与全历史加载**同判**(窗口首日恒早于截点);对新股取真实首现日, 不受影响。
    """
    from app.factors import factor_engine as fe
    from app.pit.engine import PITEngine
    since = (pd.Timestamp(anchor) - pd.Timedelta(days=250)).strftime("%Y%m%d")
    m = fe.load_market(since=since)
    pit = PITEngine(m=m, conn=conn)
    v = pit.tradable_matrix([pd.Timestamp(anchor)], columns=cols).fillna(False)
    return v.to_numpy()[0]
