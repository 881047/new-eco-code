# 我们的模型能否走 LLM 的路？——LLM 进化史 × 市场预测的可行性综述（2026-09-29）

> 性质：战略综述（用户问题："模型能否走一条类似 LLM 的、能准确预测市场的道路"）。
> 方法：先读 LLM 进化全程文献（Transformer→Scaling Laws→RLHF→测试时计算/RLVR→数据墙），
> 再读金融侧 TSFM 基础模型与 LLM 预测市场的正反实证，最后映射到本仓 18 天全部证据链。
> 关联：29 补病因诊断（拥挤短周期 α × 月频车）、赛马 V2（模型是资产）、
> plans/return-stability-deep-analysis-20260929.md（S1 证伪/S2 PBO 0.195）。

## §一 LLM 进化史的成功配方（文献总结）

| 阶段 | 关键论文/事件 | 配方要素 |
|---|---|---|
| 2017 | Vaswani et al., *Attention Is All You Need* | 通用可并行架构（弱先验） |
| 2019-20 | GPT-2/GPT-3, *Language Models are Few-Shot Learners* | 自监督 + 规模 → in-context learning **涌现** |
| 2020 | Kaplan et al., *Scaling Laws for Neural Language Models* | 损失随算力/数据/参数**幂律可预测下降** |
| 2022 | Hoffmann et al., *Chinchilla*（compute-optimal） | 算力-数据配比的可计算最优 |
| 2022 | Ouyang et al., *InstructGPT*（RLHF）+ Wei et al., *Emergent Abilities* | **冻结底座 + 廉价对齐**（后训练） |
| 2024-25 | OpenAI o1 → DeepSeek R1（GRPO/RLVR）、测试时计算（TTS） | 推理时扩展（多算几步更准）；可验证奖励 |
| 2024-25 | 数据墙辩论 → 合成数据时代（~30% 配比、BeyondWeb） | 数据护城河 = 采集→清洗→配比的工程 |

**配方本质五条**：①数据生成过程**平稳且自监督可压缩**（语言有稳定规律、每个 token 都是
密集监督信号）；②数据**边际成本≈零**（网页抓取）；③**规模→可预测的收益**（幂律）；
④底座冻结后**廉价的偏好对齐**（RLHF/RLVR）即可解锁可用性；⑤**评测基建**（基准）
让进步可度量。哲学根基 = Sutton **Bitter Lesson**：通用方法+算力规模长期胜过人类先验。

## §二 逐条映射到市场：五个前提的断裂（反面文献）

1. **平稳性断裂（反身性）**：语言不会因为 GPT 学会了它而重组自己；市场会——预测本身
   移动价格、策略拥挤自我毁灭（CFA Institute "Algorithmic Market Hypothesis"：AI 让市场
   变成"自身预测的脆弱回音室"）。本仓同构证据：2024-02/2026-09 微盘挤兑 = 量化拥挤的
   自我攻击；29 补"拥挤的短周期反转 α 的慢速收割器"。
2. **信号密度断裂**：语言每个 token 都是监督信号（~100% 密度）；市场月度截面 IC≈0.096，
   有效宽度 BR_eff≈38/年——学习信号几乎平的。115 个月度样本 vs 数万亿 token。
3. **数据成本断裂**：LLM 数据免费；金融新数据族要真金白银和工程（本仓实证：459 万条
   公告标题零成本采集后，标题级 valence/PEAD 全部机械判死——**采集免费 ≠ alpha 免费**）。
4. **规模收益断裂**：金融 ML 的真实文献（Gu-Kelly-Xiu 2020, RFS）证明 ML>经典模型、
   深度网络最优、收益来自动量/流动性/波动的**交互项**——但金融没有幂律：本仓 13 轮
   "改模型"（含更大容量的 E2E-DL）全部 REJECT，赛马证明 LightGBM 在同特征下已接近
   该数据族的产出上限（ridge 只有它一半，rankens 为负）。**容量不是约束，数据/α 丰度才是**。
5. **"准确预测"目标本身的断裂**：Grossman-Stiglitz (1980)：均衡下不存在免费信息优势——
   人人可得的智能（含 LLM）会被套利到零超额；Lo Adaptive Markets：α 是生态瞬时均衡。
   LLM 预测市场的实证也如此：Lopez-Lira & Tang（JFE 即刊）ChatGPT 新闻情感→次日收益
   可预测，但**开盘后即衰减**、小盘最强，2024-25 后续研究一致显示 alpha 衰减与模型版本
   敏感。TSFM（TimesFM/Chronos/TimeGPT/Kronos）对**波动率/价格水平**有用，
   对**截面收益预测弱**（收益近鞅），正面证据多需微调，还有数据泄漏坑。
   ⇒ **"像 LLM 一样准确预测市场"在均衡意义上不是一个稳定存在的目标**；
   能稳定存在的只有"比对手便宜地维持边际信息优势"。

## §三 但 LLM 的方法论骨架真的可迁移——而且本仓已经自发长出了同构

| LLM 要素 | 本仓同构物 | 状态 |
|---|---|---|
| 预训练底座（大而通用，不常动） | LightGBM 4 种子等权集成（赛马 V2 证明是资产） | ✅ 已是 |
| 后训练/对齐（冻结底座+廉价偏好叠加） | chase6 闸、factor_exclusions 剔 5、vol 缩放观察——**全部是"底座不动、叠加层 A/B"** | ✅ 自发同构 |
| 评测基建（基准让进步可度量） | 三层门禁 S1-S5 + pin 台账 + PBO 0.195 + 组合层档 | ✅ 同构且更严 |
| 数据护城河（语料工程） | 公告语料 459 万条 + rubric v2 + 正文级 PDF 链路 GO | 🟡 唯一在途 |
| 测试时计算（多算更准） | 4 种子分位秩均值（推理端方差缩减） | 🟡 **16/32 种子扩容未测**——全仓唯一未触碰的 LLM 式便宜杠杆（E2a 显示 top6 对扰动稳健 ⇒ 预期边际小，但可 30 分钟 A/B 证伪） |
| 合成数据/数据增强 | 无对应（金融"合成数据"= 模拟器/反事实，本仓无） | ⚪ 不做（数据泄漏与过拟合风险高） |

**LLM 史给量化最深的两条教训恰好是我们已经用血换来的**：
①"数据质量>架构"（Bitter Lesson 的真实含义是**喂对数据**，不是堆模型——本仓 13 轮
改模型全否 + 赛马 V2 是同一结论的私有版本）；②**冻结底座+便宜对齐**是迭代效率最高的
形态（我们采纳过的每一个改动——chase/excl——都是这个形态；改底座的 13 轮全死）。

## §四 结论与路线

**字面上的"LLM 路"（堆算力+自监督+规模→涌现→准确预测市场）走不通**：五个前提在市场
全部断裂，且外部实证（TSFM 弱、LLM 情感 alpha 衰减、均衡套利逻辑）一致支持。
**但 LLM 的方法论骨架不仅可迁移，本仓已自发同构**——说明这条骨架是"AI 迭代效率"的
通用解，不专属于语言。

务实路线（按成本递增）：
1. **T1 测试时计算扩容**：4→16/32 种子推理集成 A/B（~30 分钟，判据=分数方差↓且
   Δsys_ann ∈ ±1pp 内即采纳——它是方差缩减不是 alpha 主张，判据应比改模型宽松一档）；
2. **T2 数据护城河**：公告语料正文级深加工（须新预注册+拍板）——这是我们的
   "语料工程"，也是 29 补诊断的治本路；
3. **T3 评测基建复利**：纸面盘就是我们的"基准演化"——让 paper track record 继续
   积累，它是唯一不被回测过拟合污染的评测。

一句话：**不要找市场的 GPT 时刻（不存在）；要把系统变成"金融版 LLM 工程学"——
它已经是了，剩下的差距全部在数据侧，不在模型侧。**

## 检索来源
LLM 主线：[Toloka LLM Timeline](https://toloka.ai/blog/history-of-llms) ·
[arXiv 2402.06853 LLM 发展综述](https://arxiv.org/html/2402.06853v1) ·
[Cameron Wolfe Scaling Laws](https://cameronrwolfe.substack.com/p/llm-scaling-laws)；
推理时代：[Karpathy 2025 Year in Review](https://x.com) · GRPO/RLVR 综述；
市场反面：[CFA: Algorithmic Market Hypothesis](https://rpc.cfainstitute.org) ·
[Grossman-Stiglitz (AER 1980)](https://www.aeaweb.org) ·
[Lopez-Lira & Tang arXiv:2304.07619](https://arxiv.org/abs/2304.07619)；
TSFM：Rahimikia 2025 (Manchester) "Re(Visiting) Time Series Foundation Models in Finance" ·
[FinCast arXiv 2508.19609](https://arxiv.org/html/2508.19609v1)；
金融 ML：[Gu-Kelly-Xiu (RFS 2020)](https://academic.oup.com/rfs/article/33/5/2223/5758276)；
哲学：[Bitter Lesson 原文](https://www.cs.utexas.edu/~eunsol/courses/data/bitter_lesson.pdf) ·
[Wikipedia](https://en.wikipedia.org/wiki/Bitter_lesson)；数据墙：
[SynthLLM (Microsoft)](https://www.microsoft.com/en-us/research/articles/synthllm-breaking-the-ai-data-wall-with-scalable-synthetic-data) ·
[合成数据 Scaling Laws arXiv:2503.19551](https://arxiv.org/html/2503.19551v3)。
