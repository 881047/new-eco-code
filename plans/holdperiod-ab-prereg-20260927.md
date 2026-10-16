# 创新③ 持有期组合层 A/B 预注册（2026-09-27，跑数前冻结）

> 背景：创新③ 探针层结清（v1 预注册推导符号错误 + 单位错配作废；v2 用 decile-κ 下界锚
> 后 82/82 全 viable 饱和、HL 两组无区分度）——**单因子读数无法裁决持有期分层，问题
> 升级到组合层**。本轮 = 生产路径持有期 A/B（engine 增量缝 `anchor="2month_even/odd"`，
> 加法式、默认 `month` 逐位不变——同 `anchor="week"` 先例 2026-09-21）。
> 只读承诺：`run_tag` 用 `hpab_` 前缀留痕；不换 pin、不写 `model_score`、不改生产 config。

## 一、四臂（冻结）

| 臂 | anchor | 含义 |
|---|---|---|
| A（对照） | `month` | 现役月频调仓；**复现校验**：读数须与 `experiments/nf_ab_backtest_20260927.json` 的 ctrl 臂逐位一致（同 config 同评分表的现行 pin 口径） |
| B1（候选） | `2month_even` | 月末锚点每隔一个保留（偶相位）⇒ 持有 2 个月 |
| B2（候选） | `2month_odd` | 奇相位（同上，防**相位脆弱**） |

双窗：prod = config `eval_start` 起；full = 全史（与 nf/chase A/B 同约定）。

## 二、判据（冻结）

- **机制校验**：B 臂 `periods_per_year` ≈ 6 且**年化**换手（每期 avg_turnover × ppy）
  相对 A 降幅 ≥ 40%——不满足 ⇒ INDETERMINATE（缝未生效，读数无意义）。
  （**跑前勘误**：初稿按每期 avg_turnover 直接比——每期口径跨频率不可比，双月臂每期
  换手自然升高；年化口径才是"调仓更少"的机制语义。引擎 `ppy` 推断同步扩展：
  锚点间隔 >35 交易日 ⇒ 6 期/年（reporting 层，month/week 路径不变）。）
- **P1 收益轴**：双窗系统超额年化 Δ(B−A) 同向为正 ⇒ 过轴；双窗异号 ⇒ INDETERMINATE。
  机制上界参考（**跑前修正**：初稿按 F52 旧换手 0.137 估 +0.25pp/年，实测生产月换手
  ~0.89/期（nf ctrl_sa 在册）⇒ 上界 ≈ 0.89 × 0.003 × 12 × ~50% ≈ **+1.6pp/年**；bar 仍取
  方向 + 相位稳健，不设虚高点）。
- **P2 回撤轴**：Δmax_drawdown(B−A) 劣化 ≤ 2pp（双窗）。
- **P3 相位稳健**：B1 与 B2 的 Δexcess 同号；异号 ⇒ INDETERMINATE（相位脆弱）。
- **判决词（机械；生产变更须拍板）**：P1∧P2∧P3 ⇒ "PASS（进拍板）"；机制校验失败或
  相位异号 ⇒ "INDETERMINATE"；其余 ⇒ "REJECT（线结清，登记勿重跑——沿周频化先例）"。

## 三、产出与语义

- engine 缝：`app/backtest/engine.py` `_load_inputs` 增 `2month_even/odd` 分支（仅 sd 过滤，
  不碰撮合/成本口径——**非基线变更**，默认路径由全量 golden 测试守卫）。
- runner：`scripts/oneoff/holdperiod_ab_20260927.py`；产物
  `experiments/holdperiod_ab_20260927.json`；登记 05 号台账 十七补。
- 无论判决如何，**不直接改生产持有期**；PASS 只产生"进拍板"候选（chase6 惯例）。

## 四、限制

1. 2 月持有臂的 chase 闸/迟滞带只在调仓日评估（跨月不重估）——这正是"持有 2 个月"的
   语义，但与月频臂的差异不只是换手（机制解读时须记住）；
2. 全史窗含 2019-2020 起步段（首期结转语义），双窗并报；
3. 单一评分表（生产 model_score）⇒ 无双种子集维度（配置级 A/B，沿 chase6 两窗惯例）。
