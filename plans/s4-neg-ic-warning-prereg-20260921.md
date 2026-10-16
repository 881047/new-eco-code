# S4 预注册：负 IC 不进 warning 分支（只写判据，未改码）

> 日期：2026-09-21 · 状态：**✅ 已实施（2026-09-21）** · 对应 14 号台账开放项 **S4**
>
> ⚠️ **2026-09-22 状态更正**：本行原写"待你拍板后才实施"，而 14 号判决总表已判「已做（2026-09-21 实施）」
> ⇒ 两处**互相矛盾**（2026-09-22 结项审计发现）。以**代码为准**（纪律："代码与产物 > 知识库"）：
> `app/weekly/health.py` 已去掉 `0 <`；`tests/test_weekly_health.py` 有红线用例
> （`rolling_ic=-0.0072, neg_streak=3 ⇒ warning`）+ **先红后绿实测**；自证门 S4-a / S4-b 均已加断言
> ⇒ 本文件状态回写为**已实施**，不再是待办。
> 性质：判据修正（一行）+ 一条红线用例；**不动训练/打分/撮合口径 ⇒ 不换 pin**
> 依据：台账要求"改判据须预注册 + 你拍板"（开放项 S4 行）

## 1. 问题（已核实到代码行）

`app/weekly/health.py:62 evaluate()` 的分支自上而下：

| 行 | 条件 | 结论 |
|---|---|---|
| L66 | `neg_streak >= 12` | `dead` |
| L69 | `neg_streak >= 6` 或 `turnover > 3×BT` | `degrading` |
| **L75** | `0 < rolling_ic < 0.5×frozen_ic` 或 换手 2~3× 或 投入率 0.50~0.60 | `warning` |
| L85 | 其余 | `healthy` |

**缺陷**：L75 的 `0 < rolling_ic` 把**负 IC 排除在外** ⇒ 负 IC（且 `neg_streak < 6`）直接落 L85 判 `healthy`。

**实证**（`experiments/b_stage_20260920/m1_ic_scale_flip.json` 的 `flip_anchors`）：
- `20170609`：rolling_ic = **−0.0072**（负）⇒ 判 **healthy**
- `20170623`：rolling_ic = **+0.0005**（正但极小）⇒ 判 **warning**
⇒ 出现了"负的反而比正的健康"的翻档。

## 2. 冻结判据（实施时按此改，不得临时放宽）

### 判据 S4-1（一行）
将 L75 的
```python
elif ((rolling_ic is not None and 0 < rolling_ic < 0.5 * frozen_ic)
```
改为
```python
elif ((rolling_ic is not None and rolling_ic < 0.5 * frozen_ic)
```
即**去掉 `0 <`**，让负 IC 也进入 warning 分支。

### 判据 S4-2（红线用例，必须新增）
构造 `rolling_ic` 为负、`neg_streak < 6` 的输入：
```python
evaluate(rolling_ic=-0.0072, neg_streak=3, turnover=<正常>,
         deployed_ratio=<正常>, frozen_ic=FROZEN_IC)
```
**断言 `level == "warning"`**（修复前是 `healthy` ⇒ 该用例现在必须红、修复后转绿 —— 即先红后绿）。

### 自证门
- **S4-a**：`neg_streak >= 6` 的负 IC 仍应优先落 `degrading`（分支顺序不变），不得因本改动降级为 `warning`。
- **S4-b**：`rolling_ic is None`（无数据）时行为不变 —— 仍不得进 warning 分支（保留 `is not None` 判断）。
- **S4-c**：改动只影响 `level` 判定，**不改** `reasons` 文案的既有格式。

## 3. 影响面（预估，实施后实测回填）

- 直接后果：`neg_streak ∈ [1,5]` 且 `rolling_ic < 0` 的锚点由 `healthy` 变 `warning`。
- 历史上被误判为 healthy 的负 IC 锚点会翻为 warning ⇒ **看板/邮件的健康度显示会变差**（方向正确：本来就不该 healthy）。
- **不影响** `dead` / `degrading` 两档（分支顺序未动）。

## 4. 触发 / 复活条件

- **触发**：袖套出现连续负 IC 而档位仍显示 healthy 时（正是本条要修的现象）。
- **不做的代价**：健康度对"负 IC"失明，直到连续 6 期才由 `neg_streak` 接住 ⇒ 早期预警失效。

## 5. 验收

| 项 | 标准 |
|---|---|
| 判据 | L75 已去掉 `0 <` |
| 红线用例 | 新增且修复后转绿（修复前必须红） |
| 自证门 | S4-a/b/c 三条均有断言 |
| 全量 | `pytest tests/ -q -rs` ≥ 1400 passed / 2 skipped |
| 台账 | S4 行更新为"已做"，指针指向本文件 + 新测试 |
| pin | **不重钉**（不动口径） |

## 6. 状态

- [x] 判据冻结（本文件）
- [ ] 你拍板
- [ ] 实施 + 补测试 + 跑全量
- [ ] 台账登记
