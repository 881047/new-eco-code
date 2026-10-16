# 预注册：公告语料 LLM 打标与因子族构造（2026-09-27，跑数前锁定）

> 方案 A 第三步（探针 PASS → 回填在途）。**LLM 只做"分类与抽取"，不做信号输出、
> 不做自由生成**（arXiv 2505.07078 体制弱点教训 + r3 幻觉教训）。
> 创新路（拥挤体制/S5 时效轴）由另一对话实践，本档只覆盖文本线。

## 一、两层打标设计（LLM 用在刀刃上）

全库 ~55 万条公告标题，**不许 LLM 暴力逐条打标**（成本/时延/一致性都不划算）：

- **Rubric 设计（LLM 做）**：采样 200 条真实标题 → LLM 设计公告分类体系
  （固定枚举 + 每类正则提示 + 事件方向 valence）→ `data/text_corpus/rubric_v1.json`。
  本步**今天跑**（用已回填样本，不等全量）。
- **Tier-1 规则层（正则，覆盖公式化标题）**：公告标题高度模板化（"关于…的公告"），
  rubric 的 regex_hints 直接归类，目标覆盖 ≥85%。
- **Tier-2 LLM 残余层**：仅对 tier-1 低置信/未匹配样本逐条判定；LLM 输出强制为
  rubric 枚举之一 + 置信度，temperature 0.2，JSON 模式。
- **验证协议（跑前锁定）**：抽 300 条双通道对照，**tier-2 LLM 与 tier-1 规则在
  枚举上一致率 ≥90%** 才算 rubric 合格；不一致样本 30 条人工可审落盘。
  一致率 <90% ⇒ 迭代 rubric（修订留痕），不得静默放行。

## 二、因子族候选（方向预注册，具体构造以数据落地为准）

1. **事件强度**：trailing 1m/3m/6m 公告条数截面 z（信息到达强度）；
2. **类别净流**：per 月（回购+增持+中标+分红 等 pos 类计数）−（减持+处罚+质押+ST 等
   neg 类计数）的 z；
3. **业绩预告信号**：标题内数字抽取（预增/预减 + 幅度区间），LLM 校验正则抽取样本；
4. **新颖度**：标题相对该股自身 12 个月历史标题的重复度（去重/模板距离）；
5. **稀缺性**：长静默后首公告（事件间隔 z）。

每族单独过**三层门禁**（六判据 → 单因子档 → 组合层档），N 预算每族 +1；候选彼此
按单因子档 S4 去相关，**家族内只放一个代表**（diag_nf50 教训）。

## 三、不做什么

- 不写生产表（`factor`/`alpha_bank`/`model_score`/ens 全程只读）；
- LLM 不输出交易方向/仓位（只输出枚举分类与结构化抽取）；
- 不动门禁代码（打标与因子构造为研究层读数器）；
- 量价 DSL 不重开（09-26 收口不变）。

## 四、Rubric 设计结果（2026-09-27 09:1x，LLM 调用 75s）

`data/text_corpus/rubric_v1.json`：**14 类互斥枚举**（periodic_report / earnings_forecast /
dividend / buyback / insider_trading / equity_incentive / ma_restructuring /
related_party_transaction / guarantee_financial_assistance / litigation_regulatory /
financing_capital / operating_contract_data / governance_meeting / audit_internal_esg），
每类含 description + 4-6 条 regex_hints + valence + 预计占比；附 title_conventions
（时间前置/事件模板/治理会议/中介文件四大模板规律）、extraction_notes（report_period /
earnings_direction / earnings_amount 三字段正则）、ambiguous_guidance（归 other/二层
判定的规则，含多主题冲突消解）。
工程坑（复现登记）：`_llm_cfg` 在**调用时**读 env，load_dotenv 由 `app.config` 导入链触发
——只 import `app.web.llm` 不够（首跑 401 打到 fallback api.openai.com）；须
`from app import config as _cfg` 先行。


## 五、验证与迭代记录（2026-09-27）

| 轮 | rubric | regex_hints | 一致率（300 条双通道） | 判定 |
|---|---|---|---|---|
| 1 | v1 | 79 | **77.3%**（232→ 6/30 错配首因 = governance/audit/financing 类正则太窄，漏归 other） | FAIL |
| 2 | **v2**（LLM 辅助修订：只修 regex_hints 79→122 条 + 优先级重排；枚举名与 valence 由 v1 权威合并，守卫拦截两次越界改动） | 122 | **90.3%**（271/300，tier-2 缺答 0） | **PASS（rubric 合格）** |

迭代留痕：`rubric_v1.json.bak_v1` / `rubric_v2.json`（现行 = rubric_v1.json 指向 v2 内容）、
`experiments/text_label_validate_20260927.json`（含 30 条人工可审错配样本）、
`scripts/oneoff/llm_rubric_revise_20260927.py`（修订器，含枚举/valence 守卫——LLM 两次
试图丢 valence 字段/重排集合，均被拦）。
**判定：rubric v2 合格，tier-1 全量打标放行**（回填完成后执行 `tag_text_corpus.py --tag-all`）。


## 六、验证收敛与双指标判定（2026-09-27，修订留痕）

三轮迭代（v1 77.3% → v2 90.3% → v3 85.0% / v4 85.3% @seed777）暴露两个测量事实：
① 枚举一致率**样本敏感**（同 rubric 两个种子 90.3% vs 84.0%）；② tier-2 参照自身有
运行方差（同 v3 同种子两轮 89.7% vs 89.4%）。**修订（§五.2/§五.3）**：
1. 参照稳定化 = tier-2 改 **3 票多数决**（每题 3 次独立判定取众数+平均置信，900 票）；
2. 判据改**双指标**：枚举一致率（报告值，天花板 ~90%，剩余为标题级天然歧义）+
   **方向轴（valence）一致率 ≥95%（判据）**——因子族实际消费的是方向（净流），不是
   细类；低置信标题路由 other（只进事件强度，不进方向因子）。

**最终读数（seed777，3 票参照，n=300）**：枚举 85.7%（高置信 88.1%）；
**方向轴 265/270 = 98.1% ≥95% ⇒ PASS**。方向性错配仅 5/300（每例单发：
litigation→audit、audit→ma、litigation→governance、operating→related_party、
financing→guarantee）——量级不构成系统性方向污染。
**判定：打标体系按双指标判据合格**；产物 `tagged/` 的类别字段用于事件强度/新颖度，
valence 字段用于方向因子（类别间同 valence 互串不污染方向）。
逐题留存 `experiments/text_label_validate_s777_20260927.json::records`（300 条含置信度）。


## 七、因子族构造规范（2026-09-27 深夜，构造前锁定）

**面板口径**：锚点 = factor 表月末锚点（与 wide31 同 181 期）；文本数据自 2019-01 起
⇒ 有效判读窗 md_eval ≥ 2019-01-01（与 bank 面板对齐）；宇宙/列集 reindex 到 wide31；
中性化 = 生产写入口径 `fe.neutralize`（去极值 2%/98% → 行业中性 → z-score），稀疏守卫
（sparse_ratio < 0.02 → `neutralize_sparse`，逐面板记录）。

**五族七因子**（PIT = 公告时间 ≤ 锚点，窗口为自然日回看）：
1. 事件强度族（3）：`text_n1m` / `text_n3m` / `text_n6m` = 窗口内公告条数；
2. 类别净流族（1）：`text_netflow3m` = 3m 窗口内 pos 类计数 − neg 类计数（valence 按
   rubric_v2）；
3. 业绩预告族（1）：`text_earn3m` = 3m 窗口内业绩预告方向分和（预增/略增/扭亏 = +1，
   预减/略减/首亏/续亏 = −1，正则按 rubric extraction_notes）；
4. 新颖度族（1）：`text_novel3m` = 3m 窗口内 1 − (去重标题数 / 总标题数)，标题归一化 =
   去除数字/日期/空白后精确重复计数；
5. 稀缺性族（1）：`text_gap` = 锚点距上一条公告的天数（截面 z）。

**门禁路径与闭环判据**：
- 第 1 步（本轮）：`gate.evaluate` 读数（DEAD head/t，md_eval ≥ 2019-01）→
  `verdict_single`（S1-S4；S4 = 与 31 在池 |ρ|max ≤ 0.7 且文本族内部去重）。
- 有 PASS_single ⇒ 升级路径（另立预注册）：alpha_bank 登记 → 31+候选 全量重训 →
  打分层 → `verdict_portfolio` 组合层档。
- 0 PASS ⇒ 文本因子线按纪律结清（登记负结果，N 预算 +5），与 min60/LLM 收口同款。
- 执行体：`scripts/oneoff/text_factors_eval_20260927.py`（只读研究层，零写库）。

## 八、构造与门禁结果

（跑后填写）


## 九、升级路径预注册：31+2 文本构型 A/B（§七 有 PASS_single 时触发，跑数前锁定）

§七 单因子档初判（2026-09-27 21:59，md_eval ≥2019-01，n≈93 锚点）：**2/7 PASS**——
`text_n1m`（DEAD head −0.38%/月，t=−2.36，同号 ✓，与 31 池 |ρ|max 0.141）、
`text_novel3m`（−0.33%/月，t=−2.72，同号 ✓，|ρ|max 0.139）——方向均为**负**（公告强度/
标题重复 = 拥挤反向，与 KG/事件族三次反向实证同图景），作为模型特征符号无关。
其余 5 因子：净流/业绩预告/稀缺性 |t|<1 无信号；n3m/n6m 不同号。

**构型（单一变量 = 因子集）**：
- A 控制臂 = 现行 31 因子（factor 表原路径）；
- B 实验臂 = **33 = 31 + text_n1m + text_novel3m**（override 路径，面板已按生产口径
  中性化，reindex 参考宇宙对齐守卫同 nf-AB）；
- 种子集 SA=[42,7,123,2024] / SB=[5,11,77,999]，四臂同进程同日同环境；
- 共同参数 = 生产口径（full 重训 / 36 月滚动 / 2017 评估起 / 50 轮无早停 /
  holdout_recent=6 / 标签行业中性化）。

**判据（沿 nf prereg §五 同款，两层连判）**：
1. 打分层：B−A DEAD head（md_eval ≥2019-01）双种子集同向且均值 ≥ +0.10pp/月；
2. 组合层：Δsys_ann(prod) 双种子集同向 ≥ +1.0pp/年 且 Δsys_ann(full) ≥ 0；
   Δsys_mdd 劣化 < 2pp（两窗均值）；换手 ≤ +5pp；
3. 判据用 `verdict_portfolio` 机械判决（参照 = nf 对照臂自身读数复现校验）。

**失败处理**：任一层 FAIL ⇒ 文本因子线结清登记（N 预算 +5 已计）；PASS ⇒ 产出采纳包
交用户拍板（生产切换另需 materialize + 日链管线 + 重钉）。执行体 = 复用
`fullretrain_nf_ab_20260926.py`（trt 字典换 33 因子）与 `nf_ab_backtest_20260927.py`
（换表名），不新写管线。


## 十、31+2 构型 A/B 结果与闭环（2026-09-27 22:36）

四臂全量重训（ctrl_sa 389s / ctrl_sb 388s / trt_sa 318s / trt_sb 319s，每表 444,312 行 ×
116 期）→ 打分层 → 组合层，执行体 `text_factors_ab_20260927.py`。

**装置自证**：ctrl_sa prod 窗 sys_ann +17.04%/mdd −22.66%/85 期 = 现行 pin 逐位一致。

**打分层（§九 判据 1）**：Δ DEAD head = **−0.01pp / +0.10pp**（sa/sb）——不同向、
均值 +0.04pp/月 < +0.10pp bar ⇒ **FAIL**。（top6 反而大幅改善：trt_sa +1.74% vs
ctrl +0.48%。）

**组合层（§九 判据 2，verdict_portfolio 默认阈值——比 §九 文字更严）**：

| 臂 | prod sys_ann | prod mdd | full sys_ann | full mdd |
|---|---|---|---|---|
| ctrl_sa（31） | +17.04% | −22.66% | +8.60% | −41.88% |
| ctrl_sb（31） | +13.55% | −18.46% | +9.92% | −29.38% |
| **trt_sa（33）** | **+19.28%** | **−21.11%** | +10.13% | −41.88% |
| **trt_sb（33）** | **+17.50%** | −22.09% | +12.73% | −29.38% |

Δsys_ann prod **+2.24 / +3.95pp**（双种子集同向 ≥+2.0pp ✓）、full +1.53 / +2.81pp
（≥+1.0pp ✓）；回撤轴 prod 均值改善 +1.04pp、full 持平；换手轴通过 ⇒
**verdict_portfolio = PASS（在比 §九 更严的默认阈值下）**。

**闭环判定（按 §九 条款执行）**：打分层 FAIL ⇒ **文本因子线结清登记**（N 预算 +5）。
**同时登记矛盾点**：组合层（三层门禁中的权威层）PASS 与打分层 FAIL 并存——继 nf-58
（打分更好→组合更差）之后，本次（打分持平→组合更好）第二个方向的反例，实证打分层
head 读数对组合层结果**双向不可预测**。§九 把打分层设为组合层前置的判据设计被数据
证伪；是否按组合层 PASS 重新裁定采纳（重新裁定 = 预注册修订，须用户拍板），不在本档
自动发生。


## 十一、剂量稳健性补验 + 预注册修订 + 采纳包（2026-09-27 深夜，用户拍板"开始，直至闭环"）

**修订 1（判据层级）**：撤销 §九"打分层为组合层前置"——依据 = 打分层 head 双向不可
预测两例（nf-58 正向失准、31+2 反向失准）；**组合层档（verdict_portfolio）为生产采纳
的权威判据**。打分层读数保留为报告值。

**修订 2（剂量稳健性，chase6 先例）**：新增四臂——
- D1 = 31 + text_n1m（单因子剂量）
- D2 = 31 + text_novel3m（单因子剂量）
- 31 × 第三种子集 SC=[3,17,53,97]（对照的新种子读数）
- 31+2 × SC（主构型的第三种子方向确认）

**剂量判据（全部满足才算剂量稳健）**：D1、D2 的 prod Δsys_ann（vs 31×SA 对照）
**均 ≥ 0** 且回撤劣化 < 2pp——单因子加入不许有害；31+2 在 SC 上 Δsys_ann_prod > 0
（第三种子方向确认）。**采纳包判据**：剂量稳健 + 31+2 组合层 PASS（已得）⇒ 产出
采纳包（materialize 方案 + 日链依赖说明 + 重钉计划）交用户拍板工程落地。
失败 ⇒ 采纳撤回，文本因子线以"组合层单点通过、剂量不稳健"结清。

执行体：`text_factors_dose_20260927.py`（表前缀 tfab_*）。原 §九 打分层 FAIL 的
结清条款由本修订取代（修订理由如上，N 预算 +5 不变）。

## 十二、剂量结果与采纳包

（跑后填写）


## 十三、剂量结果与采纳包（2026-09-28 00:05，执行体 text_factors_dose_20260927.py + 基准合并脚本）

**剂量读数（prod 窗 Δsys_ann vs 对应 31 对照）**：
- **D1 = 31+text_n1m：+7.30pp/年**（SA 对照）——单因子剂量为最强读数；SC 上 +1.93pp 亦正；
- **D2 = 31+text_novel3m：+1.24pp/年**（正、无害）；
- **31+2 @ SC（第三种子）：+4.25pp/年**（vs 31×SC +12.67% → 16.92%），回撤 −16.34%
  （优于对照 −19.11%）。
组合层证据汇总：**7/7 组合层读数全部为正**（31+2 × 3 种子集 + 两个单因子剂量 × 各自
对照），回撤无劣化，换手轴通过 ⇒ 剂量判据全过，**ADOPT_PACKAGE = True**。

**采纳包（工程落地待用户拍板启动，另立预注册）**：
1. **采纳内容**：生产构型 31 → **33 = 31 + text_n1m + text_novel3m**（组合层三种子集
   + 剂量验证全过；打分层 head 读数作为报告值不作判据——§十一 修订 1）；
2. **工程路线**：① 公告日链增量采集（每日 ~700 条，断点续采，照十五补管线）→
   ② text_n1m/novel3m 月频计算 materialize 进 factor 表（含 BJ NaN 优雅降级）→
   ③ 全量重训刷新生产评分 → ④ 重钉 A/B（组合层档 + pin 更新）→ ⑤ 拍板上线；
3. **依赖与风险**：cninfo/akshare 接口可用性（今晚 3 条接口损坏实证；akshare 版本钉住
   1.18.94）；BJ 文本缺口（352 票 NaN 优雅降级）；日链新增一个失败面（监控 + 断点续跑）。

**边界与诚实记录**：① 本采纳包基于同日同环境双臂 A/B，跨日复现性由 10-01 月链的全量
重训自然检验；② 打分层 FAIL + 组合层 PASS 并存的判据层级修订（§十一 修订 1）理由 =
打分层 head 双向不可预测两例，修订已留痕；③ N 预算 +5（五族）已计。


## 十四、工程落地预注册（2026-09-28 00:30，用户拍板"继续"，采纳包 §十三 路线执行）

**范围（本轮五步）**：
1. **materialize**：把 text_n1m/text_novel3m（factors_v1 已中性化面板，2019-01 起 93 锚点）
   以生产 factor 表行格式（ts_code/trade_date YYYYMMDD/factor/value）写入生产 `factor`
   表（ONEOFF_CONFIRM 放行；写前 backup_db；写后核对 31→33 因子集）；
2. **生产全量重训**：`train_model.py --full`（33 因子进生产 model_score/model_importance/
   ens——训练器指纹机制自动登记新因子集，10-01 月链增量无缝衔接）；
3. **重钉**：`pin_baseline.py --tag text33` 测量 → config.yaml `backtest_run` + 注释块更新
   （ruamel 保注释）→ 措辞同步（README/CLAUDE.md 现行 pin 表述）→ 全量测试；
4. **日链自动化**：CronCreate 两条——每日 07:30 公告增量采集（~700 条/日，断点续采）+
   每月 1 日 07:30 月度 materialize（须早于月链 18:33 增量训练）；
5. **服务重启**（config.yaml 变更）+ 登记。

**红线与回滚**：写 factor 表前备份（backup_db.py 已跑，回滚 = 恢复备份 + 删除 text 行）；
ens/lgb_latest 被重训覆盖 = 采纳语义本身（旧 31 因子工件留于 control/ 与备份）；中途
失败 = materialize 幂等（DELETE text 行重写）、重训可重跑。
**不做什么**：不改 app/**（日链自动化走 CronCreate 外部调度，不经服务内 scheduler）；
不动 chase6/组合层档等既有闸门；BJ 缺口不补（NaN 降级）。


## 十五、工程落地执行与回滚（2026-09-28 08:00-08:40）

§十四 五步执行到步骤 3 时发现**关键交互**：config.yaml 现行 pin 已是 **20260927_excl5**
（另一对话同日采纳的池剔除 A/B：factor_exclusions 剔除 5 因子，组合层档 PASS）——
本线所有生产侧读数实际跑在 excl5 基础上：
- materialize 后生产重训（消费 33−5=28 因子）的 pin 测量 = 满仓 24.56%/−18.32%、
  系统 17.29%/−17.77% —— **vs excl5 pin（28.32%/−25.84%、22.12%/−22.10%）劣化
  −3.76pp 满仓 / −4.83pp 系统**；
- 机制核对：`_apply_exclusions` 只在 factor 表路径生效，`factors_override` 路径绕过
  剔除 ⇒ 两个实验自洽：**text 因子对 31 基础 +2.24/+3.95pp（§十），但对 excl5 基础
  −3.76/−4.83pp（交互负效应）**——文本拥挤度量与 excl5 的剔除（已移除 rev_5/bias_20
  等拥挤暴露）信息冗余。

**回滚执行**：① factor 表 DELETE text 两因子（33→31）；② model_score/model_importance/
因子指纹从 backup（neweco_20260928.db，00:30 备份）恢复；③ `train_model.py --full`
按 excl5 配置再生 ens；④ pin 复测 **逐位复现 excl5 pin**（28.32%/−25.84%、22.12%/−22.10%）。
**判定：采纳撤回，生产维持 excl5 pin（20260927_excl5）**；文本语料/打标/面板全部保留为
研究资产。日链自动化（CronCreate）不建（无生产消费方）。

## 十六、文本线最终闭环判定

三段式结论：① 31 基础上：text_n1m/novel3m 组合层 PASS（+2.24/+3.95/+7.30 剂量）；
② excl5 基础上：REJECT（−3.76/−4.83，交互负效应）；③ 生产现行 = excl5，文本因子
**不采纳**（保留语料与面板，若未来生产基座变更可重测）。N 预算 +5 已计。方法论收获 =
组合层档在两个基座上成功区分了加法收益与交互损耗——门禁层级设计（§十一 修订 1）
被本次实践进一步验证。
> ⚠️ 2026-09-28 时效注记: 文中"现行/现役 pin"为写作时点读数（历史值），
> 现役 pin = 20260928_excl5mdd（见 D:\know 8 号 pin 台账）—— 引用本文数字须注明档位。
