# `logs/` 目录约定（分类与判读）

> 建立于 2026-09-24（"一轮结清"轮）。**用途：让下一个人一眼看懂"哪个日志是什么、该看哪个"。**
> ⚠️ `logs/` 在 `.gitignore:56` 被忽略 ⇒ 本文件**故意放在 `docs/` 而不是 `logs/README.md`**：
> 知识库/台账里**不许出现指向 `logs/` 的证据指针**（`scripts/audit_ledger_14.py` 会把
> `logs/...` 一律判红，因为它被 gitignore ⇒ 只落在那里的证据会消失）。放 `docs/` 才能被引用。

## 一、**最要紧的一条**：两个日志通道别搞混

| 通道 | 文件 | 谁写的 | 内容 |
|---|---|---|---|
| **业务日志** | **`logs/app.log`** | `app/logger.py`（`logging`，轮转 5 MB × 3） | **缓存 / 训练 / 调度 / 告警 / 成交 —— 查业务行为只看这个** |
| uvicorn 访问日志 | `logs/server.log` | **启动命令的 stdout/stderr 重定向**（`start.bat` 与 `service_watchdog.py` 里的 `>> logs\server.log 2>&1`） | HTTP 访问行、uvicorn 启停、未捕获异常 |
| uvicorn 错误日志 | `logs/server.err.log` | 同上（桌面双击路径） | 同上 |

**2026-09-24 实踩**：审查时先 grep 了 `server.log` 找缓存相关日志 ⇒ **一条都没有**，
差点据此判定"缓存代码没跑"。**其实那行 WARNING 在 `app.log` 里躺了 2 小时。**
⇒ **查业务行为必须看 `app.log`**；`server.log` 只回答"服务起没起、有没有 HTTP 报错"。

## 二、分类（2026-09-24 实测快照：共 82 个文件）

### ① 运维 · 服务与自愈（16）
`app.log` · `server.log` · `server.err.log` · `server.err` · `server.out.log` ·
`server_restart_{out,err}.log` · `watchdog.log`（脚本自己的日志） · `watchdog_task.log`
（**计划任务入口的输出**） · `watchdog_events.log` · `launch.log`（自启/快捷方式） ·
`baostock_chain_launcher.log` · `service_heal.json`（自愈台账，保留 14 天） ·
`service_watchdog.state`（**冷却时间戳**，重启前要删的那个） · `service_watchdog.alert`（失败告警节流）

### ② 运维 · 采集回填（13）
`backfill_*.log` / `*.stdout.log`（baostock min60 / witness / dividend_ts / lhb / lhb_ths）、
`report_rc_backfill.*` / `research_report_backfill.*` / `top10_holder_backfill.*`
（长采集都是**断点续跑**，进度存 `data/.{report_rc,research_report,top10_holder}_backfill.json`）

### ③ 一次性 · 实验与诊断（52）
`ab_*` / `ab3_*` / `exp_*` / `diag_*` / `audit_*` / `f78*` / `retrain_*` / `crosscheck_*` /
`b1_pin_evidence_*` / `alpha_panel_scale_gate.*` 等 —— **都是历史跑数留下的**，
不参与任何运行逻辑。**同名 `.json` 是读数产物，`.log` 是过程输出。**

### ④ 其它（1）
`.__app.lock` —— 历史遗留的锁文件（当前生效的锁是 `data/service.lock`，见 03 号）。

## 三、**为什么不把它们拆成 `logs/ops/` + `logs/exp/`**（2026-09-24 的决定）

用户 2026-09-24 先选了"全量拆分"，**执行前实测发现爆炸半径远超计划估计**，遂改为最小卫生：

| 项 | 实测 |
|---|---|
| 硬编码 `logs` 路径 | **32 个文件**（`app/` 2 + `scripts/` 6 + `scripts/oneoff/` 17 + `tests/` 8） |
| 额外风险 | **1 个 Windows 计划任务入口** `scripts/watchdog_task.cmd` 写 `logs\watchdog_task.log`，而它是 **ASCII+CRLF 敏感文件** —— 07 号 **#3 / #30 / #31 三次事故全在这一类**（LF 行尾、中文 `rem`、无 BOM `.ps1` 各踩一次） |
| 守卫测试 | 3 个（`test_test_isolation` / `test_service_watchdog` 的 AST 守卫 / `test_ledger_14_pointers`） |
| **收益** | **0 功能增量** —— `logs/` 本就 gitignored，不属"程序入库"；拆了只是目录好看一点 |

⇒ **决定：不拆目录**。改为"**把分类写下来**"（本文件）+ 清理畸形文件名。
**本文件的收益/成本比高于拆目录**：读者要的是"该看哪个"，不是"文件在哪个子目录"。

**若将来仍要拆**：前置条件是先把那 32 处收口到一个函数（`app.config` 里给
`log_ops_dir()` / `log_exp_dir()`，**注意 `_isolation.setup()` 会 patch `LOG_DIR`，
所以必须是函数而不是模块常量**），并对 `.cmd` 做**二进制安全编辑**（保 ASCII + CRLF）。

## 四、已做的清理（2026-09-24）

- `logs/launch.log)` ⇒ **改名为 `logs/launch_20260913_autostart_typo.log`**。
  它是 2026-09-13 一次重定向笔误的残渣（`>> logs/launch.log)` 把 `)` 写进了文件名，
  内容 51 字节：`周日 2026/09/13 18:55:40.48 登录自启(隐藏`）。
  ⚠️ **改名而非删除** —— 沿用项目"归档而非删除"的纪律（同 `data/crash` 的处置），
  消除畸形名的同时不丢证据。
- 复查：`logs/` 下**已无**含非 `[A-Za-z0-9._-]` 字符的文件名。
