# 因子有效性与时效性 · 全球检索综述 + 创新路（2026-09-27）

> 性质：外部检索综述（同款免责——**外部数字未经本仓测量**；落地项走三层门禁 + 预注册）。
> 触发：用户指令"探索因子有效性和时效性问题，全球检索寻求方法，走出创新路"。
> 本仓实证底座：动量 2005-2019 有效/2020 后死亡（fterm）、KG/事件特征是拥挤**反向**指标
> （金股热度 t=−2.73、lr5 −1.26% t=−5.08）、W2 M1"α 只在首期 lag≥1 转负"、六判据门
> DEAD/ACTIVE 体制分区、单因子档 S1-S4、组合层档、N 预算/Bonferroni 分母。

## 一、全球文献的已知事实

**有效性衰减（decay 是常态不是意外）**
- **McLean & Pontiff (2016, JF)**：97 个异象，样本外收益 −26%、**论文发表后 −58%**——
  统计偏差 + 资本套利学习双因。后续 Chen (2024) 有批评性重检。
- **Gordon et al. (2025) /《Factor Timing with Transaction Costs》(2026-09)**：因子广泛
  采纳 ⇒ 拥挤 ⇒ 平均收益显著下滑（美国市场为主证）。
- **Quoniam (2025) / CFA UK**：alpha decay = 成功信号被拥挤侵蚀；"**crowding often
  precedes alpha decay**"——拥挤是衰减的**前导变量**。
- **《Financial Epiplexity》(arXiv 2026)**：理论模型——衰减 = 私有信息向价格的迁移，
  拥挤 = 因子的可压缩性。
- **容量面**：《Taxonomy of Anomalies and Their Trading Costs》——异象容量与交易成本
  定量（衰减速度与容量负相关）。

**复验之争（有效性≠普遍失效）**
- **Jensen-Kelly-Pedersen (2023, JF)**：贝叶斯因子复验框架——**80%+ 美股因子内部有效**、
  跨定义/跨市场复现（jkpfactors.com 全球因子库）；对手方 Hou-Xue-Zhang《Replicating
  Anomalies》认为多数异象是构造伪影。 ⇒ "衰减存在但真因子大体真实"，问题在**条件性**
  （何时失效）而非"是否存在"。

**时效性测量方法（标准化工具箱）**
- **IC 衰减曲线 → 半衰期**：对同一信号算 lag-1..k 期 IC（或分段持有收益），拟合指数
  衰减，半衰期 = IC 跌至峰值 50% 的持有期；据此匹配换手与调仓频率（ practitioners
  标准流程，多份方法论来源 + 开源实现 signal-hunter）。
- **多期优化**：Sivaramakrishnan (2015) 把 IC 衰减直接进多期组合优化的目标函数。
- **拥挤度量工具箱**：估值价差（factor value spread vs 历史）、成分股两两相关、
  空头利息/券源占用（QES 框架）、资金流；MSCI crowding scores；**SSRN 2025
  《Dynamics of Factor Crowding》(5023380)**。
- **A 股实证（2025-2026）**：公募年报直言"**量价因子逐渐失效**、资金涌入致拥挤度增加"
  （南方中证 500 增强年报）；浙商科创年报要求"因子拥挤度监控与风险敞口管理"适应性
  调整；券商金工拥挤度专题 = 换手率偏离/估值价差/多空分歧系。

## 二、本仓独有的实证资产（创新路的原料）

1. **行为/关注度型拥挤反向指标**：KG（龙虎榜经纪商）/金股/事件热度特征在本仓三次
   独立实证为**拥挤的反向定价信号**（t=−2.73 / −5.08 / 反号臂全塌）。全球拥挤度工具箱
   全是**价格/持仓衍生**（估值价差、两两相关、空头利息）——**关注度衍生的拥挤度量**
   在检索范围内无同类（券商研报拥挤度也还是换手偏离系）。这是我们最独特的原料。
2. **体制门控骨架已内建**：`gate.zone_of`（DEAD/ACTIVE）已进六判据门——当前体制轴是
   价格状态，拥挤是天然的**第二体制轴**。
3. **三层门禁已闭合**：单因子档（S1-S4）→ 打分层 → 组合层档；任何新判据有落点。
4. **"半衰期 < 执行延迟"的内部铁证**：W2 M1"配对差 t=+2.73 但换手 0.89/期"与
   "α 只在首期 lag≥1 转负"= 半衰期短于调仓延迟的极端案例——**当时若有时效性判据
   （S5）可在门禁层拦住，不必烧 70 分钟机时 + 组合层回测**。
5. **公告语料在途**：文本因子的信息半衰期（渐进定价）天然较长，与量价短命信号正交。

## 三、创新路（按 独特性/成本 排序，全部预注册后落地）

**创新 1（首推，最独特）：拥挤度反向指标"转正"为体制状态变量**
把 KG/金股/事件热度从"alpha 候选（已证伪）"转正为**关注度型拥挤度量器**：构造
`crowding_score`（多热度成分等权 z，PIT），与 zone_of 的价格体制正交成**二维体制**
（价格 × 拥挤），六判据门/单因子档读数按四象限分别估计；当拥挤度抬升时门禁 bar
自适应收紧。文献差位：全球拥挤度量都是价格/持仓系，行为热度系在 A 股无同类（券商
研报亦然）；本仓已有三次显著反向实证作先验。落地 = 一个 crowding 探针（读数器）
+ zone_of 扩展预注册，零生产风险（只加研究层读数）。

**创新 2（便宜且机械）：单因子档加 S5 时效轴（IC 半衰期）**
S5 = 对候选算 lag-1..6 期 IC 衰减曲线 → 指数拟合半衰期 → 判据
**半衰期 ≥ 2× 调仓周期（月频 = ≥2 个月）**。直接封杀"存在性显著但衰减快于执行"
的假阳性（M1 类）。实现 = gate.py 加纯函数 + decay 读数进 single_factor_retest v3，
成本半天。**注意与 chase6 教训兼容**：反向显著信号的正确用法仍是排除/风控——
S5 只挡"当 alpha 用"的短命信号。

**创新 3：衰减感知持有期分层**（Sivaramakrishnan 2015 思路）
因子按半衰期分层 → 长半衰期因子配月频臂、短半衰期因子只进周/日频臂或排除器；
与本仓 chase 排除器哲学一致。依赖创新 2 的半衰期读数。

**创新 4：JKE 式贝叶斯复验 bank 231**
把 N 预算/Bonferroni（频率派）升级为 shrinkage 先验-后验框架（先验 = 单因子档读数，
数据 = 滚动 OOS），对 bank 做"有效概率"排序。成本高（方法论重活），放远期。

## 四、检索源

- McLean & Pontiff (2016)：[doi 10.1111/jofi.12365](https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.12365)（综述级）
- Factor Timing with Transaction Costs (2026-09)：[sciencedirect.com](https://www.sciencedirect.com)（摘要级）
- Financial Epiplexity (arXiv 2026)：检索摘要级
- Jensen-Kelly-Pedersen：[SSRN 3774514](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3774514) · [NBER w28432](https://www.nber.org/system/files/working_papers/w28432/w28432.pdf) · [jkpfactors.com](https://jkpfactors.com)
- IC 半衰期方法论：[Quant Memo](https://www.quantmemo.com) · [signal-hunter (GitHub)](https://github.com/AnishaRoy5555/signal-hunter) · [Multi-period optimization with alpha decay](https://optimization-online.org)（均摘要级）
- 拥挤度量：SSRN《Dynamics of Factor Crowding》(5023380) · MSCI crowding scores · QES 框架（摘要级）
- A 股实证：南方中证 500 增强年报（量价因子失效）· 浙商科创 2026 年报（拥挤度监控）· 凤凰网《跌停潮之后》（摘要级）
- 检索时间 2026-09-27 08:10-08:25；**外部主张未经本仓测量**。
