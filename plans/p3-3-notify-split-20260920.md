# P3-3 预注册：拆分 `app/notify.py`（MI 7.71，全仓最低）

> 日期：2026-09-20 · 类型：**重构**（不改任何通知内容与发送行为）
>
> ⚠️ **2026-09-22 状态注记：不采纳（未执行，也不再执行）** —— 本文件末段自述"维持现状，仅在此登记"，
> 14 号台账同判。保留为**预注册证据**（按纪律：证据不删，只加状态注记），不再作为待办。
> 来源：2026-09-20 全面审查 A-4 / 可维护性层
> 依据：`CLAUDE.md` §7（一个逻辑改动一个 commit）与 §2（改完必须全量测）
> **不涉及回测口径** —— notify 不参与撮合/成本/标签，因此**不需要重钉 pin**

## 1. 现状（2026-09-20 实测）

`app/notify.py`：**956 行 / 20 个函数 / MI 7.71（全仓最低）**，三类职责混在一个模块：
HTML 拼装、SMTP 投递、投递校验。

| 函数 | 行 | 行数 | CC | 职责 |
|---|---|---|---|---|
| `send_daily_brief_email` | 835 | 122 | **39 (E)** | 投递 + 拼装 |
| `send_daily_summary_email` | 544 | 125 | **38 (E)** | 投递 + 拼装 |
| `build_daily_brief` | 740 | 93 | 23 (D) | 拼装 |
| `send_paper_email` | 359 | 39 | 20 (C) | 投递 |
| `_ops_section` | 400 | 55 | 19 (C) | 拼装 |
| `_sleeve_section` | 488 | 54 | 16 (C) | 拼装 |
| `send_signal_email` | 314 | 43 | 14 (C) | 投递 |
| `_signal_section` | 457 | 29 | 11 (C) | 拼装 |

其余为小工具：`_html`(70) / `_envelope`(30) / `_lhb_block`(45) / `_style_block`(37) /
`_table` / `_money` / `_yuan` / `_log_send` / `_send_with_retry` / `_rows`。

### 依赖方（拆分时必须全部保持可用）

- `app/scheduler.py` —— 6 处 `from app.notify import send_XXX`
- `app/weekly/report.py` —— `from app.notify import _send_with_retry`
  （注释明确写"复用生产重试发送器，不复制发信逻辑"）
- `scripts/alert_service.py` —— `from app.notify import send_ops_alert`
- `tests/test_notify.py` / `test_notify_ops_alert.py` —— `from app import notify`

## 2. ⚠️ 头号风险：拆包子包会让 mock 静默失效

若按直觉拆成 `app/notify/{__init__,render,send,check}.py` 并在 `__init__.py`
re-export，`from app.notify import X` 与 `from app import notify` 都还能用，
**但 `mock.patch.object(notify, "send_X")` 打的桩会失效**：
调用方持有的是 `__init__` 导入时绑定的函数对象，patch `__init__` 的属性
不会改变已绑定的引用；若内部改走子模块，patch 更是完全落空。

这与 P3-1 第 2 步遇到的同款陷阱（`test_scheduler_lock.py` 有 9 处
`mock.patch.object(scheduler, "_proc_start_ticks")`）——当时的解法是保留模块级名字转调。

**因此本项的可行路径有两条，必须先选定并写死：**

| 方案 | 做法 | 优点 | 风险 |
|---|---|---|---|
| **A（推荐）单文件内渐进拆** | 保持 `app/notify.py` 单文件，只把两个 120+ 行函数内部的重复片段抽成小函数（`_summary_*` / `_brief_*`） | mock 路径完全不变；改动面小 | 模块仍偏大，MI 改善有限 |
| B 拆子包 | 建 `app/notify/` 包 + re-export | 结构清晰 | **需逐一核查所有 mock 是否仍生效**；改动面大 |

**建议先做 A**：它零 mock 风险、可立即验证，且能先把两个 E 级函数（CC 38/39）降下来。
B 留待确有必要（例如 render 需要被其他模块单独复用）时再做，且必须逐条核对 mock。

## 3. 判据（跑前冻结）

### N1 — 行为不变（主门）
`tests/test_notify.py` + `tests/test_notify_ops_alert.py` **全绿**，
且**禁止**修改这两个测试来"配合"重构（改测试 = 改判据）。

### N2 — 依赖方不受损
`app/scheduler.py` / `app/weekly/report.py` / `scripts/alert_service.py`
的 import 路径**一行不改**。

### N3 — 全量回归
`pytest tests/ -q -rs` 保持 `1400 passed / 2 skipped / 293 subtests`。

### N4 — 圈复杂度下降
目标：`send_daily_summary_email` 与 `send_daily_brief_email` 两者 CC 均 **< 20**；
模块 MI 由 7.71 提升（不强设阈值，但必须实测记录）。

### N5 — 不触碰回测口径
本项与 `_exec_side` / `_month_return` / `PRE_CLOSE_QUANTIZE` /
`tradable_mask` / `allocate_lots` 无关 ⇒ **pin 不重钉**。

## 4. 不通过的处理

- N1/N2/N3 任一红 ⇒ 回退该步（`git revert`），不得改测试或改判据。
- N4 未达标但 N1–N3 全绿 ⇒ 可接受（结构已改善），记录实测值即可。

## 5. 进度

- [x] 现状分析 + 依赖方梳理 + 风险识别（本文件）
- [ ] 选定方案（建议 A）
- [ ] 实施第一步：抽 `send_daily_summary_email` 内部片段
- [ ] 跑 N1–N4
- [ ] 实施第二步：抽 `send_daily_brief_email` 内部片段
- [ ] 跑 N1–N4
- [ ] 提交 + 重启服务（改了 `app/**`）

## 6. 已发现但**暂不抽取**的重复：费用口径文案

`fee_params` 的费用说明在两处重复：
- **L121-123**（`_html()` 内，方案邮件的「③ 成本」）
- **L638-641**（`send_daily_summary_email()` 内，日终邮件的「口径」）

**决定暂不抽取，原因**：两处的 HTML 空白不同 —— L121 是带换行缩进的多行文本，
L638 是单行 f-string 拼接。HTML 渲染会把连续空白折叠成一个空格，
**渲染结果相同**，但**字符串不完全相同**。
若有测试按完整 HTML 字符串比对，抽取就会被打破；而收益只有 3 行。

⇒ 要抽取必须先确认 `tests/test_notify*.py` 是否做完整 HTML 比对；
在此之前维持现状，仅在此登记，避免后来人误以为"没人发现"。

（这呼应本轮反复出现的原则：**收益小 + 有隐蔽风险 ⇒ 先登记，不急着动手**。）
