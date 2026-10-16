# 批次 2 第一步量化（2026-09-20）

- 产物：`audit_b_stage_first_step.json`（sha256 前 16 位 `354cae920b94ddf6`）
- 生成：`D:\Python313\python.exe -X utf8 scripts/oneoff/audit_b_stage_first_step.py`
- 门限（跑前冻结）：`plans/b-stage-criteria-20260920.md`
- **为什么这份在 git 里而 `logs/` 里那份不在**：`logs/` 被 .gitignore 忽略 ⇒ 只落 logs
  的量化证据会消失（本轮已发生一次）。凡进台账的读数必须落这种被跟踪的目录，
  并与脚本**同一个 commit** 入库。

# 批次 3 常数敏感性（2026-09-20，Q5 ⇒ 关闭维持 52）

- 产物：`per_year_52_sensitivity.json`（canonical sha16 **`8aeba915161c19cf`**）
- 生成：`scripts/oneoff/audit_per_year_52_sensitivity.py`；门限 `plans/b-stage-criteria-20260920.md`
- ⚠️ **台账 14 号 1488 行与 `scripts/oneoff/README.md` 原先记的 `66bf3049a59b8704` 复现不出**，
  原因是 Q5 的判据逻辑被作者当场修过两次（单位混、把"本来就在线下方"的负余量算成翻档风险）
  ⇒ **重跑覆盖了同名文件，但文档里的哈希没跟着重记**（不是产物被改）。已按
  `experiments/b1_pin_20260920/README.md` 声明的哈希口径重算并就地更正；口径本身经核无误
  （同目录下另两份产物的记录值用同一口径逐字复现）。

# 结清轮量化 M1/M2/M4/M5（2026-09-20 晚，判据先冻结）

门限与判据正文：`plans/ledger-settlement-criteria-20260920.md`（含 M4 第一版仪器作废的更正记录）。

| 产物 | 测什么 | 一句话判决 |
|---|---|---|
| `m1_ic_scale_flip.json` | Q3：IC 复权尺 vs 未复权尺会不会翻健康档 | K=5/607 翻档、全在 `healthy↔warning` ⇒ 按冻结分流"待跑第二步"；但 `warning` 不停买 ⇒ 呈报"第二步不采纳" |
| `m2_stuck_rollover_interest.json` | Q7：跨月停牌结转的占资少计息上界 | **关闭**：`U=0.0269pp/年`（门槛 0.2）；两窗合计仅 2 个结转格·月，量级小是因为**几乎不发生** |
| `m4_pool_filter_bias.json` | `pool_index` 那条 `ABS<=21` 护栏把池线抬高了多少 | **关闭**（深跌半边 0.0108pp/月）；副产品：上行半边 −0.3862pp/月 ⇒ 该线净体**偏低**，新开放项 |
| `m5_g3_channel_gate.json` | 批次 5 动作①：G3-① 换成"通道唯一性门"后重判批次 4 | **达**（对照分离 2.1e5×、6/7 条能判红）⇒ 批次 4 的 G1/G2 解除"不可引用" |

哈希口径与批次 2/3/4 一样按 `experiments/b_stage_20260920/README.md` 顶部（= `b1_pin_20260920/README.md`
声明的 canonical 式），由 `scripts/oneoff/audit_m*.py` 现算，不手抄。

# 批次 4 双尺门禁 A/B（2026-09-20）

- 产物：`alpha_panel_scale_gate.{json,md}`（sha256 前 16 位 `af2d2e12e1eac04e`）；
  中间臂 `_arm_{raw,adj,raw2}.json`（各 158 条读数）、`_probe.json`（G3-② 除权抽样）、
  `_mech.json`（G3-① 机制归因）。
- 生成：`scripts/oneoff/ab_alpha_panel_scale_gate.py --arm raw|adj|raw2|probe|mech|merge`
- 门限（跑前冻结 + 三次跑前更正）：`plans/b1-q1-dual-scale-gate-ab-20260920.md`
- 一句话结论：**RAW 裸价 vs ADJ 复权，两臂 158 条各跑一次死区/活跃区门禁，
  `save=False` 全程不写 `alpha_bank`/`alpha_pool`（G4 指纹前后一致）**。
  G0①同臂二次逐位确定 True；G1 两臂 PASS 均为 0（N=347 下无一可过）；
  G2 临界翻动 16 条（阈值 5）触发，且 16 条幅度都超过噪声地板；
  **G3-① FAIL（族内 max|Δhead|=3.2e-5 > 冻结容差 1e-6）⇒ 按冻结判据本轮"不可引用"**。
  机制归因（`_mech.json`）证明 G3-① 的不达是**判据前提错误**（秩选出的均值在 float32 下
  本就到不了 1e-6：恒等式残差≈3.7e-9 说明两臂差异只经"top-k 选面"这一个离散通道），
  不是构造错。⇒ 结论进 `D:\know\14` 三补三十·续2 + 批次 5 提案（改 G3-① 为值面板判据），
  统一尺与否由用户拍板。
