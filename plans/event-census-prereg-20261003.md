# 事件族普查预注册（2026-10-03，跑数前冻结）

> 来源：用户拍板"按照换收割形态继续"（2026-10-03）——立项**事件驱动收割层**的第一步：
> 原料盘点。事件驱动要成立需要一族事件而非单族（问询函单族的教训）；本普查把
> 问询函事件研究（`plans/inquiry-family-prereg-20261003.md` §三，已机械验证的管线）
> 泛化到语料全部事件类，产出**方向 × 强度 × 体制**符号图谱。普查本身不立项任何
> 收割机；阳性类进收割层预注册须各自另立。

## 一、数据与事件（冻结）

- 语料 = `data/text_corpus/tagged/*.parquet`（5,459 只 × rubric v3 tier-1 打标 458 万行，
  列含 category/valence，2019-01 起，巨潮口径无北交所）。
- **14 个事件类**（rubric v3 类目，一字不改）：operating_contract_data / ma_restructuring /
  financing_capital / related_party_transaction / audit_internal_esg / equity_incentive /
  buyback / litigation_regulatory / insider_trading / guarantee_financial_assistance /
  dividend / earnings_forecast / periodic_report / governance_meeting。`other` 不入普查。
- 事件 = 该类标题、**排除元事件**（标题含 延期|取消|进展|撤回|终止|催告|更正，与问询函
  研究同一排除集）、按 (ts_code, 公告日期) 去重；t0 = 公告日（日粒度）。
- 全类共用一次语料扫描与一次个股日收益加载（`daily.pct_chg/100` 除权昨收口径；
  基准 = `index_daily` 000300.SH）。

## 二、读数（冻结，与问询函研究同源）

每类：
- **主读数：市场调整 CAR(21) = Σ_{d=E+1..E+21}(r_stock,d − r_bench,d)**，E = t0 后首个
  交易日，严格 PIT（只读 E 收盘后）；
- 描述读数：CAR(5/10/42/63) 市场调整与 raw 腿；即时反应 IR = (E 收盘/t0 收盘 −1) −
  基准同窗；
- 事件有效条件：E 存在个股收盘、窗末日存在个股收盘、窗内缺失 ≤3（缺失日个股腿计 0
  并计数）；不满足丢弃并计数；
- **聚类稳健读数（新，普查必备）**：按 t0 日期聚合的逐日均值 CAR(21) 的 t（事件日内
  截面相关会使逐事件 t 虚高——分红/定期报告类尤其如此）；
- 子窗（2019-2022 / 2023-2026）逐事件 t；n_events / n_stocks / 逐年事件数。

## 三、判据（冻结）

- 每类判决：**|t_逐事件| ≥ 2 且双子窗同号且各自 |t| ≥ 1.5 ⇒ "效应成立(±)"**，否则
  "本构造下未证实"。与问询函研究判据逐字同源（跨类可比）。
- **普查级多重比较**：14 个主读数的 Bonferroni p_adj（min(1, 14×p)）作为描述列照登；
  普查的角色是**筛选图谱**——任何阳性类推进到收割层须另立预注册（事件组合层回测
  + 容量/成本 + 前瞻纸面），届时自带门禁。
- 事后不许：加类/删类/换窗/换基准/子样本重跑。

## 四、已知限制（跑前写死）

1. 市场调整 ≠ 规模/风格调整——密集类（dividend/periodic_report）的事件股有系统风格
   倾斜，CAR 含风格成分；普查只给方向图谱，不剥风格；
2. 打标为 tier-1 正则（v3 与 rubric 一致率口径在册），类内噪声由"收割层预注册"的
   值域/人工抽检关卡承担；
3. periodic_report 类事件极大且日期全市场已知（预定披露），预期无漂移——照跑作
   阴性对照。

## 五、边界与留痕

- 只读零写库；不写 alpha_bank、不计 factor 分母；不碰生产；
- runner：`scripts/event_census.py`（向量化 CAR 管线）；产物
  `experiments/event_census_20261003.json`（14 类全读数 + 符号图谱汇总）；
- 判决登记：台账 05 号七十二补（canonical → `scripts/sync_know.py`）；
- 与在册证据的关系：问询函(litigation_regulatory 的子族,已单独判)在册 −1.49%；
  指数调样纳入(非语料事件,卫星线)在册 +1.70%——普查结果与这两条互为对照。
