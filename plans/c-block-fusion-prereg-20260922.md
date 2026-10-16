# C 案预注册：分组子模型 + 等权秩融合 vs 31 联合基线（2026-09-22）

> 性质：实验预注册。**判据先于任何跑数冻结**；跑完不得修改。
> 起因：用户拍板"先做 C，C 被证伪再做 D"（因子重组分析的四案裁决，见会话
> `plans/project-review-and-next-steps-20260922.md` 前的分析；产物 `experiments/factor_corr_36m.csv`）。
> 纪律：不改 `app/**`、不碰生产 `model_score`、不换 pin、不写 `factor` 表；
> 所有分数落隔离表 `model_score_cblock_*`；实验臂与生产种子 [42,7,123,2024] 零重叠。

## 一、回答什么问题

把 31 个生产因子按相关块分成 6 组、**每组独立训练一个生产同款 LightGBM**、
组间做**等权秩融合**（分数层），相对**31 因子联合训练**的基线，研究层超额是否
有可辨提升？（C 案 = 因子重组分析中唯一未被在册判决覆盖的分支；先验 =
effect_indistinguishable，理由：F22「加 die 稀释」+ F56 PBO=0.434。）

## 二、冻结设计

### 2.1 分块映射（31 = 6 块全覆盖，逐名核对）

| 块 | 成员 | n |
|---|---|---|
| B1 流动性/规模 | size, amount_log, illiq | 3 |
| B2 波动/彩票/β | vol_20, vol_60, ret_max20, turnover_20, skew_20, beta_250, turnover_chg | 7 |
| B3 动量/趋势 | mom_20, mom_60, mom_120, mom_250, high_52w, low_52w, bias_60 | 7 |
| B4 短窗反转 | rev_5, bias_20 | 2 |
| B5 估值/质量/杠杆 | ep, bp, dv_ttm, roe, netprofit_yoy, or_yoy, grossprofit_margin, eps_yoy, debt_to_assets | 9 |
| B6 龙虎榜 | lhb_net20, lhb_org_net20, lhb_cnt20 | 3 |

归属原则：按 `experiments/factor_corr_36m.csv`（36 个月末截面 Spearman 均值）
的最近邻块归属；beta_250 / turnover_chg 归 B2（与波动块 ρ 最高），debt_to_assets 归 B5。

### 2.2 臂与种子（配对设计）

| 臂 | 内容 | 种子集（K=4） |
|---|---|---|
| BASE_s1 / BASE_s2 | 31 联合训练（= 生产路径，`factors_override=None`） | S1 / S2 |
| C_s1 / C_s2 | 6 块各训一个模型（同一 S1/S2）+ 等权秩融合 | S1 / S2 |

- S1/S2 用确定性 seed 池（`base=1_000_003, step=7919`）的第 100~103 / 200~203 切片，
  两组零重叠、与生产零重叠；同对内 BASE 与 C 共用同一 seed 集（配对）。

### 2.3 训练配置（冻结，逐项同生产）

`trainer.run(full=True, train_months=36, holdout_recent=6, backtest_start="2017-01-01",
num_boost_round=trainer.ENSEMBLE_ROUNDS, early_stopping_rounds=0, ensemble_seeds=S*,
sample_weight_mode=None)`；块模型经 `factors_override={因子名: (月末×股票) 面板}`
（面板从生产 `factor` 表透视，值逐位同源）注入。

### 2.4 融合口径（冻结）

每块模型分数 → 逐锚点**截面百分位秩**（pandas rank pct）→ 6 块**等权算术平均**
→ 作为 C 臂分数面板；不搜索块权重、不做优先级加权（那是已否的 B 案）。

### 2.5 读出口径（冻结，F56/F76b/F78 逐字同源）

研究层 top100 等权**净超额**序列：生产 PIT 掩码、`HORIZON=21`、
`sell_rate = fees.effective_rate(25000, "sell", "20260914")`；窗口 2017-01~2026-08（115 期）。
另报 IC/IC_IR（分数 vs forward_excess，生产掩码）。

## 三、判据（跑数前冻结）

**G0 门（sanity，不过则本轮读数全部作废重跑）：**
- G0-1：BASE 两臂研究层年化落在 **[5.5, 9.5]pp** 带内（F78 研究层臂分布 6.77~8.96 的宽容带）；
- G0-2：C 的 12 个块模型全部产出 ≥95% 期望锚点的分数；融合面板逐锚点可交易 ≥200 名的锚点数不劣于 BASE。

**主判据（L1 门，两套 seed 全部满足才算通过）：**
- ① 配对 Δ = ann(C) − ann(BASE)（同 seed 对）**≥ +1.6pp**（研究层 T=115，SE 0.984pp 门，与 F78b L1 同）；
- ② 配对逐期差的 t ≥ 1.6（SE 只含时间抽样；引用时注明可能虚高 ≤1.20×）。
- 通过 ⇒ 采纳送组合层（另立预注册，双轴判据）；任一不满足 ⇒ **effect_indistinguishable / 不采纳**。

**副读数（不参与判决）：** IC/IC_IR 对比、ACTIVE 段（2024 起）Δ、逐期差 σ、块模型间分数秩相关。

## 四、留痕与后续

- 产物：`experiments/05_label/C_block_fusion_result.json` + `C_block_fusion_series.csv`；
  隔离表 `model_score_cbase_{s1,s2}`、`model_score_cblk_{b1..b6}_{s1,s2}`。
- 成本预估：14 次训练 × ~6.6min（K=4）≈ **92min** + 读出 ~5min。
- **无论判决如何**：结果登记 05 号台账；随后落地 D 案（分组监控/归因层，
  零口径变更）——这是用户在 C 之前已拍板的顺序（"C 被证伪再做 D"）。
