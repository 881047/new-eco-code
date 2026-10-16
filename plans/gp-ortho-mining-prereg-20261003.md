# GP 正交增量挖矿首跑预注册（2026-10-03，跑数前冻结）

> 来源：`plans/h0-ortho-fitness-prereg-20261003.md` §三.3（"ortho 模式的**搜索运行**
> （GP 预算/分母/判读）另立预注册"）+《T3预注册草案》§H0（冻结定义："候选适应度 =
> 对 model_score 残差 + 对 alpha_bank 现有池的增量 IC；**GP/LLM 候选须在正交增量口径
> 过五判据方可 POOLED**"）+ 用户拍板 B2=立项（位级审计已完成 25/25 逐位一致）。
> 先例 = `plans/gp-mining-prereg-20260926.md`（gp1）。**改任何一条 = 换判据。**

## 一、唯一目的与预期管理（跑前写死）

第一次把 ortho 适应度真实跑通：搜索目标从"候选本体的死区 head"换成"候选对控制集的
**正交增量残差**的死区 head"（AlphaGen 思路：对生产 model_score 残差 + 对 alpha_bank
现有 POOLED 池增量）。

诚实预期：本仓正向选股方向全线证伪（四铲四否、文本 α 全灭、模型层 14 轮全否、
gp1 量价 GP 1 PASS 但为 size 影子）⇒ **0 PASS 是基准预期，不是失败**。产出价值 =
① ortho 机制首跑与可复现性（seed 冻结）；②"正交增量 head"这个量的死区读数分布
（factory 建成以来第一次）；③分母入账后"要不要再挖"继续有价签。

与 gp1 的关系：**同预算同 seed，唯一差异 = 适应度的量测对象（head → ortho 残差）**
⇒ 两次 Pareto 前沿可逐名对照。gp1 的结论"量价同源 GP 的 PASS 是已知因子影子"正是
本轮要正面回答的问题：把影子在适应度端就残差化掉，看还剩什么。

## 二、冻结参数

| 项 | 值 |
|---|---|
| 提议器 | 仅 `GPProposer(fitness_mode="ortho")`（h0 已落地；head 缺省路径一字不动） |
| 预算 | `GPBudget()` 默认 = population 60 × generations 15，seed=20260911，max_depth=3 ⇒ **声明分母 900**（缓存命中重复个体照计） |
| 门禁分母 | `N_used = max(900, 跑前 bank.cumulative_tests() + 900)`（跑前读登记簿；2026-10-03 实读 247 ⇒ 1147）；在任何登记动作之前一次性读定 |
| 评估窗 | `eval_since=2019-01-01`，warmup 400 自然日（PreRegistration 默认，gp1 同口径） |
| 搜索域 | 死区 2020-09~2023-12（GPProposer 物理裁切，活跃区在适应度端不可达）+ PIT 可交易域（`Factory._tradable`） |
| 适应度 | h0 §一冻结链：`eng.eval → fe.neutralize → _ortho_residual → PIT 掩码 → gate.evaluate → mean(head_excess)` + 表达式复杂度（NSGA-II 第二维） |

**控制集 C（求值开始时点在册冻结，求值开始后不增补）**：

1. **生产 `model_score`**（ts_code × trade_date × score，2026-10-03 实读 117 个月，
   20170126~20260930）→ 月末截面日频前向结转。**PIT 规则（冻结）**：月 M 的分从
   **M+1 的第二个交易日**起生效（月度链在 M+1 首个日历日 18:33 后完成 ⇒ 该规则对
   "链最晚跑到 M+1 首个交易日晚上"也安全；门禁只读月末锚，一日陈旧无损），ffill
   至下一月分接管，首个生效日之前为 NaN（该控制当期按 h0 规则剔除）；
2. **alpha_bank `status=POOLED` 成员面板**：求值时点在册名单（2026-10-03 实读 =
   仅 `gp_000` = `Med(Div(Abs(Div($low, $total_mv)), $vwap), 5)`），在与 GP 求值
   **同一批行情**（`Factory.market()` 同一 m）构建的全窗 Engine 上求值（`build_panel`
   确定性 ⇒ 与任何同窗重算同值）。

## 三、判决链（冻结）

H0 判据"须在**正交增量口径**过五判据方可 POOLED" ⇒ `Factory._evaluate` 的 head 路径
判决**不作为本轮登记依据**。运行流程：

- `Factory(PreRegistration) → add(GPProposer ortho) → declare() → run(save=False)`：
  工厂流程照走（撞车快道、head 路径对照读数），**save=False 不写任何库**；
- **登记全部由运行器按以下链执行**（对 `run()` 实际产出 records 的每个成员）：

1. `raw = Engine(E.build_panel(m)).eval(expr)`（m = Factory.market() 同批行情，全窗）；
2. `neu = fe.neutralize(raw, ind)`（ind = fe.industry_map，与 Factory 同源）；
3. `res = factory._ortho_residual(neu, controls, gate.MIN_NAMES)`
   ← **与 head 链的唯一差异**（= h0 §一链路在判决端的延伸）；
4. `panel_md = factory._masked_panel(res, tradable, md)`（tradable 与工厂 eval 槽同一域）；
5. `det = gate.evaluate(panel_md, fwd, md, zones)`；
6. `cost = critic.churn_cost(panel_md, md)`；
7. `rec = gate.summarize(alpha_id, det, N_used, extra={含 fitness_mode=ortho 留痕、
   head 对照读数、预注册 hash}, cost_per_month=cost["cost_per_month"],
   n_tests_family=900, n_tests_prior=跑前累计)`；
8. 撞车慢速道 `bank.check_duplicate(values=panel_md, md=md)`（head 路径已按 head 面板
   查过；残差面板值撞车 = 增量与在册因子重合，同样拒登）→ 通过者
   `bank.save_values(alpha_id, panel_md)` + `bank.upsert(rec)`。

六判据一字不改（死区 head ≥0.5%/月含成本调整、t≥2、死/活同号、Bonferroni
p_adj<0.05 @ N_used、死区 top6≥0、成本 fail-closed）。前沿成员**全部**按本链处置，
不许捞回、不许挑。求值失败/无有效截面的成员按工厂既定语义落日志不登记。

- `--smoke`：`GPBudget(8,3)`、零登记零分母（gp1 先例）——只验证接线（适应度出现
  有限值、残差面板非空、判决链端到端可走），其读数不入任何判决。

## 四、判据与出口声明（跑前写死）

1. **PASS = 残差面板六判据全过 ⇒ POOLED**；PASS = 有资格进组合层回测，**不等于可用**
   （在册反例 5 个：die_flow/head_vt/h10/margin_net_share/gp_000）；任何 PASS 的
   组合层判决另立预注册；
2. 本轮"成功"定义 = 完整跑通 + 前沿成员全部按 ortho 口径处置 + 读数 JSON 产物 +
   分母入账——**不是**"挖出 PASS"。0 PASS 的结论写作："GP 在本仓死区 × 900 预算下，
   对 model_score + POOLED 池的**正交增量**无可检出增量"；
3. 事后不许：改判据 / 重跑挑窗 / 前沿外捞回 / 用 head 口径读数反向翻案（某成员
   head 口径好看而 ortho 口径不过，结论照登——那正是"影子因子"的验收）；
4. 若 smoke 或真实轮暴露适应度全 worst（指纹：gen 行"最好 -head"恒为 -1e9）= 接线
   缺陷 ⇒ 修接线后**重跑 smoke 验证**，真实轮读数只认接线修复后那次（接线缺陷不是
   数据结论，与 gp1 的 bind_factory 缺陷同类）。

## 五、限制（跑前写死）

1. 60×15 小预算（gp1 同规模）⇒ 只回答"机制通不通 + 小预算下有没有明显正交增量"，
   **不回答"GP 挖不出"**——那需要分母预算的拍板；
2. 控制集 = model_score + gp_000 两列 ⇒ "正交增量"是相对**当前生产基座**的增量；
   换基座（重训/excl 变动）后须重挖；
3. 残差化为秩回归 ⇒ 控制张成空间之外的非线性增量才幸存（h0 §三.2）；
4. 分母 900 永久入账 ⇒ 后续任何候选门禁更严——这是本轮最真实的成本；
5. POOLED 名单时点依赖（h0 §三.1）：控制集以运行装配时点在册为准，如实留痕；
6. model_score 覆盖 2017-01 起 ⇒ 死区各月末锚均有控制存留（实读 min 月 2,704 行，
   远超 MIN_NAMES=200）；早期 warmup 日期可能仅有 gp_000 单控制，残差照算（h0 规则）。

## 六、运行与留痕

- runner：`scripts/alpha_factory_ortho1.py`（`--smoke` / 默认真实轮；scripts 根，
  非 oneoff）；写库 = 仅 alpha_bank 登记 + alpha_pool 值面板（factory 既定登记路径），
  生产 model_score/factor/backtest/config 全程只读，不换 pin；
- 产物：`experiments/alpha_factory_ortho1_20261003.json`（前沿成员 head/ortho 双口径
  读数、控制集留痕、分母、N_used、撞车计数、耗时）；
- 判决登记：`D:\know\05_实验台账.md` 六十九补（canonical 先写再 `scripts/sync_know.py`）。
- 边界声明：本运行属 T3 **H0 线**（正交增量机器改造）；H1 线（拥挤体制条件化）已由
  前瞻纸面在途（`scripts/h1_crowding_paper.py`，2026-10-31 起窗），两线互不代入。

## 附：smoke 接线修正记录（2026-10-03，真实轮跑数前，不改任何判据）

1. **model_score 生效日越窗钳位**：数据窗起点之前的印章，`searchsorted` 返回 0 把全部
   早期印章挤到窗内第 2 日 ⇒ reindex 重复标签崩溃。修复 = 生效期早于数据窗的印章钳到
   窗首日（语义不变：该分在窗起点已在效力），同一生效日只保留最新印章。
   `--smoke` 第 1 次跑以此崩溃（exit=1），修复后控制覆盖 0/73 零控制锚。
2. **GP 前沿 id 前缀**：`GPProposer` 缺省 `gp_%03d` 编号与 gp1 在册记录撞 id
   （gp_000 已 POOLED）⇒ 撞车硬闸按 id_conflict 把 smoke 全部 4 条成员拒跑、工厂
   head 对照 records = 0。修复 = 运行器内子类 `OrthoGPProposer`，前沿交付时改用
   `gpo1_%03d` 专属前缀（只改名字，不改搜索/适应度/交付语义）。
3. smoke 实证：适应度有限值（gen 行最好 -head 0.0008，非 -1e9 全 worst 指纹）、
   死区 40 截面、控制覆盖 model_score min 3,732 / gp_000 min 3,989 名。
   ⚠️ 耗时读数：ortho 残差化 ~19s/次求值（冻结函数 iterrows+逐截面秩回归）⇒
   真实轮 900 分母预计 4-6 小时，后台运行。
4. **可复现性（实测降级，2026-10-03 smoke C/D/E/F 四跑）**：`PYTHONHASHSEED=0` 已钉死
   `_build_pset` 集合迭代序（不钉则同 seed 必漂移——smoke 两次求值 20 vs 16 实证）。
   在此基础上：**逐表达式确定性已证**——初始 8 棵树×2 进程适应度 repr 级逐位一致
   （raw/neu/res 中间量 sha 一致），C/D 共同成员 head+ortho 判决逐位一致；**但进化
   轨迹跨进程不可复现**（C/D/E/F 四跑四样，seed+hashseed 均同；根因未定位，与
   "生成确定+适应度确定+进化环节理论确定"矛盾，登记待查）。**处置：本轮以当次
   进程的产物 JSON 为唯一有效留痕**（前沿成员、双口径读数、预算、分母全量在档）；
   "可复现"的定义收窄为**判决链逐表达式可复现**（已证），轨迹复现降级为待查。
   分母 900 是声明值不依赖轨迹，Bonferroni 入账不受影响。
