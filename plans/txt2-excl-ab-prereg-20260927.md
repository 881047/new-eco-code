# 26+2 vs 26 文本因子增量 A/B 预注册（2026-09-27，跑数前冻结）

> 背景：文本线（二十九/三十补方案 A 全链路）交付 31+2 构型 A/B——组合层 **PASS**
> （Δsys_ann prod +2.24/+3.95pp 双同向）但打分层 FAIL，矛盾留拍板；且其基座 = 31 因子
> （旧 pin），**今天生产已切 26 因子（excl5）**——PASS 不传导。本预注册把问题翻译到
> **现生产基座**：**26+2 vs 26**——"剔除 stale 的 +5pp"与"文本 2 因子的增量"是否可加。
> 用户指令"跑"批准立项。

## 一、两臂（冻结，机械派生）

- **对照 = `excl_min_sa/sb`**（26 因子，今训已验证：prod sys +22.12% = 现生产逐位复现；
  26 = 31 − {bias_60/high_52w/lhb_cnt20/lhb_net20/rev_5}，机械派生自 S1-S5 官方重测）。
- **处理 = `txt2_trt_sa/sb`**：26 因子面板 + **text_n1m + text_novel3m**（文本线单因子档
  PASS 的 2 个：−0.38%/−0.33%/月、与池 |ρ|≈0.14 真新信息）= 28 因子。
- 文本面板来源 = `data/text_corpus/factors_v1.parquet`（文本线留档原值）→ reindex 月锚
  + 参考宇宙 → **fe.neutralize 生产写入口径（稀疏守卫：sparse_ratio < 0.02 走
  neutralize_sparse，沿 nf 先例）**。
- 训练 = `trainer.run(factors_override=28 面板)` × 双种子集 sa/sb，参数与 excl_ctrl
  逐字一致（36 月/full/2017/50 轮×4 种子/holdout 6/标签行业中性化）。同日同进程。

## 二、判据（冻结，组合层档 v1 逐字——加入语义）

经 `portfolio_admission_gate.py --ctrl excl_min --cand txt2_trt --tag txt2pead`：
- **P1 收益**：双种子集 Δsys_ann prod ≥ +2.0pp 且 full ≥ +1.0pp；
- **P2 回撤**：MDD 劣化 < 2pp(prod) / 3pp(full)；
- **P3 换手**：≤ +5pp。
- **判决词（机械）**：P1∧P2∧P3 ⇒ "PASS（进拍板：26→28，第 7 次重钉走流程）"；
  否则 "REJECT（线结清）"；收益轴双种子集异号 ⇒ "INDETERMINATE"。拍板由人。

## 三、产出与语义

- 训练体：`scripts/oneoff/txt2_excl_train_20260927.py`（隔离表 txt2_trt_*/txt2imp_*，
  生产表只读）；判决：padm 复用（run_tag=padm_txt2pead_ 留痕）。
- 登记：05 号台账 三十二补。无论判决不直接改生产——PASS 也须拍板后才重钉。

## 四、限制

1. 文本面板 2019 起有值（2017-2018 训练窗行为 NaN，行过滤自然处理）——文本 α 的
   训练信息量集中在 2019+，与因子评估窗一致；
2. text_n1m 为公告计数、text_novel3m 为新颖度（1−重复率）——两臂同用，方向由模型学习；
3. 链断裂修正已在上游（factors_v1 为修正后产物，行数守恒 444k→... 校验过）；
4. 双窗判据沿组合层档 v1（加入语义，无方向修订）。
