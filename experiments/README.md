# `experiments/` —— 实验产物索引（2026-09-22 立）

> **为什么要有这份索引**：本目录此前**没有根层 README** —— 只有 `b1_pin_20260920/`
> 与 `b_stage_20260920/` 两个子目录各自带 README。结果是 11 个裸 JSON 只能靠
> `docs/know/14_未结清单与接手起点.md` 反引，**入口分散**：想知道"这个 json 出自哪个脚本、
> 支撑哪条结论"，得先猜文件名再全仓搜。（2026-09-22 结项审计发现。）

## 一、裸 JSON（根层）—— 一份一行，写清"出自哪、支撑什么"

| 产物 | 生成脚本 | 支撑的结论 |
|---|---|---|
| `s5_tiebreak_factor_level.json` | `scripts/oneoff/s5_tiebreak_factor_level.py` | **S5 因子级**对照：翻档 35702/7655774（**0.4663%**），122/189 因子受影响 ⇒ 推翻"零影响"预期 |
| `s5_tiebreak_impact.json` | `scripts/oneoff/s5_tiebreak_impact.py` | S5 **生产分**对照：翻档 12/44385（0.027%）⇒ 判据已被上面那份取代 |
| `s2_pool_guard_impact.json` | `scripts/oneoff/s2_pool_guard_impact.py` | S2 护栏影响：翻档率 **8.57%**（现行 vs 无护栏） |
| `s2_pool_scale_candidates.json` | 同上批脚本 | S2 **四臂换尺** V0~V3 ⇒ **推翻方向**：现行尺偏**进攻**不是偏防守 |
| `m3_q2_percentile_ab.json` | `scripts/oneoff/m3_q2_percentile_ab.py` | M3/Q2 两把尺阶段 A：秩相关 0.999475 ≥0.99 ⇒ **不越门**（阶段 B 关闭） |
| `seed_resolution.json` | — | **验证分辨率刻度**：L1~L4 的可辨 Δ（"堆 seed 救不了小效应"） |
| `seed_variance_decomposition.json` | — | F78 方差分解（σ_time / σ_total / σ_b 与各杠杆） |
| `weekly_freq_baseline.json` | `scripts/oneoff/weekly_freq_baseline.py` | 周频基线（走自带后缀的隔离表，不碰生产） |
| `weekly_freq_hyst_scan.json` | — | 周频迟滞扫描 |
| `weekly_rebalance_ab.json` | — | **周频调仓 A/B ⇒ 证伪**（信号更好但净值不通过） |
| `restore_drill_readonly.json` | `scripts/oneoff/restore_drill_readonly.py` | **备份只读恢复演练（段 A）**：生产库与备份 `quick_check` 均 ok、备份停在过去时点 ⇒ 可用于恢复 |

## 二、子目录

| 目录 | 项数 | README | 内容 |
|---|---|---|---|
| `b1_pin_20260920/` | 8 | ✅ | 承载现行 pin 的 2.22pp 与 P-a/P-b 证据；**含 `MANIFEST.json`**，由 `scripts/oneoff/track_b1_evidence.py --verify` 守（篡改即红） |
| `b_stage_20260920/` | 14 | ✅ | B 阶段四档筛选口径的产物（含 `m4_pool_filter_bias.json`） |
| `05_label/` | 220 | ❌ | 标签/训练类实验（F1~F78 系列）+ **27 份分数面板 `.npz`（253MB）**。⚠️ 这 27 份**不是孤儿** —— 它们是若干实验**共享的基线臂存档**，被大量脚本引用；详见 §三 |
| `01_data/` | 8 | ❌ | 数据层实验 |
| `09_cost/` | 6 | ❌ | 成本/费用层实验 |
| `10_label_adj_ab/` | 11 | ❌ | 标签调整 A/B |
| `mirror/` | 5 | ❌ | 镜厅类实验（M0 相关；部分叙述性文档已由 `4a2bfd72` 删除，可 `git show 1dc2f5c3:<path>` 复取） |

## 三、已知未闭环（如实登记，未处理）

1. **5 个子目录无 README**（`01_data` / `05_label` / `09_cost` / `10_label_adj_ab` / `mirror`）
   —— 上面表里的"内容"一列是本次审计**按文件名推断**的，不是产物自述。**要复核请以
   14 号台账与 `docs/know/05_实验台账.md` 为准。**
2. ~~**`05_label/` 里 27 份 `.npz` 是孤儿产物**：约 270MB，被 `.gitignore` 忽略、
   无批次说明、不知属于哪轮实验。~~
   **⚠️ 2026-09-22 更正：这条**错了**，而且是个**危险的错**。** 复核后的事实：
   那 27 份 `.npz`（**253MB**，非 270MB）**全部可按文件名归属到实验**，
   且**被大量脚本引用** —— 它们不是"没人要的残留"，而是若干实验的**共享基线臂存档**：

   | 组 | 数量 | 体积 | 性质 |
   |---|---|---|---|
   | `F4_scores_seedA.npz` | 1 | 10.4MB | **被 41 个文件引用** —— F12~F25、F40~F46 等一整族实验的**基线臂** |
   | `F4_scores_seedB.npz` | 1 | 10.4MB | **被 28 个文件引用** —— 同上，第二套种子 |
   | `F4_weekly_lgb_scores.npz` | 1 | 10.4MB | 生产者的原始输出名；与 `seedA` **字节完全相同**（sha256 都是 `7fb21c0e…`）⇒ `seedA` 是它的副本（给种子一个稳定名字） |
   | `F29_scores_*` | 8 | 81MB | F29 各臂 |
   | `F47_scores_*` | 10 | 96MB | F47 各臂 |
   | `F32_/F33_/F70_scores_*` | 6 | 44MB | 各自实验的各臂 |

   **再生路径**：`F4_weekly_lgb.py:166` 用 `np.savez_compressed` 写出
   `F4_weekly_lgb_scores.npz`（即 seedA）；seedB 是另一次训练 ⇒ **可再生，但要重训**（不便宜）。

   ⇒ **结论：不要删。** 它们是"一整族实验的可复现底座"，被 `.gitignore` 忽略是**有意的**
   （大二进制、可再生），不是"没人管"。**本索引初版把它写成"孤儿、归属未知、删前确认"，
   是审计误报 —— 如果照着删了，F12~F25 / F40~F46 这些实验就失去基线臂、无法复跑。**
   （这条与 `scripts/oneoff/_guard.py` 里记的"关键词扫描把自清理脚本误报成危险脚本"是同一类
   教训：**"看起来没人要"≠"没人用"，动手前先 grep 引用**。）
3. 本索引**不参与机器校验** —— 也就是说，将来新增产物不会让任何测试变红，
   这份表同样会悄悄过期（`scripts/oneoff/README.md` 的文件数就漂移了 25 个才被发现）。
   若要让它长期可信，应像 `track_b1_evidence.py` 那样**由脚本现算**、而不是手写。

## 四、纪律提醒

- `experiments/**/*.npz` 被 `.gitignore` 忽略；`.py` / `.json` / `.csv` **入库**。
  ⇒ 承载结论的 JSON 必须落在这里（**不要只落 `logs/`** —— 那被整目录忽略，证据会消失）。
- 产物**可覆盖** + 哈希**手抄进文档** ⇒ 文档里可能留下一次已不存在的运行
  （07 号记过这个教训：`66bf3049a59b8704` 复现不出）。**重跑请换文件名（带时刻）**，
  或让哈希**由脚本现算**进 MANIFEST。
