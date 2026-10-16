# 预注册：健康档「换手」判据的样本量守卫（只写判据，未改码）

> 日期：2026-09-24 · 状态：**判据已冻结，待实施** · 性质：**判据适用条件修正**
> 依据：`01_纪律与不变量.md` §一.1（判据先冻结再跑数）、§一.5b（触及冻结判据的适用条件 ⇒ 从严按换 pin 处理）
> 触发来源：2026-09-24 深度审查（`NEW-ECO_W2_深度审查报告_20260924.html` §1 C5）

## 0. 一句话

`health.current()` 的换手估计 `turnover_est` 用 `sells.tail(12).mean()/npos`，
**在"只有 1 个卖出日"时也照样判档**；而 `evaluate()` 的 `3×` 线会触发
`risk.apply` **自动停买且不自动解除**。本预注册给这条判据加一个**样本量前置条件**。

## 1. 问题（已核实到代码行 + 实测）

### 1.1 实测：当前 `turnover_est = 0.3163` 是 **n=1 的单点观测**

`data/weekly/ledger.json` 实读（2026-09-24 22:1x）：

| 项 | 值 |
|---|---|
| `trades` 总数 | 160（买 129 / 卖 **31**） |
| **卖出日** | **只有 20260921 一天**（31 笔） |
| 买入日 | 20260914（98 笔，建仓）、20260921（31 笔，首次调仓） |
| 当前持仓数 `npos` | 98 |
| `health.current()["turnover_est"]` | **0.3163** = 31 / 98 |
| `health.current()["level"]` | `warning`，理由「换手 0.316 为回测的 2~3 倍」 |

### 1.2 代码路径

`app/weekly/health.py::current()`：
```python
sells = trades[trades["side"] == "sell"].groupby("date").size()
npos  = max(1, len(led.state.get("positions", {})))
if len(sells):
    _m  = float(sells.tail(window).mean())      # window=12
    turn = _m / npos if np.isfinite(_m) else 0.0
```

`app/weekly/health.py::evaluate()` 的换手分支：
| 条件 | 结论 | 后果 |
|---|---|---|
| `turnover > 3 × BACKTEST_TURNOVER`（= 0.411） | `degrading` | **`risk.apply` 自动停买，且不自动解除**（需人工 resume） |
| `2× < turnover ≤ 3×`（0.274 ~ 0.411） | `warning` | 仅告警 |

### 1.3 缺陷

`turn = sells.tail(12).mean() / npos` 在 `len(sells) == 1` 时**照常出数**，
且该数被当作"每期换手率"与回测基线 0.137 直接比大小。

⇒ **n=1 意味着：任意单日卖出笔数 > 40（40/98 = 0.408…）就会越过 3× 线，
直接触发不可逆的自动停买。** 这不是"风险判断"，是"用一次观测当长期率"。

### 1.4 与 07 号 #46 的关系（**姊妹缺陷，但那条没修这一面**）

07 号 #46（2026-09-14）修的是**同一段代码的另一端**：首个周期"有买无卖" ⇒
`sells` 空 ⇒ `.mean()` 得 NaN ⇒ `/api/weekly/health` 500。当时的修法是
**空序列 ⇒ `turn = 0.0`**（语义：本窗口还没卖出）。

⚠️ 那条修法**只覆盖 n=0**，对 n=1 的脆弱性一字未提。而且两端都不稳：
- n=0 ⇒ 换手判 0 ⇒ `healthy`（**乐观**）
- n=1 ⇒ 单点直接判档，可越 3× 线（**激进**）

**本项此前未被登记、未被守卫**（07 号与 14 号 grep `turnover_est` 无此议题）。

## 2. 冻结判据（实施时按此改，不得临时放宽）

### 判据 T-1：引入样本量常量（写死在代码里）

```python
#: 换手档位判定所需的最少「卖出日」数。
#: 依据：周频袖套每期调仓一次 ⇒ 4 个卖出日 ≈ 4 周，是"一个可读窗口"的下限；
#: n=1~3 时 `sells.tail(12).mean()` 是 1~3 个点，不足以支撑**不可逆**的停买决策。
TURNOVER_MIN_DAYS = 4
```

### 判据 T-2：`evaluate()` 加样本量参数（**默认即"样本不足"**）

```python
def evaluate(rolling_ic, neg_streak, turnover, deployed_ratio,
             frozen_ic=FROZEN_IC, n_turnover: int = 0) -> dict:
```

- **`n_turnover` 默认 0 = 样本不足** ⇒ 这是**保守默认**：
  任何"忘了传样本量"的新调用方都自动获得保护，而不是静默失去保护。
  （⚠️ 与"默认不启用守卫"相反 —— 后者是 footgun，本项目一贯反对静默失效。）
- `n_turnover >= TURNOVER_MIN_DAYS` 时：**行为与现在逐位一致**（2×→warning、3×→degrading）。
- `n_turnover < TURNOVER_MIN_DAYS` 时：
  - 换手**仍可**进 `warning`（信息不该被藏起来）；
  - 换手**不得**升到 `degrading`（**不可逆动作必须被样本量挡住**）；
  - `reasons` 追加一句如实标注：`换手 {t:.3f} 样本不足 n={n}（<4 个卖出日）⇒ 不升级为停买`。

### 判据 T-3：`current()` 返回真实样本量

```python
out["turnover_n"] = int(min(len(sells), window))     # 卖出日个数（上限 window）
```
并把它传给 `evaluate(..., n_turnover=out["turnover_n"])`。

### 判据 T-4：红线用例（**先红后绿**）

| 用例 | 输入 | 断言 |
|---|---|---|
| T-4a | `evaluate(IC_OK, 0, 0.45, 0.75, n_turnover=1)` | `level == "warning"`（**修复前是 degrading**） |
| T-4b | `evaluate(IC_OK, 0, 0.45, 0.75, n_turnover=12)` | `level == "degrading"`（样本足够时**不得**被削弱） |
| T-4c | `evaluate(IC_OK, 0, 0.30, 0.75, n_turnover=0)` | `level == "warning"`（2× 线不受影响） |
| T-4d | `evaluate(IC_OK, 0, 0.45, 0.75)`（不传） | `level == "warning"`（**默认保守**，防新调用方漏传） |
| T-4e | `health.current(...)` 返回值 | 含 `turnover_n`，且 = 账本卖出日数 |

### 自证门

- **T-a**：`neg_streak >= 6` 的 `degrading` 分支**不受影响**（分支顺序不变，本改动只拦"由换手单独升级"这一条路）。
- **T-b**：`neg_streak >= 12` 的 `dead` 分支不受影响。
- **T-c**：`n_turnover >= 4` 时 `evaluate()` 的输出与改动前**逐位相同**（用现有 12 条阈值用例回归）。
- **T-d**：`turnover_n` 只增字段，**不删不改**既有键 ⇒ `/api/weekly/health` 契约（`tests/test_web_api_contract.py:63` 只要求 `level`）不破。
- **T-e**：`app/web/llm.py:380` 的键白名单**不动**（那是"给 LLM 的只读快照"，本字段不属必需）。

## 3. 影响面（预估，实施后实测回填）

- **对当前生产**：`turnover_n = 1 < 4` ⇒ 换手档位**不再由换手单独升级**；
  当前 `level` 仍为 `warning`（因为 0.3163 落在 2×~3× 区间 ⇒ warning 本来就成立）⇒
  **当前档位不变**，但"越 3× 即停买"这条路被样本量挡住。
- **对历史**：不重算历史读数（`current()` 只报当前状态）。
- **对回测/选股**：**零影响**（不碰 `trainer` / `scores` / `backtest` / `fees` / 标签 / 撮合）。
- **对看板**：`/api/weekly/health` 多一个 `turnover_n` 字段；前端未消费 ⇒ 无视觉变化。

## 4. pin 判定（**从严按换 pin 处理**）

- 本改动**触及已冻结判据的适用条件**（`BACKTEST_TURNOVER` 阈值本身与 2×/3× 倍数**一字未动**，
  但"什么时候用这条阈值"变了）⇒ 按 01 号 §一.5b「边界写不下 ⇒ 从严」**按换 pin 处理**。
- **预期 Δ = 0**：不动任何回测/成交/标签口径 ⇒ `pin_baseline.py` 的读数应**逐位不变**。
  验收时**实跑 `pin_baseline.py` 留痕**（不是口头声明"应该不变"）。
- 需在 `08_pin台账.md` 登记一行：改动性质 + Δ=0 的实测证据。
- **与 07 #46 的区别要写清**：#46 判为"修 NaN bug"未走换 pin；本条因**改的是判据适用条件**
  而非纯粹的空值防护，故从严。若将来有人质疑"为什么 #46 不走、这条走"，答案就是这一句。

## 5. 触发 / 复活条件

- **触发**：`turnover_n < 4` 且 `turnover > 3×` ⇒ 停买**被样本量挡住**，`reasons` 会写明"样本不足"。
- **何时该重新放宽**：当账本积累 ≥ 4 个卖出日后，判据自动恢复全强度（**无需改码**）。
- **复活条件（要改 `TURNOVER_MIN_DAYS`）**：必须另立预注册 + 说明"为什么 4 这个数是错的"，
  并给出新的样本量依据（不许直接改常量）。
- **不做的代价**：任意单日卖出 > 40 笔即触发**不可逆**自动停买，且无人工确认环节。

## 6. 验收

| 项 | 标准 |
|---|---|
| 判据 | `TURNOVER_MIN_DAYS` 常量 + `n_turnover` 参数已落地，默认 = 0 |
| 红线用例 | T-4a~T-4e 五条新增；**T-4a 修复前必须红、修复后转绿**（先红后绿实测） |
| 自证门 | T-a~T-e 五条均有断言 |
| 回归 | 现有 `TestHealthThresholds` 12 条**只有 `test_degrading_on_3x_turnover` 需要显式传 `n_turnover=12`**（默认变保守导致），其余逐位不变 |
| 全量 | `pytest -q` exit 0 且 passed ≥ 本轮前基线 |
| 台账 | 07 号新增一条（与 #46 互引）+ 14 号登记；`08_pin台账.md` 登记 Δ=0 证据 |
| pin | 按从严**走换 pin 流程**，实测 Δ = 0 |

## 7. 状态

- [x] 判据冻结（本文件）
- [ ] 改码（`app/weekly/health.py`）
- [ ] 补测试（`tests/test_weekly_health.py`）+ 先红后绿实测
- [ ] `pin_baseline.py` 实测 Δ=0 并登记 08 号
- [ ] 台账登记（07 号 / 14 号）
- [ ] 全量测试 + 审计
