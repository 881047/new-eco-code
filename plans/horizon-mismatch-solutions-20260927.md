# 期限错配的解决方案 · 全球检索（2026-09-27）

> 性质：病因（`neweco-root-cause-diagnosis-20260927.md`）的**解决方案检索**——对症下药。
> 外部数字未经本仓测量；落地项走三层门禁 + 预注册。

## 一、对症框架：三条病因属性 → 三个文献解法

### 1. 半衰期错配 → **期限匹配的多层收割架构**（Gârleanu-Pedersen 2013 JF 闭式解）
[论文](https://w4.stern.nyu.edu/facdir/lpederse/papers/DynamicTrading.pdf)（cited 850+）：
交易成本下、信号有不同均值回归速度时，最优策略 = **朝目标组合的部分调整**，调整速度
由成本强度与信号衰减速度之比决定——**衰减越快的信号越该激进交易**。
**对本仓的推论（关键）**：单边 30bp 成本 vs 22pp/年超额 ⇒ 成本/衰减比极低 ⇒ 月频 0.89
换手本来就在最优区——**平滑 REJECT 是理论必然**（把衰减快的信号慢速交易 = 最优解的反面），
**换手从来不是病**。正确的解 = **异步多层收割**（Sivaramakrishnan 多期优化 +
[异步信号处理](https://quant.stackexchange.com/questions/2029/) 传统）：每个 α 流按自己的
半衰期 trading，快慢不搅在同一个分数里。
- 快层 = 周频袖套（top-100，5 日 α，**已运行**）；
- **慢层 = 缺的那块**（见 2）；
- 各层独立 decay-adjusted 权重，多层间不做分数级搅拌（= 平滑失败的理论解释）。

### 2. 拥挤的短周期池 → **换 α 池：公告漂移（PEAD）锚定**
- **PEAD 是最稳健异象之一**：Fink 2021 综述
  （[ScienceDirect](https://www.sciencedirect.com)，美国漂移 60-90 日）；**A 股漂移
  30-60 个交易日**（多篇中文实证 + [量化实践](https://blog.csdn.net) 一致）——
  **半衰期 1-3 个月 = 与月频持有期原生匹配**。
- 机制 = 信息处理摩擦（Bond et al 2023：业绩季越忙漂移越强），**非拥挤套利系**——
  与价格反转/流动性 α（本仓现池）低相关、不共享同一批拥挤资金。
- **LLM+公告分类是中国 2025-26 的活跃前沿**（[国联民生"基本面先声"](https://m.sohu.com)
  2026-09；腾讯云业绩预告 LLM pipeline（利好/利空/中性 → T+0/1/5 事件窗）；华泰 FinBERT）
  ——**本仓的公告语料回填（55 万条标题，在途）+ LLM 打标（14 类枚举 + valence，预注册
  在途）正踩在这个前沿上，且语料规模/时间跨度大于研报示例**。
- 落地：打标产物 → **PEAD 式因子族**（事件方向 × 漂移窗 21/42/63td 三档对齐月频）
  → 三层门禁（六判据 → S1-S5 → 组合层档）。

### 3. 拥挤挤兑尾部 → **波动管理 + 拥挤度约束**（25 补已立案，维持）
vol 缩放（26 补 INDETERMINATE 延长观察，季度复跑）；block_ic → 小盘/反转块暴露上限
（待预注册）。

## 二、处方细化（可执行清单，按序）

1. **公告语料 → PEAD 因子族**（回填完成后立即启动）：打标 14 类 + valence 映射为
   surprise 方向 → 漂移窗 21/42/63td 三档 → 每个事件类 × 漂移窗一个候选 → 三层门禁。
   **这是治本**：把 α 池从"价格反转/流动性"搬向"公告漂移/信息处理摩擦"。
2. **袖套 = 快层维持现状**（31 因子，governor+block_ic 治理）；不做分数级快慢搅拌
   （平滑 REJECT 已证）。
3. **月频慢层成熟前的过渡**：月频维持 26 因子 + vol 缩放观察；不为"加慢因子"而引入
   未过门禁的候选。
4. **拥挤度约束预注册**（独立于 1-3，可先做）。
5. **预期**：快层 13.4% + PEAD 慢层（若 IC 0.03-0.05、宽分散、低相关）→ 现实组合目标
   全史夏普 0.8-1.0；**1.5 仍需本金/工具维度**（诊断 29 补处方 2）。

## 三、检索源（2026-09-27）

Gârleanu & Pedersen 2013 JF（[NYU Stern](https://w4.stern.nyu.edu/facdir/lpederse/papers/DynamicTrading.pdf) /
[NBER w15205](https://www.nber.org/papers/w15205)，cited 850+）· Sivaramakrishnan et al
（[多期优化与 α 衰减](https://optimization-online.org/wp-content/uploads/2015/02/4785.pdf)）·
Fink 2021 PEAD 综述（[ScienceDirect](https://www.sciencedirect.com)，cited 130+）·
Bond et al 2023（业绩季拥挤与漂移）· A 股 PEAD 30-60 日（多篇中文实证）·
[国联民生"基本面先声"](https://m.sohu.com) 2026-09 · 腾讯云 AI+量化实战（业绩预告 LLM
pipeline）· 华泰人工智能系列 63（FinBERT）· [异步信号处理传统](https://quant.stackexchange.com/questions/2029/)。
