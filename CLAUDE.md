# CLAUDE.md — NEW ECO 改动纪律

> 本文件是给 AI 编码代理（Claude Code 等）的硬约束。违反任一条都可能让回测基线静默失效，
> 且**事后无法从测试结果发现**（测试全绿但基线已变）。改代码前先读完。
>
> 背景：2026-09-12 完成全项目审查，40 条发现（2 条已撤回，有效 38 条）。
> **审查报告实际路径**：`C:\Users\27817\WorkBuddy AI\2026-09-12-13-01-19\review\NEW_ECO_REVIEW.json`
> （机器可读，含稳定 ID / 行号 / 修复建议；同目录另有 `.md` 版）。
> ⚠️ **2026-09-29 实点：上述 WorkBuddy 目录已被清理，该 JSON 不存在** —— 全库 41 个文件注释里的
> `P0-xx/P1-xx/P2-xx` 稳定 ID 从此**查不到原文**；需要 ID 语境时用
> `D:\NEW ECO审查报告历史\`（历次审查报告存档）与 git 考古（各 ID 的修复 commit 均在 message 里带了问题原文摘要）。
> 修复 ID 的语义索引请以 git log + `docs/know/` 台账为准，不要再按上面的绝对路径找文件。
> 历次审查报告另存于 `D:\NEW ECO审查报告历史\`。

## 0. 项目是什么

月频 A股多因子选股系统：LightGBM 打分 → 等权 top6 → 恒定 87.3% 权益仓位 → 纸面模拟盘。
**另有一条独立支线**：`app/weekly/` 周频袖套（2026-09-12 新增）——周频调仓，自己的打分/方案/
账本（`data/weekly/ledger.json`），**不写 `paper_*` 表**（否则两套 track record 互相作废）；
三档调度（09:32 执行 / 18:05 晚间兜底 / 19:30 打分保鲜）受 `config.yaml` 的 `weekly.enabled` 控制，
缺省 false 时在读日历前返回、零副作用。改 `app/weekly/` 时注意：它**复用** `paper.engine._exec_price`
与 `board`/`fees`，**不要复制第二套撮合或费用实现**。
**回测成绩是唯一决策依据**，所以"改了什么让成绩变了"必须可追溯。当前基线：
`config.yaml` 的 `backtest_run: 20260928_exe` — 85 期，满仓 31.68%/-23.63%（夏普 1.102），系统 29.13%/-18.17%（日频回撤 -39.93%）。
全史窗 115 期：满仓 18.65%/-38.19%，系统 18.77%/-32.14%（日频 -42.51%），超额 +13.34pp/年（满仓尺，基准=中证800全收益 5.31%）。
（2026-09-28 第 8 次重钉：**改撮合口径**——`FILL_AT_OPEN` 收盘价→开盘价成交、`OVERLAP_REBAL_COST` 开启，消灭"回测收盘价/实盘开盘价"分叉，
预注册 plans/pin-exec-round-prereg-20260928.md；Δ=+7.01pp(prod)/+4.97pp(full) **统计上不稳健（配对 t=1.84/1.74），不得记作执行层 alpha**，
翻开关唯一正当理由是口径对齐。上一档 `20260928_excl5mdd`（第 7 次重钉：只动报告层指标——sys_ann 年化自适应 + 日频 MDD 锯齿去除 +
board 900xxx 归类，prod 全键逐位不变；预注册 plans/metric-fix-batch-prereg-20260928.md）读数：满仓 28.32%/-25.84%、系统 22.12%/-22.10%、
全史系统 13.80%/-34.41%；回滚 = `backtest_run` 改回 + 两个开关翻回 False。第 6 次重钉 factor_exclusions 剔 5 因子内容不变，生产仍 26 因子。）
**口径不含 2018 崩盘**（评估窗 2019-07-22 起），引用必须一并写明。
（2026-09-12 第 2 次重钉：E5 限价入参量化，P0-03；上一档 `20260912_fullhist` = 23.77%/20.47%，可用 `app/backtest/engine.py:PRE_CLOSE_QUANTIZE = False` 逐位复现。）

## 1. 🔴 改这些之前必须停下来（禁止"顺手改"）

以下改动会**改变回测撮合或成本口径 ⇒ 让 `backtest_run` pin 失效**。
判据不是"改完测试绿不绿"，而是"pin 还能不能被当前代码复现出来"。

| 位置 | 内容 | 为什么危险 |
|---|---|---|
| `app/backtest/engine.py` `_exec_side` / `_month_return` | 成交价口径（收盘 vs 开盘）、封板判定、重叠持仓成本 | P0-01 / P0-02：直接决定回测是否虚高 |
| `app/backtest/engine.py` `PRE_CLOSE_QUANTIZE` 开关 | float32 `pre_close` 的量化回分 | P0-03：实测 +1.07pp/年，**只向下**偏差。**已修**（2026-09-12 E5，P2 = PASS 与真值臂位级一致，默认 True）；**翻转这个开关 = 换 pin**（False 跑出上一档 `20260912_fullhist` 23.77%/20.47%） |
| `app/pit/engine.py` `tradable_mask` | 可交易域过滤层（**六层，不含涨跌停**） | P0-04：改一层就换一批持仓。⚠️ 2026-09-15 更正：原文"（含新增涨跌停层）"**与代码不符** —— 定域只有「上市满 60 交易日 / 未停牌 / 非 ST/退 / 板块 / 市值分位 / 流动性」六层，**封板股仍可能被选进名单**；执行侧 `_exec_side` 有收盘封板判定（买不进⇒顺延/作废）兜住主害。加第七层 = 改选股域 ⇒ 换 pin，单列一轮（外部审查 B2） |
| `app/portfolio/sizing.py` `allocate_lots`、`board.py` 限价 | 整手分配、涨跌停价计算 | P2-05 / P2-06：影响买不买得起 |
| `app/backtest/engine.py` 换手率 / 净值 / 超资防御 | 指标算式 | P2-01~04 |

**正确做法**：
1. **先做 A/B，不进生产**。写臂对照脚本放 `scripts/oneoff/`，输出 Δ（年化 / 回撤 / 夏普）。
2. **独立预注册**（先写判据再跑），判据参考 `app/portfolio/board.py:207`（要求与真值臂**逐位一致**）。
3. 决定进生产后，**必须重钉 pin**：
   - 跑 `scripts/pin_baseline.py`（只测量、不自动改配置）
   - 更新 `config.yaml` 的 `backtest_run` 与那 20 行口径注释
   - 同步 `README.md`「关键设计」表的口径说明
4. 在 commit message 写明：**改了什么口径、Δ 多少、旧 pin 值是多少**。

历史教训：2026-09 那次 D06/D07 限幅修复进了生产但**没重钉**，导致 pin 悄悄过期，咬掉了两个预注册实验（一整轮作废）。详见 `config.yaml` 的 pin 注释。

## 2. 黄金基线：禁止一键重钉

`tests/data/golden_*.json` 是回归基线。**禁止用 `REGEN_GOLDEN=1` 重新生成来"修"失败的测试。**

- 测试红了 → 说明**你的改动改变了行为** → 要么改代码改回去，要么按 §1 走 A/B + 重钉流程。
- 只有当行为变更已被确认是有意的、且已重钉 pin 时，才允许更新 golden，并**改文件名 + 在 commit 写变更理由**。
- 每个修复都应配一条**先红后绿**的单测（先写一个会失败的断言，确认它抓得住这个 bug）。

## 3. 不要破坏 config.yaml 的注释

`config.yaml` 是**基线唯一事实源**（20+ 行注释钉着评估窗 / 训练起点 / 持仓数 / 费用口径）。
修 P2-17（配置契约）时：**必须用键级 patch 或 ruamel.yaml 保留注释**，
**禁止** `yaml.safe_dump` 整文件重写（会抹掉全部注释并把 ~50 个默认键实体化）。

## 4. 数据库改动

`data/neweco.db` 约 **13.6G**（2026-10-06 实测），改主键/迁移前先确认：
- 有 `scripts/backup_db.py`，先备份
- 大表迁移不要放在启动路径（`init_db`）里同步做
- `fina_indicator` 主键改造（P1-02，加 `ann_date`）涉及全表重建，先评估耗时

## 5. 环境与测试

```bash
.venv\Scripts\python.exe -X utf8 -m pytest tests/ -q    # 基线：2026-09-24 夜实跑 1518 passed / 2 skipped / 328 subtests, ~138s
./.venv/Scripts/python.exe scripts/ci_run_tests.py       # CI 入口（含"跳过数超额度即失败"）
```

- 改动后**必须全量跑**，不能只跑相关文件（多个测试跨文件 import）。
- **前端（2026-09-16 起）**：源码在 `frontend/src/**`（Vue3+Vite），改完必须 `cd frontend && npm run build`
  —— 构建产物 `app/web/static/` **入库**，不重建 = 看板跑旧代码（`test_built_assets_not_stale_vs_source`
  会红）。契约测试（static/api 两份）扫的是 `frontend/src` 源码树；袖套可信度红线、失败横幅、
  预期溯源这些行为钉在 `tests/test_web_static_contract.py`，改前端时不得删。
- Windows 环境，中文输出：用 `-X utf8`，`PYTHONIOENCODING=utf-8`。
- CI 依赖 `requirements-lock.txt`；`deap` / `cvxpy` / `httpx2` / `pyarrow` 必须留在里面
  （缺 `deap`/`cvxpy` 会让 `test_factory.py` 直接 ERROR；缺 `httpx2` 会让 `test_local_guard.py`
  **在收集阶段**就 ERROR ⇒ 全量测试根本跑不起来；缺 `pyarrow` 则行情面板缓存写不进去）。
  `torch` 刻意不入锁（seq_die 已判否，CPU 轮子 1~2GB）——它在不在环境里决定 `test_seq_die`
  那 2 条跳不跳，见下一条。
- **跳过条数是环境条件的，不是常量**（`ci_run_tests.py` 的额度 = 6，超了就是被静默阉割）：
  两个来源 —— `test_seq_die` 的 2 条看 **torch** 能不能 import（`test_seq_die.py:173`），
  `test_market_cache::TestCacheRoundtrip` 的 4 条看 **parquet 引擎**能不能真读写
  （`test_market_cache.py` 的 `_PARQUET_OK`，定义于 :57、skipUnless 于 :152（2026-09-28 校准），判据是真实往返写，**别只看 import**，
  pandas 会把 DLL 载入失败报成"pyarrow 未安装"）。
  **2026-09-18 实点：两个依赖本机都已可用（torch 2.13.0+cpu / pyarrow 25.0.1）⇒ 全量 0 跳过。**
  **⚠️ 2026-09-24 实点（覆盖上一条，以此为准）：全量 `1518 passed / 2 skipped / 328 subtests`
  （138 s，exit 0）**；两条跳过全部来自 `tests/test_seq_die.py:173`（`TestModelShapes.setUp`，2026-09-28 校准）
  ⇒ **当前环境的正常值就是 2**。基线演进：1221(09-14) → 1397(09-20 白天) → 1400(09-20 晚)
  → 1479(09-24 审查前) → 1489(+缓存可观测 6/doctor 4) → 1499(+换手样本量守卫 10)
  → 1505(+费率单一真源 6) → 1512(+方案文件时间闸 7) → **1518**(+换手注记 2 / 阈值行纯函数 4)。
  **⚠️ 2026-09-20 实点（已被上一条覆盖，保留作对账）**：`.venv` 里 pyarrow 25.0.1 在、torch 不在
  ⇒ 全量 `1400 passed / 2 skipped / 293 subtests`（2026-09-20 晚加入
  `tests/test_code_hygiene_ratchet.py::test_no_unused_imports` 后为 1400，此前 1399），
  两条跳过全部来自
  `tests/test_seq_die.py:173`（`TestModelShapes.setUp`）。⇒ 当前环境的正常值就是 2。**
  历史口径仅供对账：09-14 那阵 pyarrow 的 `arrow.dll` 被 Windows Smart App Control 拦
  （CodeIntegrity 事件 3033/3077）⇒ 那 4 条跳；09-18 审查时报的是 2 条（torch 未入 `.venv`）；
  当晚依赖到位曾到 0；**2026-09-20 实测又回到 2**（torch 不在 `.venv`）。别把任何其中一个数字当"现行值"写进结论。
  ⚠️ `torch` 刻意不入锁（seq_die 已判否，CPU 轮子 1~2GB）⇒ 换机器/重装环境它会回到 2 条，
  那时候 2 才是正常值，不是 bug。
  **判定方法（别靠记忆报数）**：跑 `pytest tests/ -q -rs`，只要 SKIPPED 原因里出现的全是
  `torch 不可用`、且跳过总数 ≤ `SKIP_BUDGET`（6），就是**正常**，不是回归 ——
  （`-q` 不带 `-rs` 不会打印跳过原因，看不出来是 torch 还是别的东西被阉割了）。
- **测试隔离是单一实现**：`tests/_isolation.py`（日志改道 + SMTP 出口哨兵 + 测试库重定向），
  pytest 走 `tests/conftest.py` 调用它，`scripts/ci_run_tests.py` 在 `discover()` **之前**调用它。
  跑完测试后 `logs/app.log` 与生产库 `notify_log` 都必须**零增长** ——
  2026-09-14 的事故就是"CI 入口走 unittest ⇒ conftest 不加载 ⇒ 隔离没建立"（日志 +76KB、
  真的发出 1 封假告警邮件，`notify_log` id=18 有据）。守卫在 `tests/test_test_isolation.py`。
- **不要动** `data/alpha_pool/`（npz 值面板，可复算；2026-09-28 实点 **247** 个，随门禁批次增长——
  数字天然漂移，对账以 `ls data/alpha_pool | wc -l` 现点为准）、`data/*.db`、`logs/`。

## 6. 已知容易改错的地方

- `paper.paused` 的默认值方向：**当前代码取 `False`（缺省=允许交易），与本文档原要求（`True` 缺省即停）不一致** —— 现状是：`DEFAULT_CONFIG` 已补上该键，但方向**故意保持 False**，因为 `tests/test_trading_pause.py:160` 锁定了「缺键=未暂停」的既有语义，改 fail-closed 属行为变更，**需用户拍板**（2026-09-12 独立审计 #9 指出这处文档-代码冲突；勿在不改测试的情况下"顺手修代码"）。待拍板项登记在 `plans/weekly-terminal-plan-20260912.md` §9。
- SMTP 授权码**只住 `.env`**，不要写进 `config.yaml`（受 git 跟踪），也不要在日志里打印。
- `app/main.py` 的安全中间件（Host 白名单 / 同源校验 / CSP）**已有测试覆盖**（`tests/test_local_guard.py`，8 条：
  Host 白名单 / 跨源 POST 拦截 / 403 早退仍带安全头 …）。⚠️ 2026-09-14 外部审查更正：原句"没有测试覆盖"是**过期信息**。
  ⚠️ 同时记住另一面：`POST /api/run/daily|monthly` 这类**写端点只有中间件那一层**（无 token）——
  本机任意进程都能构造请求；威胁模型与处置见知识库 03 号「本机安全姿态」。
- **`scripts/oneoff/` 是受保护的证据存档，不是垃圾**（2026-09-12 独立审计 #7）：README 索引
  必须覆盖全部脚本（2026-10-06 实点：`scripts/oneoff/*.py` **208** 个 + 1 个 .ps1；守卫按目录
  双向收敛，叙述性数字天然漂移，对账以 `ls scripts/oneoff/*.py | wc -l` 为准），
  `tests/test_oneoff_hygiene.py` 守住 4 条规则（被引用脚本必须存在 / 冻结脚本必须留在顶层 /
  禁止裸名 import / README 必须索引全部脚本），另有 19 个脚本被 `app/`、`tests/` 直接引用。
  **任何"整理"都不得删除或移动这个目录里的文件**；要标注状态就在脚本头部加 `# STATUS:` 注释。
- 时间口径：`scheduler` 用 `Asia/Shanghai`，`db.py` 部分用 SQLite `localtime`，`framework.py` 用 naive——不要混改。

## 7. 提交

- 一个逻辑改动一个 commit，message 写清**为什么**（本项目 commit 历史质量高，保持这个水准）。
- 涉及口径变更的，commit message 必须带 Δ 和旧 pin 值（见 §1.4）。
