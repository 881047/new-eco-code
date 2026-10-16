# F77 组级消融预注册（Phase D / P0-3）—— 2026-09-19 冻结

> 判据先于运行写死。跑完不改本文件（结果与偏差只进 `_result.json`）。
> 主计划：`plans/p0-validation-upgrade-20260919.md` Phase D。

## 0. 对象与动机

27 号 §十二 #5 指控"31 因子可能存在冗余组"。已知先验（如实写明）：F62 加新因子族两次被否
+ IC↔top6 反相关四次复现 ⇒ 消融先验偏"**纠缠深、单组剔除难有正收益**"。本实验价值 =
给 27 号 §十二 #5 一个正式判决，不是找 alpha。

## 1. 分组（冻结；执行前 `--stage verify` 强制核对）

31 因子 = **价量22** + **财务6** + **龙虎榜3**（与 13 号清单一致；不一致处如实登记）：

- 价量22：rev_5, mom_20, mom_60, mom_120, mom_250, vol_20, vol_60, skew_20, ret_max20,
  turnover_20, turnover_chg, amount_log, illiq, bias_20, bias_60, high_52w, low_52w,
  beta_250, size, ep, bp, dv_ttm
- 财务6：roe, netprofit_yoy, or_yoy, grossprofit_margin, debt_to_assets（FINA_FIELDS）
  + eps_yoy
- 龙虎榜3：lhb_net20, lhb_org_net20, lhb_cnt20

核对规则（硬门）：三组清单与 `SELECT DISTINCT factor FROM factor` 的实名集合必须**恰为
一个划分**（并集 = 全集、两两不交、无多余无缺失），否则脚本拒绝进入后续 stage。

## 2. 臂与路径（冻结；含设计决策）

- G0 = 31 全集；G1 = 31−价量（剩 9）；G2 = 31−财务（剩 25）；G3 = 31−龙虎榜（剩 28）。
- 全部臂走 trainer `factors_override`；override 面板**从 `_load_factors_wide()` 派生**
  （`wide.xs(f, axis=1, level="factor")` ⇒ 与生产加载逐位同源，neutralize 已在写入口径做过，
  满足 run() docstring 的调用方责任条款）。
- **设计决策（本轮收口）**：G0 **也走 override**——四臂同路径 ⇒ 组间差不含"override vs
  生产加载"的路径差。生产路径另设**旁证锚**：F76b 的 base_A（生产现役 `model_score`，
  seed A 同集，已验证 = pin 复现）。
- **sanity 门（硬门，不透则停轮）**：G0_A(override) vs base_A 共同锚点分数 Spearman
  **最低 ≥ 0.999** 且 top100 集合 Jaccard **均值 ≥ 0.9**。不过 ⇒ 判定 override 构造
  ≠生产加载，全轮作废重查，**不得出任何判决**（组间读数一律作废）。
- 重训口径与 F76b 逐字同值：full=True, rounds=50×4 种子（ENSEMBLE_ROUNDS）、无早停、
  train_months=36、holdout_recent=6、backtest_start="2017-01-01"、label_neutral=industry。
- 隔离表：`model_score_ab_g{0..3}_{a,b}` + `model_importance_ab_g{0..3}_{a,b}`；
  归档 `data/models/ab_g{0..3}_{a,b}/`（archive_dir 护栏，不碰生产档案）。

## 3. 种子（冻结）

SEED_A = [42,7,123,2024]（生产）；SEED_B = [7,2024,99,555]（F56/F76b 同款独立错位集）。
4 组 × 2 种子 = 8 重训臂（exp_holdout v2 教训：单种子可被双种子推翻）。

## 4. 试点计时（冻结）

`--stage pilot`：G0_A 构型跑 3 锚点（backtest_start="2026-06-01"），×8 线性外推；
8 臂合计投影 **>6h** ⇒ 降窗到评估窗（backtest_start="2019-07-22"，85 期）并在结果 JSON
登记 deviation。LOO 单种子臂另计 5 × 每臂时长（计划附注，不进 6h 门）。

## 5. 研究层读数（F56/F76b 逐字同源）

生产 PIT 掩码（PITEngine.tradable_matrix）→ 等权 top100 → **21 日 forward_excess**
（月频口径）→ 换手 × `effective_rate(25000,'sell','20260914')`。
组贡献 = **G0 − Gk**（正 = 剔除后变差 = 该组有正贡献）；分全史（判决）/ ACTIVE
（2024-01~2026-08，expert.zone_of 口径，登记）/ DEAD（附注）。IC 与头部 100 不作判决。

## 6. 判据（冻结）

- **① 研究层（可剔除的必要条件）**：双种子 G0−Gk 全史 mean **同号且 ≤ 0**（去掉不变差）。
- **② 组合层（可剔除的必要条件）**：隔离 run 两窗（prod=20190722 / full=20170101），
  full 窗：超额轴 Δann = Gk−G0 ≥ 0 且逐月 NAV 配对 t > 1.6；MDD 轴 Gk mdd ≥ G0 mdd
  （回撤负值，越大越浅）。**双种子都要过**。
- **③ 正贡献宣称**：双种子 G0−Gk > 10%×|G0 全史 mean| 且配对 t > 1.6（噪声门，F70 标尺）。
- verdict（每组）：`removable`（①+②全过）/ `positive`（③过 ⇒ 有正贡献，不可剔）/
  `indeterminate`（其余）。整体：任一组 removable ⇒ `group_redundant_evidence`；
  否则 ⇒ `no_removable_group`。
- 任何结果都**不改生产、不换 pin、不重生成 golden**。

## 7. LOO（登记性筛选，不给机时预算升级）

用 `model_importance_ab_g0_a`（G0 seed A）按 train_date 均值取 **top-5 因子**；
单种子（SEED_A）各重训一臂（override = 31 − 该因子，隔离表
`model_score_ab_loo_<因子名>`）。**过筛** = 全史 Δ(loo−G0) > 10%×|G0 全史 mean| 且
t > 1.6（剔除该单因子显著改善）。过筛者升双种子 + 组合层复验（同②③，`--stage
loo_verify`）；未过筛只登记，不再烧机时。

## 8. 产物

`experiments/05_label/F77_group_ablation.py` + `F77_group_ablation_result.json`
（分阶段增量写）+ SHAPE_LEDGER.json 条目（kind=OTHER_SHAPE，top_n=100 研究层选股口径，
不跑 F1b 模拟器 ⇒ 无资金/出带/加成常量）。隔离表跑完**保留**（Phase E 台账引证据）。

## 9. 成本预算（试点后据实修正登记）

8 重训臂 + 16 组合回测 + LOO 5 重训臂。F76b 实测每臂 116 锚点 ≈ 6.4 min
⇒ 预计 8×6.4 + 5×6.4 ≈ 83 min 重训 + 16 回测（每个 1~2 min）。试点实测后登记实际投影。
