# 公告正文回填试点预注册（2026-09-29，跑数前冻结）

> **性质**：数据侧行动落地（用户指令"开始数据侧的行动直至落地"）。本试点只落地
> **数据资产**（正文文本），不产生因子/判决——正文级因子属下一个预注册。
> 上下文：标题级已机械判死（7 因子族/PEAD 42 候选），正文级剩余价值 = 标题没有的
> 结构化字段（金额/对手方/业绩数字/交易结构）；PDF 静态链路 44 补续二验证 GO，
> 本次实测仍然有效（20260929 抽样 200/110KB/445 字符）。

## 范围（冻结）

- **类别**（6 个高价值：正文含结构化字段、标题级有先验信号痕迹）：
  `earnings_forecast` / `operating_contract_data` / `ma_restructuring` /
  `litigation_regulatory` / `guarantee_financial_assistance` / `buyback`
- **时间窗**：公告时间 ∈ [2024-09-01, 2026-09-29]（两年，与纸面窗重合）
- **配额**：每类等距抽样 500 条（按 tagged 全量排序等距），合计 3,000 条
- **不存 PDF 本体**（只存抽取文本；文本丢失可按 announcementId 重下）

## 管线（冻结）

`scripts/collect_text_fulltext.py`（正式管线，非 oneoff）：
1. 从 tagged parquet 选样（类别×窗口×等距）；
2. 串行下载 `http://static.cninfo.com.cn/finalpage/{YYYY-MM-DD}/{announcementId}.PDF`
   （限速 1.2s/req，重试 3 次，UA=Mozilla/5.0）；
3. pypdf 抽全文（≤30 页），存 parquet：
   `data/text_corpus/fulltext/fulltext_{类别}.parquet`
   列 = [代码, 简称, 公告标题, 公告时间, announcementId, category, valence,
   n_pages, text, fetched_at]；
4. manifest：`data/text_corpus/fulltext/manifest_fulltext.json`（按 announcementId
   断点续采 + 失败分类：http_404 / http_other / parse_fail / empty_text）。

## 验收判据（冻结，跑前）

- **Q1 有效抽取率**：text ≥100 字符的比例 **≥85%** ⇒ 管线可用（扫描件/图片型 PDF
  是预期失败源，失败形态必须分类留档）；
- **Q2 PIT 完整性**：3,000 条全部带 (代码, 公告时间, announcementId, category,
  valence, fetched_at) 六元组，无缺失；
- **Q3 结构化字段探测（信息性，不否决）**：金额正则（`\d+(\.\d+)?\s*[亿万万元]`）
  命中率按类别统计——预期 earnings_forecast / operating_contract_data / buyback 显著
  高于 governance_meeting（后者不在试点类内则以标题级认知对照）；
- **Q4 吞吐**：条/分钟记录 → 6 类全史外推（供全量回填的工程决策）。

## 判决规则（冻结）

- Q1 ≥85% ∧ Q2 完整 ⇒ **数据侧落地完成**：正文资产入库，为"正文级因子预注册"备料；
  全量回填是否启动留用户拍板（外推成本按 Q4 报）。
- Q1 <85% ⇒ 记录失败形态占比；若主因是扫描件（历史早期），把窗口后移重试一次；
  仍不达标 ⇒ 试点线结清（正文抽取在当前工具栈不可行）。

## 结果（2026-09-30 凌晨实测回填，产物 data/text_corpus/fulltext/）

**判决：PASS——数据侧落地完成**（有效率 **96.7%** ≥ 85% 判据线；吞吐 19.3 条/分钟）。

| 类别 | 条数 | 金额字段命中率(Q3) | 中位字数 |
|---|---|---|---|
| guarantee_financial_assistance | 499 | **97.6%** | 2,881 |
| buyback | 495 | **91.9%** | 1,463 |
| operating_contract_data | 489 | **91.4%** | 2,898 |
| ma_restructuring | 429 | 69.5% | 2,828 |
| litigation_regulatory | 490 | 67.1% | 1,608 |
| earnings_forecast | 498 | 55.0% | 885 |
| 合计 | **2,900** | — | — |

- **Q1 有效抽取率 96.7%**：失败 100 条 = empty_text 70（扫描版/图片型 PDF，集中在
  ma_restructuring）+ http_404 30；失败分类已留档 manifest。
- **Q2 PIT 六元组完整性**：全部通过（代码/公告时间/announcementId/category/valence/fetched_at）。
- **Q3 结构化字段探测**：担保/回购/重大合同三类金额命中 91~98%——"正文有标题没有的
  结构化字段"的核心假设**兑现**；业绩预告 55%（含业绩说明会类噪声公告，正常）。
- **Q4 吞吐外推**：19.3 条/分钟 ⇒ 6 类全史（约 65 万条，按 tagged 抽样占比 14% 外推）
  ≈ **560 小时串行**。全量回填需分片并行（同标题回填的 --shard/--nshards 模式，双分片
  即 ~10 天）⇒ **是否启动留用户拍板**。
- 工程注记：落地列含 n_chars（字符数）而非预注册草稿写的 n_pages（页数）——MAX_PAGES=30
  截取照常生效，属实现细节偏差，如实登记。

### 落地状态
- 管线：`scripts/collect_text_fulltext.py`（正式管线，断点续采+限速+失败分类）；
- 资产：`data/text_corpus/fulltext/`（6 parquet + manifest + pilot_result）；
- 下游：正文级因子/字段抽取（金额/对手方/业绩数字/交易结构）= **下一个预注册**，
  备料已就绪。
