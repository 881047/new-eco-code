# Round-6 供给侧合并剔除器 — 细化版预注册（2026-09-19）

> 母计划：`plans/p0-validation-upgrade-20260919.md` Phase C（冻结文本）。本文把其中
> 每一条落到可执行口径，**判据全部写死在跑之前**。实现：`scripts/supply_excluder_r6.py`；
> 结果：`data/kg/supply_excluder_r6_result.json`。

## 0. 为什么有这一轮（与 round-5 的实质差异）

- round-5（`plans/kg-unlock-insider-round5-20260918.md`）跑的 `kg_unlock_90d_ratio`
  带着量纲错误（×1e3 且注释错 ⇒ 整列放大 10 倍）；2026-09-18 已修（`kg_materialize.py`
  内注释：circ_mv 单位是万元，×1e4→元，/收盘价=流通股本；release_shares 本身是股）。
- round-6 因此检验**修正后**的解禁信号 + 高管减持事件数，且首次测**合并规则**
  （两信号 z 平均）。round-5 分母：第一轮 18 + 第二轮 14 + round-5 批 5。
- **先验（如实写明）**：修正后 ratio 变小 ⇒ 同 θ 下剔除更少 ⇒ 比 round-5 更难过屏；
  预期"不过屏"概率高。本轮价值 = 给合并规则一个**带分母的正式判决**，不是找理由上。

## 1. 信号与数据（PIT 已物化）

| 信号 | 定义 | 方向 |
|---|---|---|
| `kg_insider_sell_60d` | 近 60 日高管减持事件数（delta_shares<0 计数，change_date PIT 键） | 越大越供给压力 |
| `kg_unlock_90d_ratio` | 未来 90 日解禁股数/流通股本（release_date 已公告安排，未来已知 = PIT 合法；×1e4 修正后） | 越大越供给压力 |

数据源：`data/kg/kg_features.parquet`（`kg_materialize.py` OUT，311,707 行 × 25 列）。

**数据现实（跑前登记，不修饰）**：
- kg 覆盖率逐锚点在 **13.5% ~ 134%** 之间**交替**（事件源覆盖所致）：约半数锚点
  kg 行数只有 500~2,200（对 model_score 截面 3,515~4,409），另半数 4,200~5,800。
  覆盖低的锚点两信号非缺失率仅 4~11% ⇒ 剔除几乎咬不到 top100 ⇒ 配对差≈0 是
  数据现实的诚实反映，不是实现错误。
- kg 锚点中 29 个日频锚点（2026-08-07~09-17，round-5 增量物化）**不使用**；
  只用与 `model_score` 贸易日精确相交的 **73 个月末锚点（2020-08-31 → 2026-08-31）**。
- model_score 早于 2020-08 的 43 个月无 kg 数据 ⇒ 研究窗就是 73 锚点，**"全史"=
  kg 覆盖窗全史**，引用时必须带这个限定（不冒充 2017 起全史）。

## 2. 截面构造（冻结口径）

- **锚点**：上述 73 个月末评分日（kg∩model_score 精确相等，无就近回退）。
- **选择域 ok**：生产 PIT 掩码（`PITEngine.tradable_matrix`）∩ 当日有 model_score 分数
  ∩ 标签可算（`forward_excess` 有限）。`ok.sum() < 200` 的锚点记 NaN（F56 同源守卫），
  全臂联合删除该锚点并登记。
- **z 域（不偷看未来）**：每锚点在「PIT 可交易 ∩ 当日有分数」（**不含**标签条件）内，
  对两信号分别算当日截面 z-score；**只有非缺失值参与 z**（NaN=无事件/无数据，不参与
  z、也不被剔除——与母计划"双缺失=不剔除"同一原则延伸到单信号臂）。
- **合并规则（母计划冻结，照抄）**：双有值 supply_z = 0.5·z_sell + 0.5·z_unlock；
  单有值者用单值；双缺失 = 不剔除。θ ∈ {1.0, 1.5, 2.0, 2.5}。
- **剔除注入点**：候选池层面——`ok &= ~(supply_z ≥ θ)` 后再取分数 top100（按分
  递补，与 round-4 `verify_excluder_production` 的 `top2 = top[~hot]` 同构）。

## 3. 研究侧筛选（12 研究臂，全部登记）

- **读数（F56 同源）**：top100 等权、5 日 `forward_excess`、换手 × 真实卖出费率
  `fee_mod.effective_rate(25_000.0, "sell", "20260914")`；基线 = 同管线无剔除。
- **臂（12）**：merge×{1.0,1.5,2.0,2.5} + 只sell×4θ + 只unlock×4θ。
- **配对差**：arm − base 逐锚点差；分段 = 全史（73 锚点）/ 活跃段
  （`expert.ACTIVE` = 2024-01-01~2026-08-31，32 锚点）。t = mean/(std(ddof=1)/√n)。
- **过屏判据（冻结）**：两段 mean(配对差) **同正** 且 **全史 t > 1.6**。
- **过屏后的选择（自由度写死，不挑最好看的）**：同族多 θ 过屏取**最小 θ**；
  跨族多臂过屏取**全史 t 最大**；再并列按 merge > 只sell > 只unlock 优先序。
  产出**至多一个**臂进生产复验（与分母 13 一致）。

## 4. 生产复验（只有过屏才跑；round-4 verify 模式 + 新鲜控制臂）

- 重建隔离分数表：`builder._top_candidates(plan_date)` 候选 − hot（supply_z ≥ θ*）
  → 隔离表 `model_score_r6excl`；**新鲜控制臂** = 同管线不加剔除 →
  `model_score_r6ctl`（不引用历史 pin run，免 config 漂移；2020-08 前两表相同 ⇒
  差异只来自覆盖窗，如实登记）。
- 回测：两臂各跑 `E.run_backtest(score_table=…, run_tag=r6excl/r6ctl, start="20190722")`，
  `cfg capital=20000.0` monkeypatch（round-4 同款，隔离 run）。
- **采纳判据（冻结，三条全过 ⇒ "采纳候选"）**：
  ① 全史超额 Δ = arm.sys_ann − ctrl.sys_ann ≥ 10%×|ctrl.sys_ann| 且月度配对 t>1.6；
  ② MDD 不劣：arm.sys_max_drawdown ≥ ctrl.sys_max_drawdown；
  ③ 留出集 = 活跃段 2024-01~2026-08（32 个月）月度配对差均值 ≥ 0（方向不为负）。
- **是否重钉 = 用户拍板，不在本轮范围**（CLAUDE.md §1：A/B 不进生产）。

## 5. 分母申报与登记

- 判决总数 **13**：12 研究臂 + 1 生产复验（若复验未触发，登记"未触发"仍占分母）。
  全部进 `data/kg/supply_excluder_r6_result.json` 与 05 号台账。
- 多重比较靠"全登记 + 生产复验只此一次确认"吸收（round-4/5 惯例）。

## 6. 产物清单

- `plans/supply-excluder-round6-20260919.md`（本文）
- `scripts/supply_excluder_r6.py`（`--stage screen` 只读；`--stage verify` 写隔离表 + 隔离 run）
- `data/kg/supply_excluder_r6_result.json`（判据、逐臂统计、覆盖率登记、分母申报）
- 05 号台账 round-6 条目（Phase E 收口时补）
