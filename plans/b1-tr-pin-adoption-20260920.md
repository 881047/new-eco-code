# B1 采纳轮预注册 —— 回测报告腿换成中证800**全收益指数 H00906.CSI**（2026-09-20 冻结）

**冻结时间**：07:35（在任何一次重钉跑数之前）。改前留证：`logs/b1_pin_evidence_before.json`
（由 `scripts/oneoff/b1_pin_evidence.py --stage before` 产出，只读）。

## 0. 本轮要办什么

上一轮真尺 A/B（`plans/b1-real-tr-index-20260919.md`）过完 C1/C2/C3 三道门，实读抬高量
**2.22pp/年，生产窗与全史窗完全一致**，且 C3 证明**回测路径上**除基准三字段外全部逐位不变。
用户拍板采纳 ⇒ 本轮 = 把这条尺落地为**现行 pin**，并按 14 号三补二十七 §⑤ 五条清单同步。
> ⚠️ 时效注记（2026-09-27 追加）：现役 pin 已更迭为 **`20260927_excl5`**（第 6 次重钉：factor_exclusions 剔除 5 个 S5 判否因子，组合层档 PASS；05 号台账 十九/二十补）。下述 pin 数值均为**当时的 pin（历史值）**。

> ⚠️ 时效注记（2026-09-22 追加）：自 2026-09-22 起现行 pin = **`20260922_trbench`**（用户拍板；2026-09-26 起现役 pin = `20260926_chase2`（chase6 建仓追高剔除闸拍板采纳重钉）
> 提前 --full 全量重训后第 4 次重钉，超额 `0.1660 → 0.1180`）；本文所述 `20260920_trbench`
> 已退为**当时的 pin（历史值，不可再生**——model_score 已被 09-22 --full 覆盖）。
> 依据：`plans/early-full-retrain-20260922.md`、08 号"第 4 次重钉"。

## 1. 对 §⑤ 的两条更正（照原实现做会出事，先记下来）

| §⑤ 原话 | 实测（读码 + 只读 SQL） | 更正后的做法 |
|---|---|---|
| -1「代码一行：`factor_engine.py:180-181` 的 bench 取码改为 `H00906.CSI`」 | **做不到"一行"**。同一个 `m["bench"]` 被 `compute_price_factors` 用来算 **`beta_250`**（`factor_engine.py:234` → `:259`），而 `beta_250` ∈ `app/models/daily_score.py:26-32 ALL_FACTORS`（22 个价量因子之一，月链与周频推分都用它）。⇒ 原地换码 = 改**生产特征** = 改打分 = 改决策，且历史因子行与新行混尺。上一轮 A/B 的 C3 不变性**覆盖不到**这条链（回测读已落库的 `model_score`，不重算因子） | **拆两腿**：`m["bench"]` 继续是价格指数（特征腿，构造逐字不动），新增 `m["bench_tr"]` = 全收益，只供回测报告腿与袖套 KPI 消费 |
| -2「金标准重钉：`bench_nav/excess_ann/win_rate` 三个字段会变」 | **不会变**。夹具与 `config.yaml` 解耦：`tests/test_backtest_golden.py:37-45 _FIXED_CFG` 根本没有 benchmark 键（落到 `benchmark_code()` 的缺省 `000906.SH`），`:105-107` 只合成 `000906.SH` 一列，`:102-104` 的 `adj_factor` 恒 1.0（夹具不产生分红 ⇒ 三补二十六 §④ 已预警过这一点） | 金标准**不重钉**（禁 `REGEN_GOLDEN=1`）。改为：`_FIXED_CFG` 显式钉 `"benchmark_nav_code": "000906.SH"` 保证两份快照一字不动，另建 `tests/test_bench_leg_wiring.py` 用合成双码把新键的接线路径钉住 |

⇒ 本轮"重钉"的对象只有：库里的 pin 行（`backtest_summary`/`backtest_nav`/`backtest_nav_daily`）+
`config.yaml` 的 `backtest_run` 与注释块 + 08 号 + 各处读数文档。**不含** `tests/data/golden_*.json`。

## 2. 判据（三条，先冻结；任一不过 ⇒ 不换 pin 并如实登记）

- **P-a 端点恒等**（前置门，卡在换 pin 之前）：`scripts/pin_baseline.py --tag trbench` 产出的
  `20260920_trbench` 行 **13 键必须与 `20260919_b1real_prod_c` 逐位相等**；`20260920_trbench_full`
  与 `20260919_b1real_full_c` 逐位相等。基准值（今日实读，`data_max` 全部 = 20260918）：
  prod_c `bench_ann 0.0602 / excess_ann 0.166 / win_rate 0.6235 / ann_return 0.2262 /
  max_drawdown −0.241 / sharpe 0.853 / avg_turnover 0.9018 / ic_mean 0.0959 / icir 0.86 /
  sys_ann_return 0.194 / sys_max_drawdown −0.1966 / sys_mdd_daily −0.3172 / periods 85`。
  ⇒ **比的是昨天的 C 臂，不是旧 pin**（理由见 §3 已知差）。
- **P-b 特征不变**：`--stage after` 重算 `compute_price_factors` 的 22 个价量因子指纹，
  必须与 before 完全相同（before 值：整体指纹 `eb0bc486562742d4`、`beta_250` 指纹
  `00e42471788cafa8`、`bench` 序列指纹 `dc8c7a8cf04681af`）。这是"两腿没被串味"的机器证明。
- **P-c 决策不变**：`tests/data/golden_backtest.json` 与 `_stop.json` 的 `git diff` 必须为空；
  全量测试除本轮新增用例外零 fail。

## 3. 已知差（不是缺陷，跑之前就算清楚，免得事后含糊）

新 pin 行与旧 pin `20260912_limitquant` 之间，除本轮有意改的三个报告字段外，还有两处**与尺无关**的差：

| 字段 | 旧 pin（09-12） | 昨天的 A/C 臂（09-19） | 归因 |
|---|---|---|---|
| `avg_turnover` | 0.8912 | 0.9018 | 比值 = 85/84，正是 05 号 P2-1（换手分母由 `n_periods` 改成"真正计入的期数"，当时 goldens 已故意重钉，只动这一个字段）。三补二十七 §① 已把它算成恒等式判定 |
| `icir` | 0.859 | 0.86 | IC 序列随 09-16~18 因子表刷新而微动（三补二十四 P0-3 sanity 注记同因）。`ic_mean` 0.0959 未动 ⇒ 是 σ 的第 4 位、不是判决档位的移动 |

⇒ 这两处**不追**（追也追不回来，且改了会毁可复现性）；但它们必须写进 08 号与 config 注释块，
否则下一个拿新 pin 比旧 pin 的人会以为是本轮换尺造成的。

## 4. 不可观测项（禁止在报告里承诺）

`app/weekly/ledger.py:431` 有"不足 8 周不报年化/超额"的硬守卫。今日实读 `/api/weekly`：
`weeks=4`、`excess_pp=None`、`note="样本仅 4 周…暂不报"` ⇒ **袖套 KPI 的同尺改动今天线上不可观测**，
只能靠接线测试证明。报告里写"看板超额已下修 X pp"即为假话。

## 5. 有意不动的边界（写死，防"顺手统一"）

| 消费方 | 保持读 | 为什么不能一起换 |
|---|---|---|
| `factor_engine.py:234,259`（`beta_250`） | `m["bench"]` 价格指数 | 换它 = 换生产特征 = 重训 = 决策变（§1 第一条） |
| `app/models/regime.py:173-175` | `benchmark_code()` | 择时均线系统算在哪个指数上直接决定 attack/neutral/defense = 改策略 |
| `app/models/style.py:98-102` | 同上 | 风格读数是"池 vs 中证800"的描述性对比 |
| `app/paper/engine.py:664-667`（`paper_nav.bench_nav`） | 同上 | 用户已定不换：账户 09-14 重置、该列只有 5 行（实读 4982.16~5071.61 = 价格指数点位），为 5 行回填要付一次 11.85GB 备份 + `ONEOFF_CONFIRM`；不换时 `notify.py:563` 的归一化对比与 `TradingView.vue:252,255` 的"价格指数"图例**仍然字面正确** |
| `app/notify.py:102/120/635` 邮件文案 | 同上 | 文案与模拟盘同源 ⇒ 不改。但 `:120` 那句"与模拟盘/超额计算同一个源"的注释本轮起不成立，要就地更正 |

## 6. 实现要点（判据之外的最小改动面）

1. `app/config.py`：`DEFAULT_CONFIG` 加 `benchmark_nav_code="H00906.CSI"` + getter + `validate_config`
   非空检查。**回落语义**：`load_config` 做 `_deep_merge(DEFAULT_CONFIG, user)` ⇒ 删掉 yaml 那行
   仍是全收益（不会静默退回旧尺）；代价是**回滚必须显式写 `000906.SH`**（进 08 号回滚段）。
2. `factor_engine.py`：一条 SQL 取两码、`panel` 加 `bench_tr`、`market_fingerprint` 加
   `bench_tr=<nav code>`（否则换码不作废缓存 = 静默供应旧腿），`_cache_save/_cache_load` 读写
   `bench_tr.parquet`；**`reindex` 不加 `ffill`**（加了破坏 P-a），NaN>0 只 `log.warning`。
3. `backtest/engine.py:612-613,665` 三处 → `m["bench_tr"]`。
4. `weekly/ledger.py:437-443` 取码 → `benchmark_nav_code()`（与 3. 同一个 commit，防"月频改了袖套没改"）。
5. 前端：`PlanView.vue:112,116` 图例改「中证800全收益指数(H00906)」+ `npm run build`；
   `TradingView.vue` 不动。
6. 守卫：`tests/test_pin_ledger_consistency.py` 的 `STALE_VALUES += ("18.82","0.1882")`。

## 7. 失败处置

P-a 不过 ⇒ **停在 S6**，`config.yaml` 一个字不改，代码 revert，并在 14 号写"采纳未成功 + 差异
明细"。不许"再改一版判据"，也不许把差异含糊成"数据漂移"——漂移只在 §3 那两处、且已算成恒等式。

## 8. 产物与登记

- 脚本：`scripts/oneoff/b1_pin_evidence.py`（只读证据，before/after 两份 JSON）。
- 重钉行 tag：`20260920_trbench` / `20260920_trbench_full`（不碰旧 pin 行，按 08 号保留不自清）。
- 备份：`data/backup/neweco_20260920.db`（11,848,900,608 B，写库前所建）。
- 登记：08 / 02 / 09 / 05 / 13 / 14 / 27 号 + README / CLAUDE，跑 `scripts/sync_know.py`。
- ⚠️ `api.py:87-88` 的 `latest` 回退脆性：本轮落 2 行新 pin + 保留 8 行实验行 ⇒ 同轮做"pin 缺失
  不再 DESC 兜底"的根治（工程项 S10-1），否则每次重钉都在加重它。
> ⚠️ 2026-09-28 时效注记: 文中"现行/现役 pin"为写作时点读数（历史值），
> 现役 pin = 20260928_excl5mdd（见 D:\know 8 号 pin 台账）—— 引用本文数字须注明档位。
