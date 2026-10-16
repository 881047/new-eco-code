# P3-1 预注册：拆分 `run_backtest`（重构，要求行为逐位一致）

> 日期：2026-09-20 · 类型：**重构**（不是口径变更）
>
> ⚠️ **2026-09-22 状态注记：不采纳（未执行，也不再执行）** —— 14 号台账判决：
> "纯结构重构无收益，`engine.py` 只动 3 行取码，不足以拆"。本文件保留为**预注册证据**
> （按 `scripts/oneoff/` 同类纪律：证据不删，只加状态注记），不再作为待办。
> 依据：`CLAUDE.md` §1（改动会改变回测撮合/成本口径 ⇒ 必须先 A/B、独立预注册、确认后重钉 pin）
> 来源：2026-09-20 全面审查 D-06 / P-1 —— `app/backtest/engine.py:373 run_backtest`
> 单函数 **407 行 / CC 88**，承载 8 项职责、20+ 个可变状态，无法局部优化也无法安全并行。

## 1. 目的与性质

把 `run_backtest` 按职责拆成数个子函数，**不改变任何计算行为**。
因此本轮 A/B 的判据不是"哪臂更好"，而是：

> **拆分后的输出必须与拆分前逐位一致（Δ = 0）。**

若 Δ ≠ 0，说明拆分引入了行为变更 —— 那就不属于"重构"，
必须按 `CLAUDE.md` §1 走完整流程：重钉 `backtest_run` pin +
同步 `config.yaml` 口径注释 + README「关键设计」表 + commit 写明 Δ 与旧 pin 值。

## 2. 拆分目标（预注册，先冻结）

拟按职责切分为（名称与边界在动手前定死，避免边拆边改判据）：

| 子函数 | 职责 | 原位置 |
|---|---|---|
| `_load_inputs()` | 配置装载、状态自适应持仓数、科创过滤、评分表读取、月末日期集合、时点名称解析 | L392–L441 |
| `_period_loop()` | 逐期选股 / 撮合 / 费用 / 净值 / 换手 / IC / 日频盯市 | L470–L740 |
| `_metrics()` | 指标汇总（年化、回撤、日频回撤、夏普） | L740–L765 |

**硬约束**：不得修改 `_exec_side` / `_month_return` / `PRE_CLOSE_QUANTIZE` /
`tradable_mask` / `allocate_lots` 任何一行 —— 这些是 `CLAUDE.md` §1 危险表里的换 pin 位。

## 3. 判据（跑前冻结，跑后不得放宽）

### A0 — 基线可复现（前置门）
当前（未拆分）代码连续跑两次，输出必须**完全相同**。
⇒ 证明回测本身是确定性的，之后的比对才有意义。
不通过 ⇒ 停止，先查非确定性来源（随机种子 / 字典序 / 时间依赖）。

### A1 — metrics 逐位一致（主门）
两次运行的 `run_backtest()` 返回值 `metrics` 字典：
- 键集合完全相同
- 每个数值字段**逐位相等**（不做浮点容差比较）
- 通过标准：**Δ = 0**

### A2 — 月频净值序列逐位一致
`backtest_nav` 表中该 `run_date` 的每一行
（`run_date, trade_date, nav, sys_nav, bench_nav`）完全相同，且行数相同。

### A3 — 日频盯市序列逐位一致
`backtest_nav_daily` 同上（`CLAUDE.md` 记该表为 R14-4/D7 日频盯市，实盘口径回撤来源）。

### A4 — 黄金基线不被扰动
`tests/test_backtest_golden.py` 必须全绿。
**禁止**用 `REGEN_GOLDEN=1` 让测试变绿 —— 那等于把行为变更藏起来。

## 4. 执行方式

- 脚本：`scripts/oneoff/ab_run_backtest_refactor.py`（本目录，证据存档）
- 两臂：`--arm base`（当前代码）与 `--arm refactor`（拆分后）
- 隔离：回测行 `run_date` 均带 `20260920_p31_<arm>` 前缀，**不碰生产 run_date**
- 产物：`logs/ab_run_backtest_refactor_<arm>.json`
- 比对：`python scripts/oneoff/ab_run_backtest_refactor.py --compare base refactor`

## 5. 通过 / 不通过的处理

| 结果 | 处理 |
|---|---|
| A0–A4 全绿，Δ = 0 | 拆分进生产；**仍需**跑全量测试（1399+ 条）确认无跨文件影响；pin **不需**重钉（行为未变） |
| Δ ≠ 0 | **回退拆分**。若要保留该变更，改走 `CLAUDE.md` §1 完整流程：重钉 pin + 同步 3 处文档 + commit 带 Δ 与旧 pin |
| A0 不通过 | 停止 —— 回测非确定，任何重构比对都无意义 |

## 6. 当前进度（2026-09-20 22:35）

- [x] 判据冻结（本文件）
- [x] A/B 脚本 + README 索引登记
- [x] A0 基线双跑（确定性验证）—— **通过**，Δ = 0
- [x] 基线快照 `--arm base`（月频 nav 115 行 / 日频 daily 2328 行）
- [x] **第 1 步**：抽出 `_load_inputs()`（原 L383-460，78 行）
- [x] 对照 `--compare base refactor` —— **A1/A2/A3 全部 Δ = 0**
- [x] A4 golden 未被扰动（9 passed，**未用** REGEN_GOLDEN）
- [x] 全量 1400 passed / 2 skipped / 293 subtests
- [x] 结论：**属重构，行为未变，pin 不需重钉**；已提交 `a58c6cca`
- [x] **第 2 步**：主循环包进嵌套 `_period_loop()`（197 行）—— A1/A2/A3 全部 Δ=0，
      golden 9 passed，全量 1400 passed；已提交 `1c5e1916`
- [x] **第 3 步**：指标段抽成 `_metrics()`（61 行）—— A1/A2/A3 全部 Δ=0，
      golden 9 passed，全量 1400 passed；已提交 `d33fd46b`
      **至此 P3-1 三步全部完成，CC 目标 <20 达成（实测 11）**

### CC 轨迹

| 阶段 | run_backtest CC | 等级 |
|---|---|---|
| 原始 | 88 | F |
| 第 1 步后（抽 `_load_inputs`） | 62 | F |
| 第 2 步后（抽 `_period_loop`） | 28 | D |
| **第 3 步后（抽 `_metrics`）** | **11** | **C** |

✅ **目标达成**：CC 从 88 (F) 降到 **11 (C)**，优于预设目标 <20。

### 第 3 步的坑（差点让它「假绿」通过）

`ann` / `bench_ann` / `max_dd` 在 `_metrics()` 内定义，但收尾
`log.info("回测完成: 年化…/基准…/超额…/回撤…")` 要用这三个量
⇒ 首版漏了，回测直接抛 `NameError`。

**更危险的是验证工具本身**：A/B 脚手架在回测失败时**保留旧产物**，
于是 `--compare` 拿上一臂的快照自比，报出「Δ = 0，全部通过」。
也就是说——第 3 步首版实际是坏的，工具却说"拆分未改变行为"。

两处都已修复：
1. `_metrics()` 改为返回 `(metrics, ann, bench_ann, max_dd)`
2. 脚手架：回测失败即打印错误类型与信息、**不覆盖旧产物**、退出码 3

⚠️ 教训：**验证工具本身必须先被验证**。一个会在失败时假绿的守卫，
比没有守卫更危险 —— 它给你确定的错觉。本轮这是第三次
（前两次：比对假差异、A0 自检假绿）。

### 第 2 步的关键决策：嵌套函数 + nonlocal，而非独立函数

原 §7 设想"引入显式状态容器 + 独立函数"。实测改用**嵌套函数 + nonlocal**：
独立函数要把 9 个跨迭代状态全改成 `st["x"]`（197 行、几十处引用，极易错位）；
嵌套函数靠闭包直接读写外层变量，**循环体代码零改动**，风险大幅降低。

两个前提必须验证（已验证）：
  ① 循环内无 `return` / `break` / `continue` —— 否则 `return` 语义会从
     "退出 run_backtest" 塌成 "退出本函数"
  ② nonlocal 变量由 AST 精确算出（循环外顶层定义 + 循环内被赋值）＝ 9 个：
     `nav` / `sys_nav` / `bench_nav` / `prev_picks` / `prev_picks_sys` /
     `prev_sh_full` / `prev_sh_sys` / `turnover_sum` / `turnover_n`
     列表类（`nav_rows` / `daily_rows` / `monthly_model` / `monthly_bench` /
     `ics` / `n_effs` / `idles`）走 append、不重新绑定，不需 nonlocal。

⚠️ 算 nonlocal 清单时踩的坑：第一版 AST 脚本只检查子节点不检查节点自身，
漏掉顶层直接赋值（如 `nav = 1.0`），算出来 before 只有 2 个 ⇒ cross=0。
**必须先处理节点自身再递归子节点。**

### 第 1 步实测收益

| 指标 | 拆分前 | 拆分后 |
|---|---|---|
| `run_backtest` CC | 88（F） | **62（F）** |
| `run_backtest` 行数 | 407 | **357** |
| `_load_inputs` | — | CC 27 / 120 行 |

CC 62 仍属 F 级 —— 主循环（最大复杂度来源）尚未拆出，需第 2、3 步。
按 §7 的顺序，第 2 步改动面最大，建议单独一轮做。

### 踩坑记录（第 1 步实测）

1. **比对假差异**：序列第 0 列 `run_date` 与 `metrics.run_date` 两臂 tag 天然不同，
   直接比会冒出 2000+ 处假差异 ⇒ 二者是**标识符不是行为数据**，比对时必须排除。
2. **拆分手法**：78 行手工复制极易出错 ⇒ 改用行级脚本切分（本次用一次性脚本），
   改完立即 A/B 验证。
3. **重启服务**：手工 `Start-Process` 启动 uvicorn 会立即退出（Hidden/Minimized 都不行），
   可靠方式是 `Start-ScheduledTask -TaskName "NEW ECO Watchdog"` 主动触发看门狗。

## 7. 实施复杂度评估（**拆分前必读**，2026-09-20 读码后更正）

原 §2 设想"按代码块切成 3 个函数"，读码后发现**过于乐观**：

指标汇总段（L726-764）依赖 **20+ 个局部变量** ——
`ann` / `bench_ann` / `max_dd` / `vol_ann` / `sharpe` / `win_rate` /
`turnover_sum` / `turnover_n` / `ic_arr` / `sys_nav` / `n_periods` /
`nav_rows` / `daily_rows` / `cfg` / `cost` / `slip` / `cand_top` / `hyst` /
`stop_loss` / `state_map` / `ind_cap` / `floor_stocks` / `capital` …

⇒ 若直接"把这块搬进函数"，会得到一个 **20+ 参数的丑陋函数**：
CC 是降了，但可读性、可测性都没改善，**不算成功拆分**。

### 修正后的拆分顺序（按依赖由外向内，**不可颠倒**）

1. **先抽 `_load_inputs()`**（L392-441）
   入参少（`cfg` / `score_table` / `start` / `end` / `st_filter`），出参明确。
   收益/风险比最高，**建议作为第一步**。

2. **引入显式状态容器**承载主循环的 20+ 中间量（dataclass 或 dict），
   再抽 `_period_loop()`。这一步改动面最大，必须配合 A/B 逐位验证（A1/A2/A3）。

3. **最后抽 `_metrics()`**
   此时它只依赖状态容器，参数从 20+ 收敛为 1 个 —— 这才是干净的拆分。

⚠️ 顺序颠倒（先抽 `_metrics`）会立刻得到一个 20+ 参数的坏函数。

### 建议的节奏
每一步单独一个 commit，每步跑一次 `--compare base refactor` 确认 Δ=0 再进下一步；
任一步 Δ≠0 立即回退该步。单次会话内建议最多做第 1 步（最稳）。
