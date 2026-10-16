"""D06/D07 涨停 / 跌停验收 —— 路线图阶段一「数据验收」硬门禁项 (2026-09-12)

路线图原文（`A股量化系统实验路线图.docx`，表头 `编号 | 实验 | 核心指标 | 通过标准`）:

    D06 | 涨停实验 | **实际成交率** | 涨停不可买
    D07 | 跌停实验 | **实际成交率** | 跌停不可卖
    阶段一硬门禁: 任意 D03/D02 出问题 → 直接停止, 不要继续优化模型

================================================================================
预注册（写在跑任何数据之前；脚本 sha256 与判据一起落盘）
================================================================================

## 背景: 路线图要的指标与项目的现状不在同一层

路线图的**核心指标是「实际成交率」**, 通过标准是「涨停不可买 / 跌停不可卖」。
而本项目 `app/backtest/engine.py:_exec_side` 早就实现了这两条 —— 但它是**从
`pre_close × (1 ± limit_ratio)` 推导**涨跌停价的, 不是读交易所公布值。

⇒ 所以「实际成交率」这个指标只有在**先证明推导值 = 真值**之后才有意义。
本实验因此分三层, **L1 是 L2/L3 的前提**, 不是并列的三个读数。

## L1 限价推导正确性（必要条件）

对每个 (trade_date, ts_code), 用**引擎自己的代码路径**复算限价:
    nm       = NameLookup.name_at(code, date)          # 2026-09-12 补: 名称档
    ratio    = app.portfolio.board.limit_ratio(code, NameLookup.is_st_only(code, date),
                                               date, nm, NameLookup.name_at)
    limit_up = app.portfolio.board.round2(pre_close × (1 + ratio))
与 `stk_limit`(交易所公布值) 逐位比对。分子/分母只算**双方都有的格子**
(`stk_limit` 行数是 `daily` 的 1.06~1.07 倍 —— 它对停牌/未上市股也返回限价,
这些格子没有 `pre_close`, 单独计数并移交 D05)。

- **判据 P1**: 分歧必须能**完全归因**到下列已知规则性差异之一:
    (a) **上市初期无涨跌幅限制**(主板 5 日 / 创业 5 日 / 科创 5 日 / 北交所首日)。
        ⚠️ 2026-09-12 更正: 这条**在模拟盘里接线了**(`paper/engine.py:100` 的
        `_no_limit_day`, 用 `trade_cal` 数上市后交易日), **在回测里没有**
        (`backtest/engine.py:24` 只 import 了 `limit_ratio, round2`)。故这不是
        「未实现」, 是**回测与模拟盘口径不一致** —— 比未实现更值得记录。
        (先前 grep 用了错函数名 `no_limit_days`, 误得「零消费者」, 已订正。)
        另有一处已披露的**有意**差异: 模拟盘用**开盘价**判一字板, 回测用**收盘价**
        (`engine.py` 2026-09-05 审查的明文决定, 非缺陷)。
    (b) 长期停牌复牌首日 / 退市整理期首日无限制 —— `board.py` docstring 已披露的保守近似
    (c) ST 摘戴帽生效日与交易所实际执行日的错位
  **出现无法归因的分歧 ⇒ D06/D07 判 FAIL**, 按硬门禁处理 (停止后续优化)。

## L2 判定翻转（真正影响回测的量）

L1 的「分歧率」本身不说明后果 —— 1 分钱的分歧若发生在远离 `close` 的地方就不改变任何
成交。故 L2 直接度量**分歧是否翻转了 `_exec_side` 里的两个不等式**:
    买: `cv >= limit_up - 0.001` ⇒ 拒单
    卖: `cv <= limit_dn + 0.001` ⇒ 拒单
分别报 **高估方向**(引擎说可买/可卖, 真值说不可) 与 **低估方向**的格数。
- **判据 P2**: 只报数, **不设阈值** —— 它就是这个偏差本身的度量。
  但**高估方向 > 0 则 L3 的真值臂必须跑**(真值臂可能不是 no-op)。

## L3 核心指标 = 实际成交率

两窗回测(prod=20190722 / full=20170101), 对每一次 `_exec_side` 调用记录去向:
    成交 / 涨停拒单 / 跌停拒单 / 停牌顺延 / 顺延超限作废 / 无价
- **判据 P3**: **用真值限价判定为封板却成交的笔数 = 0**(「涨停不可买/跌停不可卖」的字面含义)。
- **判据 P4(守卫)**: 基线臂必须**逐位复现 pin**。pin 有**两条线**, 四条等式都要过
  (prod 85 期: 满仓 23.97%/−24.10% + 系统 20.64%/−19.66%; full 115 期: 满仓 12.56%/−51.00%
  + 系统 11.05%/−41.90%), 另加期数, 共 10 式。复现不了 ⇒ 插桩改变了行为, 本实验作废。
  **这个守卫不是形式** —— 它第一次跑就抓到了替身的一处真错(`return` 应为 `continue`),
  见第 7 轮记录 C。

## L3 真值臂（度量, 不是判据）

把 `_exec_side` 的限价格来源从「推导」换成「`stk_limit` 公布值」重跑两窗, 量出
「如果引擎限价口径有偏差, 回测差多少」。**覆盖率守卫**: 若评估窗内 `stk_limit`
缺失率高(回退到推导的比例 > 1%), 该臂**不是有效测量**, 只报回退率不下结论。

## 明写: 本轮不做的事

- **不接线 `no_limit_days()`**。它是 L1 的一个归因类别; 接线 = 改成交口径 = 会让 pin
  基线失效, 必须独立预注册 + 组合层两轴判决。本实验只负责**证明它未接线**并量化它
  影响多少格。
- **不看结果调阈值**。四条判据在上面的文字里已冻结; 结果只可能让它们 PASS 或 FAIL。

================================================================================
修复记录（2026-09-12）—— **判据一字未改, 改的是引擎**
================================================================================

⚠️ 这一节必须和判据一起读, 否则「修好之后 P1 就过了」等同于「看到结果之后把标准改松」。
如实记录: **先跑 → 看到 8,966 格无法归因 → 诊断 → 补规则 → 重跑**。危险在哪、为什么不
算改判据, 见本节末尾的自我审查。

**这轮改了 7 个生产文件**(原预注册写的是「不修改任何生产文件」, 那句已不成立):

| 文件 | 改动 |
|---|---|
| `app/portfolio/board.py` | `limit_ratio` 新增 `name` / `name_of`; 补 2 条规则 + 2 个生效日常量; **删掉第 5 轮误加的"退"档**(见下) |
| `app/backtest/engine.py` | `_exec_side`/`_roll_stuck`/`_month_pnl` 新增 `name_of` 并贯通 |
| `app/paper/engine.py` | 两处调用点传当日名称 + `name_fn` |
| `app/models/daily_score.py` | 传当日名称; **裸 `round()` → `round2`**(全项目最后一处漏改) |
| `app/portfolio/monitor.py` | 传当日名称 |
| `app/portfolio/builder.py` | 传当日名称; `NameLookup` 提前构造 |
| `app/portfolio/board.py` | `limit_ratio_hist` 补 `name`/`name_of` 入参(原实现这两档恒死) |

**补的两条规则**(全部用 `stk_limit` 自身定档, 不是猜的):

1. **主板风险警示股 5%→10%, 生效日 20260706**(7,142 格)。定档证据: 5% 档最后一天
   落在 20260703(周五), 10% 档首日 20260706(周一), 142~173 只**无过渡日、无残留**同步
   切换; 创业板/科创板 ST 全程 20% 未变。原实现把「主板 ST 恒为 5%」写死。
2. **S股("S"前缀且非 ST)= 5%**(1,789 格, **全部命中无一例外**)。全市场仅存
   `600182.SH` S佳通(未完成股权分置改革), 交易所实测 ±5%。
3. **创业板退市整理期新老划断**: 改革前已进入整理期的**存量股**沿用 ±10%, 改革后才
   进入的按新的 ±20%。`300216.SZ` 千山退整理期 20200805~20200915 **跨过**改革日,
   `stk_limit` 实测全程 lu/pc≈1.1000(20200915: pc=0.19 → 上轨 0.21; 0.19×1.20=0.23)。

**⚠️ 第 6 轮: 第 5 轮加的「退市整理期 = 10%, 与板块无关」是错的, 已删除。**

第 5 轮我只看了 `300216.SZ` 千山退一只股票就下了「退 ⇒ 10%」的规则, 重跑后 2022 的
无法归因**从 0 涨到 112**、2023 卡在 154。按 (板块 × 年) 重新分组后真相是:
**退市整理期没有独立档位, 它走的就是板块常态档。**

| 板块 | 退股实测档 | 与板块常态档 |
|---|---|---|
| 主板 | 0.10 | **相同**(且"XX退"不含 ST ⇒ `is_st=False` ⇒ 天然走 `_LIMIT["main"]`) |
| 科创板 | 0.20 | 相同 |
| 北交所 | 0.30 | 相同 |
| 创业板(改革后) | 0.20 | 相同(2021+ 的 28 只无一例外) |
| 创业板(改革前) | 0.10 | 相同 |
| 创业板(跨改革日存量) | 0.10 | **不同** ⇒ 唯一需要特殊处理的一类 |

112 与 154 的逐格吻合不是巧合: 2022 年创业板退股正好 112 格, 2023 年 126(创业) + 28
(科创) = 154 格。**这两块 UNKNOWN 是第 5 轮我自己制造的**, 不是原有缺陷。
(顺带更正一处: `NameLookup.is_st_only` 的 docstring 从 2026-09-07 起就写着「退市整理期
按板块普通限幅, 不再并入 5% 档」—— 第 5 轮那条规则与项目原有设计**直接冲突**, 我当时
没读到。)

**策略影响已量化, 三类全部为零(买入侧)**: `model_score` 里「当日为 ST」的行数 = **0**
(PIT 过滤本就剔掉了); S佳通 88 期最好排名 **284**(策略买 top-6); 退市 3 只 `plan`=0。
卖侧只剩「持仓中途戴帽」这一条窄路径, 且方向保守(引擎拒卖得更早)。
⇒ **这三条修了不会让任何回测分数变好**, 所以它们不是「为了让 P1 过而进行的调参」。

**自我审查: 这算不算「看到结果之后改判据」?**
- 判据 P1/P2/P3/P4 的**文字一字未改**(改的只有上面表格里的引擎代码)。
- 两条规则各自的证据**独立于本实验**: 「142~173 只在一个周一同时切档、无过渡」这种
  结构不可能是巧合; S佳通/千山退是逐只可核的交易所公布值。
- 修引擎让分歧**消失**, 加归因桶让分歧**被解释** —— 两者都能让 P1 过。选前者是因为
  它同时让引擎更对; 但**必须承认**: 修复之后, L1 对这 2 条规则**失去了独立检验能力**
  (两边跑的是同一个 `limit_ratio`), 它们的正确性只由 `tests/test_board.py` 的单测守。
- 真正会构成改判据的动作 —— **调 atol、换分母、把 UNKNOWN 桶改宽、删掉某些年份** ——
  **一件都没做**。这三条本来就在脚本里写着「禁止」。

**第 6 轮为什么反而加强了这条纪律**: 第 5 轮我加的三条里有一条(退⇒10%)是**错的**,
它把 2022/2023 的 UNKNOWN 从低位推到 112/154 —— 即**「改引擎让分歧消失」这条路本身
也会押错方向**。如果当时我用的是「加归因桶把它解释掉」, 那 266 格错误规则就会被
永久埋进归因统计里, 而且**看起来是 P1 通过了**。第 6 轮靠的是回到 `stk_limit` 原始
分组(板块 × 年 × 档)而不是靠看复算结果, 才把它翻出来。⇒ 修复与归因都只是**待验假设**,
唯一的裁判是**独立于引擎的原始数据分组**。

================================================================================
第 7 轮（2026-09-12 晚）—— 残留 24 格定性 + 一处守卫 bug
================================================================================

**A. 残留 24 格的定性: 交易所公布文件自身的单日异常行, 不是引擎缺陷。**
证据全部**独立于引擎**(只用 `stk_limit`/`daily` 原始值与同日邻股), 三项:

1. **同日同类邻股一致性**(最强的一条)。对 21 格中的每一格, 取**当天**全部主板、
   `pre_close` 末位为 5 的股票(即同样落在 A/B 两种取整会发生分歧的价格上), 数它们
   各自跟 A 还是跟 B: **229~294 只全部跟 A, 跟 B 的 0 只**(唯一例外: 20171110 那天
   正好有两格 B, 互为对方的分母)。⇒ **该行与它自己同日的 270 只同类股票不一致** ——
   不是"交易所当时用另一套规则", 因为同一天同一批股票用的是同一套。
2. **ST 那 4 格是单日孤立**。它们的当日档位是 10%, 而**前后各 6 个交易日全部是 5%**
   (如 600721.SH: `0103:0.0501 0104:0.0501 0105:0.0503 **0106:0.0998** 0109:0.0499 …`)。
   规则变更不会只持续一天、且不改变前后任何一格。
3. **当日行情不可达**。24 格的 `high`/`low` **无一触及**引擎或交易所的任一限价
   (0/24; 见 `diag_d06_unknown.py` §7)。⇒ 这 1 分钱分歧**不改变任何一笔成交**。

补充读数: 全样本「A/B 会分歧」的格共 **807,851**, 其中 A(半进位)命中真值 **807,717 =
99.983%**、B 命中 **21**、两者都不中 **113**(113 几乎全部是已归因的
新股/复牌/ST 错位格)。21 格全落在 **2017-01~2018-09**, **2019 年及以后 0 格**;
101 格里的 4 个 ST 格两者都不中。⇒ 这 21 格的交易所值只能由"反向取整(离前收更远)"
产生, 而全市场其余 807,717 格用的是半进位。

**结论: 不新增归因桶。** P1 的判决**维持 FAIL(24)** 并如实落库 —— 把「上游文件有 24
行不可复现」说成「已归因」才是真的改判据。**但放行 L3**: 理由不是"放宽", 而是
(a) 24 行是**上游发布方的单日异常**而非引擎缺陷(上面三条独立证据);
(b) 它们对成交**零影响**(0/24 触及限价), 且**全部在 prod 评估窗(2019-07-22)之前**;
(c) **L3 臂 C(真值臂)本身就是这件事的裁判** —— 它把限价整个换成 `stk_limit` 公布值
    重跑, 若臂 C 与臂 B 读数一致, 残留无影响就是**测出来的**, 不是我说出来的。
⇒ 这是一个**在知情下做出的放行决定**, 写在这里供后来者推翻。

**B. 守卫读错列的 bug(已修)**: P4 的 pin 元组取的是**满仓线**
(`ann_return`/`max_drawdown`, prod 23.97%/−24.10%), 却拿去比 `sys_ann`/`sys_mdd`
(**系统线**, prod 20.64%/−19.66%)。两线相差 3.3pp ⇒ **原守卫必然假 FAIL**, 会把一个
好插桩判成"抄错了"。判据文字一字未改, 只是四条全比 + 期数, **约束由 2 条收紧到 10 条**。

**C. ⭐ 守卫随后抓到的**真**错: 替身把「拒单」抄成了作废, 真实现是顺延。**
P4 首跑 `FAIL`, 未过项的形状很特别: **两窗的四个回撤全部逐位对上, 四个年化全部偏高**
(prod 满仓 25.16% vs 23.97%、系统 22.65% vs 20.64%; full 13.98% vs 12.56%、13.13% vs 11.05%)。

根因: 真 `engine.py:238-243` 的涨停/跌停拒单是 **`continue`**(跳到下一个顺延日, 最多
`MAX_DELAY` 天), 而替身写成了 **`return None`(当场作废)**。后果是替身撤掉了真引擎会
**次日成交**的那些委托 —— prod 有 8 笔买单被误作废 ⇒ 少买了几只, 年化偏高而回撤不动。
修法: 逐字同构改 `continue`, 并把「被限价挡过」与「最终去向」分成两个计数器
(`BLOCKED` / `ORDER`), 否则"挡过但次日成交"的委托会被记成"成交", 丢掉限价挡单的度量。

**这条记录本身就是 P4 存在的理由**: 「零件都测过」≠「线路接对了」—— 替身是手抄的,
而手抄错的**方向恰好是"看起来一切正常"**。判据文字一字未改。

**D. 同轮第二处「legacy 臂其实不 legacy」**: legacy 分支原先只是"调用现在的
`limit_ratio` 但不传日期", 而那**不等于**修复前的函数 —— 修复前是
`return _LIMIT_ST[b] if is_st else _LIMIT[b]`, 与现在 `dstr=None` 的差别有两处:
① 主板 ST 修复前恒 5%, 现在返回 10%; ② 689xxx 修复前落 "other"(10%), 现在归 "star"(20%),
而 `689009.SH` 九号公司-WD **在 `model_score` 里**。已单列 `_legacy_ratio()` 逐字复刻旧代码。
(**不是**为了让 P4 过 —— 若补上后 P4 反而不过, 说明"基线变了"另有原因, 必须查清。)

**E. L3 结果（2026-09-12，第 3 次跑，976.0s）—— 并挖出一个 L1 结构上看不见的引擎缺陷。**

三臂（同一 `model_score`、同一窗口，唯一变量 = 限价来源）：

| 臂 | prod 85 期 满仓/系统 | full 115 期 满仓/系统 |
|---|---|---|
| **A legacy**（`_legacy_ratio`，修复前代码） | **23.97%/−24.10% · 20.64%/−19.66%** | 12.56%/−51.00% · 11.05%/−41.90% |
| **B 修复后**（推导限价） | 23.77%/−24.10% · 20.47%/−19.66% | 12.36%/−51.26% · 10.90%/−42.10% |
| **C 真值**（读 `stk_limit` 公布值） | **22.62%**/−24.10% · **19.40%**/−19.66% | **11.59%**/−51.26% · **10.16%**/−42.10% |

- **P4 = PASS**（10 式；臂 A 逐位复现旧 pin）⇒ 插桩可信。
- **P3 = PASS(构造性)** —— ⚠️ **构造性通过没有信息量**（真值臂当然不违反真值判据）。
  有信息量的读数是「臂 B 的成交里有多少笔在真值口径下是封死的」，由诊断给出（见下）。
- **总判决 = FAIL(P1)**，24 格残留（已定性为发布方异常，见 A 节）。
- **臂 A→B（限幅修复的净代价）= −0.17pp 年化 / ±0.00pp 回撤（prod）、−0.15 / −0.20（full）。**
  方向正确：旧引擎用错档位 ⇒ 关掉了「买生产买不到」的洞 ⇒ 修复后**变差**。修复不是自利。
- **臂 B→C = −1.15pp 年化 / ±0.00 回撤（prod，满仓线）、−0.77 / −0.00（full）**
  （系统线 −1.07 / −0.74）。**回撤一格未动，全在年化上** —— 这个形状本身就是线索。

**E1. −1.07pp 的完整归因：1 只股票、5 格、一个 float32 取整缺陷。**

`scripts/oneoff/diag_d67_truth_diff.py` 把两臂的「被限价挡过」集合做差：
**只被 B 挡 0 格、只被 C 挡 5 格**，且去向多重集差异只有一项 `002638.SZ sell 跌停拒单 +2`。

```
20200701 002638.SZ sell main *ST勤上  pc=2.10  close=2.00  推导dn=2.00  公布dn=2.00  C挡B放
20200702 002638.SZ sell main *ST勤上  pc=2.00  close=1.90  推导dn=1.90  公布dn=1.90  C挡B放
20200703 002638.SZ sell main *ST勤上  pc=1.90  close=1.81  推导dn=1.81  公布dn=1.81  C挡B放
20200706 002638.SZ sell main *ST勤上  pc=1.81  close=1.72  推导dn=1.72  公布dn=1.72  C挡B放
20200707 002638.SZ sell main *ST勤上  pc=1.72  close=1.63  推导dn=1.63  公布dn=1.63  C挡B放
```

**推导值与公布值逐格一模一样，臂 B 却没挡。** 唯一的差别是输入精度：

| | |
|---|---|
| `m['pre_close'].dtype`（`factor_engine.load_market`） | **float32** |
| `round2(2.10 × 0.95)`（float64，**L1 走的路**） | **2.00** ✓ 与交易所一致 |
| `round2(float(np.float32(2.10)) × 0.95)`（**引擎运行时走的路**） | **1.99** ✗ 低一分 |

`np.float32(2.10) = 2.0999999046`，×0.95 = `1.99499990 < 1.995` ⇒ `+0.5` 后 `199.99999 < 200`
⇒ `floor` 掉到 199。**`round2` 的 `1e-9` epsilon 是按 float64 标定的：在缩放单位下它只有
1e-7 分，而 float32 的表示误差是 5e-5 分 —— 小 500 倍。**

后果：引擎拿自己算出的 `dn=1.99` 去比 `close=2.00`，`2.00 <= 1.99+0.001` 不成立 ⇒ 判「卖得
出去」并以 2.00 成交。**而 002638.SZ 那 5 天是连续一字跌停，现实中根本卖不掉**（臂 C 拒绝
全部 5 天，最终 20200803 以 **1.51** 成交，−28%）。这一笔就是 −1.07pp 的全部来源。

**E2. 触发面（`scripts/oneoff/diag_float32_limit.py`，全样本 10.37M 格）：**

| 读数 | 值 |
|---|---|
| float32 让推导限价偏离 float64 公式的格 | **162,414**（1.57%） |
| 方向 | **100% 取低一分**（上轨↓162,414 / ↑0；下轨↓162,414 / ↑0） |
| 其中 **L1 说对、运行时算错** | **156,409** |
| 反向（float64 错、float32 对） | **0** ⇒ float32 路径**严格劣于** float64 |
| 后果面（限价被移动 **且** `close` 正好 = 公布限价 ⇒ 不等式真翻转） | 买 2,884 / **卖 1,058** |

方向 100% 向下不是 float32 的偏置，而是 `floor(x+0.5)` 在**十进制半整数边界**上的结构性
后果：这些格恰好是「半进位」要处理的那批，float32 的负误差把 `x+0.5` 压到整数下方 ⇒ 掉 1；
误差为正时不变。⇒ **只有向下，永不向上。**

**E3. ⭐ L1 为什么看不见它 —— 本实验最重要的一条方法论产出。**

`l1_l2`（本脚本 :338）是 `pd.read_sql` → `to_numpy(dtype="float64")` 复算的；
`_exec_side`（本脚本 :478）是 `m["pre_close"].loc[...]` → **float32**。

**同一个脚本内部，L1 和 L3 走的是两种精度。** 所以 L1 的「99.983% 吻合」是关于**公式**的
陈述，**不是**关于**引擎运行时**的陈述 —— 后者在 15.6 万格上算错。

⇒ **「元件测过」≠「线路接对了」的第二个实例**（第一个见 C 节的手抄替身）。
**L1 是必要条件、不是充分条件；真值臂（L3 臂 C）才是裁判。** 这正是三层设计里
「L1 是 L2/L3 的前提」那句话的反面：**L1 通过不蕴含引擎正确。**

**E4. 为什么本轮不修（明写，供后来者推翻）。**

修法本身很短（把 `pre_close` 量化回分，如 `round(float(pv), 2)`，即可恢复 2 位小数价格的
float64 语义）。**不修的四条理由：**

1. **它不在任何预注册判据里**。P1~P4 已判完，没有一条要求修它。
2. **改它 = 改成交口径 = 让 pin `20260911_fullhist` 失效**，按项目既定纪律必须
   **独立预注册 + 组合层两轴判决**。
3. ⭐ **在本轮修它，等于把真值臂的答案抄进引擎**。C 节自我审查里那句「看到结果后改引擎
   让它对上」是本实验明确记过的反模式 —— 上次那两条限幅规则能被接受，是因为它们各自有
   **独立于本实验的结构性证据**（142~173 只在一个周一同时切档；S佳通的公布值）。
   float32 这一条虽然也有独立复现（上表三行数值），但**它的量级完全来自臂 C**。
4. **不影响实盘**：`app/paper/engine.py:140` 从 DB 标量（float64）算限价，且它用**开盘价**
   判一字板并有 `_no_limit_day` —— 比回测更正确。**这是纯粹的回测口径缺陷**，
   代价 = 回测比实盘乐观约 1pp/年，不是资金风险。

⇒ 登记为**独立缺陷、下一轮的预注册标的**（详见 §E5）。**不删证据**：
`diag_float32_limit.log`、`diag_d67_truth_diff.log`、`D06_D07_result.json`。

⚠️ 注意改 docstring **会改脚本 sha256**（`script_sha()` 哈希的是本文件的源码）。
`D06_D07_result.json` 里冻结的是 `0d36e9401f29a71c`（第 3 次跑时）。**这不构成问题**：
docstring 的任何改动都不改变执行逻辑，而 P4 守卫在每一次真实执行时重新自证
（臂 A 必须复现旧 pin）—— 换句话说,**守卫保护的是行为, 不是哈希**。若将来要重跑并
比对结果, 先重跑一次臂 A 确认 23.97/20.64 仍在, 再谈别的。

**E5. 下一轮的现成预注册草案（本条不出自任何回测结果，是 E1~E3 的直接推论）。**

- **假设**：`_exec_side` 的限价推导应使用分精度输入（`round(float(pre_close), 2)`），
  而非 float32 矩阵原值。
- **判据（组合层两轴）**：prod 85 期与 full 115 期，满仓线与系统线的年化/回撤**四个数**
  与**臂 C（真值限价）逐位一致**。这是强判据：修复正确 ⇒ 推导 ≡ 真值。
- **P4 式守卫**：修复**前**必须先复现臂 B（23.77/20.47、12.36/10.90），否则插桩有误。
- **不接受的替代判据**：「回撤变好」「年化变好」—— 修复的正确性不由业绩判定，由
  **与真值臂一致**判定。
- **附带**：若同时接线 `no_limit_days()`（另一条独立改动），必须拆成两个 arm，否则分不清
  哪一条贡献了什么。

用法:
    ./.venv/Scripts/python.exe experiments/01_data/D06_D07_limit_fill.py            # L1+L2
    ./.venv/Scripts/python.exe experiments/01_data/D06_D07_limit_fill.py --l3       # 加跑 L3
    ./.venv/Scripts/python.exe scripts/oneoff/diag_d67_truth_diff.py                # L3 逐笔差异
    ./.venv/Scripts/python.exe scripts/oneoff/diag_float32_limit.py                 # 触发面
"""
import argparse
import hashlib
import io
import json
import sys
import time
from pathlib import Path

_ORIG_STDOUT = sys.stdout          # 必须留住引用, 否则底层 buffer 被 GC 关掉
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(_ORIG_STDOUT.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

import numpy as np                                        # noqa: E402
import pandas as pd                                       # noqa: E402

from app import db                                        # noqa: E402
from app.logger import setup_logger                       # noqa: E402
from app.models.names import NameLookup                   # noqa: E402
from app.portfolio.board import (board_of, no_limit_trading_days, limit_ratio,  # noqa: E402
                                 round2, _LIMIT, _LIMIT_ST)


def _legacy_ratio(code, is_st):
    """复刻**修复前**的 `limit_ratio(code, is_st)` —— 只给 legacy 臂(P4 守卫)用。

    修复前的函数体就是 `return _LIMIT_ST[b] if is_st else _LIMIT[b]`, 没有日期/名称。
    与现在的同名函数在 `dstr=None` 下的差别刚好两处, 都在 2026-09-12 那轮改的:

    ① **主板 ST**: 修复前恒 5%; 现在 `dstr=None` 走 `_MAIN_ST_REFORM` 分支返回 **10%**。
       不补这一条, legacy 臂在卖侧(持仓中途戴帽)就不是旧引擎。
    ② **689xxx**: 修复前 `board_of` 只认 688 ⇒ 689 落 "other"(10%); 现在归 "star"(20%)。
       689009.SH 九号公司-WD **在 `model_score` 里**, 所以这条不是理论差异。

    ⚠️ 这不是"把 legacy 臂调到能过 P4" —— 它就是把旧代码写回来。若补上后 P4 反而不过,
    那说明「基线变了」另有原因, 必须查清而不是把这两条删掉了事。
    """
    c = str(code)
    if c.startswith("689"):
        return _LIMIT_ST["other"] if is_st else _LIMIT["other"]
    if is_st and board_of(c) == "main":
        return _LIMIT_ST["main"]
    return limit_ratio(c, is_st)

LOG = Path("D:/claude-home/tmp/D06_D07_limit_fill.log")
OUT = BASE / "experiments" / "01_data" / "D06_D07_result.json"

# --- 判据常量: 冻结在预注册里, 结果出来之前不许改 -----------------------------
# P1 的归因类别。判据是「分歧全部落进这些桶」, 不是「分歧率 < X%」——
# 后者会诱使我把阈值调到刚好通过。
ATTRIB_NOLIMIT = "交易所自报无涨跌幅限制(哨兵值 up>=99999/down<=0.02)"
ATTRIB_BSE_ROUND = "北交所取整规则(floor/ceil, 非半进位) —— 对策略零影响"
ATTRIB_NEW_LISTING = "新股无涨跌幅窗口(回测缺此层, 模拟盘有)"
ATTRIB_RESUME = "复牌/退市整理期首日无限制(board.py 已披露近似)"
ATTRIB_ST_SHIFT = "ST 摘戴帽生效日错位"
ATTRIB_UNKNOWN = "**无法归因**"


class Say:
    """同时写控制台与日志文件(项目惯例: 证据落 D:/claude-home/tmp/)。"""

    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, "w", encoding="utf-8")
        self.lines = []

    def __call__(self, s=""):
        print(s, flush=True)
        self.lines.append(s)
        self.f.write(s + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


def script_sha() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


# ===========================================================================
# L1 + L2 —— 全 panel 逐格比对
# ===========================================================================
def l1_l2(say, nl: NameLookup) -> dict:
    """按年分块: 逐格复算引擎限价 → 与交易所公布值比对 → 归因 → 判定翻转。

    分块的理由是内存不是理由 —— 12.7M 格 × 6 列一次装进 pandas 约 600MB, 能装;
    分块是为了**逐年出读数**, 让任何一年的异常都能被看见, 而不是被全样本平均掉。
    """
    conn = db.get_conn()
    try:
        years = [r[0] for r in conn.execute(
            "SELECT DISTINCT substr(trade_date,1,4) y FROM stk_limit ORDER BY y")]
        if not years:
            say("stk_limit 为空 —— 先跑 scripts/oneoff/pull_stk_limit.py")
            return {}
        # 上市日(用于归因 (a)): stock_basic.list_date
        ld = dict(conn.execute("SELECT ts_code, list_date FROM stock_basic"))
        # 交易日序号(用于算「上市第几个交易日」)
        allday = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM daily ORDER BY trade_date")]
        dayidx = {d: i for i, d in enumerate(allday)}

        tot = dict(cells=0, only_limit=0, mismatch=0, ok=0)
        recs = []
        for y in years:
            q = """SELECT l.trade_date AS d, l.ts_code AS c, l.up_limit AS lu,
                          l.down_limit AS ld, d0.pre_close AS pc, d0.close AS cl
                   FROM stk_limit l
                   JOIN daily d0 ON d0.trade_date = l.trade_date AND d0.ts_code = l.ts_code
                   WHERE substr(l.trade_date,1,4) = ? AND d0.pre_close > 0"""
            df = pd.read_sql(q, conn, params=(y,))
            n_only = conn.execute(
                "SELECT COUNT(*) FROM stk_limit l WHERE substr(l.trade_date,1,4)=? "
                "AND NOT EXISTS (SELECT 1 FROM daily d0 WHERE d0.trade_date=l.trade_date "
                "AND d0.ts_code=l.ts_code)", (y,)).fetchone()[0]
            if df.empty and not n_only:
                continue

            codes = df["c"].to_numpy()
            ds = df["d"].to_numpy()
            pc = df["pc"].to_numpy(dtype="float64")
            cl = df["cl"].to_numpy(dtype="float64")
            lu_t = df["lu"].to_numpy(dtype="float64")
            ld_t = df["ld"].to_numpy(dtype="float64")

            # --- 引擎路径复算: 逐格调用 board.limit_ratio + round2 ---
            # is_st 用 NameLookup(PIT 时点名称), 与 _exec_side 里那个 is_st 回调同一实现
            ratio = np.empty(len(df), dtype="float64")
            isst = np.empty(len(df), dtype=bool)
            for i in range(len(df)):
                st = nl.is_st_only(codes[i], ds[i])
                isst[i] = st
                # 传当日名称 + name_of: S股(±5%)与创业板退市整理期新老划断只能由
                # 名称判定。**"退"本身没有独立档位** —— 它走板块常态档(见 board.py)。
                ratio[i] = limit_ratio(codes[i], st, ds[i],
                                       nl.name_at(codes[i], ds[i]), nl.name_at)
            lu_e = np.array([round2(v) for v in pc * (1.0 + ratio)], dtype="float64")
            ld_e = np.array([round2(v) for v in pc * (1.0 - ratio)], dtype="float64")

            bad_u = ~np.isclose(lu_e, lu_t, rtol=0, atol=0.005)
            bad_d = ~np.isclose(ld_e, ld_t, rtol=0, atol=0.005)
            bad = bad_u | bad_d

            # --- 归因 ---
            attr = np.array([""] * len(df), dtype=object)
            if bad.any():
                bidx = np.where(bad)[0]
                lst = np.array([ld.get(c, "") or "" for c in codes[bidx]], dtype=object)
                for j, i in enumerate(bidx):
                    d = ds[i]
                    c = codes[i]
                    # (0) 交易所**自报**无涨跌幅限制 —— 用哨兵值标注(up>=99999 / down<=0.02)。
                    # 这是数据自带的声明, 比按上市天数推算的启发式更硬, 且能覆盖
                    # 「北交所开市首日」「科创板 CDR 前 5 日」这类启发式漏掉的日子。
                    if lu_t[i] >= 99999 or ld_t[i] <= 0.02:
                        attr[i] = ATTRIB_NOLIMIT
                        continue
                    # (0b) 北交所取整规则与沪深不同: 实测上轨 floor(pc×1.3)、下轨
                    # ceil(pc×0.7)(向区间内截断, 半进位只命中 51%), 而引擎按半进位。
                    # **对策略零影响** —— model_score/daily_score 里没有任何北交所股票。
                    if board_of(c) == "bse":
                        attr[i] = ATTRIB_BSE_ROUND
                        continue
                    # (a) 上市初期无涨跌幅限制。阈值取 board.no_limit_trading_days(c)(主板/创业/科创
                    # 5 日、北交所 1 日、其它 0 日) —— 不写死 5, 否则北交所会被误归因
                    lidx = dayidx.get(lst[j], -1)
                    didx = dayidx.get(d, -1)
                    nld = no_limit_trading_days(c)
                    if lidx >= 0 and didx >= 0 and nld > 0 and (didx - lidx) < nld:
                        attr[i] = ATTRIB_NEW_LISTING
                        continue
                    # (b) 复牌首日: 前一日无成交(vol=0)但当日有成交
                    prev = allday[didx - 1] if didx > 0 else None
                    if prev is not None:
                        pv = conn.execute("SELECT vol FROM daily WHERE trade_date=? AND ts_code=?",
                                          (prev, c)).fetchone()
                        if pv is None or pv[0] is None or pv[0] <= 0:
                            attr[i] = ATTRIB_RESUME
                            continue
                    # (c) ST 错位: 前一日/后一日名称含 ST 而当日不含(或反之)
                    n0 = nl.name_at(c, d)
                    nprev = nl.name_at(c, allday[didx - 1]) if didx > 0 else ""
                    nnext = nl.name_at(c, allday[didx + 1]) if didx + 1 < len(allday) else ""
                    if ("ST" in nprev) != ("ST" in n0) or ("ST" in nnext) != ("ST" in n0):
                        attr[i] = ATTRIB_ST_SHIFT
                        continue
                    attr[i] = ATTRIB_UNKNOWN

            # --- L2 判定翻转: 分歧是否改变 _exec_side 的两个不等式 ---
            # 买: 引擎判可买(cv < lu_e-0.001) 而 真值判买不进(cv >= lu_t-0.001)
            bu_e = cl < lu_e - 0.001
            bu_t = cl < lu_t - 0.001
            sd = cl <= ld_e + 0.001          # 引擎判卖不出
            sd_t = cl <= ld_t + 0.001
            over_buy = int((bu_e & ~bu_t).sum())     # 引擎说能买, 真值说买不进 = **高估**
            under_buy = int((~bu_e & bu_t).sum())    # 引擎说买不进, 真值说能买 = 低估
            over_sell = int((~sd & sd_t).sum())      # 引擎说能卖, 真值说卖不出 = **高估**
            under_sell = int((sd & ~sd_t).sum())

            n = len(df)
            tot["cells"] += n
            tot["only_limit"] += int(n_only)
            tot["mismatch"] += int(bad.sum())
            tot["ok"] += int((~bad).sum())

            counts = {}
            for a in attr[bad]:
                counts[a] = counts.get(a, 0) + 1
            unk = counts.get(ATTRIB_UNKNOWN, 0)
            say("  %s  %8d 格(仅 stk_limit 无 daily: %6d)  分歧 %6d (%.4f%%)  判翻转 高估买 %d / 低估买 %d / 高估卖 %d / 低估卖 %d"
                % (y, n, n_only, int(bad.sum()), 100.0 * bad.sum() / max(1, n),
                   over_buy, under_buy, over_sell, under_sell))
            for a, cnt in sorted(counts.items(), key=lambda kv: -kv[1]):
                say("        %-46s %6d" % (a, cnt))
            if unk:
                say("        ⚠️ 无法归因 %d 格 —— 按预注册 P1, 这是 FAIL 条件" % unk)

            recs.append(dict(year=y, cells=n, only_limit=int(n_only),
                             mismatch=int(bad.sum()),
                             mismatch_pct=100.0 * bad.sum() / max(1, n),
                             over_buy=over_buy, under_buy=under_buy,
                             over_sell=over_sell, under_sell=under_sell,
                             attrib={k: int(v) for k, v in counts.items()},
                             unknown=int(unk)))
        return dict(total=tot, by_year=recs)
    finally:
        conn.close()


# ===========================================================================
# L3 —— 回测的实际成交率 + 真值臂
# ===========================================================================
ORDER = []          # 每次 _exec_side 调用的**最终去向**(一笔一条)
BLOCKED = []        # 每一次**被限价挡住**(涨停拒买/跌停拒卖)的事件 —— 与 ORDER 分开记,
                    # 否则「挡过但次日成交」的委托会被记成"成交"而丢失限价挡单的度量


def make_observed(truth: dict or None, fallback_counter: list, legacy: bool = False):
    """造一个 `_exec_side` 替身: **逐位复制原实现** + 记录去向 + 可选真值限价。

    `truth=None` ⇒ 限价仍由推导得出(基线臂)。
    `truth={"conn": ...}` ⇒ 限价改读 `stk_limit`; 缺格回退推导并累加 fallback_counter。

    ⚠️ **这个替身是手抄原实现的**, 手抄就可能抄错 —— 而抄错的方向恰好是「看起来一切正常」。
    所以「插桩没改变行为」不能靠读这段代码相信, 只能**实测**: 判据 P4 要求基线臂
    逐位复现 pin(prod 23.97%/−24.10%, full 12.56%/−51.00%)。P4 不过本实验作废。
    """
    from app.backtest import engine as E

    def _side(m, dates, pos, code, side, st_codes=None, name_of=None):
        n = len(dates)
        if pos + 1 >= n:
            ORDER.append(dict(code=code, side=side, why="无后续交易日"))
            return None, None, None
        why = "顺延超限作废"
        for k in range(1, E.MAX_DELAY + 1):
            dd = dates[min(pos + k, n - 1)]
            cv = m["close"].loc[dd, code]
            av = m["adj"].loc[dd, code]
            vv = m["vol"].loc[dd, code]
            pv = m["pre_close"].loc[dd, code]
            if pd.isna(cv):
                why = "无收盘价"
                continue
            if pd.isna(vv) or vv <= 0:
                why = "停牌顺延"
                continue
            # legacy=True ⇒ 复刻**修复前**的引擎(既不给 limit_ratio 传日期, 也不传名称)
            # —— 用来证明「基线跑不出旧 pin」是限幅修复造成的, 而不是我这支替身抄错了。
            _ds = dd.strftime("%Y%m%d")
            if legacy:
                if st_codes is None:
                    _ist = False
                elif callable(st_codes):
                    _ist = st_codes(code, _ds)
                else:
                    _ist = code in st_codes
                ratio = _legacy_ratio(code, _ist)
            else:
                _nm = name_of(code, _ds) if name_of is not None else None
                # name_of 兼作 name_of= : 创业板退市整理期新老划断要回查改革日名称
                if st_codes is None:
                    ratio = limit_ratio(code, False, _ds, _nm, name_of)
                elif callable(st_codes):
                    ratio = limit_ratio(code, st_codes(code, _ds), _ds, _nm, name_of)
                else:
                    ratio = limit_ratio(code, code in st_codes, _ds, _nm, name_of)
            # ---- 取限价(真值臂读 stk_limit, 否则推导) ----
            # ⚠️ 2026-09-12 第 7 轮修: 原替身在拒单时**直接 `return None`(作废)**,
            # 而真实现是 `continue`(**跳到下一个顺延日**)。P4 首跑当场抓到 —— 症状是
            # 「回撤逐位对上、年化全偏高」: 被误作废的买单在真引擎里次日成交了。
            # 这个更正**只改替身**, 不改判据; 判据 P4 存在的全部理由就是抓这种错。
            lu = ld = None
            if truth is not None:
                row = truth["conn"].execute(
                    "SELECT up_limit, down_limit FROM stk_limit "
                    "WHERE trade_date=? AND ts_code=?",
                    (dd.strftime("%Y%m%d"), code)).fetchone()
                if row is not None and row[0] and row[1]:
                    lu, ld = float(row[0]), float(row[1])
                else:
                    fallback_counter[0] += 1
                    if pd.notna(pv) and pv > 0:
                        lu, ld = (round2(float(pv) * (1 + ratio)),
                                  round2(float(pv) * (1 - ratio)))
            elif pd.notna(pv) and pv > 0:
                lu, ld = (round2(float(pv) * (1 + ratio)),
                          round2(float(pv) * (1 - ratio)))
            # 与 engine.py:238-243 逐字同构: 拒单 = continue(顺延), 不是 return(作废)
            if lu is not None and side == "buy" and cv >= lu - 0.001:
                why = "涨停拒单"
                BLOCKED.append(dict(code=code, side=side, why=why, d=dd.strftime("%Y%m%d")))
                continue
            if ld is not None and side == "sell" and cv <= ld + 0.001:
                why = "跌停拒单"
                BLOCKED.append(dict(code=code, side=side, why=why, d=dd.strftime("%Y%m%d")))
                continue
            ORDER.append(dict(code=code, side=side, why="成交", d=dd.strftime("%Y%m%d"),
                              px=cv, delayed=k - 1))
            return (cv * av if pd.notna(av) else cv, cv, min(pos + k, n - 1))
        ORDER.append(dict(code=code, side=side, why=why))
        return None, None, None

    return _side


def l3_arm(say, table, tag, start, truth, legacy=False):
    """跑一条臂; 返回 (指标, 去向统计, 回退计数)。"""
    from unittest import mock
    from app.backtest import engine as E
    ORDER.clear()
    BLOCKED.clear()
    fb = [0]
    obs = make_observed(truth, fb, legacy=legacy)
    conn_t = truth["conn"] if truth else None
    with mock.patch.object(E, "_exec_side", obs):
        m = E.run_backtest(score_table=table, run_tag=tag, start=start)
    if conn_t is not None:
        conn_t.close()
    why = {}
    for o in ORDER:
        why[o["why"]] = why.get(o["why"], 0) + 1
    blk = {}
    for o in BLOCKED:
        blk[o["why"]] = blk.get(o["why"], 0) + 1
    stat = dict(tag=tag, start=start, orders=len(ORDER), why=why, blocked=blk,
                fallback=fb[0],
                fallback_pct=100.0 * fb[0] / max(1, len(ORDER)),
                # 满仓线(ann_return/max_drawdown)与系统线(sys_*)都要收 —— pin 的两条线
                # 数值不同, P4 必须两条都比, 详见下面的修
                ann=m["ann_return"], mdd=m["max_drawdown"],
                sys_ann=m["sys_ann_return"], sys_mdd=m["sys_max_drawdown"],
                n=m.get("periods"), run=m["run_date"])
    say("  [%s] 委托 %d 笔 | 回退 %d (%.2f%%) | 满仓 %.2f%%/%.2f%% 系统 %.2f%%/%.2f%%"
        % (tag, stat["orders"], fb[0], stat["fallback_pct"],
           stat["ann"] * 100, stat["mdd"] * 100,
           stat["sys_ann"] * 100, stat["sys_mdd"] * 100))
    say("        最终去向: %s" % " ".join("%s=%d" % kv for kv in sorted(why.items())))
    say("        被限价挡过: %s" % (" ".join("%s=%d" % kv for kv in sorted(blk.items()))
                                   if blk else "无"))
    return stat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--l3", action="store_true", help="追加跑 L3 回测(耗时, 需 stk_limit 覆盖完整)")
    args = ap.parse_args()
    setup_logger()
    db.init_db()
    say = Say(LOG)
    t0 = time.time()
    say("=" * 100)
    say("D06/D07 涨停/跌停验收 —— 实验路线图阶段一硬门禁")
    say("脚本 sha256[:16] = %s   (预注册冻结在这个哈希上)" % script_sha())
    say("=" * 100)
    nl = NameLookup()
    say("\n--- L1 限价推导 vs 交易所公布值 / L2 判定翻转 ---")
    res = l1_l2(say, nl)
    if not res:
        say.close()
        return
    tot = res["total"]
    say("\n合计: %d 格 | 分歧 %d (%.4f%%) | 仅 stk_limit 有而 daily 无 %d 格(移交 D05)"
        % (tot["cells"], tot["mismatch"], 100.0 * tot["mismatch"] / max(1, tot["cells"]),
           tot["only_limit"]))
    unk = sum(r["unknown"] for r in res["by_year"])
    p1 = "PASS" if unk == 0 else "FAIL"
    say("判据 P1(分歧全部可归因): %s —— 无法归因 %d 格" % (p1, unk))
    ob = sum(r["over_buy"] for r in res["by_year"])
    os_ = sum(r["over_sell"] for r in res["by_year"])
    say("判据 P2(报数): 高估方向 买 %d / 卖 %d 格 ⇒ %s"
        % (ob, os_, "真值臂必须跑" if (ob + os_) > 0 else "真值臂预期是 no-op"))

    verdict = {"P1": p1, "P2_over_buy": ob, "P2_over_sell": os_}

    if args.l3:
        say("\n--- L3 实际成交率(核心指标) ---")
        conn = db.get_conn()
        try:
            cov = conn.execute(
                "SELECT COUNT(DISTINCT trade_date) FROM stk_limit WHERE trade_date>='20190722'"
            ).fetchone()[0]
            nd = conn.execute(
                "SELECT COUNT(DISTINCT trade_date) FROM daily WHERE trade_date>='20190722'"
            ).fetchone()[0]
        finally:
            conn.close()
        say("stk_limit 覆盖 prod 窗内 %d 个交易日 / daily %d 个 (%.1f%%)"
            % (cov, nd, 100.0 * cov / max(1, nd)))
        # ---- 臂 A: legacy = 复刻修复前的引擎(限幅不传日期)。**这是插桩守卫。**
        # 旧 pin 是用带 bug 的限幅算出来的, 所以修复后的基线**必然**不再复现它。
        # 让 A 复现旧 pin 才能证明「基线变了」是限幅修复造成的, 不是我抄错了替身。
        say("  -- 臂 A legacy(复刻修复前限幅, 应逐位复现旧 pin) --")
        legacy = {}
        for wn, st in (("prod", "20190722"), ("full", "20170101")):
            legacy[wn] = l3_arm(say, "model_score", "d67_legacy_%s" % wn, st, None, legacy=True)
        # ⚠️ 2026-09-12 第 7 轮修（**改的是读哪一列, 不是判据**）: 原 pin 元组
        # (0.2397, −0.2410)/(0.1256, −0.5100) 取的是**满仓线** `ann_return`/
        # `max_drawdown`, 却拿去比 `sys_ann`/`sys_mdd`（**系统线**）。两条线在本 pin 上
        # 相差 3.3pp（prod: 23.97 vs 20.64）⇒ 原守卫**必然假 FAIL**, 而假 FAIL 会把
        # 一个其实是好的插桩判成「抄错了」。现四条全比, 外加期数。
        # 判据文字「基线臂必须逐位复现 pin」**一字未改**, 约束由 2 条**收紧到 10 条**。
        pin = {  # 来源: backtest_summary `20260911_fullhist`(prod, 85 期)
                 #       + 控制臂 `20260911_headvt_base_full`(full, 115 期, 与 pin 逐位同)
            "prod": dict(ann=0.2397, mdd=-0.2410, sys_ann=0.2064, sys_mdd=-0.1966, n=85),
            "full": dict(ann=0.1256, mdd=-0.5100, sys_ann=0.1105, sys_mdd=-0.4190, n=115),
        }
        bad = []
        for w in legacy:
            for k, want in pin[w].items():
                got = legacy[w]["n"] if k == "n" else legacy[w][k]
                if got is None or abs(got - want) >= 5e-4:
                    bad.append("%s.%s got=%s want=%s" % (w, k, got, want))
        p4 = not bad
        say("判据 P4(臂 A 逐位复现旧 pin: 满仓/系统 × 年化/回撤 + 期数, 共 10 式): %s"
            % ("PASS" if p4 else "**FAIL — 插桩抄错了, 本实验作废**"))
        if bad:
            say("    未过项: %s" % "; ".join(bad))
        verdict["P4"] = "PASS" if p4 else "FAIL"
        verdict["legacy"] = legacy
        if not p4:
            say("  (P4 未过 ⇒ 不再往下跑)")
            say.close()
            return

        # ---- 臂 B: 修复后的引擎(限幅带日期)。**这是新的基线候选。**
        say("\n  -- 臂 B 修复后(限幅按当日历史规则) --")
        base = {}
        for wn, st in (("prod", "20190722"), ("full", "20170101")):
            base[wn] = l3_arm(say, "model_score", "d67_fixed_%s" % wn, st, None)
            lg = legacy[wn]
            say("     Δ年化 %+.2fpp  Δ回撤 %+.2fpp   (vs 修复前)"
                % ((base[wn]["sys_ann"] - lg["sys_ann"]) * 100,
                   (base[wn]["sys_mdd"] - lg["sys_mdd"]) * 100))
        verdict["fixed"] = base

        # ---- 臂 C: 真值限价。量「修复后还剩多少偏差」。需 stk_limit 覆盖完整。 ----
        if cov >= 0.99 * nd:
            say("\n  -- 臂 C 真值(stk_limit 公布限价) --")
            for wn, st in (("prod", "20190722"), ("full", "20170101")):
                tm = l3_arm(say, "model_score", "d67_truth_%s" % wn, st,
                            {"conn": db.get_conn()})
                verdict["truth_%s" % wn] = tm
                say("     Δ年化 %+.2fpp  Δ回撤 %+.2fpp (vs 臂 B; 回退率 %.2f%% ⇒ %s)"
                    % ((tm["sys_ann"] - base[wn]["sys_ann"]) * 100,
                       (tm["sys_mdd"] - base[wn]["sys_mdd"]) * 100, tm["fallback_pct"],
                       "有效测量" if tm["fallback_pct"] <= 1.0 else "**回退率过高, 不是有效测量**"))
            say("判据 P3(封板却成交 = 0): 臂 C 的成交笔全部通过**真值**限价检查 ⇒ PASS(构造性)")
            verdict["P3"] = "PASS(构造性)"
        else:
            say("\n  -- 臂 C 跳过: stk_limit 只覆盖 prod 窗 %.1f%% 的交易日, 回退率会淹没读数 --"
                % (100.0 * cov / max(1, nd)))
            verdict["P3"] = "未判(覆盖率不足)"

    say("\n" + "=" * 100)
    say("总判决: %s" % ("PASS" if verdict.get("P1") == "PASS" else "**FAIL(P1)**"))
    say("耗时 %.1fs" % (time.time() - t0))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"experiment_id": "D06_D07", "script_sha": script_sha(),
                               "verdict": verdict, "l1_l2": res},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    say("结果已写 %s" % OUT)
    say.close()


if __name__ == "__main__":
    main()
