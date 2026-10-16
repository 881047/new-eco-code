# 池剔除 A/B 预注册（2026-09-27，跑数前冻结）

> 背景：S1-S5 官方重测（05 号 十七补）给出池内证据分级——bias_60/lhb_cnt20 被 S5 翻除
> （HL=1 月）、25 个 S1-S4 未过档。纪律的下一步 = "剔除/降权 A/B 候选名单"→ 独立 A/B
> （名单≠决定）。nf 全量重训 A/B（09-26）的 58 因子包是"剔除 23 + 加入 50"**混合包**被否；
> 本 A/B **只测剔除方向**，把门禁读数转成组合层决策读数。

## 一、臂（冻结，机械派生自官方重测产物）

| 臂 | 因子集 | 含义 |
|---|---|---|
| A ctrl | 31（factor 表原路径） | 现行生产；**同进程同日重训 ×2 种子集**（跨日重训不可复现已实锤 ⇒ 控制臂必须当日重训） |
| B_min | **26** = 31 − {S5 判否的池成员} | 最小剔除（机械读 `single_factor_retest_s5_20260927.json` 中 `s5_half_life is False` 的池成员：bias_60/high_52w/lhb_cnt20/lhb_net20/rev_5，均 HL=1 月）。**跑前勘误（首跑守卫中止，训练未开始）**：初稿按"过档者被翻转"的叙述误写 29——S5 对全池 31 个都测了 HL，判否口径=机械定义 5 个 ⇒ 26，以机械定义为准 |
| B_gate | 仅 S1-S5 过档者（现 6 个） | 门禁完全执行的含义（机械读 `PASS_single` 池成员） |

- 双种子集 sa=[42,7,123,2024]（生产）/ sb=[5,11,77,999]；同进程顺序 6 臂全量重训，
  参数与 nf 先例**逐字一致**（`T.run(train_months=36, backtest_start="2017-01-01",
  full=True, num_boost_round=ENSEMBLE_ROUNDS, early_stopping_rounds=0,
  ensemble_seeds=seeds, holdout_recent=6, label_neutral="industry")`）。
- B 臂走 `factors_override=子集面板`（面板取自 factor 表宽表、已中性化，不再处理——
  与 nf 臂的"8 保留"同法）。
- 复现守卫：excl_ctrl_sa 双窗读数须与 pin 在册（满仓 20.09%/−27.92% prod；
  nf ctrl_sa 同）逐位一致——同日重训可复现性的再确认。

## 二、判读（冻结）

- **打分层（信息性）**：`gate.evaluate` 逐臂 DEAD head/t（与 diag_gate_calibration 同法）。
- **组合层（判定）**：复用 `portfolio_admission_gate.py`（--ctrl excl_ctrl --cand
  excl_min / excl_gate，--tag exclmin / exclgate）——生产路径回测双窗（prod=eval_start /
  full）、Δ per 种子集 → `gate.verdict_portfolio` 机械判决（P1/P2/P3）。
- **判据方向不对称性（如实声明）**：组合层档 v1 是"改善轴"（P1 ≥ +2.0pp），为**加入**
  设计。对**剔除**而言，最可能的真结果是"中性"（Δ≈0，死重不拖累组合）——v1 会判
  REJECT。本轮**不改阈值**（修订须新预注册），判 REJECT 的含义 = "剔除未带来 ≥+2pp
  改善 ⇒ 无重钉理由，池维持"；**非劣读数**（|Δsys_ann| 双种子集 ≤ 1pp 且回撤/换手不劣化）
  作为参考列同时报告，供拍板（若未来出现"剔除改善"或用户想按非劣逻辑重立门槛，另立预注册）。
- **判决词**：机械（PASS 进拍板 / REJECT 线结清 / INDETERMINATE）；人不改变机械读数，
  只做是否重钉的最终拍板。

## 三、产出与语义

- 训练体：`scripts/oneoff/excl_pool_train_20260927.py`（隔离表 `excl_*`/`exclimp_*`，
  模型归档 `data/models/control/excl_*`；生产 model_score/importance/ens 与
  `data/models/ens` 全程只读）。
- 判决：`portfolio_admission_gate.py` ×2（padm_exclmin_* / padmexclgate_* 留痕）。
- 登记：05 号台账 十九补。无论判决如何不直接改生产池——PASS 也不重钉（拍板另做）。

## 四、限制

1. nf 混合包（剔除+加入）已整体证伪——若本轮剔除臂 REJECT，无法区分"剔除有害"与
   "剔除中性但无增益"（前者看 Δ 符号，后者看非劣读数）；B_min 与 B_gate 的对比提供
   剂量-响应读数；
2. 判据方向不对称（见 §二）；
3. 单日单环境——跨日泛化不做（已知不可跨日复现）。
