# LLM 找因子 · 第二轮全球检索 + 超额收益破局方案（2026-09-27）

> 性质：外部检索综述（09-26 版 `llm-mining-survey-20260926.md` 的续篇；同款免责——
> **社区/论文数字均为外部主张，未经本仓测量**，落地项一律走三层门禁 + 预注册）。
> 触发：用户指令"全网检索 LLM 找因子方法，寻求新的超额收益破局之法"。
> 本仓背景：r3 收口（静态失败率 85%→30% 达标、0/12 PASS）；09-27 又实证
> **打分层过闸 ≠ 组合层可用**（nf 构型第 6 例）并落地组合层档门禁。

## 一、09-26 综述之后的新工作（增量部分）

| 工作 | 机制要点 | 对本仓的含义 |
|---|---|---|
| **FactorMiner**（arXiv 2602.14670，ICLR 2026，清华） | 自演化 agent：**模块化技能库 + 结构化经验记忆**，每轮挖矿沉淀"什么路子在什么域死掉"；明确针对 **Correlation Red Sea**（挖出因子高冗余的红海） | **直接补 r3 的持久层缺口**：r3 只有单次运行内的错误摘要反馈，跨轮经验全靠人。经验记忆 = JSON 化的失败族谱进 prompt |
| **FactorEngine**（arXiv，program-level） | 知识注入的程序级因子引擎；显式讨论**换手口径的因子逻辑精炼** | 换手应在**生成端**就作为口径（信号自相关/lag-1 重合代理），不是等组合层才发现 |
| **QuantaAlpha**（arXiv 2602.07085 v2） | 演化框架细节落地：**轨迹级变异与交叉**（不只变异单条因子，变异"挖矿过程轨迹"） | GP(NSGA-II) 与 LLM 循环的中间形态参考 |
| **《The Alpha Factory Illusion》**（LLMQuant Substack 批评文，抓取超时仅摘要级） | 结构性拆解：多数 LLM 因子 agent "看起来在学"——**毛收益好看、计入换手与交易成本后蒸发** | **与本仓 09-27 组合层教训完全同向**（第 6 例的外部印证）：适应度函数不含成本/换手 = 全行业通病 |
| **《Expected Returns and Large Language Models》**（SSRN） | LLM 嵌入从**新闻文本**提取的信息**被价格缓慢而非即时吸收** ⇒ 存在可挖的 alpha 通道 | **"异构口粮"结论的外部佐证**：LLM 的正确矿脉是文本/另类数据族，不是量价 DSL 重涂 |
| **Cross-Stock Predictability via LLM-Augmented Semantic Networks**（arXiv 2026-04） | LLM 推理增强文本衍生金融网络的**跨股可预测性** | 事件/关系网络的下一步：本仓 KG(龙虎榜/股东) 已有网络底座 |
| **TianShu Quant**（SSRN，A 股） | LLM 驱动多因子策略分析 + **市场异常检测**，A 股落地 | LLM 作"复盘/异常检测"角色的 A 股先例（= 本仓 09-26 已结论的可行角色之一） |
| **《Can LLM-based Investing Strategies Outperform?》**（arXiv 2505.07078） | LLM 策略**牛市过度保守、熊市过度激进**——体制依赖弱点 | LLM 不该直接出信号；它的位置在体制门控的**旁路**（读体制、不交易） |
| A 股另类数据实操（Algolab HK 等，摘要级） | 支付/卫星类数据比财报**早 15–20 天**反映基本面，小票尤甚 | 新数据族方向的业界佐证；可及性（合规/价格）是本仓的现实约束 |

## 二、三层失败模式 × 业界解法（本仓现状映射）

| 失败层 | 本仓现状 | 业界解法 | 差距评估 |
|---|---|---|---|
| ① 生成层：DSL 静态非法 | r3 已解（85%→30%，Hubble 机制落地） | — | **已对齐** |
| ② 门禁层：0/12 PASS（弱信号） | 六判据 bar=集成强度，弱信号成批死 | FactorMiner 经验记忆（别再挖死过的方向）；S4 缺口=候选彼此不去重（diag_nf50：50 因子仅 25 有效维） | **可低成本补**：家族去重 + 经验记忆进 r4（若重开） |
| ③ 组合层：稀释/成本蒸发 | 09-27 实证 + 组合层档门禁已建 | Illusion 批评 + SSRN 成本感知适应度 + FactorEngine 换手精炼 | **业界同病**；我们的组合层档反而是领先项 |
| 数据层：量价 DSL 红海 | 异构口粮结论（09-26） | 文本嵌入因子（SSRN 渐进定价）、语义网络、另类数据 | **最大机会差**：本仓尚无文本语料数据族 |

## 三、破局方案（按 期望值/成本 排序）

**A. 异构口粮：文本数据族入库（LLM 找因子的正路，首推）**
新闻标题/公告摘要语料（akshare/tushare 可及源）→ LLM 离线打标/嵌入 → 因子族
（情绪/确定性/新颖度/事件类别）→ 走**三层门禁**（含新组合层档）。依据：SSRN 渐进
定价证据 + 本仓"新数据族入库才再挖"既定结论 + Algolab 小票另类数据优势。
成本：一个新数据管线（采集→存储→月频对齐），与 min60/CB 管线同量级。

**B. LLM 复盘/异常检测角色（零风险，立即可做）**
每日/周 agent 读 run 留痕与账本，输出异常报告（TianShu 同款；09-26 已列可行角色）。
不碰信号层，纯读——先把"LLM 在体制门控旁路"的位置站住（arXiv 2505.07078 的教训：
LLM 直接出信号有体制依赖弱点）。

**C. r4 挖矿循环（仅当 A 的数据族入库后才值得）**
FactorMiner 经验记忆（跨轮失败族谱 JSON）+ **家族内去重预筛**（S4 缺口，diag_nf50
实证 50 名额只有 25 有效维）+ **换手代理进适应度**（lag-1 信号自相关，W2 有现成机
制）+ 组合层档终判。量价 DSL 本身**不再重开**。

**D. 非 LLM 破局（诚实清单，超额收益的另一半答案）**
① 资金规模（W2-F50 实测：10 万下 k=5 成本 26.79→9.05bp，唯一的结构性杠杆）；
② CB 双低轨道（W2-CB 已纸面，+5.7%/年 夏普 0.71）；③ 组合层档防稀释（已建）。
**破局的真话：在 2 万本金约束下，执行结构与新数据族 > 更多因子。**

## 四、检索源

- FactorMiner: [arxiv.org/abs/2602.14670](https://arxiv.org/abs/2602.14670) · [ICLR 2026](https://iclr.cc/virtual/2026/10021261)
- QuantaAlpha: [arxiv.org/html/2602.07085v2](https://arxiv.org/html/2602.07085v2)
- Cognitive Alpha Mining: [arxiv.org/html/2511.18850v2](https://arxiv.org/html/2511.18850v2)
- LLM-MCTS: [arxiv.org/html/2505.11122v2](https://arxiv.org/html/2505.11122v2)
- Chain-of-Alpha: [alphaxiv.org/abs/2508.06312](https://www.alphaxiv.org/abs/2508.06312)
- AlphaAgent: [arxiv.org/abs/2502.16789](https://arxiv.org/abs/2502.16789)
- LLM 策略体制弱点: [arxiv.org/html/2505.07078v6](https://arxiv.org/html/2505.07078v6)
- TianShu Quant (A 股 LLM 异常检测): [SSRN 6854633](https://papers.ssrn.com/sol3/Delivery.cfm/5854a31a-608f-4938-a71f-0fedfd8769f7-MECA.pdf?abstractid=6854633&mirid=1)
- 成本感知适应度: [SSRN 7498983](https://papers.ssrn.com/sol3/Delivery.cfm/c71ebc80-326b-4fd7-a571-0b0956551ddb-MECA.pdf?abstractid=7498983&mirid=1)
- The Alpha Factory Illusion: [llmquant.substack.com](https://llmquant.substack.com/)（抓取超时，摘要级）
- 文本嵌入因子/语义网络: SSRN《Expected Returns and Large Language Models》·
  arXiv《Cross-Stock Predictability via LLM-Augmented Semantic Networks》(2026-04)（均摘要级）
- 检索时间：2026-09-27 00:30–01:00；摘要级信息未逐篇核读，**外部主张未经本仓测量**。
