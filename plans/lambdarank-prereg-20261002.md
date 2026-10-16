# 第 14 轮改模型:排序目标(lambdarank)vs 回归 —— 预注册(2026-10-02,跑判决前冻结)

> 触发:用户指令"训练目标不应该是预测,应该是排序"。**在册状态**:lambdarank 曾有
> "完败"判决但**无实证留存**(管线已删,MOD-02 审计缺口登记;09-29 检索确认 2023-26
> 无新外部证据)——本轮 = 用户授权的缺口补证,同时是模型层目标族矩阵的缺失格:
> regression ✓(现役冠军,13 轮幸存)/ binary ✓(第 11 轮 head_cls/vt)/ **lambdarank ✗**。
> 机制理由(用户立场):下游只消费**序**(score 只看 rank,top6),MSE 把容量花在拟合
> 幅度噪声上;LambdaRank 直接优化序。反方证据(rankens 无学习排序 −2.67%、E2E listwise
> 已 REJECT 但换了模型类)——本轮保持 LightGBM 不变,**唯一变量 = 目标族**。

## 一、臂定义(冻结)

- **A 臂(用户立场)**:LightGBM `objective="lambdarank"`,标签 = 当月截面(训练域)行业
  中性 21 日收益的**十分位序**(1..10,method='first' 确定性),**group = 训练窗内逐月
  截面**(query 语义),其余超参逐字复制生产 PARAMS(lr 0.05/31 叶/min_data 200/ff 0.8/
  bagging 0.8/1/L2 1.0),num_boost_round=50 无早停(轮数扫描结论同源),4 种子分位排名
  均值集成——**唯一变量 = 目标族,其余与生产逐字同源**。
- **A 对照**:生产 pin(20260928_exe / _exe_full 库内 metrics,不重跑)。
- **B 臂**:同上,种子组 B=[7,13,99,2024];**B 对照**:model_score_hw_base_b 的回测
  (B1 轮回归基线同种子集,缺窗用 run_backtest 补跑,run_tag=ltr_ctrlB_*)。
- 走查 = 同特征(_load_factors_wide + factor_exclusions 26 因子)/同标签域(行业中性 21 日
  delist_last_price=True)/同 walk-forward(36+6,PIT 域)——复用 mlattr 赛马已验收的
  month_block/prepare 路径;隔离表 model_score_ltr_a/b(ensure_score_table,小写,
  archive_dir 显式)。**结构对齐验收**:行数 = 444,312 / 月数 = 116,否则判实现失败不判策略。

## 二、判据(冻结,B1/E2E 同款)

- **P1**:双种子集同向达标——Δsys_ann(臂−对照)prod ≥ **+2pp** 且 full ≥ **+1pp**,
  A/B 两组均须满足(阈值+同向)。
- **P2**:回撤劣化 prod < 2pp / full < 3pp(两组)。
- **P3**:换手(avg_turnover)≤ +5pp(两组)。
- **判决**:P1∧P2∧P3 ⇒ **PASS(进拍板)**;P1 FAIL 且双窗/双组同向为负 ⇒ **REJECT
  (线结清,同时了结 MOD-02 审计缺口)**;收益轴异号或仅一组达标 ⇒ **INDETERMINATE**。

## 三、诚实条款

- lambdarank 输出的数值幅度无语义,引擎只看序(兼容 ✓);预测期单月单 group。
- 本轮不触碰生产链、不重钉;PASS 后的生产切换须独立拍板(与 B1/E2E 同流程)。
- **无论结果如何,MOD-02 缺口(完败无实证)就此了结**——这是本轮的底线收益。

## 四、产物

`scripts/oneoff/lambdarank_ab_20261002.py`(独立走查,复用 mlattr month_block/prepare);
隔离表 model_score_ltr_a/b;读数 `experiments/lambdarank_ab_20261002.json`;台账 N+1。
