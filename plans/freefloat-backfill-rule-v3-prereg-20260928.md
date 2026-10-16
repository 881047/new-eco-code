# 自由流通股本回填 + 选股规则 v3（ff_ratio）复活预注册（2026-09-28 深夜）

> 背景：v2 判决（plans/index-rebal-satellite-v2-prereg-20260928.md）= edge GO 级但
> 5,000 元档纸面轨 NO-GO，原因是冻结选股规则 weight_proxy（weight/circ_mv）在公告时点
> **不可计算**（新纳入股无月末权重），其可计算孪生需要自由流通股本——库内无。
> 用户指令：去找去拉（联网）。**数据源已验证**：tushare `daily_basic.free_share`
> （历史到 2015，逐日更新；仓库 daily_basic 表建表时未存该字段）。
> 性质：只读回填 + 研究层复验；不改生产、不换 pin、不动 `app/**`。

## 1. 数据（先回填，后复验）

- 拉取：tushare `daily_basic`，字段 `ts_code, trade_date, total_share, float_share,
  free_share, circ_mv, total_mv`，**月末交易日快照**（2019-01 ~ 2026-09，~93 个月末，
  每月末一次全市场调用）。
- 落地：`data/satellite_rebal/freefloat_monthly.parquet`（卫星线数据资产，gitignore，
  可由 tushare 重拉再生）。
- 验收：月末快照数 = 月度交易日历月末数（±2）；全市场单月末行数 ≥ 5,000；
  `free_share ≤ float_share` 违例率记录（预计 ≈0，违例行剔除并计数）。
- 单位：股本为"万股"口径（tushare daily_basic），比值类判据与单位无关。

## 2. 规则 v3：ff_ratio（weight_proxy 的可计算孪生）

机制推导（冻结）：中证宽基为自由流通市值加权 ⇒ `weight_i ∝ 自由流通市值_i = free_share_i×p`
⇒ **`weight_i / circ_mv_i = (free_share_i×p)/(float_share_i×p) = free_share_i/float_share_i`**。
即 v2 获胜规则（年均 +345 元）的排序 = 自由流通股本占流通股本之比的排序，
在公告时点用**公告前最近一个月末快照**真实可算（不依赖未来权重）。

- 选股 = 每批 top-2 by `free_share/float_share`（降序），逐股剔 688/689；
- 对照（同批同窗重算，防口径漂移）：weight_proxy（v2 原规则）、mcap_asc（v2 判否规则）。

## 3. 判据（跑数前冻结）

- **V1 覆盖**：纳入事件中 ff_ratio 可计算占比 ≥ 90%（快照缺失/float≤0 视为不可算）。
- **V2 G3 复验**：ff_ratio 规则 R5 年均 ≥ **+150 元**（口径与 v2 R5 完全一致：
  5,000 元/批 top-2 等权、真实费率含 5 元最低佣金、滑点 0.1%×2、退出 W_B）。
- **V3 机制等价**：ff_ratio 与 weight_proxy 的 top-2 选择 **Jaccard ≥ 0.5**
  （同批同指数池内）——这也是"tushare 自由流通口径 ≈ 中证自由流通口径"的交叉验证
  （weight 腿用的是中证真实指数权重）。
- **V4 稳健**：ff_ratio 规则两子窗（2019-2022 / 2023-2026）年均同号。
- **判决**：V1∧V2∧V3∧V4 全过 ⇒ 追踪器升级：群体追踪之外**加记 ff_ratio top-2 虚拟腿**
  （全成本纸面子账，为真金拍板攒轨道）；任一失败 ⇒ 规则 v3 线结清登记，勿重测。

## 4. 诚实边界（跑数前写下）

1. tushare 的 free_share（财汇口径）与中证指数公司实际使用的自由流通量定义可能有差
   （解禁滞后、战略持股认定）——**V3 的 Jaccard 就是这个口径差的量化**，不能预设为 1；
2. 月末快照 vs 公告日当天：月内解禁/增减持会移动 ff_ratio，用"公告前最近月末"是
   保守的实时可得口径（与实盘可得性一致，不是缺陷）；
3. 本轮不重判 v2 的 edge 读数（G1/G2/G4 已判），只动选股层；
4. 单月末全市场调用对 tushare 积分与限速友好（~93 次调用，0.35s 间隔）；
5. 若 V2 过而 V3 不过（赚但不等价）⇒ 说明 tushare 口径与中证口径系统性分叉，
   规则 v3 仍结清（不能拿"恰好同结果"当等价），分叉本身登记。

## 5. 产物

- 回填：`scripts/oneoff/backfill_freefloat_monthly_20260928.py` → `data/satellite_rebal/freefloat_monthly.parquet`
- 复验：`scripts/oneoff/index_rebal_rule_v3_20260928.py` → `experiments/index_rebal_rule_v3_20260928.json`
- （判决 GO 时）`scripts/satellite_rebal_paper.py` 增 `--rule-report` 模式（ff_ratio top-2 虚拟腿）
- 台账：05 号 40 补
