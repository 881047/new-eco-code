# 检索综述：模型层训练目标/损失/加权改进（2026-09-29）

检索执行：general-purpose agent（39 次检索，~975k tokens），英文+中文 2023-2026 优先。
动因：用户目标"模型层面优化改进"；本仓两条结构性约束——①同配置跨 seed 全窗年化差
5.9pp（F76b 实测，重训类候选 0.x pp 级效应几乎不可判定）；②"全截面 IC 与 top-6 收益
反相关"4 次独立复现（训练窗加长 IC 升收益崩等）。

## 判定汇总

| 候选 | 判定 | 依据 |
|---|---|---|
| 端到端组合层训练（可微 Sharpe/top-k，Zhang-Cucuringu-Zohren 2021 Sharpe 2.60 vs 0.86；Butler-Kwon QF 2023；Fernandes-Desell 2026 开源） | **远期** | 全部要求 optimizer-in-loop ⇒ 必须换 NN，撞"DL 11 轮判否+tabular 数据量"；无 A 股月频 top-k 长多可复制实证；SPO 病理（arXiv:2605.01176：决策聚焦训练系统性 prediction inflation + 放大换手——与发现②同构的外部印证） |
| **LSW2 式多头端样本加权**（广发《机器学习选股训练手册》2025-06） | **GO（本轮 B1）** | 沪深300 池多头年化 18.91%→19.46%（+0.55pp）、超额最大回撤 −6.51%→−4.66%；华泰 AlphaNet 系列（人工智能 34）独立同方向。实现=LightGBM weight 一行；与 uniqueness（时间重叠加权，F76b indistinguishable）机制不同源（按截面位置） |
| **LightGBM 自定义 pairwise margin/hinge 损失**（CIKM 2025, arXiv:2510.14156） | **GO（B2 条件触发）** | Transformer/S&P500 110 股 top-5：**所有损失 IC 挤 0.073-0.077 窄带，但组合年化 MSE 14.78% vs Margin 16.23%（+1.4pp）/Sharpe 0.664→0.753**——发现②的外部第三次独立同向（"strong portfolio performance does not always directly correlate with predictive metrics"）。LightGBM 可用自定义 objective 实现（按月 group、组内 pair 采样、top 对加权 margin），不换模型不换标签。效应 ~1-1.5pp < 5.9pp 种子方差 ⇒ 须多种子配对协议 |
| 可微 RankIC / 全截面 listwise（方正 2025-12：RankIC 12.48%/多空 42.97%） | 不适用 | NN 路线 + 优化 IC = 优化脱节方向（发现②的反面教材）；方正之三（2026-06）自曝等权 RankIC 训练致小盘 alpha 偏好——"分域加权"思想可远期迁移 |
| lambdarank/rank_xendcg 2023-2026 新证据 | 未找到 | 本仓"完败"判决（无实证留存）不翻案；审计缺口维持登记不重测 |
| SWA/snapshot/rank-average 跨种子稳定 | 不适用/无证据 | GBDT 无权重空间；rank-average 是实践共识无正式实证（本仓 4 种子集成已在范式内） |
| 确定性配置测量协议（deterministic=true/force_row_wise/固定子采样） | GO（协议） | arXiv:2604.17694 subbagging 稳定性保证——A/B 判定力的结构性缓解，本轮已用"同种子配对"落实同效 |

## 对本仓的两条互证

1. CIKM 2025 与发现②（IC-收益反相关）**独立同向**：外部同样看到"损失改进不改 IC、只改
   top-k 组合"⇒ 本轮判决量必须是组合层读数而非 IC/head（预注册 §判据已按此设计）。
2. 方正之三自曝"等权目标→小盘偏好"——与本仓"模型自己在跑的不全是预测收益"（小盘价差
   t=2.74 唯一显著暴露）同族现象，本仓 top6 的小盘漂移已在 chase/excl5 轮间接管理。

## 结论

模型层在"不换架构（LightGBM 保留）+ 不换标签（回归 21 日收益保留）"的约束内，未测且
有外部效应量证据的只剩**损失/权重面**：B1 多头端加权（成本≈0，A 股唯一数字）为主候选，
B2 pairwise margin（+1.4pp 外部证据）条件触发。判据按组合层档 + 双种子集配对
（F76b 噪声刻度校准，预期真效应 +0.55pp 量级 → 大概率 INDETERMINATE，方向符号仍有效）。
