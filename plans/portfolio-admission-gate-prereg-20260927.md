# 预注册：组合层级准入档（verdict_portfolio）—— 2026-09-27，构建前锁定

> 背景：09-26/27 新因子构型线（`plans/nf-fullretrain-prereg-20260926.md`）实证：
> 单因子档 + 打分层 head **双过闸**的构型在组合层崩溃（sys_ann −13.27pp/年、回撤劣化
> ~15pp）——"门禁 PASS ≠ 可用"第 6 例。缺失的一层 = 打分层过闸之后、生产切换之前，
> 用**组合层读数**机械判决。本档补齐门禁层级：
> **六判据门（集成级）→ 单因子档（verdict_single，池健康度）→ 组合层档（verdict_portfolio，生产切换准入）**。

## 一、适用范围与输入

候选 = 一张**评分表**（已按生产口径同日同环境训练落库，与控制臂成对，单一变量 =
评分表）。判决器不自跑训练、不改任何生产表；它只跑生产路径回测
（`E.run_backtest` 生产默认参数，双窗：prod=eval_start 起 / full=全史）并机械判决。
每次运行在台账 N 预算记账（多候选按运行次数累计）。

## 二、判据（阈值写死为默认参数，可显式覆写留痕）

输入 per 种子集（≥2 套，双种子集铁律）：Δsys_ann_prod、Δsys_ann_full（候选−控制）、
Δsys_mdd_prod、Δsys_mdd_full、Δ换手（可缺省 = 未测不否决，沿 S4 惯例）。

- **P1 收益轴**：**所有**种子集 Δsys_ann_prod ≥ **+2.0pp/年** 且 **所有**种子集
  Δsys_ann_full ≥ **+1.0pp/年**；
- **P2 回撤轴**：Δsys_mdd 两套种子集**均值**：prod ≥ **−2pp** 且 full ≥ **−3pp**；
- **P3 换手轴**：**所有**种子集 Δ换手 ≤ **+5pp**。

**判决词映射（机械；人只做最终拍板）**：
- 收益轴双种子集**异号** ⇒ **INDETERMINATE（读数不可辨）**——补样本/换构型再测，N +1；
- P1 ∧ P2 ∧ P3≠False ⇒ **PASS（进拍板）**——产出采纳包交用户；生产切换另需
  materialize/日链管线/重钉 A/B；
- 其余 ⇒ **REJECT（线结清）**。

**阈值依据（登记）**：种子噪声实测 = 同构型异种子集的 Δ——ctrl_sb vs ctrl_sa：
prod −3.49pp / full +1.32pp；trt_sa vs trt_sb：prod +3.53pp / full +1.61pp ⇒ 单种子集
±3.5pp 内的 Δ 不可作数。P1 要求双种子集一致 ≥ +2.0pp（≈噪声幅度的 60%，两次独立
方向确认）；chase6 先例 +2.07pp 过闸同量级。**校准样本 N=2 组对照，阈值随每个新
候选的登记复盘**（本档 v1）。

## 三、校准计划（实现后立即运行，预期写死）

1. **已知 REJECT 案例**：nfab_trt（58 因子构型）vs nfab_ctrl ⇒ 必须 **REJECT**
   （P1 双负 + P2 败，见 nf prereg §六）；
2. **种子噪声对照**：nfab_ctrl_sb vs nfab_ctrl_sa（同构型异种子的构造性"伪候选"）
   ⇒ 必须**不 PASS**（INDETERMINATE 或 REJECT）；
3. 若校准不符预期 ⇒ **判据有缺陷**，修判据须新预注册，本档不得放行。

判决函数：`app/alpha/gate.py::verdict_portfolio`（纯函数，与 verdict_single 同居）
+ `tests/test_gate_portfolio.py`（含"摘掉判据必须变红"的守卫）。
判决器：`scripts/oneoff/portfolio_admission_gate.py`（只读生产 config + 跑回测留痕，
不写生产表）。

## 四、校准结果（2026-09-27 01:13 判读）

| 案例 | 输入 | 判决 | 预期 | 一致 |
|---|---|---|---|---|
| 1 已知 REJECT 案例 | nfab_trt vs nfab_ctrl（58 因子构型） | **REJECT**（P1/P2/P3 全败：Δann prod −16.78/−9.76pp、full −10.59/−10.30pp；Δmdd prod −14.58/−15.34pp；换手 +7.13/+3.39pp） | REJECT | ✓ |
| 2 种子噪声伪候选 | nfab_ctrl_sb vs nfab_ctrl_sa（同构型异种子构造） | **INDETERMINATE**（prod 异号 −3.49pp vs 0.0 ⇒ 收益轴同向规则触发，不 PASS） | 不 PASS（INDETERMINATE 或 REJECT） | ✓ |

两例均按 §三 预期落判 ⇒ **判据校准通过，组合层档生效**。
实现留痕：`app/alpha/gate.py::verdict_portfolio`（纯函数，默认阈值=本档 §二）+
`tests/test_gate_portfolio.py`（10 条守卫，含三条"摘掉判据变红"红检）+
`scripts/oneoff/portfolio_admission_gate.py`（判决器，已登记 KNOWN_DB_WRITERS/
README 索引）。全量测试 **1563 passed / 2 已知跳过**；`app/**` 改动后服务已按
AGENTS.md 程序重启（watchdog 拉起，health 200）。
**使用纪律**：每跑一次判决器 = N 预算 +1，读数登记台账；阈值 v1，随复盘修订（修订须新预注册）。

