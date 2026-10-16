"""Alpha Factory —— 芯片式架构的因子产生层(2026-09-11)

对应用户框架图里的 `Alpha Factory → Alpha Pool → Devil/Critic → OOS验证 → Alpha Bank`
这一段。本包只负责**产生与登记因子**, 不碰打分链路、不碰回测引擎。

| 模块 | 职责 |
|---|---|
| `expr`   | 表达式引擎(端口自 microsoft/qlib 的 Alpha158 算子语义, Apache-2.0) |
| `zoos`   | 种子因子库(Alpha158 移植 + 本项目存量 + 新数据源) |
| `gate`   | **预注册**门禁(死区 head/t + 同号 + Bonferroni + **强制双读 top6**) |
| `bank`   | Alpha Bank: 登记 / 查询 / 退役 / 晋升 |
| `critic` | Devil/Critic: 生成**否证实验**(不产出结论) |

⚠️ 纪律(与 `app/models/expert.py` 的 KGD 同源):
  1. 门禁判据写死在 `gate.py`, **改这里等于改判据**, 必须在看到结果之前决定
  2. `head`(前 10%)与 `top6`(策略真正买的 6 只)**必须一起报** —— 已有三个
     "head 好看 top6 为负"的反例(`die_flow` / `head_vt` / `h10`)
  3. 门禁通过 ≠ 可用。**判决只能是组合层两轴**(见 `scripts/oneoff/exp_die_stage_b.py`)
  4. 不做「看到结果再挑最好的几个」—— Bonferroni 的分母 = 实际测过的候选数
"""
