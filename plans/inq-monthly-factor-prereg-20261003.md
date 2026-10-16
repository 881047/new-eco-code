# 月频「近期被问询」指示因子预注册（2026-10-03，跑数前冻结）

> 来源：问询函事件研究（`plans/inquiry-family-prereg-20261003.md` §三，效应成立：
> CAR21 −1.49%/事件 t=−5.71，IR≈0 慢漂移，2023-26 强 3.5 倍）登记的后续候选线；
> 用户 2026-10-03 指令"继续做月频'近期被问询'指示因子" = 本预注册的拍板。
> 先例口径：文本族单因子档评估（verdict_single）+ 生产口径中性化。

## 一、构造（冻结）

- 事件源 = 问询函事件研究的**同一冻结正则**（`scripts/probe_inquiry_family.py` 的
  RE_LETTER/RE_REPLY/RE_INTER/RE_EXCLUDE，运行器直接 import 保证单一来源）；
  清洁问询函按 (ts_code, 公告日期) 去重（6,930 封）。
- 月末锚 = 生产月末交易日（`fe.month_end_dates`，eval_since=2019-01-01，warmup 400
  自然日，与门禁家族同口径）。
- **因子族（2 个候选，声明分母 = 2）**：
  - `inq_hit_21`：锚点前 **21 个交易日**内（cutoff, anchor]（日历区间）收到 ≥1 封
    清洁问询函 ⇒ 1.0，否则 0.0；
  - `inq_hit_63`：同式，窗口 **63 个交易日**（≈ 事件研究漂移主段 21-63d）。
- PIT：信件日期 ≤ 锚点日才计入；语料时间戳为公告日粒度。

**机械悬崖（跑前预告，非事后解释）**：`fe.neutralize` 的 2%/98% winsorize 在截面
非零占比 <2% 时把整行归零（F35 教训）。函流量 ~80 封/月（2019-2023）⇒ `inq_hit_21`
死区各锚 k≈45-55（<100）**预期整行机械归零、读数全空**——这是构造与机器的不匹配，
不是信号缺失；`inq_hit_63` 死区 k≈130-170（>100）预期存活。两候选**照跑照登记**，
21d 的机械失败也是证据。逐锚非零占比全部落盘产物。

## 二、判据（冻结，单因子档 verdict_single）

`gate.verdict_single(dead_head, dead_t, active_head, shadow_abs_rho)`（apps/alpha/gate
现行实现，一字不改）：

- S1 |DEAD head| ≥ 0.3%/月；S2 |DEAD t| ≥ 2.0（p_adj 取消 = 单因子档惯例）；
- S3 死区/活跃区 head 同号（事件研究先验：两区同为负）；
- S4 去相关：与**现役池 26 因子**（factor 表 31 名 − config `factor_exclusions` 5 名）
  及 model_score 的合并 Spearman |ρ| max ≤ 0.7；
- S5 时效轴不适用（构造内嵌固定 21/63 交易日时窗，衰减由构造决定非估计），传 None
  = 未测不否决（verdict_single 向后兼容语义）；
- 求值链 = 门禁家族同款：`fe.neutralize`（生产口径）→ `factory._masked_panel`（PIT
  可交易域 `factory.tradable_mask`）→ `gate.evaluate(fwd=gate.forward_return(m,21),
  md, zones)`。

**PASS_single ⇒ 判决 = "值得进剔除器 A/B 拍板的候选"**（剔除器 A/B 本身**另立预注册**
——在册先验"剔除器 A/B 十连败"，本预注册不预支任何组合层结论）；
**FAIL ⇒ 线结清**（死区弱腿稀释后 |head| 不足是最可能的死因，如实登记）。

## 三、边界（跑前写死）

1. 只读零写库：**不写 alpha_bank、不计 Bonferroni 分母**（单因子档无 p_adj；家族
   声明试验数 = 2，登记于本预注册与产物）；
2. 不做剔除器 A/B、不进生产、不碰 pin；
3. 事后不许：换窗（21/63 之外加窗）/换方向（取反当 alpha——F75b 三度证伪的正解是
   排除不是取反）/子样本重跑/把 21d 的机械失败解读为信号不存在（预注册已在 §一
   预告该失败是构造性）。

## 四、运行与留痕

- runner：`scripts/inq_monthly_factor.py`（只读 DB + 语料）；
- 产物：`experiments/inq_monthly_factor_20261003.json`（逐锚非零占比、双候选读数、
  S1-S4 明细、判决）；
- 判决登记：台账 05 号七十一补（canonical → `scripts/sync_know.py`）。
