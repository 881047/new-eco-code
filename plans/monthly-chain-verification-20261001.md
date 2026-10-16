# 10-01 月度链验证检查单（26 因子首跑，2026-10-01 18:33）

> factor_exclusions 采纳（26 因子 + pin 20260927_excl5）后的第一次生产月度链。
> 本检查单 = 各阶段验证点 / 日志关键词 / 异常与回滚触发。执行人按序核对并登记 05 号台账。

## 0. 链前检查（18:00 前）

- [ ] `config.yaml`：`factor_exclusions: [bias_60, high_52w, lhb_cnt20, lhb_net20, rev_5]`、
  `backtest_run: 20260927_excl5`、`chase.enabled: true` 无误；
- [ ] `data/models/ens/lgb_latest_meta.txt` 时间戳 = 09-27（26 因子集成在位）；
- [ ] `model_meta` 中 `factor_fp:model_score` = 26 因子指纹（09-27 14:24 登记）；
- [ ] 磁盘余量 ≥ 20GB（月链含 459 万行级因子重算缓存）。

## 1. 链中各阶段验证点（按 scheduler 阶段序）

| 阶段 | 正常信号（logs/app.log） | 异常信号 ⇒ 动作 |
|---|---|---|
| monthly_sync | 数据水位到 09-30 | 水位不足 ⇒ 等待补数后重跑 |
| factors | 因子覆盖最新月末 | 覆盖不足 ⇒ 训练就绪门控会中止（设计行为） |
| **train** | `生产因子剔除(配置 factor_exclusions): [...]` 出现；`因子指纹` = 26 个；**增量训练被指纹守卫拒绝 ⇒ 预期（指纹应为 26=26 相等则放行）** | 指纹不等 ⇒ 查 factor 表是否混入未剔除口径；勿强跑 |
| backtest | run_tag=月链新 run；系统年化量级 ≈ pin（±种子噪声 3.5pp 内） | 偏离 >5pp ⇒ 停，查 config_sha 与 ens 版本 |
| plan/paper/email | 方案 6 只、邮件发出 | 空方案 ⇒ 查可交易域 |

## 2. 关键判读

- **增量训练语义**：9 月末新增 1 期，指纹 26=26 ⇒ 增量放行（无需 --full）；
- **文本因子不在 factor 表**（33 = 31+2 从未 materialize）⇒ 训练因子集 = 26 ✓；
- vol 缩放层未接线（观察中）⇒ 链上无该阶段。

## 3. 回滚触发（任一即回滚）

- 月链 train/backtest 阶段 error 且重跑一次仍败；
- 回测读数偏离 pin > 5pp（排除新月份因素后）；
- 方案生成失败或持仓数 ≠ 6。

回滚 = `config.yaml` `factor_exclusions: []` + `backtest_run: 20260926_chase2` +
`train_model.py --full`（31 因子重训）——**须用户拍板后执行**。

## 4. 附带事项

- [ ] vol 缩放季度复跑（下次窗口 2026-12 月初）；
- [ ] L1 打新：月链持仓确认后，每只新股申购持续执行（账户操作层）；
- [ ] PEAD/文本线：维持线结清（勿重测），语料正文级深加工须新预注册。
