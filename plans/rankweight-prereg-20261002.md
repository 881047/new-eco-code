# 分数排名加权 sizing 层 A/B 预注册(2026-10-02,跑判决前冻结)

> 触发:元标签探针(66 补)副发现——top6 内**零 ML 分数排名加权** R 臂 +35.59% vs 等权
> base +22.10%(+13.5pp/年,NAV 级近似)。用户指令"开始实践创新道路,直至落地或证伪"
> ⇒ Phase 1:引擎级 A/B。本改动触碰 `app/portfolio/sizing.py allocate_lots`(§1 禁区)
> ⇒ 走 §1 完整流程:预注册 + 缺省逐位不变缝 + A/B 臂 + 判决;生产切换(重钉)须用户拍板。

## 一、缝设计(冻结)

- `allocate_lots(..., rank_weighted: bool = False)`:True 时第 r 名(按传入顺序=分数降序)
  的 slot = equity_cash × (K+1−r)/Σ(K+1−j),K = min(6, len(pool))——即探针同款线性
  排名权重 (7−r)/21 的归一化形态;第 7 名起 slot=0(跳过,留现金)。
  False(缺省)= **逐位不变**(`slot = equity_cash / max_stocks` 原路径)。
- 引擎 `run_backtest(..., rank_weighted=False)` 传递至满仓线与系统线两次 allocate_lots
  调用;hysteresis=0(生产 config)下 keep-boost 为无操作,权重序 = 分数序。
- **先红后绿**:单测先于实现跑(缺 kwarg = TypeError = 红),实现后转绿;
  缺省路径恒等由既有 golden/回归测试钉死。

## 二、A/B 臂与对照(冻结)

- 臂:`run_backtest(rank_weighted=True)` 双窗(prod=缺省/full=20170228-20260831),
  run_tag=`rankwt_{wname}`;对照 = 生产 pin `20260928_exe` / `20260928_exe_full`(库内 metrics)。
- 本改动无训练成分 ⇒ 单臂确定性,无双种子集(与 B1/E2E 的差异点,预注册声明)。

## 三、判据(冻结)

- **P1 经济性**:Δsys_ann prod ≥ **+2pp** 且 full ≥ **+1pp**(双窗同向;NAV 探针读数
  +13.5pp,考虑整手约束衰减后仍须过此门)。
- **P2 集中度尾部**:sys_max_drawdown prod ≤ pin+2pp、full ≤ pin+3pp(加权 = 集中度↑,
  这是本候选的头号风险)。
- **P2b 波动率守卫(F55 先例)**:月度超额(sys−bench)标准差比 臂/pin ≤ **1.10**
  (周频 1.05 档的月频放宽档)。
- **P3 摩擦**:avg_turnover ≤ pin+2pp;avg_cash_idle ≤ pin+15pp(整手下 rank-6 槽位
  ~952 元常买不起 ⇒ idle 必升,有界容忍)。
- **判决**:P1∧P2∧P2b∧P3 ⇒ **GO(提请重钉拍板)**;P1 双窗同向为负 ⇒ **NO-GO(线结清)**;
  其余 ⇒ **INDETERMINATE**。

## 四、产物

`tests/test_sizing_rank_weight.py`(先红后绿);engine/sizing 缝;A/B 器
`scripts/oneoff/rankweight_ab_20261002.py`(头条 Δ 数字写入产物 JSON——64 补惯例);
读数 `experiments/rankweight_ab_20261002.json`;台账 N+1。
