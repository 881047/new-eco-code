# NEW ECO（D:\NEW ECO）三仓联合审查报告 — 2026-10-06

> 与 Final / ECO-W2 同轮审查。修复批 commit：`6294c4db`（前端+后端+测试+构建产物）。
> 测试基线：1756 → **1757 passed**（+1 条新守卫）。**服务未重启**（无 app/ 行为改动，
> 前端构建产物静态服务按请求读盘即已生效，浏览器已验证新前端在线）。

## P1（带病上线 bug，已修）

1. **`stockLink` 缺 import**：`format.js:42` 有定义，TodayView（今日信号/最近信号 表的
   代码列 :150、龙虎榜名称列 :164）与 DataView（打分榜代码列 :85）使用处未 import——
   运行时 ReferenceError 被 `main.js:16` 全局 errorHandler 吞进 console，三张表的代码列
   渲染为空且无界面提示。3c9f19e4 引入、d1a1dc02 清理时只查了 StockDetailView。
   修复后浏览器实测 `/#/today` 渲染 89 个 `.stock-link`。

## 前端结构收敛（行为逐字一致，契约测试全绿）

2. **AsyncSection.vue**：五视图 13 处"加载中/取数失败(±重试)/正文"三态同构块收敛
   （AllAccountsView×1、TradingView×2、TodayView×2、DataView×5、PlanView×3）；
   特殊三态块（storyError、拥挤度卡、健康度行）按契约测试要求保留原文案原结构。
3. **weeklyNarrative.js**：TradingView 的 opsHtml/healthHtml/expectationHtml 三个
   v-html 拼串器外移为纯函数（契约钉字符串 `_shortSrc(ex.basis)`/`snapSrc`/
   `expectation_at_plan_time` 原样保留）；HEALTH_ICN 单源化（SleeveStatusBar 改 import）。
4. **acctCols.js**：AllAccountsView 的 6 组表格列配置 + money/thousand/pct/pctCls/
   side 助手 + MODEL_ORDER/MODEL_NOTES/SERIES_COLORS 外移（视图 442→~380 行）。
5. **ChartLine `with-range-return`**：dataZoom 拖选区间收益绑定内聚进 ChartLine 渲染
   生命周期——TradingView/PlanView 的手工 bind + COR-03"数据未到绑定静默跳过"的
   watch/nextTick 补绑 hack 全部消失；charts.js 增共用 `rangeReturnTxt()`。
6. **横幅归位**：TradingView paused / PlanView 风格分歧两处 inline 样式 → `.banner-note`。
7. **全账户页深链**：下钻态 `activeModel` 双向同步 `?model=` query——刷新/直链
   （如 `/#/accounts?model=fterm`）可还原单模型视图（浏览器已验证）。

## 视觉打磨（tokens.css；契约钉值与断点原样）

分层阴影（亮暗两套）、侧栏激活态 accent 指示条、KPI hover 边框、按钮过渡、
自定义滚动条、901-1100px 笔记本中断点。`--ds-blue/--r-card/--up/--down` 四个钉值
与 560px/900px 断点一字未动（契约测试守护）。亮/暗双主题浏览器截图核验通过。

## 前端守卫新增

8. **契约测试 +1（⑱）**：`acctCols.js MODEL_ORDER` 必须等于 `app/all_accounts.py
   MODEL_SCHEDULE` 键集合——后端加模型而前端忘了时，新模型卡片会在门户**静默不渲染**。

## 后端（无行为改动 → 未重启）

9. **api.py `_merge_live` 与 `/weekly` 内联合并：审查后判定"同机制、非同实现"，保留两份**
   ——细读发现 4 处边界语义已各自演化：①回退价字段（月频 close vs 周频 last_px）；
   ②成本键（avg_cost vs cost）；③行情价 falsy 时（月频仍用之 ⇒ mv=0 vs 周频回退
   last_px ⇒ mv 不同）；④pct 缺失时键语义（显式 None vs 不写键）+ quoted_at 有无。
   参数化合并需 4 个开关且无法保证响应逐字节等价——按仓库先例（round_money/round2）
   改为交叉引用注释 + 差异清单登记（改任一处必须对照另一处）。
   **这也是本轮未重启 8787 的原因**（原计划去重需要重启；改为注释后无 app/ 行为改动）。
10. **`data/quant.db`**：0 字节遗留文件（09-19 产生），全仓无引用 → 删除（data/ 未跟踪
    该文件，无 git 痕迹）。
11. **CLAUDE.md 过期数字**：库 11G→13.6G（2026-10-06 实测）、oneoff 173→208（实点）。

## 登记不动 / 后续候选（报告-only）

- **超长函数**：`trainer.run`（409 行，模型层本轮不碰）、`scheduler._weekly_health_slot`
  （212）、`portfolio/monitor.run_monitor`（223）、`paper/engine.execute_pending`（173）、
  `models/daily_score.score_today`（168）等——拆分收益是可读性、风险是行为漂移，
  建议各自配测试后单独小步做。
- **`/api/kg/positions_story` 逐股串行**（≤120 只 × get_stock_story）：该端点耗时大头；
  有单股失败留痕，可考虑并发/缓存，属读端点性能优化，单独一轮。
- **`app/web/static/assets` 329 个历史 chunk 入库**：`emptyOutDir:false`（防旧标签页
  懒加载 404）的有意代价；仓库缓慢膨胀，可做"保留最近 N 版"的清理脚本，低优先。
- **8790 SYSTEM 旧 fterm 看板进程**：待用户提权清理（在册事项）。
- **store.js sleeveStatus 靠 doctor 文案字符串匹配**（"空占位"/"等 18:05"/"已落账"）：
  后端改文案即碎——建议后端未来给 ops 加结构化 status 字段（前后端各一小改+契约测试），
  单独一轮。

## 纪律确认

本轮零触碰：backtest/engine.py、pit/engine.py、portfolio/sizing.py、board 限价、golden、
config.yaml、scripts/oneoff/；`experiments/h1_crowding_paper_status.json`（他人运行时
产物）未混入 commit。config.yaml 未动，无重钉需要。
