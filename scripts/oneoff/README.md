# `scripts/oneoff/` —— 一次性脚本（实验 / 诊断 / 回填 / 验证）

> **本目录是证据，不是代码垃圾堆。** 每个脚本都对应台账里的一条结论：
> 「哪个结论出自哪次运行、哪张表、哪个脚本」是钉死的。因此：**不得删除**。
> 索引生成于 2026-09-12（审查 P2-22）；**2026-09-22 复点：当前 129 个文件**
> （128 个 `.py` + 1 个 `.ps1`；`README.md` 本身不计）。**2026-09-26 复点：139 个文件（同日再点 147：新增 `alpha_factory_gp1.py`、`gp000_combo_ab.py`、`min60_factor_gate_r2.py`、`backfill_min60_tail.py`、`llm_mine_loop_r2.py`）**（138 个 `.py` + 1 个 `.ps1`，较 09-22 新增 10 个脚本，本次含 `monthly_rule_expectancy_backtest.py`）。
> 沿革：2026-09-14 记 104 个、2026-09-18 实点仍是 104 —— 此后陆续新增，
> **该数字落后了 25 个**（外部审查当年也指出过"79"已过期，同一类漂移）。
> ⚠️ 注意本行只是**叙述性计数**，不参与机器校验：`tests/test_oneoff_hygiene.py` 守的是
> "**每个脚本都要在下面索引里**"（漏一个就红），不校验这行数字 ⇒ 数字会悄悄过期。
> 本次一并更正，并在 `_guard.py` 同类位置记下"计数类叙述不设守卫会漂移"这条教训。

## 两条硬规矩

1. **不许删。** 台账（`NEW_ECO 证据台账`，在仓库外）把「run / 表 / 模型目录 / 脚本 /
   日志 → 结论」一一钉住。删掉一个脚本 = 台账里若干条结论失去可复核的入口。
2. **不许批量搬家。** 本目录脚本用 `__file__` 推项目根，写死了「我在 `scripts/oneoff/`
   **正下方**」这个前提：43 处 `Path(__file__).resolve().parents[2]`、24 处
   `BASE = ...parent.parent.parent`、9 处同义写法（合计 76+ 处）。把文件往下挪一层
   （比如 `oneoff/archive/2026-08/`），`parents[2]` 就变成 `scripts/` ⇒ 读不到 `data/`，
   甚至**静默**造出一个假的 `scripts/data/`。同类后果还波及：`tests/test_expert.py` 按
   路径 import 本目录的冻结脚本、台账与 app/ 注释里按路径引用本目录脚本
   （`tests/test_oneoff_hygiene.py` 会把这类悬空引用测红）。

   ⇒ 要归档，**先**解决 ROOT 推导（给脚本加显式 `--base`，或改用显式定位），**再**搬；
   在那之前只能原地放着。

## 冻结脚本（被生产代码 / 测试引用：内容与位置都不许动）

判据冻结比 DRY 值钱：这些脚本的**判据在跑之前写死**，改了等于换判据。名单不靠人记 ——
`tests/test_oneoff_hygiene.py` 从 `app/` `tests/` `scripts/*.py` 与仓库根 md 里扫引用
（路径式 `oneoff/x.py` 与裸名 `x.py` 都算），再逐个断言文件确实存在。下表由同一次扫描生成：

- `ab_const_n_5v6.py` ← app/config.py, scripts/pin_baseline.py
- `ab_const_n_5v6_r2.py` ← tests/test_logger_utf8.py
- `ab_style_guard.py` ← app/config.py
- `alpha_factory_v1.py` ← app/alpha/factory.py
- `diag_allnan_samples.py` ← app/models/fusion.py, app/models/trainer.py, tests/test_fusion.py
- `diag_drawdown_attrib.py` ← app/models/regime.py
- `diag_float32_limit.py` ← app/portfolio/board.py
- `diag_style_bench.py` ← app/models/pool_index.py, app/models/regime.py
- `exp_deadzone_ic.py` ← app/alpha/gate.py, app/models/expert.py, tests/test_alpha.py 等 4 处
- `exp_die_head.py` ← app/models/trainer.py
- `exp_die_pit.py` ← app/models/trainer.py
- `exp_die_stage_b.py` ← app/alpha/__init__.py, app/alpha/factory.py, app/alpha/gate.py 等 10 处
- `exp_die_weekly.py` ← app/models/trainer.py
- `exp_fusion_router.py` ← app/alpha/bank.py, app/models/fusion.py
- `exp_gp_factory.py` ← app/framework.py, app/models/seq_die.py
- `exp_stage_b_flow_die.py` ← app/factors/factor_engine.py
- `exp_start_2019.py` ← app/config.py
- `exp_train_months.py` ← app/config.py, scripts/pin_baseline.py
- `scan_const_weight.py` ← app/config.py

> 另：``xxx.py`` 出现在引用里但**本目录并没有这个文件** —— 它是 app/logger.py 的 docstring 里
> 当示例写的**占位名**，不是脚本（同一条由 `tests/test_oneoff_hygiene.py` 的占位名白名单钉住）。

## 命名前缀约定

| 前缀 | 含义 |
|---|---|
| `exp_` | 实验：预注册判据 / 对照臂。**跑完即为证据**，不要改判据重跑 |
| `ab_` | A/B 对照（费率、持仓数、权重、训练窗、风格…），多为某轮结论的直接来源 |
| `diag_` | 诊断：查一个具体异常（NaN、float32、PIT、归因、口径差…） |
| `backfill_` | 数据回填 / 补历史（含全市场重算） |
| `migrate_` | 一次性数据迁移（起始日、打分表…） |
| `verify_` | 复刻校验（证明某次重构前后逐位一致） |
| `probe_` / `scan_` / `analyze_` / `attrib_` | 探针 / 扫描 / 分析 / 归因报告 |
| 其余 | 运维与一次性动作（`reset_paper`、`restore_ens_archive`、`power_on_framework`…） |

## 索引

（首行说明取自各脚本的模块 docstring；★ = 冻结）

-   `_conn_ctx_wrap.py` — 连接卫生改造工具(2026-09-13): 把 `conn = get_conn(); …; conn.close()` 机械改造成 `with db.conn_ctx() as conn:`（只搬文本，逐段自证"除缩进与删 close 外逐字不变"）
-   `_relaunch_service.ps1` — (PowerShell 辅助脚本)
-   `ab3_decompose.py` — ab_train_start_2019 结果分解: 按年 + 按训练窗口是否满 36 月分段
-   `ab3_ic.py` — ab_train_start_2019 的模型质量分解: 月度 rank IC(截面 Spearman), 按段对比
-   `ab_bench_total_return.py` — A/B(2026-09-19 审查 B1): 回测基准腿 价格指数 vs 合成全收益 四臂对照(生产窗 85 期 + 全史 115 期)，判据跑前冻结在 `plans/b1-total-return-benchmark-ab-20260919.md`；三道门 A0 复现/A1 不变性/A2 量级(产物 `logs/ab_bench_total_return.json`；回测行带 `20260919_b1tr_*` tag，不碰生产 run_date)
-   `ab_bench_tr_real_index.py` — A/B(2026-09-19 审查 B1-真尺): 基准腿 价格指数 `000906.SH` vs **真实全收益指数 `H00906.CSI`**(两窗×两臂)，判据跑前冻结在 `plans/b1-real-tr-index-20260919.md`(§7 记了 C1 容差更正)；C1 对等性/C2 量级带/C3 不变性(复用 `ab_bench_total_return.py` 的 run_arm/diff；产物 `logs/ab_bench_tr_real_index.json`；tag `20260919_b1real_*`)
-   `ab_alpha_panel_scale_gate.py` — 双尺门禁 A/B(2026-09-20 批次 4, Q1): 唯一差别是 `expr.build_panel` 的输出尺 —— RAW 裸价(今天的实现) vs ADJ 把 `open/high/low/close/pre_close/vwap` ×`adj_factor`；158 条 qlib158 各跑一次门禁。**只判不改**：`Factory.run(save=False)` ⇒ `alpha_bank`/`alpha_pool` 一字节不写(G4 跑前后指纹自证)。判据跑前冻结在 `plans/b1-q1-dual-scale-gate-ab-20260920.md`(G0 复现 / G1 PASS 集合 / G2 临界翻动 ≥5 条 / G3 四项构造自证 / G4 零写入)；每臂一个子进程，四跑 `--arm raw|adj|raw2|merge`（raw2 = G0① 同臂二次逐位确定）；产物 `experiments/b_stage_20260920/alpha_panel_scale_gate.{json,md}`。⚠️ 一处**有意偏离**：两臂对称打桩 `bank.check_duplicate`（否则撞车硬闸会吃掉最不受尺子影响的表达式 ⇒ 翻动数系统性偏小）
-   `audit_b_stage_first_step.py` — **只读**第一步量化(2026-09-20 批次 2): 真跑 Q4(回测 IC 窗口错开+幸存者偏差) / Q5(`PER_YEAR=52` 高估幅度) / Q6(insider 方向未证实行是否被消费) / Q8(标签口径两格进没进过打分表) / Q9(builder vs 回测可交易性笔数)；Q1/Q2/Q3/Q7 **如实标未跑**并写明原因(Q1 属阶段 B、Q7 要插桩)。门限跑前冻结在 `plans/b-stage-criteria-20260920.md`；产物**双写** `logs/audit_b_stage_first_step.json` + 被 git 跟踪的 `experiments/b_stage_20260920/`(因 `logs/` 在 .gitignore ⇒ 只落 logs 的证据会消失，本轮已发生一次)。⚠️ 同日另一版同名脚本的"九条判决表"读数已整段撤回，见 14 号三补二十九·补
-   `audit_per_year_52_sensitivity.py` — **只读**常数敏感性(2026-09-20 批次 3 Q5): 拿 F66 逐期序列 按 `F1b:422` 同一个几何式把 `PER_YEAR=52` 与实际每年期数各算一遍 ⇒ 全史池口径超额动 0.29pp(~1.6%)、留出集只动 0.03pp，逐条比「挪动 vs 线上余量」⇒ **无跨线、维持 52 不改**；产物双写 `experiments/b_stage_20260920/per_year_52_sensitivity.json`(canonical sha16 `8aeba915161c19cf`；⚠️ 本行与 14 号 1488 行原先记的 `66bf3049a59b8704` 是**两次自错修正前**那一次运行的值，重跑覆盖文件后没重记哈希 ⇒ 已更正)。⚠️ 含两次门逻辑自错(单位混、负余量算成翻档)的更正记录
-   `audit_m1_ic_scale_flip.py` — **只读**结清轮 M1(2026-09-20, Q3): 同一批锚点、同一条链上算**复权尺 vs 未复权尺**两套逐锚点 IC ⇒ 各自喂 `health.evaluate()` 数"判档翻了几次"(K)；门限跑前冻结在 `plans/ledger-settlement-criteria-20260920.md §M1`，四道实现自证门(与 `health._fwd_excess` 逐位一致 / 窗口平移会动 / 钉在 0.5×FROZEN_IC 的负对照必须抓住 ≥1)；产物 `experiments/b_stage_20260920/m1_ic_scale_flip.json`。实测 K=5/607 且全部是 healthy↔warning(⇒ `risk.apply` 不动作)
-   `audit_m4_pool_filter_bias.py` — **只读**结清轮 M4(2026-09-20): `pool_index.py` 那条 `ABS(pct_chg)<=21` 护栏把退市终点 −100% 挡在月内累计**之前** ⇒ 池等权线偏乐观，量它多大；三臂对照 V0=双滤(生产)/V1=放开深跌/V2=放开暴涨，判决只看 `monthly(V0)−monthly(V1)`；⚠️ 第一版仪器作废(两条尾巴混在一个对照 + 共用 WHERE 把 −100% 本身滤掉)，更正见预注册「更正记录」；产物 `experiments/b_stage_20260920/m4_pool_filter_bias.json`
-   `audit_m2_stuck_rollover_interest.py` — **只读+拦写**结清轮 M2(2026-09-20, Q7): 进程内包一层 `engine._roll_stuck` 记"跨月结转格·月"，把 `db.conn_ctx`/`upsert_df` 换成写拦截代理后跑两窗回测 ⇒ 算"结转占资少计的息"上界 U(pp/年)；四道门(零写入指纹前后同 / 与在册 pin 逐位同 / 响应性对照:清空=0 且翻倍=翻倍 / 非0守卫)。实测 `U=0.0269pp/年`(门槛 0.2)、两窗合计仅 **2 个结转格·月** ⇒ **关闭**；产物 `experiments/b_stage_20260920/m2_stuck_rollover_interest.json`；判据 `plans/ledger-settlement-criteria-20260920.md §M2`
-   `audit_m5_g3_channel_gate.py` — **只读**结清轮 M5(2026-09-20, 批次 5 动作①): 不重跑 158×2，只用已入库 `_mech.json` 把批次 4 的 G3-① 从"数值容差 1e-6 卡秩选均值"(前提不成立)换成**通道唯一性门**(恒等式残差/读数 ≤1e-6 + 值差 ≤1e-3 + 秩相关 ≥0.999)；可执行阴性对照=拿两臂读数差冒充残差 ⇒ 6/7 条判红、分离 2.1e5×；判决 G3-①′ **达** ⇒ 批次 4 的 G1/G2 解除"不可引用"。⚠️ 阈值在看到 `_mech.json` 已有读数之后设定 ⇒ 重判不是独立确认，此边界与判决同引
-   `track_b1_evidence.py` — **只搬运不改数**(2026-09-20 结清轮): 把承载 B1/B2 判决、此前只活在 `logs/`(被 `.gitignore:56` 忽略 ⇒ 不在版本控制内)的六份 JSON 复制进 `experiments/b1_pin_20260920/` 并生成 `MANIFEST.json`(逐份带台账引用位置)；哈希口径 = 先 `json.load` 再重序列化取 sha256[:16]，避开 CRLF/BOM 混用。`--verify` 只校验不写(不一致退出码 1，已实测能红)
-   `b1_pin_evidence.py` — **只读**证据(2026-09-20 B1 采纳轮 S0): 换 pin 前后各跑一次，留 `backtest_summary` 关键行 / `beta_250` 等 22 个价量因子指纹 / 三条读接口读数 ⇒ 机器验收 P-a 端点恒等与 P-b 特征不变；产物 `logs/b1_pin_evidence_{before,after}.json`(两份 JSON 的 `data_max` 相同才可比；`sha_scheme` v1/v2 不可混比)
-   `ab_capital_cost.py` — 资金规模 A/B 第二段: 按资金规模校准交易成本(2026-09-09 深夜)
-   `ab_capital_diversify.py` — 资金规模 × 持仓只数 A/B(2026-09-09 深夜, 头部校准诊断 D5 的引擎层验证)
-   `ab_const_n.py` — 恒定权益仓位 × 持仓只数策略(2026-09-10)
-   `ab_const_n_2x2.py` — 权益仓位 × 持仓只数 2x2 解耦(2026-09-10, 仅全窗口, 逐笔真实费用)
- ★ `ab_const_n_5v6.py` — 固定 6 只 vs 固定 5 只 —— 费用算术与两轴回测的对撞(2026-09-12, **预注册**)
- ★ `ab_const_n_5v6_r2.py` — 固定 6 只 vs 固定 5 只 · 第 2 轮 —— 修正控制臂基线后重跑(2026-09-12, **预注册**)
-   `ab_const_n_fees.py` — 恒定权益仓位 × 持仓只数 —— 逐笔真实费用口径复测(2026-09-10)
-   `ab_equity_const.py` — 恒定仓位对照(2026-09-09 深夜): regime 择时到底有没有价值?
- ★ `ab_fina_pk_ann_date.py` — PIN-A(2026-09-18, **预注册**): `fina_indicator` 主键加 `ann_date` 会不会改因子值(只读体检 + 副本库 v2 逐行比对); 默认只读, `--write` 须护栏放行 + 先备份
-   `ab_equity_weight.py` — 权益暴露映射扫描(2026-09-09 深夜): 系统线的「年化-回撤」有效前沿
- ★ `ab_paper_preclose_gate.py` — PIN-B(2026-09-18, **预注册**): 缺 `pre_close` 时封板闸门被跳过会不会改成绩; 两臂 flip 内存开关, 要求 paper 与 backtest **两侧同批改**
- ★ `ab_run_backtest_refactor.py` — P3-1(2026-09-20, **预注册**): 拆分 `run_backtest`(407 行/CC 88)的**行为不变性**对照, 判据跑前冻结在 `plans/p3-1-run-backtest-refactor-20260920.md`(A0 复现/A1 metrics 逐位/A2 月频净值逐行/A3 日频盯市逐行/A4 golden 不受扰; **不做浮点容差, 要求 Δ=0**); 回测行带 `20260920_p31_<arm>` tag, 不碰生产 run_date; 产物 `logs/ab_run_backtest_refactor_<arm>.json`
- ★ `ab_style_guard.py` — 风格防守 A/B(2026-09-10): 择时该看哪个指数?
-   `ab_train_start_2019.py` — 对照实验(2026-09-09): 训练数据起点 2017 起 vs 仅科创板上市(2019-07-22)起, 资金 2 万
-   `alpha_critic_report.py` — Alpha Critic 报告 —— 对 alpha_bank 里的候选跑 Devil/Critic 否证清单(2026-09-11)
-   `tag_text_corpus.py` — **公告语料两层打标 + 验证协议**（2026-09-27，预注册 `plans/text-factor-llm-labeling-prereg-20260927.md` 的执行体）：tier-1 = rubric 正则层（`--tag-all`，类别序=优先级序）写入 `data/text_corpus/tagged/`；`--validate` = 300 条双通道一致率（tier-1 vs tier-2 LLM 批判，强制枚举 JSON，批内→全局索引映射 + 0 基编号）。v1 77.3% FAIL → LLM 辅助修订（`llm_rubric_revise_20260927.py`，枚举/valence 守卫）→ v2 **90.3% PASS**。产物 `experiments/text_label_validate_20260927.json`。
-   `text_factor_materialize_20260927.py` — **文本因子 materialize 进生产 factor 表**（2026-09-28，预注册 §十四 步骤 1；ONEOFF_CONFIRM 护栏）：factors_v1 中性化面板 → factor 表行格式，幂等 DELETE+INSERT，写后核对因子集。执行后因 excl5 交互负效应**回滚**（text 行已删，脚本保留可复用）。
-   `text_factors_dose_20260927.py` — **文本因子剂量稳健性检验**（2026-09-28，预注册 §十一 的执行体）：D1=31+text_n1m / D2=31+text_novel3m / 31+2@第三种子 SC，六次全量重训 + 14 次回测。结果：D1 prod **+7.30pp**、D2 +1.24pp、31+2@SC +4.25pp——31 基础上剂量全过 ADOPT_PACKAGE=True；但 excl5 基础上交互负效应 ⇒ 采纳撤回（§十五）。产物 `experiments/text_factors_dose_20260927.json`。
-   `text_factors_eval_20260927.py` — **公告文本因子族构造+门禁读数**（2026-09-27，预注册 `plans/text-factor-llm-labeling-prereg-20260927.md` §七 的执行体）：五族七因子（text_n1m/n3m/n6m、netflow3m、earn3m、novel3m、gap），PIT=公告时间≤锚点，生产口径中性化；gate.evaluate + verdict_single（S4 与 31 池+族内去相关）。结果 2/7 PASS（text_n1m −0.38%/月 t=−2.36、text_novel3m −0.33% t=−2.72，方向均为拥挤反向、与池 ρ≈0.14）。产物 `experiments/text_factors_eval_20260927.json` + `data/text_corpus/factors_v1.parquet`。
-   `text_factors_ab_20260927.py` — **31+2 文本构型 A/B 闭环**（2026-09-27，同预注册 §九 的执行体）：四臂全量重训（ctrl 31 / trt 33 × 双种子集）→ 打分层 → 组合层。结果：打分层 FAIL（Δ +0.04pp/月）但组合层 **verdict_portfolio PASS**（prod Δsys_ann +2.24/+3.95pp 双同向、回撤改善）——按 §九 打分层 FAIL ⇒ 线结清登记，同时登记"打分层 head 双向不可预测"的判据冲突（交用户裁定）。产物 `experiments/text_factors_ab_20260927.json`。
-   `llm_text_rubric_20260927.py` — **公告分类 Rubric 设计（LLM）**（2026-09-27，预注册 `plans/text-factor-llm-labeling-prereg-20260927.md` 的执行体；方案 A 第三步）：采样 200 条已回填真实标题 → deepseek 设计公告分类体系（两层打标的 rubric 层；LLM 只做分类/抽取不输出交易方向）。产出 14 类枚举 + regex_hints + valence + 模板规律/抽取字段/歧义指引（`data/text_corpus/rubric_v1.json`）。工程坑：`_llm_cfg` 依赖 `app.config` 导入链触发 load_dotenv。产物 `experiments/llm_text_rubric_20260927.json`。
-   `diag_nf50_redundancy.py` — **新过线 50 因子冗余诊断**（2026-09-27，只读；nf 构型组合层崩塌的机制支撑）：逐锚点秩相关（46/71 锚点子样本）测 ①50 新因子内部冗余（mean |ρ| 0.172、max 1.0、90% 方差仅 25 有效维——S4 只对 31 池测去相关、候选彼此没测，蜡烛/CORD/m60 等家族以独立名额成批过闸）；②与保留 8 / 剔除 23 的相关（均 0.126——新 50 不是旧信息影子，是另一层弱微结构信号，max 影子 BETA10–bias_20 0.804）；③家族内冗余表（cord 0.507/m60 0.530/beta_resi_rsv 0.632）。产物 `experiments/diag_nf50_redundancy_20260927.json`。
-   `probe_text_corpus_20260927.py` — **文本语料源可行性探针**（2026-09-27，预注册 `plans/text-corpus-probe-prereg-20260927.md` 的执行体；方案 A 第一步）：四问（可及性/覆盖度/对齐性/体量）逐源 try/except。结果 = 巨潮个股公告 `stock_zh_a_disclosure_report_cninfo` **PASS**（000001 自 2019-01 起 730 条，代码+公告时间戳+标题链接，单票 7.5 年 7.8s）；东财新闻（akshare 正则 bug）/全市场公告（站点改版）/财联社电报（接口移除）/tushare news（积分墙）全败如实记录。只读，零写库；产物 `experiments/text_probe_20260927.json`。
-   `portfolio_admission_gate.py` — **组合层级准入判决器**（2026-09-27，预注册 `plans/portfolio-admission-gate-prereg-20260927.md` 的执行体）：门禁第三层（六判据门 → 单因子档 → **组合层档**）。给成对评分表（`--ctrl/--cand` 前缀，展开 `_sa/_sb` 双种子集）跑生产路径回测双窗（prod=eval_start / full=全史，单一变量=评分表），算 Δ 读数调 `gate.verdict_portfolio` 机械判决：P1 收益（双种子集一致 ≥ +2.0pp/+1.0pp，阈值依据种子噪声实测 ±3.5pp）、P2 回撤（劣化 <2pp/3pp）、P3 换手（≤ +5pp）；判决词 PASS(进拍板)/REJECT(线结清)/INDETERMINATE(读数不可辨)。只读生产 config，回测留痕 `padm_` 前缀不自清；产物 `experiments/padm_<tag>_<date>.json`。
-   `fullretrain_nf_ab_20260926.py` — **新因子构型全量重训 A/B**（2026-09-26/27，预注册 `plans/nf-fullretrain-prereg-20260926.md` 的执行体；用户拍板"剔除未过线+加入过线"）：四臂同进程 = 控制臂 31 因子（factor 表）× 实验臂 58 因子（保留 8 + bank 过档 50，npz 面板生产口径 neutralize + 参考宇宙对齐守卫）× 双种子集 [42,7,123,2024]/[5,11,77,999]。`trainer.run(factors_override=)` 实验缝，生产表/ens 只读；`--judge-only` 免重训补判读。产物 `experiments/nf_fullretrain_ab_20260926.json`。
-   `nf_ab_backtest_20260927.py` — **新因子构型组合层 A/B**（2026-09-27，同预注册 §五）：四张实验评分表 × 生产路径回测 × 双窗，单一变量=评分表。判决：Δsys_ann −16.78/−9.76pp 双种子集同向为负、回撤劣化 ~15pp ⇒ ADOPT_PACKAGE=False 线结清（"门禁 PASS≠可用"第 6 例）。产物 `experiments/nf_ab_backtest_20260927.json`。
-   `gp000_combo_ab.py` — **gp_000 组合层判决 A/B**（2026-09-26，预注册 `plans/gp000-combo-prereg-20260926.md` 的执行体）：机制复制自 Phase 2 `ab_factor_expansion.py`（同种子 [42,7]/同 36 月窗/同 21 日行业中性标签），臂 A 复用其 OOS 打分缓存（运行时断言行情未前进），臂 B = 31+gp_000（**被判决的那一版** `alpha_pool/gp_000.npz`）；判据 = 全史 ΔIC>0 且 eval ΔIC>0 且 全史 Δtop6>0 三条同时；诊断 = gp_000 与 31 因子的逐月截面 ρ（增量 vs size）。只读研究层、零写库；产物 `experiments/gp000_combo_ab_20260926.json`。
-   `single_factor_retest.py` — **单因子档因子重测**（2026-09-26，预注册 `plans/single-factor-gate-prereg-20260926.md` 的执行体；用户拍板'改门禁适合单因子测试'）：单因子档判据 S1(|head|≥0.3%)/S2(|t|≥2)/S3(同号)/S4(与现役 |ρ|max≤0.7, top6 与 p_adj 取消)。批①=31 在池因子全量过档（库健康度官方重测，与生命周期仪表盘互证）；批②=bank 全部有读数候选按新档重判（npz 面板在者现算去相关 ρ，无面板者未测不否决）。只测量分级不决策——剔除/组合 A/B 各自另立预注册。产物 `experiments/single_factor_retest_20260926.json`。
-   `s5_decay_retest_20260927.py` — **S5 时效轴（IC 半衰期）重测**（2026-09-27，预注册 `plans/s5-half-life-prereg-20260927.md` 的执行体；创新路②）：执行延迟衰减曲线 `gate.ic_by_lag`（lag 0..5 锚点步数，"信号晚 L 期执行还剩多少边缘"）→ 机械半衰期 `gate.half_life_from_curve` → 判据 HL≥2×调仓周期（封杀 M1 型"存在性显著但衰减快于执行"假阳性）。范围=31 池因子全量（HL 分布=bar 校准读数）+ bank 过档 50；判定区 DEAD、ACTIVE 只记读数；内置 8 池过档因子 IC_0 vs evaluate 逐位交叉校验（tol 1e-9）。只测量分级不决策——不重写 20260926 重测官方判决，产物给"若并入 S5 将翻转的名单"；S5 正式并入判据序列待拍板。产物 `experiments/s5_decay_retest_20260927.json`。
-   `crowding_meter_probe_20260927.py` — **关注度型拥挤度量器探针**（2026-09-27，预注册 `plans/crowding-meter-probe-prereg-20260927.md` 的执行体；创新路①）：KG 家族 18 块 npz（机械枚举 kg_*+llm_kg_*，不挑拣）等权截面 z ⇒ crowding_score（C1 全家族 / C2=kg_broker_rec_3m 经典单组件）。三问：R1 拥挤度与价格体制（DEAD/ACTIVE）正交性（强共线则"第二体制轴"被否）；R2 核心=31 池因子逐锚点 IC 按区内拥挤度中位数分桶，ΔIC 跨因子汇总（median<0 且 t≤−2 且负向≥2/3 ⇒ 进 zone_of 扩展预注册）；R3 量器自身读数（sanity，非发现声明，家族已证伪不翻案）。只读零写库。产物 `experiments/crowding_meter_probe_20260927.json`。
-   `hl_holding_probe_20260927.py` — **创新③ 衰减感知持有期探针**（2026-09-27，预注册 `plans/hl-holding-probe-prereg-20260927.md` 的执行体）：纯读数器，从 S1-S5 官方重测曲线推 G(H)=mean(IC_0..IC_{H−1})、盈亏平衡单边成本 c*(H)=(IC_0−G(H))/(1−1/H)（参考成本=config `single_side_cost`=0.003）。R1 池 viable 占比 ≥1/3 ⇒ 存在可行延长持有层；R2 冻结预测 HL≥4 组 viable 占比应 > HL≤2 组（验证②读数的决策价值）；R3 分层候选名单（读数非判决，生产分层须组合层 A/B+拍板）。只读零写库。产物 `experiments/hl_holding_probe_20260927.json`。
-   `holdperiod_ab_20260927.py` — **创新③ 持有期组合层 A/B**（2026-09-27，预注册 `plans/holdperiod-ab-prereg-20260927.md` 的执行体）：engine 增量缝 `anchor=2month_even/odd`（月末锚点每隔一个=持有 2 个月，双相位防相位脆弱；默认 month 逐位不变）。四臂双窗：A=现役复现校验（vs nf ctrl_sa=pin 在册）、B1/B2=2 月持有；判据=机制校验（ppy≈6+换手降 ≥40%）→ P1 双窗 Δexcess 同向为正 → P2 Δmdd 劣化 ≤2pp → P3 相位同号。只读，run_tag=hpab_ 留痕。产物 `experiments/holdperiod_ab_20260927.json`。
-   `eb_bank_reverify_20260927.py` — **创新④ JKE 式贝叶斯复验第一轮（EB 有效概率排序）**（2026-09-27，预注册 `plans/eb-bank-reverify-prereg-20260927.md` 的执行体）：两组模型——先验=E 段（除最后 12 锚点）动物园经验分布（f0=N(0, MAD 尺度)、π0 Storey 式、f1=N(μ̂,·)），证据=前瞻段 t_F，后验 eff_prob。三问：C1 过档组/拒绝组 eff_prob 分离度、C2 与 S5 HL 的冗余度（应正交互补）、C3 eff_prob≥0.9 名单（读数非判决）。前瞻验证=月链累积新锚点后复跑（登记待办）。只读零写库。产物 `experiments/eb_bank_reverify_20260927.json`。
-   `excl_pool_train_20260927.py` — **池剔除 A/B 训练体**（2026-09-27，预注册 `plans/excl-pool-ab-prereg-20260927.md` 的执行体）：六臂同进程同日同环境全量重训（ctrl=31 / min=29=31−{S5 翻除} / gate=仅 S1-S5 过档 6 个，臂机械派生自官方重测产物不手工挑拣；双种子集 sa/sb；参数与 nf 先例逐字一致）。B 臂走 `factors_override=子集面板`（factor 表已中性化，不再处理）。隔离表 excl_*/exclimp_*，生产表只读。判决由 `portfolio_admission_gate.py --ctrl excl_ctrl --cand excl_min/excl_gate` 另做（padm_ 产物）。产物 `experiments/excl_pool_train_20260927.json`。
-   `sleeve_bank_screen_20260927.py` — **周频袖套 × bank 过档因子筛选**（2026-09-27，预注册 `plans/sleeve-bank-screen-prereg-20260927.md` 的执行体；用户指令'开始实践，直至有判据'）：bank S1-S5 过档 39 → 机械分域（qlib158 24 经自有表达式引擎逐字求值 + margin/mf 4 经 flow_factors 日频面板周锚点取样 + 后置 11 未测）。V0 验证门：实现 vs 官方 npz 在重叠月锚点的中性化截面 Spearman 中位 ≥0.99（npz 存中性化值——首跑 raw-vs-neutral 28 全灭的教训在册）。三道门 G1 时效（HL≥2 周锚步）/G2 强度（|IC0(5d)|≥0.02）/G3 族增量（vs 袖套 31 |ρ|max≤0.7）。判决=机械：15 个过门（CORD/IMAX/IMXD/KLEN/KLOW/KUP/CNTP5/VSUMP60/VSUMD60，全 qlib158 价格形态族）⇒ 袖套 A/B 候选名单；margin/mf 5 日强度不足（G2 线结清）；G1 周粒度偏保守的注记在册。只读零写库。产物 `experiments/sleeve_bank_screen_20260927.json`。
-   `sleeve_ab_20260927.py` — **周频袖套 A/B（31 vs 31+15）**（2026-09-27，预注册 `plans/sleeve-ab-prereg-20260927.md` 的执行体；用户指令'继续直至完成'）：两臂同种子 [42,7] 同锚点走袖套自有 `scores.train`（月度重训/5 日标签/50 轮），Arm B 追加二十三补的 15 个过门候选。组合层 = 配对简化模拟器（top-100/band=2/t+1 开盘/双边 30bp，两臂同规则 ⇒ 配对 Δ 有效；生产采纳须 replay 全保真验证）。判决=机械：**INDETERMINATE**（配对 Δ +0.58bp/期 t=0.20 不显著；MDD 改善 3.6pp、换手 +6.2pp 均在容差内）——经济量级可忽略，袖套维持 31 因子，季度复跑待新数据。只读零写库。产物 `experiments/sleeve_ab_20260927.json`。
-   `volscale_overlay_20260927.py` — **月频波动率缩放层 A/B**（2026-09-27，预注册 `plans/volscale-overlay-prereg-20260927.md` 的执行体；两频综述创新 1）：在 pin 双窗日线 NAV 上做逐期线性缩放叠加（w=clip(σ_base/σ_est, 0.5, 1.0)，σ_est=63 日年化波动、σ_base=252 日滚动均值，全 real-time 只减不加；现金 0 保守）。判据：P1 MDD 改善 ≥5pp（一窗达标另一窗 ≤2pp 劣化）/P2 年化非劣 ≥−1.5pp/P3 触发覆盖 15-60%。判决=机械：**INDETERMINATE**（MDD 改善 prod +4.4pp/full +2.8pp 双窗为正，prod 差 0.58pp 未达门槛；年化非劣 ✓；覆盖 49%/55% ✓）。事故在册：v1 runner improvement 符号写反（首判 REJECT 有误），修正后改判。**延长观察：季度复跑，两窗 MDD 改善持续 ≥5pp 再议生产接线**。只读零写库。产物 `experiments/volscale_overlay_20260927.json`。
-   `costaware_smooth_20260927.py` — **成本感知评分平滑（λ-收缩）A/B**（2026-09-27，预注册 `plans/costaware-smooth-prereg-20260927.md` 的执行体；两频综述创新 2）：生产 model_score 逐月秩百分位 → S_used=(1−λ)p_t+λ·S_used(t−1)（换手正则一阶实现，链断以新分重启保全宇宙），λ∈{0.3,0.5} → 隔离表 → 生产引擎双窗回测（含 chase）。判据：P1 换手 ≤对照×0.80 / P2 excess ≥对照−1.5pp 双窗 / P3 MDD 劣化 ≤2pp。**事故在册**：v1 EMA 链断裂 ⇒ 宇宙塌缩至 55%，首判无效；修正后判决=机械 **REJECT 线结清**——λ=0.3 换手 −27%/−24% 达标、全史窗 excess +1.95pp 且 MDD 改善 10.8pp，**但 prod 窗非劣失败（−2.49pp）**：平滑在 2017-19 段有效、近年有害（体制依赖再现）。重开 = 新预注册（细网格 λ∈0.1-0.2 + 体制条件设计）。只读生产表；隔离表自建。产物 `experiments/costaware_smooth_20260927.json`。
-   `sleeve_factormom_ab_20260927.py` — **周频袖套 · 因子动量/分块倾斜 A/B**（2026-09-27，预注册 `plans/sleeve-factormom-prereg-20260927.md` 的执行体；两频综述创新 3）：Arm B 在每个重训点动态剔除'冷'分块（orientation=训练窗全段块 IC 方向、trailing=最后 12 锚点、oriented trailing<0 判冷、每点最多剔 2 块；分块=weekly/blocks.py canonical）。组合层=24 补配对简化模拟器。判决=机械 **REJECT**：配对 Δ **−9.91bp/期（t=−2.13）显著为负**——'冷'块剔除后反弹（因子层面均值回归/whipsaw），动量外推在月度重训粒度反向。机制活动度：44/94 重训点触发（B5 22 次/B4 17/B3 13/B1 5/B2 2）。判读：简单因子择时第三次失败（本次 + 择时三证伪 + 平滑 REJECT），因子动量文献的批评派在本仓复现；该轴线结清。只读零写库。产物 `experiments/sleeve_factormom_ab_20260927.json`。
-   `llm_rubric_revise_20260927.py` — **公告分类 rubric 修订器**（2026-09-27，文本线；labeling prereg §五）：LLM 辅助修订 regex_hints（79→122 条）+ 优先级重排，**枚举名与 valence 由 v1 权威合并、守卫拦截越界改动**。迭代留痕 `rubric_v1.json.bak_v1`/`rubric_v2.json`。产物 `experiments/llm_rubric_revise_20260927.json`。
-   `text_factors_eval_20260927.py` — **公告文本因子族构造与单因子档评估**（2026-09-27，文本线；labeling prereg §七）：打标产物 → 7 因子面板（事件强度/新颖度/净流/业绩预告/稀缺性族）→ 门禁家族口径评估。产物 `experiments/text_factors_eval_20260927.json`。
-   `text_factors_ab_20260927.py` — **31+2 文本因子构型 A/B**（2026-09-27，文本线；labeling prereg §九）：双种子集四臂（ctrl=31/trt=31+2），打分层 + 组合层档（verdict_portfolio）双判据。隔离表 nfab2_*/nfimp2_*。产物 `experiments/text_factors_ab_20260927.json`。
-   `txt2_excl_train_20260927.py` — **26+2 vs 26 文本因子增量 A/B 训练体**（2026-09-27，预注册 `plans/txt2-excl-ab-prereg-20260927.md` 的执行体）：把文本线的组合层 PASS 翻译到现生产基座——处理臂 = 26 因子（excl_min 同源同构）+ text_n1m + text_novel3m（factors_v1 留档 → neutralize 稀疏守卫）= 28 因子，双种子集同日重训；对照 = excl_min_sa/sb（已验证）。隔离表 txt2_trt_*/txt2imp_*。判决由 portfolio_admission_gate.py --ctrl excl_min --cand txt2_trt 另做。产物 `experiments/txt2_excl_train_20260927.json`。
-   `pead_factor_eval_20260928.py` — **PEAD 因子族评估（激活执行体）**（2026-09-28，预注册 `plans/pead-factor-prereg-20260927.md` 激活后跑数；两频综述处方 1）：打标语料（category×valence）→ 事件强度 E=Σ valence_signed（14 类 × 漂移窗 21/42/63 交易日，交易日历锚定 cumsum 差分）= 42 候选 → 门禁家族口径（neutralize 稀疏守卫/PIT/naive 21 日标签）→ 六判据门（含 churn_cost 真实费率成本）+ 单因子档 S1-S4。只读零写库。产物 `experiments/pead_factor_eval_20260928.json`。
-   `pead_s5_readings_20260928.py` — **PEAD 因子族 S5 时效轴补测**（2026-09-28，三十三补·续；pead-factor-prereg §三 S1-S5 的 S5 收尾）：42 候选面板重建（**稀疏守卫必须走 neutralize_sparse**——首跑漏守卫面板全行归零读数无效）→ `ic_by_lag`（lag 0..5 月锚步）→ `half_life_from_curve`（bar 2.0）。读数：21 通过/3 判否/18 未测——事件强度因子大多时效达标，结合 S1-S4 0/42 ⇒ 组合判据维持线结清。产物 `experiments/pead_s5_readings_20260928.json`。
-   `kline_entry_probe_20261001.py` — **K 线择机入场探针**（2026-10-01，预注册 `plans/kline-entry-timing-prereg-20261001.md` 的执行体；用户 sequenced 路径第二项）：选股流重建（model_score→PIT 域→chase→top-6 行业帽/top-20）+ 入场日族 k=0..10（t+1 开盘 vs 第 k 交易日收盘）→ **oracle 包络判死门**（逐股票-月 max_k 组合均值 = K 线规则族严格上界，<bar 则 DEAD 条件臂不跑）→ 冻结条件臂（锚点日收跌⇒k=0，否则首个收跌日收盘，死线 D=10）。**只读零写库**（无 danger-guard 义务）。产物 `experiments/kline_entry_probe_20261001.json`。
-   `trough_tilt_20260930.py` — **低谷加仓探针 A/B**（2026-09-30，预注册 `plans/trough-tilt-prereg-20260930.md` 的执行体；L3 拥挤约束 REJECT 的镜像假设，用户指令 sequenced 路径第一项）：触发器逐字复用 L3 B1（激活月 2024-06/07，覆盖 2.2%）⇒ 激活月两臂——T1 强镜像（候选池只留流通市值最低五分位）/ T2 弱镜像（剔最高五分位），评分表层隔离表 trough_t1/trough_t2 → 生产引擎双窗。对照同 session 重跑（pin 20260928_exe 复现守卫，硬闸）。bar 先标定后冻结：C1=+0.44%/年（激活锚点前瞻收益为负=相对韧性非 V 反弹），bar=max(0.30×C1,+0.50pp)=+0.50pp/年；P1 Δexcess≥bar 双窗同向/P2 ΔMDD≥−1pp/P3 覆盖≥2%。判决词由脚本机械产出，拍板由人。产物 `experiments/trough_tilt_20260930.json`。
-   `crowding_cap_20260928.py` — **拥挤度暴露约束 A/B**（2026-09-28，预注册 `plans/crowding-cap-prereg-20260928.md` 的执行体；两频综述 L3）：B1(规模/流动性)块 IC 序列 → 冷判定（recent 6 月×sign(long 24 月)<0 且 |long|≥0.01）→ 连续 2 期冷 ⇒ 下月候选池剔除流通市值最低五分位（评分表层：model_score 激活月删行 → 隔离表 crwd_cap → 生产引擎双窗回测）。判据：P1 MDD 改善 ≥2pp(prod)/P2 年化非劣 ≥−1.5pp/P3 激活占比 5-40%。判决由对照 pin 在册读数（同表同引擎）。隔离表 crwd_cap。产物 `experiments/crowding_cap_20260928.json`。
-   ★ `text_factor_materialize_20260927.py` — **文本因子 materialize 进生产 factor 表**（2026-09-28，公告文本线交接件；labeling prereg §十四 步骤 1）：text_n1m/novel3m 写入生产 factor 表（33 因子），自带 `_guard.require_confirm` 默认拒绝。**⚠️ 未执行**：26+2 vs 26 A/B（padm_txt2pead）已判 REJECT（文本因子在现 26 因子基座 −2.7pp/年）——materialize 与该判决冲突，是否执行待用户拍板。运行前必须 backup_db.py。
-   `text_factors_dose_20260927.py` — **文本因子剂量-响应诊断**（2026-09-27，文本线）：因子暴露剂量的收益响应读数（只读诊断）。
-   `llm_mine_loop_r3.py` — **LLM 自主挖因子循环 r3**（2026-09-26，预注册 `plans/llm-mine-loop-r3-prereg-20260926.md` 的执行体；机制依据全球检索综述 `plans/llm-mining-survey-20260926.md`——Hubble 2604.09601 / AlphaAgent 2502.16789）：生成端约束（受控词汇表+合法/非法样例）+ **AST 三层校验器**（变量/算子白名单+arity+复杂度，错误分类进迭代 2 反馈）+ **去相关预筛**（与 31 因子 |ρ|max>0.7 标记影子）+ KG 降噪域平行读数。主判据 = 静态失败率 ≤40%（r2=85%）；门禁六判据照旧。产物 `experiments/llm_mine_loop_r3_20260926.json`。
-   `diag_gate_placebo.py` — **门禁安慰剂检验**（2026-09-26，用户指令'先核实门禁合理性'；只读）：150 个安慰剂因子（iid 随机 100 + size 逐锚点截面重排 50——保分布、去预测力）过两道门量假放行率。结果：**集成门与单因子档的假放行率均为 0/150**（安慰剂 max |head| 0.24% < S1 的 0.3%、max |t| 3.0 < 集成门隐含 3.7）——两道门的阈值都被噪声极值自然校准，50/231 的单因子过档者不是噪声漏入。产物 `experiments/diag_gate_placebo_20260926.json`。
-   `diag_gate_calibration.py` — **门禁校准诊断**（2026-09-26，（用户问询“门禁是否不合理”；只读）：把**现役生产打分**(model_score)与最强现役因子(size/rev_5/turnover_20/ep)放进同一道门禁(gate.evaluate+PIT 域+naive 标签)量读数，并给出 p_adj 隐含 t 门槛随分母的漂移表(N=7→2.69 … N=249→3.72)。结论：在产信号轻松过门(死区 head +1.05%/月 t=5.21)⇒ bar=在产强度、校准合理，0 PASS 挖掘史=候选真弱。产物 `experiments/diag_gate_calibration_20260926.json`。
-   `min60_factor_gate_r2.py` — **min60 异构口粮变换族门禁轮 r2**（2026-09-26，预注册 `plans/min60-gate-r2-prereg-20260926.md` 的执行体）：数据族 `data/min60_derived/daily.parquet（x1 尾盘成交占比/x2 尾盘收益/x3 收盘对日 VWAP；全市场 5,444 只、2020-01 起），r4 冻结净化后 12 条滚动/交互/反号变换面板经 Factory → gate 六判据（分母 12）。先验：日值基列 F75 0/3 反向显著低于/贴 head 门槛 ⇒ 0 PASS 为基准预期；PASS 的组合层第一问 = 增量 vs rev_5/size。只读生产表；产物 `experiments/min60_factor_gate_r2_20260926.json`。
-   `llm_mine_loop_r2.py` — **LLM 自主挖因子循环 r2 + KG 降噪读数**（2026-09-26，预注册 `plans/llm-mine-loop-r2-prereg-20260926.md` 的执行体）：两迭代自主闭环——迭代 1 读【31 因子+DSL+KG 25 列字典+失败知识库+拥挤目录】提 ≤10 条（kind=expr/kg 机械变换，每条带 noise_hypothesis），迭代 2 读迭代 1 全部读数后修正方向；全部候选过 factory 标准六判据（分母 2×提案数，双域保守），另测 **KG 降噪域平行读数**（死区剔 kg_resonance+kg_pagerank_ind 拥挤前十分之一后的 head 对比，报告用不参与判据）。LLM 只离线提案不进生产链路（不变量 #4）；0 PASS 为基准预期。产物 `experiments/llm_mine_loop_r2_20260926.json`。
-   `backfill_min60_tail.py` — **min60 尾部增量刷新**（2026-09-26，09 号数据族登记的待办执行体）：★冻结脚本 `backfill_baostock_min60.py` 的 import-only 伴生 runner——复用其取数/重登/时窗原语，加新鲜度门控（`done.last` < 最新交易日的股才重拉，修正按股 done 终结语义）与**账户级**请求预算（baostock 日额度账户共记：启动时种入原 checkpoint 的 `req_today`，自设上限 2 万 < 5 万硬限，串行+0.3s）。只写 `data/baostock_min60/*.parquet` + 自有 checkpoint；`min60_derived` 重建待办（生成脚本在仓库外，重建须先重叠窗逐位比对）。自有断点 `data/.baostock_min60_tail.json`。
-   `alpha_factory_gp1.py` — **GP 首次真实试挖**（2026-09-26，预注册 `plans/gp-mining-prereg-20260926.md` 的执行体）：`Factory → GPProposer(60×15=900) → declare → run`，适应度只在死区(2020-09~2023-12)+PIT 可交易域（与门禁同一个量）；Pareto 前沿全员走 gate 六判据并入 `alpha_bank`。**本轮是 bind_factory 修复(09-13)后 GP 第一次真实运行**；`--smoke` 为 save=False 接线验证（不入册不计数）。判据与出口声明见预注册：PASS=POOLED=资格非可用；成功定义 = 跑通入册 + 分母永久入账，不是挖出 PASS。产物 `experiments/alpha_factory_gp1_20260926.json`。
- ★ `alpha_factory_v1.py` — Alpha Factory v1 —— 芯片式架构的因子产生层首跑(2026-09-11)
-   `analyze_market_2019.py` — A 股 2019-07-22(科创板开市)起全景解析 —— 为模型改造提供事实基础
-   `analyze_model_attrib.py` — 模型归因 + 简单因子组合对照(2026-09-11) —— 「深度改造」的靶子在哪
-   `attrib_final_stats.py` — 归因补充(2026-09-09): 生产基线的统计强度 + ST 过滤 A/B 的逐月拆解
-   `attrib_report.py` — 归因报告(2026-09-09): 回测年化从 38.51% 跌到 5.94% 的逐项拆解
-   `attrib_st_filter.py` — 归因(2026-09-09): 回测年化从 38.51% 掉到 9.61%(ab0) 的主因 —— ST/退 PIT 过滤
-   `audit_alpha_panel_adj.py` — **只读**量级实测(2026-09-19 审查 B2): alpha 工厂的 `build_panel` 用**未复权价**、门禁标签用**复权价**，158 条里 133 条踩价格水平；真实引擎两臂对比横截面秩相关(产物 `logs/audit_alpha_panel_adj.json`，**非判决**、未重跑门禁)
-   `audit_bench_total_return_gap.py` — **只读**量级实测(2026-09-19 审查 B1): 回测基准腿取 `index_daily.close`(价格指数) 而持仓腿取 `close×adj`(全收益) ⇒ `excess_ann` 被抬高一个基准股息率；用 `index_weight`∩`dv_ttm` 市值加权估抬高量(产物 `logs/audit_bench_total_return_gap.json`，**非判决**、未改口径未换 pin)
-   `backfill_full_market.py` — 一次性回填: 全市场扩展(2026-09-07) —— 新纳入板块个股的完整历史行情
-   `backfill_full_market_fina.py` — 一次性回填: 全市场扩展(2026-09-07) —— 新板块个股的财务指标与历史名称
-   `backfill_lhb.py` — 一次性回填: AkShare 龙虎榜 2015-01 至今 (约 2800 交易日, 预计 15~30 分钟)
-   `backfill_lhb_fullmarket.py` — 一次性回填: 全市场扩展(2026-09-08) —— 龙虎榜全市场历史
-   `backfill_new_sources.py` — 新信息源回填(2026-09-11 芯片式架构 阶段 0a)
- ★ `backfill_broker_recommend.py` — 第 11 族(券商月度金股)全史补库(2026-09-13): 只写新表
    `broker_recommend`(Tushare `broker_recommend`, 2021-01 起 16162 行); 带 `ONEOFF_CONFIRM` 护栏
- ★ `backfill_report_rc.py` — 第 12 族(分析师研报预测)全史补库(2026-09-13): 接口 **1 次/分钟 +
    10 次/小时** ⇒ 约 83 次调用 / 9 小时, 故做成**断点续跑**(检查点 `data/.report_rc_backfill.json`,
    按月 + offset 分片、390 秒节流、撞上限自动翻页); 只写新表 `report_rc`
- ★ `backfill_research_report.py` — 第 12 族**主力源**(东财研报, 2026-09-13): 实测 Tushare
    `report_rc` 限额其实是 **10 次/天**(全史 8 天多)、AkShare 逐只封装 4.6 秒/只(≈8 小时),
    故直打东财 `reportapi` 的 `code=*` + 日期区间(19 页/月 ≈ 45 秒, 全史约 1 小时); 字段更全
    (评级/上次评级/评级变动/三年 EPS·PE/目标价/infoCode); 断点续跑检查点
    `data/.research_report_backfill.json`; 只写新表 `research_report_em`
- ★ `backfill_baostock_min60.py` — **baostock 60 分钟线全市场补库**(2026-09-15, 用户拍板"开始但要严格
    按照规则执行"): **只采分钟线**——日线/估值/财务已在 Tushare 且 2026-09-13 已抽样核对过(52 只票:
    日收益/停牌 0 不一致、PE/PB 排序 1.000/0.997) ⇒ 用户纪律"不要重复拉取已在别的平台获得的数据";
    实测 baostock 分钟线只到 **2020-01-02 起**(2019-11 无)、5min 78,048 行/票·173.6s、60min 6,504 行/票·9.9s;
    严格串行 + 自设每日 20000 请求上限 + 时窗 `6-24`(用户 2026-09-15 允许**采集进程**在
    09–12/14–18 也跑, 原话"允许, 但 codex 依旧不许工作" ⇒ Codex 本人的工作窗没放开) + 断点续跑(检查点
    `data/.baostock_min60_backfill.json`、日志 `logs/backfill_baostock_min60.log`),
    产物 `data/baostock_min60/<ts_code>.parquet`; **只写这三个路径, 不碰生产表**
-   `backfill_sparse_daily.py` — 一次性回填: 覆盖率不足个股的 daily/adj_factor/daily_basic 逐只历史 (2026-09-07 完整性检查)
- ★ `crosscheck_baostock.py` — **独立数据源交叉校验**(2026-09-13, 只读): 用 baostock 复核「日收益 /
    停牌标记 / ST 标记 / 估值排序」——只比口径可比量(收益、布尔、排序), 不把"数值不同"当错误;
    严格串行(用户给的规矩: ≤5 万次/天、不许并发), 默认抽样约 90 只, 产物 `logs/crosscheck_baostock.json`
- ★ `backfill_baostock_witness.py` — **可交易域独立见证**(2026-09-15): baostock `query_all_stock(day)`
    给出"当日全量证券 + tradeStatus(1=交易/0=停牌)", 实测 **2015 起有历史**(3,341 行/日) ⇒ 用来独立核对
    我们的可交易域/退市判定(KB 09 开头最贵的事故类型), **不产因子、不进模型**; 2,845 个交易日 / 每票每次 1 请求,
    串行 + 每日 2 万上限 + 时窗 6-24 + 断点续跑; `--after` 排队(等 `data/.baostock_min60_backfill.json`
    的 `finished=true`, 因为 **baostock 不许并发**); 产物 `data/baostock_witness/all_stock_*.parquet`
- ★ `backfill_dividend_ts.py` — **分红送转事件族**(2026-09-15, 新事件族): 走 **Tushare `dividend`**
    (主源; `ann_date` 一次拿全市场, 全史 ≈4,276 次调用) 而不是 baostock 的 `query_dividend_data`
    (那个必须给 code ⇒ 6.5 万次, 还会抢 baostock 配额); 按**公告日**逐日遍历 2015-01-01~今(含非交易日);
    `--interval 2.0` 刻意只吃账号限速预算的 ~15%, 给生产档留余量; 产物 `data/dividend_ts/div_*.parquet`;
    `alpha_bank` 撞车预检 = 171 条里 0 条分红/送转类
- ★ `backfill_top10_holder.py` — 第 13 族(**十大流通股东**, 持仓结构类)全史补库(2026-09-13):
    Tushare `top10_floatholders`, 单次上限 6000 行 + `offset` 翻页, **无明显限速** ⇒ 全史约 220 次
    调用/8 分钟; **PIT 用 `ann_date`**(公告日)而绝不用 `end_date`(报告期); 最近 2 期无条件重拉;
    断点检查点 `data/.top10_holder_backfill.json`; 只写新表 `top10_holder`
-   `diag_2018_alpha.py` — 2018 负 alpha 归因(2026-09-10 深夜): 是"选股失效"还是"风格失效"?
- ★ `diag_allnan_samples.py` — 诊断 ②(2026-09-09): 训练集/推分截面里「全 NaN 特征」样本的真实规模
-   `diag_alloc_reject.py` — 分配器拒绝原因诊断(2026-09-10 深夜, 承接 diag_exposure.py)
-   `diag_d1_diff.py` — D1 复刻差异的定性诊断: **换序** 还是 **换刻度**?
- ★ `ab_exec_window_20260928.py` — **执行窗口 A/B**(2026-09-28, 预注册
    plans/exec-window-ab-prereg-20260928.md): 只改成交时点、不动打分表 ⇒ 不换 pin、不重训。
    四臂 × 双窗 —— 控制臂=**开盘价**(现役 pin 20260928_exe 同口径)、收盘价(历史对照)、
    尾盘 1h VWAP(min60)、全日 VWAP(amount*10/vol)。
    ⚠️ 偏离上游计划: 计划把 P1 排在 P1.5 之前故控制臂定为收盘价, 但 P1.5 已先落地
    ⇒ 控制臂改为开盘价, 否则 Δ 测的是相对一个已废弃的口径。
    **结果: 三臂 Δ 全为负且配对 t 显著** —— 尾盘 1h −8.43pp(t=−2.11)、全日 VWAP
    −7.49pp(t=−2.60)、收盘价 −8.25pp(t=−2.00), 双窗同号 ⇒ 触发止损线(Δ 均 <+1.0pp),
    **执行窗口作为 alpha 来源结清**; 读数转为「口径测量」性质, 不采纳为改进动作。
    反向含义: 现役开盘价口径不只是"与实盘一致", 在收益上也是四臂中最优 ⇒ P1.5 的决策
    得到独立复核(且这是本轮唯一统计上站得住的 Δ)。
    G1 自检: 尾盘 VWAP vs 收盘价中位差 17.53bp、异常格 0.027%(门槛 50bp/1%), 命中率见产物。
- ★ `ab_oi_excluder_20260928.py` — `oi_spread` 剔除器 A/B(2026-09-28, 预注册
    plans/oi-excluder-prereg-20260928.md)。门禁过闸的 oi_spread 是**负向**强信号
    (DEAD head −2.118%/月 t=−4.03), 按项目三次实证「反向信号取反当 alpha 必塌」
    ⇒ 不做 P4 重训, 改测 chase6 同款**剔除器**(删行 ⇒ 次优递补 = 补位语义)。
    **结果 REJECT**: W=60 三档 Δsys_ann 全负(−0.75/−0.63/−1.83pp), G1 不达标;
    仅 w20_t2.5 为正(+1.16pp)但同族不同号、未达门槛。
    ⚠️ 唯一一致的信号是 **full 窗回撤 6/6 臂改善 +2.4~3.2pp**, 但 prod 回撤未改善
    ⇒ 按保险档也不达标, 线结清。
- ★ `hetero_gate_20260928.py` — 异构信息门禁(2026-09-28, 预注册
    plans/hetero-gate-prereg-20260928.md): 一次性把 **P2 股东户数 2 个** +
    **P3 隔夜-日内 8 个** 放进同一道门禁(S1 强度/S2 t/S3 死活同号/S4 非影子/S5 半衰期)。
    不改生产、不换 pin(走 gate.forward_return 朴素标签, 不经回测撮合)。
    隔夜-日内只需日频 open/pre_close/close(2013 起全史), **不受 min60 的 2020-01 起点限制**;
    第一版写死为**规模剥离版**(减当日截面均值), 因为隔夜跳空截面差异主要由流动性/规模驱动。
- ★ `index_rebal_event_study_20260928.py` — **指数调样事件流探针**(2026-09-28, 预注册
    plans/index-rebal-event-study-prereg-20260928.md, 判据跑数前冻结): 只读——tushare
    `index_weight` 月末快照(000300.SH/000905.SH, 2019-2026)重建 6/12 月半年度调样事件
    (进后即出的临时调整过滤), 生效日=第二周五次一交易日, 公告日近似=生效日−14 日历日,
    窗口 [公告后首个交易日, 生效前最后一交易日] 收盘→收盘, 个股腿用 `close×adj_factor`
    (跨分红季无污染), 指数腿=目标指数自身。**结果 PASS**(产物
    `experiments/index_rebal_event_study_20260928.json`): 纳入臂 n=1015、事件均值
    **+1.70%**(≈10 交易日)、HAC t=6.41、**批级 t=3.59**(30 批, 主判据), 两子窗
    +1.68%/+1.63% 几乎不衰减, 扣往返成本 +1.40% ≥ +0.5% 线(V1∧V3∧V4 全过);
    调出臂负号但不显著(−0.30%, t=−0.98)⇒ 剔除器方向不支持。机制=被动资金流,
    与全部已证伪信息族正交; **PASS=卫星层提案留拍板**(闲置现金座位 avg_cash_idle≈0.313,
    不碰模型链不换 pin), 真公告日校验是卫星提案前置义务(预注册 §4.1)。
- ★ `index_rebal_event_study_v2_20260928.py` — **卫星层 v2 探针**(2026-09-28 晚, 预注册
    plans/index-rebal-satellite-v2-prereg-20260928.md): 扩指数池(300/905/016/510 可交易
    +688 只旁注) × 退出窗口(W_A/W_B/W_C) × 公告日敏感性带(−18/−14/−10) × 选股双规则对照
    + 账户级 5,000 元模拟(真实费率含 5 元最低佣金)。**判决: edge GO 级但纸面轨冻结判据
    NO-GO** —— R1 纳入臂 n=1011、均值 +1.38%、批 t=2.22(G1✓); 带差 0.18pp(G2✓);
    W_A>W_B>W_C(+1.06/+0.93/+0.51% ⇒ 漂移在生效日前完成、生效后反转); **选股两规则
    同 edge 分化 ~1000 元/年**(weight_proxy +345 元/年 vs mcap_asc −623 元/年 ⇒ G3 在
    纸面规则下失败) ⇒ **2 名额/批的收割层被选股噪声主导, 5,000 元档不建仓**。
    实践形态改为全纳入群体追踪器 `scripts/satellite_rebal_paper.py`(零选股零资金)。
    事故在册: R5 年均口径首版漏乘年批次数(57 元 → 修正 345 元), 量纲自检教训再现。
- ★ `backfill_freefloat_monthly_20260928.py` — **月末自由流通股本快照回填**(2026-09-28 深夜,
    预注册 plans/freefloat-backfill-rule-v3-prereg-20260928.md): tushare `daily_basic.free_share`
    (仓库建表时未存的字段, 历史到 2015)按 93 个月末交易日全市场拉取(0.35s 间隔)，
    442,350 行落地 `data/satellite_rebal/freefloat_monthly.parquet`(gitignore, 可重拉再生)；
    验收: 覆盖 100%、free≤float 违例 75 行(0.017%)。
- ★ `index_rebal_rule_v3_20260928.py` — **选股规则 v3(ff_ratio)复活复验**(2026-09-28 深夜,
    同上预注册): 机制推导=自由流通加权指数下 weight/circ_mv = free_share/float_share
    ⇒ v2 获胜规则在公告时点可算(公告前最近月末快照)。**判决 NO-GO 按冻结判据线结清**:
    V1✓(覆盖 99.8%)/V2✓(ff_ratio 年均 **+545 元** ≥ 150)/V4✓(两子窗 +421/+669 同号)，
    但 **V3✗ Jaccard=0.40 < 0.5** —— tushare 自由流通口径与中证权重排序系统性分叉
    (预注册 §4.1 预设情形:"V2 过 V3 不过=赚但不等价, 仍结清, 分叉登记")。
    ff_ratio 转入**前瞻 OOS 观测**(追踪器 `--pick` 模式, 2026-12 批起逐批记录)。
    事故在册: v2↔v3 的 weight_proxy 读数漂移(+345 vs +651 元/年, NaN 腿处理差异,
    待查证登记); 三规则对照 mcap_asc −137 元/年(再证 v2 判决)。
-   `refresh_pin_staleness_notes_20260928.py` — 换 pin 后批量刷新历史脚本里的「时效注记」
    (2026-09-28 第 8 次重钉用)。历史实验脚本的 docstring 普遍写着"现役 pin 已重钉至 X",
    换 pin 后这句注记本身过期 ⇒ `test_pin_ledger_consistency` 报红。本脚本只换注记里的
    档位标签与数值, **不动正文里"当时的 pin 读数"**(证据只增不改); 编码按 utf-8→gbk→
    latin-1 探测并以原编码写回, 不强行统一。
- ★ `diag_exec_pin_delta_20260928.py` — P1.5 换 pin 的查明脚本(2026-09-28): 翻 `FILL_AT_OPEN`
  后系统线 prod +7.11pp/full +5.02pp, 远超 E6 旧基线的 +2.33pp ⇒ 按预注册必须查明。
  **按 `trade_date` 严格配对**两 run 的 NAV 序列(首版误用 `ORDER BY nav` 按净值排序 ⇒
  月度 Δ 全是噪声、结论作废, 已修正并留痕), 输出 Δ 的配对 t、符号一致率、集中度与
  最大贡献月明细。结论: t=1.84/1.74 未达 2、符号一致率 54.8%/51.8% 未达 2/3、
  最大 3 月占 Δ 比 67%/65% 超 60% ⇒ **Δ 不构成"执行层 alpha"的证据**, 翻开关的
  正当理由只是"回测与实盘同口径"(实盘 `paper/engine.py` 一直是开盘价成交)。
-   `diag_d67_truth_diff.py` — D06/D07 —— L3 臂 B(推导限价) vs 臂 C(真值限价) 的**逐笔差异归因**。
- ★ `diag_drawdown_attrib.py` — 回撤/年化归因诊断(2026-09-10) —— 回答「回撤这么大、年化不高, 是不是模型不靠谱」
-   `diag_exposure.py` — 权益暴露/集中度插桩诊断(2026-09-10 深夜)
- ★ `diag_float32_limit.py` — D06/D07 诊断的延伸 —— **float32 的 `pre_close` 让多少格的推导限价偏了一分**。
- ★ `diag_stklimit_window_check.py` — stk_limit「引擎推导限价 vs 交易所公布值」**逐格验证**(2026-10-05) ——
  回填窗口/北交所取整接线(`limit_price(board="bse")`)的常设验证器; 哨兵两族(沪深 100000 /
  BSE 99999.99+0.0)与无 pre_close 行单独计数; 2026 全窗口 988,321 格零分歧。
-   `diag_head_calibration.py` — 头部校准诊断 + 免重训廉价候选(2026-09-09 深夜, 用户"继续挖改进方向")
-   `diag_head_paired.py` — 头部学习目标两臂的诊断(非判据): 逐年拆 top6 + 同覆盖域成对检验
-   `diag_head_variants.py` — 头部去噪: 排序变体筛选(2026-09-09 深夜, 免重训)
-   `diag_holding_period.py` — 持有期经济性诊断(2026-09-09 深夜): 换手/成本 vs alpha 衰减
-   `diag_pit_arms.py` — PIT 三臂的**诊断**(非判据): 逐年 top6、B/C 互相的秩相关、同覆盖域对照
-   `diag_pit_paired.py` — PIT 三臂的**成对**检验 + 修正后的逐年覆盖率(非判据)
- ★ `diag_style_bench.py` — 池子基准诊断(2026-09-10 修正版): 策略吃的是谁的 beta?
-   `exp_capital_real_fees.py` — 资金规模扫描 —— 逐笔真实费用口径(2026-09-11 第五轮)
- ★ `exp_deadzone_ic.py` — 阶段 0c: 新数据家族「死区体检」—— 芯片式架构的硬门禁
-   `exp_die_flow.py` — 阶段 2: 第一个异构 die —— 资金流家族(margin + moneyflow)
- ★ `exp_die_head.py` — 任务 #22: 把 die 的学习目标从「全截面收益」换成「头部」
-   `exp_die_headvt_bt.py` — 任务 #22 阶段 B: `head_vt` 的组合层回测 —— **本项目第一次为「改模型」跑回测**
- ★ `exp_die_pit.py` — 缺陷修复实验: 把行业中性化从「当前快照」换成「申万 PIT 时点行业」
- ★ `exp_die_stage_b.py` — 芯片架构「阶段 B」通用脚手架: 一个过了 KGD 的 die 值不值得封装?
- ★ `exp_die_weekly.py` — 任务 #24: 把 die 的训练目标从月频(21 日)改成周频(5 日)
-   `exp_ens_stage_b.py` — 阶段 B 判决 —— `model_score_ens`(stacking 融合器) 值不值得封装 (2026-09-11)
-   `exp_fb_window.py` — 因子组合 vs 模型 —— **同窗口**控制(2026-09-11)
-   `exp_fee_floor.py` — 最低佣金 5 元到底吃掉多少钱(2026-09-11 第五轮): 唯一未被量化的非模型杠杆
- ★ `exp_fusion_router.py` — 阶段 3: 融合器 —— 滚动路由(芯片类比里的 router / DVFS)
- ★ `exp_gp_factory.py` — Alpha Factory 薄 CLI —— `app/alpha/factory.py` 的命令行壳(2026-09-11)
-   `exp_illiq_stress.py` — illiq 臂的证伪性压力测试(2026-09-11)
-   `exp_label_style.py` — 模型赚的是个股 alpha 还是小盘风格 beta —— 标签风格中性化实验(2026-09-11)
-   `exp_null_random.py` — 模型 vs 随机基线(2026-09-11 第五轮): 「改造模型」到底有没有靶子
-   `exp_seq_die.py` — 阶段 5: 时序 die —— PatchTST 式 Transformer(`app/models/seq_die.py` 的薄壳)
-   `exp_slot_recycle.py` — 槽位回收实验(2026-09-10 深夜): 20.5% 的资本被整手粒度机械性闲置, 能不能救回来?
- ★ `exp_stage_b_flow_die.py` — 阶段 B 判决: `margin_net_share` 零参数 die 值不值得封装?(2026-09-11)
- ★ `exp_start_2019.py` — 起点实验(2026-09-10 深夜 第四轮): 「数据剔除到 2019 科创板上市」拆成两个旋钮
-   `exp_stoploss.py` — 止损 / 迟滞带实验(2026-09-10 深夜 第二轮): 深度解析「回撤还是大, 收益还是小」
- ★ `exp_train_months.py` — 训练窗口长度实验 —— 检验「36 个月窗口使模型追 regime」假设(2026-09-11)
- ★ `exp_holdout_recent.py` — holdout_recent 留出实验 —— 检验「近端留出 6 个月」保险的价与保额, 6/3/0 三臂 + 回撤轴判据(2026-09-16)
- ★ `exp_holdout_recent_seeds.py` — holdout 两套种子复验(2026-09-16, 预注册)：B=[11,37,101,777]/C=[5,53,208,1999] 只训 ho6/ho3 两臂，双种子集同过三轴判据才维持采纳建议（用户拍板的稳健性扩展）
- ★ `verify_ledger_identities.py` — 资金账本三线恒等式核查(2026-09-16, 全项目逻辑审查·只读)：paper nav/现金走查/持仓走查 + 袖套 nav/现金/快照共 6 条恒等式对真库逐行校验(容差舍入级)
- ★ `verify_tm36_retrain.py` — 漂移判决实验(2026-09-16, 复核): 今天重训 tm36 同配置写入隔离表 `model_score_tm36_chk`, 与 09-10 表 / ho6 表三方向逐位对比 —— 判决「快照漂移」还是「训练非确定」
-   `exp_weight_rate.py` — 仓位兑换率实验(2026-09-10 深夜 第三轮): 回撤在**现行基线**下多少钱一 pp?
-   `fix_alpha_bank_rerun_20260918.py` — 回填被 2026-09-18 审查 Q-05 抹掉的 10 行 alpha_bank 读数（撞车留痕的整行 upsert 把已在册行覆盖成 NULL 空壳）；默认 DRY-RUN，`--write` + `ONEOFF_CONFIRM` 才动库
-   `fix_ann_dates.py` — 一次性修正: 财务兜底数据披露日改为法定披露截止日(年报前视修复)
-   `m3_q2_percentile_ab.py` — M3(Q2 百分位两把尺, 2026-09-21, **只读**): `scores.py` L441-450 在 `with_production=True` 时把周频 die 分与生产分两把尺各自 `rank(pct=True)` 后按 2:1 加权平均, 但两者**域不同**(周频面板 cols vs `trainer.tradable_mask` 生产可交易域)。判据冻结在 `plans/ledger-settlement-criteria-20260920.md §M3`; 自证门 M3-a(域从 PITEngine 现读)/M3-b(两臂共用同一特征矩阵)/M3-c(A 臂自比=1.0,否则判失败)。实测三量: 受影响格数占比 均值 5.98%/最大 11.01%, 逐锚点秩相关 最小 0.999475(**达标** ≥0.99), 前十分位重合率 最小 0.757(**不达标** <0.99) ⇒ **越门, 须立阶段 B(双臂重训 + 另写预注册)**; 产物 `experiments/m3_q2_percentile_ab.json`。⚠️ 前两版 A 域定义错误(拿 `model_score` 自有票集当 A 域 ⇒ 等于自比), 测出假的"受影响 0.01%", 已更正为真正重现两把尺。
-   `migrate_daily_score.py` — 一次性迁移 (P1-11/P2-2/P0-3): python scripts/migrate_daily_score.py
-   `migrate_start_2019.py` — 生产起点迁移到 2019-07-22(2026-09-10 用户拍板, 科创板开市日)
-   `monthly_since2019.py` — 回测窗口缩到科创板上市(2019-07-22)之后, 导出逐月数据(2026-09-09, 用户要求)
-   `power_on_framework.py` — 通电 —— 第一次把 `framework.run_stages` 的五个阶段真的打开跑一遍 (2026-09-11)
-   `probe_st_cells_impact.py` — 三类「无法归因」残差的**策略影响量化** —— 它们到底碰不碰得到策略域。
-   `pull_stk_limit.py` — 拉 `stk_limit`(涨跌停价) —— 实验路线图 D06/D07 的数据基础 (2026-09-11)
-   `recompute_factors_fullmarket.py` — 一次性重算: 全市场扩展(2026-09-07)后全历史月末因子重算
-   `archive_paper.py` — **只读**归档(2026-09-14): 把模拟盘 6 张表(账户/持仓/挂单/成交/净值/计息)导成
    `data/backup/paper_archive_<时间戳>.json`(逐行 + 行数 + sha256 + 库指纹), `--verify` 可逐位复核。
    起因: 2026-09-10 12:03 那次重置**没有留痕**, 事后只剩 4.6 GB 全库备份可翻; 重置前先跑它。
-   `reset_paper.py` — 模拟盘全量初始化: 清空持仓/挂单/成交/净值/计息, 资金重置为初始资金, 并重新应用当前月度方案。
    2026-09-14 起**自动先归档再重置**(归档自检不过即中止) + 重置后写 `pipeline_run(stage='paper_reset')` 一行
-   `restore_ens_archive.py` — 查 `data/models/ens/` 的 464 个月度归档能否**忠实复原**（2026-09-11）
-   `s2_pool_guard_impact.py` — S2(`pool_index` 护栏影响, 2026-09-21, **只读**): `app/models/pool_index.py` L53 的 `ABS(pct_chg)<=21` 护栏除"深跌半边"外还有**上行半边**(1692 行 >+21%, 主要新股首日) ⇒ 池指数系统性偏低 ≈0.3862pp/月。本脚本对照「有护栏(现行)」vs「无护栏(修正)」两条池指数(按 `build_pool_index()` 同一口径折收盘, **不落库**), 两臂共用同一个 `regime._pool_state_frame`(**复用而非重写**, 避免状态规则被抄歪)。实测: 被护栏挡掉 **2519** 行; 状态可比 3082 天中翻档 **264** 天 ⇒ **翻档率 8.57%**; 方向一致 —— 现行比修正后**多 137 天 defense、少 150 天 attack** ⇒ 定量证实"现行系统性偏防守"(与先前 2.3% 推算一致)。产物 `experiments/s2_pool_guard_impact.json`; 判决建议: 8.57% 非可忽略 ⇒ 建议立预注册换尺, 但会改写历史防守信号与在册读数 ⇒ 需用户批准, **不自动执行**。
-   `s2_pool_scale_candidates.py` — S2 换尺**设计探针**(2026-09-21, **只读**, 不落库/不改 `pool_index`): `s2_pool_guard_impact.py` 建议"换尺", 但它那把"无护栏"尺(V1)**本身可能不是正确的尺** —— 被护栏挡掉的上行 1692 行主要是**新股首日**(>+21% 只有新股首日与退市整理期首日才可能), 而选股域(`app/models/daily_score.py:145` 与 `trainer.tradable_mask` 同口径)要求**上市满 60 交易日** ⇒ 那笔 +44% 在策略侧**根本不可获得**。本脚本对照三把尺: **V0**=现行(`ABS(pct_chg)<=21`, 无上市天数过滤) / **V1**=无护栏(含新股首日 ⇒ 策略不可获得) / **V2**=**规则化**(剔"上市未满 60 交易日" + 跌幅 clamp −99%, 与选股域同口径) ⇒ **候选正确尺**。三臂共用同一个 `regime._pool_state_frame`(**复用而非重写**)。产物 `experiments/`; 判据: 若 |V0−V2| 显著小于 |V0−V1| ⇒ 8.57% 与 0.3862pp/月**高估了现行尺的偏差**, 换尺优先级下调 ⇒ **先定义正确的尺, 再谈换尺**。
-   `s5_tiebreak_impact.py` — S5(选面 tie-break 影响, 2026-09-21, **只读**): `app/alpha/gate.py` L96 用 `f.sort_values(ascending=False)`(默认 quicksort, **非稳定**) ⇒ 分数并列时"谁进 top-k"取决于算法而非数值 ⇒ 不可复现。本脚本用**真实生产分** `model_score.score` 逐截面(116 期)对照「当前非稳定」vs「稳定排序(kind=stable)」⇒ 翻档 **12 槽位 / 44385(0.027%)**、存在并列的截面 14/116。⚠️ **局限**: 对照用生产分(连续值、并列少), 而 S5 选面发生在 `gate.py` 对**因子值**排序; M5 批次 4 实测「4/7 条零次齐次因子头部边界并列」⇒ **本对照很可能低估 S5 影响**, 须**因子级数据**复核后才能判定"翻档是否=0" ⇒ 暂缓实施(预注册 `plans/s5-gate-stable-tiebreak-prereg-20260921.md`)。产物 `experiments/s5_tiebreak_impact.json`。
-   `s5_tiebreak_factor_level.py` — S5(选面 tie-break 影响, **因子级**, 2026-09-22, **只读**): 上一行那次用的是**生产分**(连续值、并列少 ⇒ 很可能低估)。本脚本改用**真实因子值面板**(`data/alpha_pool/<alpha_id>.npz`, `bank.load_values` 读到的就是 `gate.evaluate()` 收到的那个 `panel_md`), 逐**因子**逐截面重做对照(两臂只差 `kind`)。口径直接 import `gate.TOP_FRAC`/`gate.MIN_NAMES`, 不自己写常量。实测: 189 因子 × 16646 截面、7,655,774 个 top 槽位 ⇒ **翻档 35702 槽位(0.4663%)**, 涉及 **122/189 因子**、**2974/16646 截面**; **125/189 因子存在边界并列(`boundary_gap == 0`)**。受影响截面的翻档规模: 中位 **4**、p90 **20**、**最大 440** 槽位; 占该截面 top-k 比例中位 1.03%、**最大 90.19%**。⇒ 与生产分那次(0.027%)比 **约 17×**, 即 **S5 不是"零影响"**, 须评估是否动门禁(动 ⇒ 在册 189 条读数成为"旧选面" ⇒ 另立预注册 + 用户批准)。产物 `experiments/s5_tiebreak_factor_level.json`。
-   `s5_tiebreak_return_impact.py` — S5(**选项 C**: 翻档对**收益**的影响, 2026-09-22, **只读**): 把对照从"成员资格"推进到"**前向收益**", 让 A/B 决策有数字。fwd 用 `gate.forward_return`(**与门禁读数同一把尺**, 朴素标签), zone 过滤/头部/均值基准逐字镜像 `gate.evaluate`; 两臂只差 `kind`; **判据先冻结再跑**(不可辨 = |t(Δ)|<2 且全部 |ΔDEAD_head| < 5bp/月(门禁下界一成), 否则可辨 ⇒ 用户拍板)。实测(189 因子 × 12864 门禁口径截面, 327s): 翻档 28366/6065092(**0.4677%**); 受影响 2336 截面 Δhead_excess 均值 **−0.16bp、朴素 t=−0.258** ⇒ **总体不可辨**(当前非稳定排序不占便宜也不吃亏); **但 8/17 条 `kg_*` 整数计数因子** ΔDEAD_head 越线(+19.6 ~ **−44.0bp/月**, 最差 `kg_unlock_90d_cnt` ΔACTIVE **+132.7bp/月**), **0/172 条连续因子**越线(最大 CNTD5 −2.89bp)。⇒ 收益影响**真实但完全集中于整数计数因子**(整数取值 ⇒ 头部边界天然大片并列), 8 条全是 CANDIDATE、无一在产; 改稳定排序对它们是"重抽签"(5 好 3 坏), **不是免费改进** ⇒ 数据支持选项 A(不动门禁, 登记"kg 计数因子读数为并列脆性")。产物 `experiments/s5_tiebreak_return_impact.json`。
-   `restore_drill_readonly.py` — 备份**只读恢复演练**（段 A, 2026-09-22, **只读**）: 补的是"备份从没做过恢复演练"这个缺口 —— `tests/test_db_backup_verify.py` 只验 `quick_check` + "校验不改文件", 只能证明**文件没坏**, 证明不了**里面的数据完整可用**。本脚本以 `?mode=ro` **只读 ATTACH** 备份(不复制 11GB 文件), 跑 `quick_check` + 逐表比对关键表行数与最新 `trade_date`(生产 vs 备份) + 抽查结构一致性 ⇒ 回答"备份停在哪个时点、能不能用"。**不做**段 B(复制到隔离目录再跑月链 —— 成本高、按需再做)。产物 `experiments/restore_drill_readonly.json`。用法: `D:\Python313\python.exe -X utf8 scripts\oneoff\restore_drill_readonly.py [--backup neweco_20260919.db]`。
- ★ `scan_const_weight.py` — 恒定权益仓位前沿扫描(2026-09-10 晚): 去掉 regime 择时后, 仓位定多少?
-   `smoke_022_trainer_inert.py` — ② 修复的独立验证(2026-09-09): 全 NaN 样本兜底对生产训练是否数值惰性
-   `verify_d1_replica.py` — 阶段 1 验收: KGD 测试台能否**逐股复现**现行模型(D1 复刻)
-   `verify_pit_refactor.py` — PIT Engine 重构的**逐位验收** —— 任务 #32(2026-09-11)
-   `factor_lib_probe.py` — **只读**探针(2026-09-14): quantskills 因子库里"本仓 31 因子没有的四族"各取 1~2 条, 量死区/活跃区头部超额与对现役因子的秩相关(产物 `logs/factor_lib_probe.json`)
-   `exp_weekly_label_adj.py` — **只读**实测(2026-09-14, 外部审查 P1-1): 周频标签"复权 vs 未复权"逐格比(2,242,458 格:
    \|Δ\|>50bp 占 6.4%、符号翻转 1.36%、但逐锚点秩相关 0.9925、前十分位重合 99.3%、集中在每年 5 月)。
    训练 A/B 的**预注册判据写在它 docstring 里**(阶段 B 未跑) —— 命令 `D:\Python313\python.exe -X utf8 scripts\oneoff\exp_weekly_label_adj.py`
-   `exp_weekly_label_adj_b.py` — **阶段 B 训练 A/B**(2026-09-14 夜, F73): 只换训练标签(裸 open vs open×adj),
    特征/掩码/参数/种子两臂共享; RAW 臂 = 生产档案, 另加"同面板重训"对照排除数据版本混杂; 判定照抄 F52。
    **结果: 不采纳复权**(同面板对照下留出集 −2.38/−3.05pp、全史 −1.24/−1.64pp、IC −0.0022/−0.0032)。
    产物 `experiments/10_label_adj_ab/AB_result.json`(+逐期序列; 打分 npz 不入 git)。**不碰生产产物**。
-   `../build_kg.py` — 金融事件知识图谱构建(2026-09-17, 图谱轮): 5 类事件边+PIT 行业边
    → kg_edge_event/kg_edge_relate 邻接表(带时点, PIT)
-   `../negative_excluder_r4.py` — 第四轮三信号负向剔除器(2026-09-17 预注册):
    0.5×z(图谱拥挤)+0.25×z(尾盘动量5d)+0.25×z(收盘偏离vwap5d), θ∈{1.0,1.5,2.0},
    判据=死区不伤害+全期≥+1pp+回撤, 判决即结清
-   `../llm_crowding_excluder.py` — 第三轮 LLM 拥挤度剔除器(2026-09-17 预注册):
    sample(死区锚点候选子图采样) → score(DeepSeek 拥挤分 1~10, 引用边校验不实计零)
    → backtest θ(剔除臂组合回测, 两轴判决)
-   `../llm_title_extract.py` — 研报标题 LLM 语义抽取(图谱轮文本边完全体): 近 3 年 5 万条,
    主题/情绪/确定性/催化剂/数字承诺, 断点续跑 → kg_edge_event + data/kg/title_semantics.jsonl
-   `../llm_factor_gate_r1.py` — LLM 提案第一轮门禁执行(2026-09-17): A 组(id60_* 日内组合, 23 条)
    经 ZooProposer.panels 直通(基列=min60 物化面板)进 Factory, declare(50) 预注册 → 死区评估 → alpha_bank
-   `../llm_factor_propose.py` — LLM 因子提案第一轮(2026-09-17): DeepSeek 读新矿字典+31 因子清单,
    提案 50 条(预算冻结)产出 data/llm/factor_proposals_r1.json, 供 LLMProposer 装载进工厂
-   `kb_table_coverage.py` — **只读**检查(2026-09-14): 列出"`data/neweco.db` 里**有数据**但 `D:\know\*.md` 未按名提及"的表(起因: `alpha_bank` 171 条实验证据一直没进知识库)
-   `diag_data_drift_0916.py` — **只读**诊断(2026-09-16, 复核): 09-13 周备份 vs 当前库逐表对比历史行修订(daily/adj_factor/daily_basic/fina_indicator 等), 定位「全量重训不可跨日复现」的数据修订源；产物 `experiments/05_label/diag_data_drift_0916_result.json`
-   `diag_factor_row_drift_0929.py` — **只读**诊断(2026-09-29): factor 表**月末行**跨日漂移检验(09-22 周备份 vs 现库)——20260831 行 75% 值变(统一乘性 ~6.4e-5)而窗外月末(0731/0630)零变化 ⇒ 锁死「日频链天天覆盖月末日行」为漂移通道；产物 `experiments/05_label/diag_factor_row_drift_0929.json`
-   `diag_source_row_drift_0929.py` — **只读**诊断(2026-09-29): 20260831 中性化截面的**源表**定位(含 0916 漏查的 lhb 族)——daily/adj/basic/lhb 零修订、stock_basic 6 行(601777 行业重分类) ⇒ 根因=上游行业快照修订 × 日频链覆盖；产物 `experiments/05_label/diag_source_row_drift_0929.json`
-   `f1_monthend_freeze_exp_0929.py` — **只读**实验(2026-09-29, 预注册 plans/f1-monthend-freeze-prereg-20260929.md): F1 月末行冻结两项实验——E1 根因复现 FAIL(存在第二漂移源, 运行时状态不可事后重建) ⇒ F1 线结清; E2a 决策影响 PASS(现役集成下漂移前后 top6 完全一致/Spearman 0.99993) ⇒ 漂移对生产决策零影响；产物 `experiments/05_label/f1_monthend_freeze_exp_0929.json`
-   `mirror_pool_reference.py` — **只读**（2026-09-15，F74，镜厅 M0-lite）：按 `F52_live_vs_research_shape.py:136-151` 的**原样算法**复现"可交易池等权参照线"，并在**同一锚点口径**下与"袖套 − 中证800"对比 ⇒ 输出两个 σ 与分辨增益 `G_pool`。判据先冻结在 `plans/mirror-m0-lite-prereg.md`；复算器 `experiments/mirror/mirror_m0_recompute.py`。⚠️ DB 只读(`mode=ro`)、分数存档只读、产物只落 `experiments/mirror/` —— 不写生产表、不重训。
-   `mirror_twin_rho.py` — **只读**（2026-09-15，F74 续）：孪生 ρ 单向模拟 —— 随机游走选股（top100 / 8× 带 / 等权 / 开→开复权 / 无费用）与袖套的**同尺相关**，先按换手带 ±0.20 校准 ρ 再主测。判据先冻结在 `plans/mirror-twin-rho-prereg.md`。含**支配性自检**：孪生 σ_d 若 > 池线 σ ⇒ 即便过闸也不该造机器。
-   `mirror_hac_clock.py` — **只读**（2026-09-15，F74 续）：对 F74 的配对差序列做 **HAC(Newey-West, Bartlett, L=2)** 的 z 与"当前已排除 |α| < X pp/期"**排除线**；自带合成自检（AR(1)，断言 HAC 落在截断求和与 LRV 之间）。⚠️ **报告仪器、不是闸门**（看视点纪律写死在 docstring）。
-   `weekly_freq_baseline.py` — 周频化 A/B（**先袖套**路径，2026-09-21）：把**已是周频**的袖套打分（`scores_<asof>.npz`，`HORIZON=5` + 周锚点 + 651 锚点）导入**隔离表** `model_score_weekly_chk`，跑 `run_backtest(score_table=ST, run_tag="weekly_chk")` ⇒ 由于隔离表 `trade_date` 是周锚点，调仓频率=周 ⇒ 得到真正的周频回测。产物 `experiments/weekly_freq_baseline.json`。⚠️ **关键教训**：`trainer.HORIZON` **只改「模型预测多远」，不改「多久调仓」**（`trainer.py` L370-373 明写，调仓频率由 `md_all` 月末截面决定）⇒ 周频化**不能**靠改 HORIZON，必须用周频锚点的信号源（袖套）或改回测锚点。首轮结果（无退出带）：IC 0.0951/ICIR 0.787（**优于** F50 的 0.0875/0.685）但换手 0.9258、超额 **−6.54%** ⇒ 成本吞噬。
-   `weekly_freq_hyst_scan.py` — 周频化 A/B 的**退出带扫描**（2026-09-21）：对同一 `hysteresis`（0/8/16/24）分别跑月频（生产 `model_score`）与周频（隔离表 `model_score_weekly_chk`）⇒ 对比 换手 / 超额 / MDD / 胜率。产物 `experiments/weekly_freq_hyst_scan.json`。⚠️ **发现**：`config.yaml` 的 `hysteresis_buffer: 0`（回测默认**无退出带**）与实盘袖套 `pick_shape` 的 `band`（1e6 档 = **8.0** 退出带倍数，SHAPES L47-53）**不是一套参数** ⇒ 跨口径比较前必须标定退出带，否则换手被高估、结论不可信。
-   `weekly_rebalance_ab.py` — 周频化 A/B 的**调仓频率**对比（2026-09-21，**判决：不采纳**）：A = 生产 `model_score` + `anchor="month"`（月频调仓，现役）vs B = 隔离表 `model_score_weekly_chk` + `anchor="week"`（**周频调仓**），同一 `hysteresis`(0/8/16/24) 下对比。产物 `experiments/weekly_rebalance_ab.json`。结果：**信号通过**（周频 IC 0.1084 / IC_IR 0.809，优于月频 0.0859 / 0.735 与 F50 0.0875 / 0.685）但**净值不通过**（周频超额 −0.169 ~ −0.216，比月频差 −0.21 ~ −0.28；**MDD 从 −0.52 恶化到 −0.82 ~ −0.89**）⇒ 按预注册判据③**不采纳**，已登记证伪（05 号第四节）。⚠️ 本脚本依赖本轮新增的 `anchor` 参数：默认 `month` 与现役**逐位一致**，`week` 才保留全部周锚点 —— **在此之前的所有"周频回测"都被 `me_dates` 降采样成了月末，测的并不是调仓频率**。
-   `seed_resolution.py` — **验证分辨率刻度**（2026-09-21，阶段 2「验证方法改进」）：由 F76b 实测"同配置 `base_A` vs `base_B` 全窗年化差 **5.9pp**"反推单 seed 标准差 σ ≈ 4.17pp，按 `SE = σ/√N ≤ Δ/2` 给出"要分辨 Δ 需要多少 seed"表（0.04pp → **43,512**；1pp → 70；2pp → 17；3pp → 8）。产物 `experiments/seed_resolution.json`。⇒ **堆 seed 救不了小效应**：候选效应 < ~2pp 应直接判 `effect_indistinguishable` 并登记，改用**配对 / 分层**降方差设计；"5.9pp seed 方差"是一切重训类候选的先验门槛。已登记 05 号第五节。⚠️ 本脚本的两个前提已被下一支脚本复核推翻一半（见下）。
-   `seed_variance_decomposition.py` — **方差结构分解**（2026-09-21，上一条的复核）：**只读** F76b 归档产物，把 seed 方差拆成 `σ_b(持续性) + σ_e/√T(逐期)`。三个结论：① **两臂不独立**——生产是 4-seed 集成（`trainer.ENSEMBLE_SEEDS`），F76b 的 `base_A`/`base_B` 共享 2/4 个 seed ⇒ ρ≈0.5 ⇒ σ ≈ **5.90pp** 而非 4.17pp ⇒ 上一条的分辨率表**偏乐观约 2 倍**；② **配对已经做过且不够**——F76b 自己有 `paired_monthly`，配对只削 σ_time（2.85±0.19pp，n=114）≈ 30~45% 方差，主导的是持续性分量（占 53~84%）⇒ 5.3 第 1 条"优先配对"必须降权，真正有效的是加**集成 seed 数 K**（σ∝1/√K，**同时改善模型本身**）而不是独立重复 N 次（只改善测量）；③ **研究层分辨率是组合层的 2.7 倍**（1.07 vs 2.85pp）⇒ 候选应先在分数面板筛。附带警告：既有"双 seed 配对 t"只含时间抽样 SE ⇒ t 可能虚高至多 1.77×（F76b seed_B 1.894 ⇒ ≈1.07），但 **n=2 定不了论**，待标定。产物 `experiments/seed_variance_decomposition.json`。**DB 只读、不重训、不碰 pin、不改判据**（只量仪器）。
-   `block_ic_replay_2021.py` — **2021 年块级 IC 预警有效性回放**（2026-09-22，D 案 §四验收项）：判决命题是"块级报警须早于全模型 IC 报警 ≥4 周"。判据**不在脚本里** —— 块报警一律走 `app.weekly.health.block_ic_from_ic`，脚本只做取数 + 逐锚点循环 + 合成全模型读数（31 因子定向等权，与块同构造）+ 落盘。预注册 `plans/block-ic-replay-2021-prereg-20260922.md`（R1–R9，提交 `44a2ec97`，**先冻结再跑数**）；产物 `experiments/block_ic_replay_2021.json`（96 锚点/45s）。**结果 NULL**：B1 与全模型同为 20210129 首警；B2/B3/B4 分别晚 4/22/9 周 ⇒ 无一块提前 ≥28 自然日。两个**跑数前**就冻进文档的约束：① `factor` 表 2021 只有 12 个月末锚点 ⇒ 分辨率（1 锚点≈4 周）**就是**要证明的效应量；② 2021 的 window 12 期 = 12 个月，生产当前 ≈12 个自然日 ⇒ 验证的是**判据结构**而非日频窗长。**DB 只读、不改判据、不换 pin**。⚠️ 判 NULL 后**不许**改 window / ratio / 区间 / 块划分去救结论（R7 失败即止），换提前量定义须升 v2 预注册 + 用户拍板。
-   `block_ic_replay_holdout.py` — **v2：块级读数"独立信息"判决**（2026-09-22）：v1 判 NULL 后换**命题**（不再问"是否更早"，改问"全模型健康时块级是否**单独**报警"）+ 换**区间**（判决区间 = **2022+2023**，v1 从未计算、我从未看过 ⇒ 防事后凑阈值）。预注册 `plans/block-ic-replay-v2-holdout-prereg-20260922.md`（V1–V8，提交 `e41cb061`，**先冻结再跑数**）；产物 `experiments/block_ic_replay_holdout_2022_2023.json`（120 锚点/37s）。判据参数（window/baseline/ratio/horizon/定向/块划分）**与 v1 逐字相同**；取数/合成**直接 import v1 脚本** ⇒ 两份回放共用同一份实现。**结果 PASS**：2022 零命中、2023 命中 **5 个锚点**（0131/0228/0731 B2、0428/0531 B1），命中时全模型比值 0.68~0.79（健康）。辅测度（不判决）：v1 的"提前量"命题在 2023 上对 **B2 = +59 天（≈8.4 周）** —— v1 判 NULL 的命题在未见区间上对 B2 成立，但 **2022 零命中、2021 的 B2 反而晚 28 天** ⇒ 时有时无，**不是稳定的提前预警器**。**DB 只读、不改判据、不换 pin**。⚠️ PASS 只证明"独立信息"，**不证明处置有效** ⇒ 块读数继续只报警不动作。
-   `block_diag_b1b2.py` — **B1/B2 报警的只读定位诊断**（2026-09-22）：报警文案明写的第一步是"定位是哪个成员掉的"，本脚本就干这一件事 —— 逐成员给出 当期 IC / 基线 / 比值 / 判定（**符号翻转** vs 幅度衰减）/ 拐点锚点 / 连续跌破期数 / 近 12 期同号比例 / 同块同步性，外加小盘 vs 大盘（`index_daily` 000905/000300）旁证。预注册 `plans/block-diag-b1b2-prereg-20260922.md`（D1–D8，**先冻结再跑数**）；产物 `experiments/block_diag_b1b2.json`。取数复用 `block_ic_replay_2021.build_ic_rows`、块读数走 `health.block_ic_from_ic`，**无第二份判据**。**硬闸 D4**：算出的块读数必须**逐位复现**生产缓存里的 −0.0199/0.0633 与 0.0074/0.0422，复现不出就先修脚本、不许改口径去凑。⚠️ **只诊断、不处置**：剔除/降权属换 pin 级（最近一次重钉要重写全史 116 期 444,312 行、上一档不可再生），且 B2「删七因子」已有**不采纳**在册判决 ⇒ 动它必须先说明比那次多了什么新证据。**DB 只读、不换 pin**。
-   `block_ic_window_study.py` — **块级 IC 的窗长/前瞻口径研究**（2026-09-22，用户拍板立项）：只**测量**，不改 `window`/`horizon`（改 = 改判据 = 须另立预注册）。量的是日频段（2026-08 起）vs 月频段的 **M1 报警占比**与 **M2 状态抖动**，外加 `horizon=10/21` 的稳健性对照；并按 W6 报告"窗口覆盖自然天数"与"不重叠前瞻窗个数"。预注册 `plans/block-ic-window-calib-prereg-20260922.md`（W1–W7，**先冻结再跑数**）；产物 `experiments/block_ic_window_study.json`。门槛（**先冻结**）：日频段任一 P0 块 `M1 ≥ 0.50` 或 `M2 ≥ 0.30` ⇒ 判"报警过敏"；B1/B2 至少一块过敏 ⇒ 结论"建议立项改窗长/前瞻"。起因见 `plans/block-diag-b1b2-prereg-20260922.md` §4.4（window=12 在日频下只有 ≈2.5 周且前瞻 5 日重叠）。⚠️ **只报"是否过敏"，不判"改成多少最好"**。**DB 只读、不改生产参数、不换 pin**。
-   `block_alarm_persistence.py` — **块级报警的持续期分布**（2026-09-22）：回答"报警一旦触发，历史上通常持续几个 asof"，给运维一个"观察几期再决定要不要升级"的经验依据（起因：B1/B2 正在报警，而诊断结论要求"定位 + 观察"，但"观察多久"原本没有依据）。预注册 `plans/block-alarm-persistence-prereg-20260922.md`（P1–P6，**先冻结再跑数**）；产物 `experiments/block_alarm_persistence.json`。主段 = **月频段**（79 个 asof，1 期≈1 个月，样本够）；日频段（24 个，1 期≈1 个交易日）只报不判。测度 = 报警 run（连续报警段）的长度分布（中位/均值/p90/最大）+「≤2 期就消退的 run 占比」。门槛（**先冻结**）：该占比 ≥0.50 ⇒ 观察窗口取 **2 期**；否则取中位持续期数。⚠️ 两种**单位不可混用**（月频 1 期≈1 月 vs 日频 1 期≈1 天）；持续期**不证明**报警有用/无用（那是 v1/v2 的问题）。**DB 只读、不改判据、不换 pin**。
-   `watchdog_restart_forensics.py` — **S13「自愈」计数的来源分类**（2026-09-22 23:0x）：不是找服务死因，而是先把"每天自愈 3~8 次"**这个计数本身**拆干净 —— 因为项目手册要求改 `app/**` 必须重启服务（`Stop-Process` → 删 `watchdog.state` → `schtasks /run`），计数里很可能混着我们自己的计划性重启。只读 `logs/watchdog.log` + `git log`，把每次探活调用分成**定时档**（分钟尾数 5 且秒 ≤9，即计划任务 `PT10M` 的 cadence）与**额外调用**，再算各自的"自愈"命中率。判据冻结在 `plans/watchdog-restart-forensics-prereg-20260922.md`（F0–F5；只在**未见区间** 09-15~09-21 判决）；产物 `experiments/watchdog_restart_forensics.json`。**结果**：定时档 985 次调用 → 自愈 8 次（0.81%）；额外 30 次 → 26 次（86.7%）⇒ C1 PASS ⇒ **自愈计数主要来自非定时的计划性重启**；真·自发死亡 ≈ **1.1 次/天**，每次 <10 分钟。⚠️ **证据上限**：本机 `Microsoft-Windows-TaskScheduler/Operational` 日志 **enabled: false** ⇒ 无法区分"手动 `/run`"与"任务延迟启动"，结论只到"不合计划 cadence"这一层，**不许**写成"已证实是手动重启"。**全程只读**：未启用日志通道、未改计划任务、未改任何阈值/判据。
-   `monthly_exec_attribution.py` — **月度方案的执行归因**（2026-09-23）：对比「按策略锚点/决策价建仓的**理论组合**」与「按实际成交价建仓的**实际组合**」，把差额拆成四层 ①执行损耗（再拆 *延迟段* 与 *相对开盘 arrival 的成交滑点*）②权重偏离（Brinson 单层: Allocation/Selection/Interaction）③各标的收益贡献 ④市场/行业/风格（申万 L2 等权篮子剔除自身 + 流通市值分组的风格篮子）。判据与全部公式**先冻结**在 `plans/monthly-exec-attribution-prereg-20260923.md`；产物 `experiments/monthly_exec_attribution.json`。两条恒等式必须校验通过才可出报告：`Δ = Σw^a(P^a−P^f)/P^a`（零残差）与 Perold 乘法式 `(1+r^a→t)=(1+r^delay)(1+c^slip)(1+r^post)`。⚠️ **结论反直觉但必须照实写**：亏的是**理论**（−1.76%，约 88% 由大盘解释），实际是**赚的**（+3.90%），全部差额来自**延迟 15 天入场的价格优势 +5.45pp** —— 单样本，**这是运气不是能力**，不得写成"晚下单更优"。**DB 只读、不改成交口径、不换 pin**。
-   `monthly_rule_expectancy_backtest.py` — **月度选股规则「长期期望是否为正」回测**（2026-09-26，预注册 `plans/monthly-rule-expectancy-prereg-20260923.md` 的执行体）：model_score 116 锚点按 score 降序取前 k(k∈{5,10,20,30,50}) 等权、T+1 开盘建仓、月度开盘全换仓；不可成交顺位补足，可交易域直接用生产 `PITEngine.tradable_mask`（六层/时点名称/首现锚定）；成本直接 `import` 生产 `fees.trade_cost`（最低佣金 5 元非线性逐笔计入），资金两档 A=11,898 元 / B=2,000×k；判决格 m>0 且 t≥2 ⇒ 存在正期望 / t<2 ⇒ 不可辨 / m≤0 ⇒ 未见正期望；**不挑最优 k**。产物 `experiments/monthly_rule_expectancy.json`。⚠️ 只读（mode=ro）：不写生产表、不改成交口径、不换 pin；涨跌停不可成交是冻结的乐观偏差（各臂同偏差）。

---

## ⚠️ 危险脚本护栏（2026-09-13 新增，独立审查 §4.3）

上面那份索引说的是"每个脚本干什么"。这一节说的是"**哪些脚本按一下回车就会动生产数据**"。
精确复扫（只认真正的写语句，排除注释与"内存里 monkeypatch `load_config`"这类假阳性；
详见 `tests/test_oneoff_danger_guards.py` 的判据）——本目录里**真会写生产表的只有 6 个**：

| 脚本 | 它会动什么 |
|---|---|
| `migrate_daily_score.py` | `DELETE` 生产 `model_score`、`INSERT OR REPLACE INTO daily_score`、`UPDATE paper_position` |
| `reset_paper.py` | 清空模拟盘各表 + 重建 `paper_account`（重置纸面账户；**先自动归档**, 再 `pipeline_run` 留痕） |
| `ab_style_guard.py` | **`UPDATE regime SET state=?`**（改生产市场状态）＋结束时清理本次 run 的 `backtest_*`；**平铺脚本：导入即执行** |
| `fix_ann_dates.py` | `UPDATE fina_indicator SET ann_date=?`（改全市场披露日） |
| `exp_die_flow.py` | `DELETE FROM {--table}`（表名来自命令行，可能指向生产打分表） |
| `fix_alpha_bank_rerun_20260918.py` | `UPDATE alpha_bank`（expr/n_tests/gate_json/note）＋门禁重跑 `upsert` 覆盖那 10 行读数；**默认 DRY-RUN 不写**，`--write` 才动库，写前逐行归档并读回自检 |

这 6 个都已加护栏（`_guard.require_confirm`）：**默认拒绝执行**，必须显式放行 ——

（护栏实现：`_guard.py` —— 默认拒绝、显式放行、把"要动什么"写进报错信息；它不替代备份。）

```powershell
$env:ONEOFF_CONFIRM = "脚本名.py"     # 或 = "ALL"（明确知道自己在做什么）
D:\Python313\python.exe -X utf8 scripts\oneoff\<脚本名>.py
```

跑之前请先 `D:\Python313\python.exe -X utf8 scripts\backup_db.py`。

**另有 28 个脚本会写库、但只动自己那摊**（`DELETE FROM {} WHERE run_date=?` 清本次回测 run；
或只删/重建自己的隔离表 `*_tmp` / `model_score_{SUFFIX}` / `model_score_die_d1replica`），
已逐个复核、**不需要**护栏，但名单钉在 `tests/test_oneoff_danger_guards.py::KNOWN_DB_WRITERS` 里：
新脚本要么进"危险"（加护栏），要么进那份已复核名单（写清理由），否则测试会红。
- ★ `inquiry_delay_probe_20260928.py` — **问询回复延迟因子探针**(2026-09-28 深夜,
    预注册 plans/inquiry-delay-probe-prereg-20260928.md, 38 补登记候选执行轮):
    打标语料标题正则配对 ask/reply(1,547 对/846 股, 365 日窗口最近值 clip 365)
    → 生产口径中性化 → gate.evaluate + verdict_single(S1-S4 与 31 在池去相关)。
    **判决: 覆盖中位 0.41% ≪ 5% 门槛 ⇒ 覆盖不足线结清**(数据不支持, 非因子无效;
    方向暗合假设——DEAD head −0.85%/月 t=−1.18、与 31 在池 |ρ|max 0.091 高度正交,
    但截面 n=21/3 不足以判读)。复活条件 = 问询-回复数据源升级到全量结构化。
    只读零写库; 产物 `experiments/inquiry_delay_probe_20260928.json`。
- ★ `portfolio_synth_anchorkey_20260928.py` — **组合合成锚点键更正核算**(2026-09-28 深夜,
    07 号事故 #66 / 05 号四十二补): 41 补组合合成把袖套行按执行日(周一) ISO 周做键,
    而收益归属期=锚点周五→执行周一持有周 ⇒ 相对车腿错位一周, ρ(车,袖套) 0.65 被压成
    0.054。本脚本按锚点键(执行日−3 天)重算 42 补 §二全部数字(双流 P1r 夏普 0.748-0.801
    ≤ 车单独 0.851; 三流 P3 0.929-0.949 仍成立)。只读, 数字已冻结登记。
- ★ `eps_revision_eval_20260929.py` — **O4 EPS 修正动量族正式评估**(2026-09-29, 预注册
    `plans/eps-revision-prereg-20260929.md`): research_report_em 的 fy1/fy2/fy3_eps 按
    采集时点财年重映射(fy1=FY2026 预测, 四证据在预注册 §0)⇒ 同财年跨报告修正 3 候选
    (分母 3)→ 门禁家族口径 + verdict_single(S1-S5 未测如实) + ACTIVE 降级读数
    aS1/aS2/S4(hetero 同款现役池 Spearman)。读数: rev_fy1_1m ACTIVE −0.65% t=−2.01
    (28 锚点)与现役 |ρ|max 0.056 ⇒ 触发 A/B; 1m 反向/3m 正向变号; 财报季无效应=
    关注度反向家族第 4 例。只读零写库; 产物 `experiments/eps_revision_eval_20260929.json`。
- ★ `eps_revision_ab_20260929.py` — **O4 修正剔除器 A/B**(2026-09-29, 预注册 §3.3 执行体):
    rev_fy1_1m top 10% 分位剔除不补位(chase6 同款缝注入), ctrl/B 85 期 + ctrl_fh/B_fh
    全史窗。**有效性门 ✓(ctrl 逐位复现 pin 20260928_exe); 判决 REJECT(P1 未过:
    ACTIVE Δsys_ann −0.14pp; 全史 Δ+0.00 结构性稀释)** ⇒ O4 线结清(05 号 44 补)。
    事故在册: 留痕表键=引擎返回 run_date(带日期前缀)非自拼 tag; fh 臂剔除池须按
    fh 锚点单独构建。run 留痕 `20260929_abeps_*` 保留不自清。
    产物 `experiments/eps_revision_ab_20260929.json`。
- ★ `head_weight_ab_20260929.py` — **B1 多头端样本加权 A/B**(2026-09-29, 预注册
    `plans/head-weight-prereg-20260929.md`): trainer 新缝 head_weight(None 逐位不变)
    —— 保持回归标签只对截面标签 top10% 加权 w=1+(hw−1)·1{head}(LSW2 式, 广发 2025-06
    手册; ≠head_vt 换标签/≠uniqueness 时间加权)。双种子集配对(A=生产组/B=[7,13,99,2024])
    3 重训 + 4 回测。**判决 REJECT(ΔA −0.30pp/ΔB −0.10pp 同向为负, 回撤劣化 −14/−10pp)
    —— 第 12 轮"改模型"失败**(05 号 46 补): LSW2 +0.55pp(沪深300池)不迁移, 头部加权
    =放大风格暴露反噬; B2(margin 损失)按条款不触发。隔离表 hw*_*/归档 hw*/留痕
    `20260929_abhw_*`。事故在册: 实验表基建五折清单(表名小写/metrics 键经
    _metrics_from/ensure_score_table+importance DDL 同建/archive_dir 传 Path)。
    产物 `experiments/head_weight_ab_20260929.json`。
- ★ `e2e_ab_20260929.py` — **E2E-DL 双种子集 A/B**(2026-09-29, 预注册
    `plans/e2e-dl-prereg-20260929.md`): 第 13 轮"改模型"=**范式变更**——损失=组合层
    目标本身(top-softmax 交叉熵/ListNet, τ=逐期 std(y) 自适应), 绕开"IC 与 top-6
    收益反相关"代理脱节。numpy 手写两层 MLP(26→64→32→1, ~3.3k 参数, torch 被
    Smart App Control 拦 WinError 4551 见预注册 §实现引擎变更; 梯度数值对照单测
    tests/test_e2e.py 钉死)。walk-forward 同生产(36 月窗/holdout6/PIT 域/26 因子),
    4 种子秩平均。E2E_A vs 生产 pin + E2E_B vs B1 轮 ctrl_B 复用, 判据=组合层档
    P1+P2+P3(换手=端到端病理针对性判据)。留痕 `20260929_abe2e_*`。
    产物 `experiments/e2e_ab_20260929.json`。
-   `watchdog_s13_eval_20261001.py` — **S13 取证复跑（验收体）**（2026-10-01，14 号在册到期项；判据 F0-F2 逐字同源 `watchdog_restart_forensics.py`，只换评估窗与判决语义）：评估窗 = CREATE_BREAKAWAY_FROM_JOB 处置启用后（09-25~10-01，09-24 四次计划内重启按日期过滤天然窗外）→ 定时档自愈率 vs 基线 0.81%（主判据，纯定时档桶）+ C2 识别计划性重启（辅助）。判决映射：0 次=支持 / ≤½基线=部分支持 / 不变=无效回到"不猜"。产物 `experiments/watchdog_s13_eval_20261001.json`。
-   `s1_excess_recheck_20261002.py` — **S1 探针主判轴偏差只读复算**（2026-10-02 全仓审查发现）：`s1_confidence_probe.py` 原实现 `excess = r6.mean() - f_x.mean()`，f_x 为截面去均值行 ⇒ 减数恒等 0，主判轴实际 = top6 原始收益（市场分量混入，|t| 稀释），与冻结判据 §5.2 "top6 − 截面等权均值" 不符。本脚本同一数据快照三轴并排（raw/fixed/ind）：raw 轴逐位复现在册读数（C1 t=−1.45），fixed 轴 C1 t=−2.17 过 T1 门槛但 T3 两子窗仍异号 ⇒ **20260929 在册判决 CLEARED 不被翻转**；ind 轴（原实现即正确）T1∧T3 双过 = 维持"旁证只登记"。产物 `experiments/s1_excess_recheck_20261002.json`；已登记证据未触碰，探针修正后产物改写 `s1_confidence_probe_v2_20261002.json`。
-   `gap_waterfall_20261002.py` — **差距瀑布分解**（2026-10-02，用户指令"继续审查分析，我要知道我们的量化程序差在哪里"）：同基准逐层量化"模型 α 本体 → 实现"的漏损——L0/L1（top6 21 日前瞻 vs 截面池/PIT 域内）与 **L1_index（vs 中证800 同窗前瞻，与回测满仓超额同基准，截面等权池≠市值加权指数不可混用）**→ 满仓超额 → 系统超额。**读数（prod/full）：域过滤 0.0/0.0；机械损耗 +10.7/+12.3pp/年 ≈ 成本 3.2 + 建仓时点漂移 ~7（t 收盘→t+1 开盘，池子正漂移 = 短半衰期 α 的物理税，K 线择时全族已证不可挽）+ 整手零星；现金缓冲折扣 +2.6/−0.1pp（换 MDD 改善 5.5pp，结构选择非损耗）；纸面 12 日超额 +3.91%（执行层与回测同构无异常）。结论：模型/选股域不是短板，候选洞均被在册实验证伪可挽回性。**产物 `experiments/gap_waterfall_20261002.json`。
scripts/oneoff/tender_offer_event_study_20261002.py — **要约收购事件族 Phase 1 事件研究**（2026-10-02，预注册 plans/tender-offer-event-study-prereg-20261002.md 跑前冻结；小资金路线 S1 主候选，用户拍板'同意'）：锚点 = 每票首个'要约收购+报告书'标题公告（60 日窗去重、北交所排除），事件臂 = t0+1 开盘买入持有 N 交易日超额 vs 中证800；判据 C1 事件≥100 / C2 CAR21>0 且 t≥2（做多-only，负=不可交易）/ C3 扣双边 0.6% ≥ +1.0%/事件。产物 `experiments/tender_offer_event_study_20261002.json`。
scripts/oneoff/exec_early_oracle_20261002.py — **执行时点前移 oracle 上界探针**（2026-10-02，预注册 plans/exec-early-prereg-20261002.md 跑前冻结）：量'组合敞口从 t0+1 开盘前移到 t0 收盘'的隔夜漂移上界（top6 等权近似 pin 持仓，市场基准=全截面 EW 隔夜，换手净效应分离报告）。判据：prod 隔夜超额 ≥+0.10%/月 GO 立项真实臂 / <+0.05% NO-GO 线结清 / 中间 INDETERMINATE；换手净效应侵蚀条款。产物 `experiments/exec_early_oracle_20261002.json`。
scripts/oneoff/probability_audit_20261002.py — **概率审计**（2026-10-02，用户问'炒股是概率的事情我们做得如何'，只读）：A 单注优势=月度超额 胜率/赔率/利润因子/最长连亏/偏度 + HAC t 与年化 CI；B 广度=top6 个股跑赢截面均值的 base rate（二项 z vs 50%）；C 校准=IC 正月份近似。产物 `experiments/probability_audit_20261002.json`。
scripts/oneoff/gold_grid_backtest_20261002.py — **黄金 ETF 网格回测**（2026-10-02，预注册 plans/gold-grid-prereg-20261002.md 跑前冻结）：固定锚点 1% 间距 20 格/底仓 50%/T+0 双向/双佣金口径（万2.5 最低5元 vs 万1 免五），判据 D1 净年化−货币垫≥+2pp / D2 单笔胜率≥60% 且月胜率≥65% / D3 MDD≤B&H+2pp，D0=用户券商免五。定位 = S3 现金垫升级候选（高胜率结构在 2 万账户的唯一可行形态）。产物 `experiments/gold_grid_backtest_20261002.json`。
scripts/oneoff/lambdarank_ab_20261002.py — **第 14 轮改模型:排序目标(lambdarank)vs 回归 A/B**（2026-10-02，预注册 plans/lambdarank-prereg-20261002.md 跑前冻结，用户指令'训练目标应该是排序'）：独立走查复用 mlattr month_block/prepare，唯一变量=目标族（regression→lambdarank 十分位 1..10 group=逐月截面），超参逐字复制生产 PARAMS+50 轮，双种子集（A=[42,7,123,2024] vs 生产 pin / B=[7,13,99,2024] vs hw_base_b）双窗换表回测，P1/P2/P3 判据同 B1/E2E。**同时了结 MOD-02 审计缺口（lambdarank 完败无实证）。**产物 `experiments/lambdarank_ab_20261002.json`。
-   `collect_tender_offers_20261002.py` — **要约收购报告书正文采集**（2026-10-02，预注册 plans/tender-offer-phase2-prereg-20261002.md §一）：事件列表与 tender_offer_event_study_20261002.load_events 逐字同源（importlib 载入禁裸名 import），静态 PDF 地址/直连/限速 1.2s/重试/断点续采，209/215 份成功（97.2%）。产物 data/text_corpus/tender_offers/（gitignore）。
-   `tender_offer_spread_study_20261002.py` — **要约价差研究（Phase 2 判决器）**（2026-10-02）：空白归一（pypdf 换行劈词）+ 表格压平行正则抽要约价/类型/比例（D1 命中 82.9% PASS）；**判决 NO-GO：要约价 > t0+1 可执行价的事件 0 起**（median 溢价 −68%，公告日跳空一步到位）。产物 `experiments/tender_offer_spread_study_20261002.json`。
- ★ `exec_early_oracle_20261002.py` / `gold_grid_backtest_20261002.py` / `probability_audit_20261002.py` — 10-02 三只读探针（exec-early/gold-grid/概率审计三预注册或评估文档），详见台账六十/六十二/六十一补。
- ★ `tender_offer_event_study_20261002.py` — 要约事件族 Phase 1 判决器（台账五十八补）。
- ★ `lambdarank_ab_20261002.py` — 第 14 轮排序目标 A/B 判决器（台账六十三补，REJECT）。
`lambdarank_diagnose_20261002.py` — **lambdarank REJECT 判决对抗性自查**（2026-10-02，用户问'有没有可能被错误判负'）：T1 标签方向(+0.903)/T2 样本内学习(+0.34)/T3 与 pin 序相关(+0.01)/T4 欠训练排除(早停 best_iter=14 仍反向) ⇒ **样本内正、样本外负 = 真实泛化失败，REJECT 加固**；机制 = lambdarank 默认指数 label_gain 背诵历史 top 十分位身份，OOS 均值回归反向。产物 `experiments/lambdarank_diagnose_20261002.json`。
`falsification_meta_audit_20261002.py` — **证伪判决元审计**（2026-10-02，用户指令'对所有证伪实验复核'）：111 份产物三层复核（存在性/台账数字一致性/判决词），14 份精确匹配、2 份证据链缺口（平滑 −2.49pp/拥挤 −5.68pp 会话内现算未落盘，方向经 DB 独立证实）。产物 `experiments/falsification_meta_audit_20261002.json`。
`meta_label_probe_20261002.py` — **元标签层探针**（2026-10-02，预注册 plans/meta-labeling-prereg-20261002.md 跑前冻结）：696 注赌注级数据集 + 8 PIT 特征 walk-forward（numpy Logistic / 原生 LGBM 双变体），四臂含 H&T 零 ML 对照。判决 NO-GO（F 臂灾难跑输，单注胜率仅 +0.6pp）；副发现 R_size 候选（F55 先例警示）。产物 `experiments/meta_label_probe_20261002.json`。
`rankweight_ab_20261002.py` — **分数排名加权 sizing A/B 判决器**（2026-10-02，预注册 plans/rankweight-prereg-20261002.md 跑前冻结）：引擎级 A/B（rank_weighted 缝，缺省逐位不变），双窗 vs pin。判决 INDETERMINATE——prod Δsys −8.47pp 且 **ΔMDD −18.85pp**（集中度尾部代价实锤）、full +1.70pp 在种子噪声带内 ⇒ 落地说不成立，路径结清。产物 `experiments/rankweight_ab_20261002.json`（全量 Δ 持久化）。
