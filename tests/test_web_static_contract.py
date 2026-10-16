"""看板静态资源的**契约测试**（2026-09-13 晚创建；2026-09-16 前端重构后改扫 Vue 源码）

为什么：前端已从单文件 `app/web/static/{index.html,app.js}` 重构为
`frontend/src/**`（Vue3 + Vite，构建产物进 `app/web/static/assets/`）。
契约测试的职责不变 —— 钉**行为性事实**（不钉像素/排版），每条都对应一个
真实发生过的缺陷。静态产物是压缩过的 bundle，扫它没意义 ⇒ 改扫源码目录。

每条的来历（与旧版一一对应）：
  ① 取数失败横幅 `bootWarn` 必须存在且**默认不显示**（v-show + failed 数组）；
  ② 失败后必须**自动重试**（store.boot 里 setTimeout(boot, 30000)）；
  ③ 周频预期来源必须读 `ex.basis`（读不存在的 ex.source 会一直显示兜底文案）；
  ④ 方案快照 `expectation_at_plan_time` 必须被使用（溯源）；
  ⑤ DeepSeek token 与等宽数字必须在（对标基线，见 KB 06 §五）；
  ⑥ 授权码输入必须是 `new-password`（否则浏览器弹保存密码）；
  ⑦ 首屏信息层级：袖套状态条在今日总览视图**第一块数据卡之前**；
  ⑧ 袖套状态可信度红线：空占位净值点（0 成交/0 持仓）不许显示成"已执行"；
     —— 三个判定分支（空占位 / 等 18:05 / held>0||traded>0）都在 store.sleeveStatus；
  ⑨ "已持"必须是**实时账本持仓**（不用方案快照 n_held 冒充）；
  ⑩ 判据文案剥 markdown 强调符（doctor 文案里的 **加粗** 直出会显示成星号）；
  ⑪ 状态芯片 role="status"（读屏可播报）+ 事实格 dt/dd 语义；
  ⑫ 窄屏断点在（手机宽度 KPI 不撑破）。

运行: python -m pytest tests/test_web_static_contract.py -q
"""
import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SRC = BASE / "frontend" / "src"

REQUIRED_FILES = [
    "App.vue", "main.js", "store.js", "api.js", "router.js",
    # 2026-10-08 批3 导航单一真源（四个入口都 import 它；丢了导航就全空）
    "nav.js",
    "components/SleeveStatusBar.vue", "components/ActionCenter.vue",
    "components/CommandPalette.vue", "components/AskPanel.vue",
    "views/TodayView.vue", "views/AllAccountsView.vue", "views/StockDetailView.vue",
    "views/TradingView.vue", "views/PlanView.vue",
    "views/DataView.vue", "views/SettingsView.vue",
    # 2026-10-06 分类细化拆出的功能页（少一个 = 那个功能从导航上静默消失）
    "views/PaperView.vue", "views/LhbView.vue", "views/FundsView.vue",
    "views/StyleView.vue", "views/LlmReviewView.vue", "views/StatusView.vue",
    "views/NotifyView.vue",
    # 2026-10-06 看板重构（全账户枢纽）；2026-10-08 改名：Final/W2 盘面页
    "views/FinalView.vue", "views/W2View.vue",
    # 2026-10-08 三模型选股交集页（/picks，用户需求：三模型选股前一百名交集）
    "views/PicksIntersectionView.vue",
    # 2026-10-08 抽公共组件（骨架/账户卡/模型信息卡/选股榜卡）：少一个 = 盘面页
    # 静默退回"各写一份"的老路径，改一处忘另一处的漂移又会回来
    "components/ModelPageShell.vue", "components/AccountCard.vue",
    "components/AccountCardGrid.vue", "components/ModelMetaCard.vue",
    "components/PicksBoard.vue",
    # 2026-10-08 批4 信息架构重排新增：NEW ECO 盘面页（补三盘面对称）+ 运维合并页
    "views/NewEcoHomeView.vue", "views/OpsView.vue",
    # 2026-10-08 批5 三盘面配置驱动：差异声明在 models.js，由 ModelPage 统一渲染；
    # 三个盘面视图退化为薄壳。丢任一文件 ⇒ 三个盘面页同时白屏。
    "models.js", "components/ModelPage.vue",
    # 2026-10-08 批6 新增：只读参数配置卡 + 跨盘面对比页
    "components/ModelParamsCard.vue", "views/CompareView.vue",
]


def _all_src() -> str:
    """frontend/src 下全部 .vue/.js/.css 源码拼接（单处拼接，多个断言共用）。"""
    parts = []
    for p in sorted(SRC.rglob("*")):
        if p.suffix in (".vue", ".js", ".css"):
            parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts)


class TestDashboardSourceContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert SRC.exists(), f"前端源码目录不见了: {SRC}"
        cls.all_src = _all_src()
        cls.html = (BASE / "frontend" / "index.html").read_text(encoding="utf-8")

    def test_source_tree_complete(self):
        """重构后的源码树完整（少一个视图/组件 = 那个区块从看板上静默消失）。"""
        missing = [f for f in REQUIRED_FILES if not (SRC / f).exists()]
        self.assertEqual(missing, [], f"前端源码缺文件: {missing}")

    # ---- ①② 取数失败可见 + 自动重试 ------------------------------------------
    def test_boot_warning_banner_wired_and_retries(self):
        self.assertIn('id="bootWarn"', self.all_src, "取数失败横幅不见了")
        self.assertIn("v-show", self.all_src, "横幅必须默认不显示（v-show + failed）")
        self.assertIn("failed.join", self.all_src, "横幅必须点名失败区块")
        self.assertIn("setTimeout(boot, 30000)", self.all_src,
                      "失败后必须自动重试一次（30 秒）")

    # ---- ③④ 预期来源与快照溯源 -------------------------------------------------
    def test_expectation_shows_real_source_and_snapshot(self):
        self.assertIn("_shortSrc(ex.basis)", self.all_src,
                      "实时预期来源必须读 ex.basis（读 ex.source 会一直落到兜底文案）")
        self.assertIn("expectation_at_plan_time", self.all_src, "方案快照必须被用于溯源")
        self.assertIn("snapSrc", self.all_src, "快照来源要真的参与渲染（只取不用=没接）")

    # ---- ⑤ 设计 token 与等宽数字 ------------------------------------------------
    def test_deepseek_tokens_and_tabular_numbers(self):
        for tok in ("--ds-blue:#3964fe", "--r-card:24px", "--up:#c0392b", "--down:#1f9d55"):
            self.assertIn(tok, self.all_src, f"缺少 token {tok}（对标基线见 KB 06 §五）")
        self.assertIn("tabular-nums", self.all_src, "看板数字必须等宽对齐")

    # ---- ⑥ 密码管理器 ----------------------------------------------------------
    def test_password_field_opts_out_of_password_manager(self):
        self.assertIn('autocomplete="new-password"', self.all_src,
                      "授权码输入框会被浏览器当密码保存，必须 new-password")

    # ---- ⑦ 首屏信息层级（袖套 = 主策略在前） -----------------------------------
    def test_first_screen_shows_weekly_sleeve_status(self):
        """主策略（周频袖套）状态必须在今日总览视图 KPI 之前（TodayView 模板顺序）。"""
        tv = (SRC / "views" / "TodayView.vue").read_text(encoding="utf-8")
        self.assertIn("ActionCenter", tv)
        self.assertLess(tv.index("<SleeveStatusBar"), tv.index("<KpiGrid"),
                        "袖套状态必须在月频 KPI **之前**（否则又把主策略藏到下面）")

    # ---- ⑧ 袖套可信度红线（07 号 #34 的前端侧） --------------------------------
    def test_sleeve_status_never_fakes_success_on_empty_marker(self):
        s = (SRC / "store.js").read_text(encoding="utf-8")
        self.assertIn("空占位", s, "空占位分支被删 ⇒ 会把'没建仓'显示成'已执行'")
        self.assertIn('der.indexOf("等 18:05")', s, "盘前占位的判定分支必须存在")
        self.assertIn("held > 0 || traded > 0", s, "只有真有成交/持仓才允许绿色")

    # ---- ⑨ 已持 = 实时账本 -------------------------------------------------------
    def test_first_screen_held_count_is_the_live_ledger_value(self):
        s = (SRC / "components" / "SleeveStatusBar.vue").read_text(encoding="utf-8")
        self.assertIn('["目标 · 已持(实时)"', s, "标签必须写明是实时口径")
        i = s.index('["目标 · 已持(实时)"')
        cell = s[i:i + 200]
        self.assertIn("${held}", cell, "已持必须取实时持仓")
        self.assertNotIn("pl.n_held", cell, "不许再用方案快照的 n_held 冒充实时持仓")

    # ---- ⑩ markdown 强调符不许进渲染字符串 ---------------------------------------
    def test_doctor_verdict_text_strips_markdown_emphasis(self):
        s = (SRC / "store.js").read_text(encoding="utf-8")
        self.assertIn('replace(/\\*\\*/g, "")', s,
                      "doctor 判据文案里的 **加粗** 必须剥掉，否则页面显示星号")

    # ---- ⑪ 可访问性 ---------------------------------------------------------------
    def test_sleeve_status_is_accessible(self):
        self.assertIn('role="status"', self.all_src, "状态芯片要能被读屏播报（role=status）")
        self.assertIn("<dt", self.all_src, "事实格用 dt/dd 语义，不用裸 div")

    # ---- ⑫ 窄屏断点 -----------------------------------------------------------------
    def test_narrow_screen_breakpoint_present(self):
        self.assertIn("@media(max-width:560px)", self.all_src,
                      "窄屏断点丢了 ⇒ 手机宽度 KPI 会被撑破（2026-09-13 实测）")

    # ---- ⑬ 表格行点击必须真的发出事件（2026-09-18：逗号表达式写法静默失效）-------------
    def test_table_row_click_really_emits(self):
        """ZCode 轮（b94e8163）写的是 @click="('row-click', r)" —— 逗号表达式，永不 emit；
        源码与已提交产物里的行点击都点不动，而全量 pytest 对前端零覆盖 ⇒ 靠这条钉死。"""
        s = (SRC / "components" / "DataTable.vue").read_text(encoding="utf-8")
        self.assertIn("$emit('row-click'", s,
                      "行点击必须 $emit('row-click', r)（写成 ('row-click', r) 是逗号表达式）")
        self.assertNotIn('@click="(\'row-click\'', s,
                         "逗号表达式写法：事件发不出，功能静默失效")
        self.assertIn("clickable", s,
                      "行手型必须由 clickable 控制（否则全站表格都显示假手型）")
        tv = (SRC / "views" / "TradingView.vue").read_text(encoding="utf-8")
        i = tv.index("@row-click=")
        cell = tv[max(0, i - 400):i]
        self.assertIn("clickable", cell, "打开 row-click 的表必须显式打开 clickable")

    # ---- ⑭ 事件流：取数失败 / 不在列表 / 确实无事件，三态必须分开 ------------------------
    def test_story_fetch_failure_is_not_shown_as_empty(self):
        """2026-09-18 审查：catch 把失败写成 events: [] ⇒ 界面显示"近 90 天无图谱事件"，
        失败与"真没有事件"同形（07 号"看着装好了"一类）。"""
        tv = (SRC / "views" / "TradingView.vue").read_text(encoding="utf-8")
        self.assertIn("storyError", tv, "取数失败必须有独立状态，不许塌进空事件分支")
        self.assertIn("取数失败", tv, "失败文案必须与'无事件'文案区分")
        kc = (SRC / "kg_story_client.js").read_text(encoding="utf-8")
        self.assertIn("missing", kc, "返回列表里没有该股 ≠ 该股无事件（必须区分）")

    # ---- ⑮ 折叠开关必须键盘可达（07 号 #12/#60 一类：看着能点，实际操作不到）-------------
    def test_long_answer_toggle_is_a_button(self):
        """2026-09-18：展开提示原为 `<div @click>` —— 鼠标能点、键盘/读屏用不了；
        且文案把字符数说成"段"。改成 <button type="button"> + 说人话的计数。"""
        ap = (SRC / "components" / "AskPanel.vue").read_text(encoding="utf-8")
        i = ap.index("回答较长")
        self.assertIn("<button", ap[max(0, i - 300):i],
                      "展开开关必须是 <button>（div@click 键盘不可达，没有 tabindex/role 就是死路）")
        self.assertNotIn('<div v-if="m.role !== \'q\' && m.text.length > 600', ap,
                         "回归到 div@click 了")
        self.assertIn("约 {{ m.text.length }} 字", ap,
                      "字符数不许再写成'段'（Math.ceil(len/300) 本身也是瞎猜）")

    # ---- ⑯ 问答 history 不得把当前问题再发一遍（2026-09-18 审查 Q-01）-------------------
    def test_ask_history_excludes_the_current_question(self):
        """前端 `ask()` 先 `msgs.push({role:'q'})` 再 `slice(-6)` ⇒ 切片必然含刚提的问题，
        而 `app/web/llm.py` 拼的是 `[system, *history, user(question)]`、**后端不去重**
        ⇒ 同一个问题在上下文里出现两遍（双份 token、且与 f44ee18a 自钉的
        "history 不含当前问题"契约直接相悖）。"""
        ap = (SRC / "components" / "AskPanel.vue").read_text(encoding="utf-8")
        i = ap.index("const hist")
        seg = ap[i:i + 400]
        self.assertIn("slice(0, -1)", seg,
                      "history 必须先摘掉末尾刚 push 的当前问题，再截 6 条")
        self.assertNotIn("this.msgs.slice(-6)", ap,
                         "回归到含当前问题的切法了（问题会被双发进上下文）")
        self.assertIn("LLM 调用失败", seg,
                      "失败占位文案不许以 assistant 身份混进模型上下文（错误会自我强化）")

    # ---- ⑰ 拥挤度卡不得把"取数失败"渲染成"没有风险"（2026-09-18 审查 Q-02）--------------
    def test_crowd_card_surfaces_failed_fetches(self):
        """后端逐股 `except: continue` 若全炸，positions 为空 ⇒ hotCount=0，
        这张卡会从"数据缺失"读出"无拥挤风险"的确定性结论。"""
        tv = (SRC / "views" / "TradingView.vue").read_text(encoding="utf-8")
        i = tv.index("crowdRisk() {")
        self.assertIn("n_failed", tv[i:i + 900],
                      "crowdRisk 必须把后端的 n_failed 带出来")
        self.assertIn("取数失败", tv,
                      "界面上必须显示失败只数（空列表与失败必须可辨）")
        kc = (SRC / "kg_story_client.js").read_text(encoding="utf-8")
        self.assertIn("3660", kc, "days 客户端也要夹住，避免白跑一个必然 422 的请求")

    # ---- ⑱ 模板里用的 format 常量必须挂在实例上（2026-09-18 白屏事故）--------------
    def test_template_imported_consts_are_exposed_on_instance(self):
        """options API 的模板只能看到**实例上**的东西。`import { STATE_CN } from '../format'`
        直接写进模板会编译成 `_ctx.STATE_CN` = undefined，取值即抛
        `Cannot read properties of undefined (reading 'neutral')` —— 报错里的
        'neutral' 是 `pool_state` 的**值**，不是属性名，极具迷惑性。

        2026-09-18 实测后果：`/api/style` 当晚 19:30 档刷出 `diverged=true`
        （池子中性 / 基准防守），PlanView 的风格分歧横幅**第一次**被渲染就整页白屏；
        且抛错的渲染效应挂在响应式 flush 队列里，每次 store 轮询刷新都再抛一次、
        打断同队列其它组件的更新 ⇒ 用户长开的标签页里连别处区块也停止刷新。
        全量 pytest 与契约测试都抓不到它（前端无单测框架），只能靠这条钉死。
        """
        for p in sorted((SRC / "views").glob("*.vue")) + sorted((SRC / "components").glob("*.vue")):
            txt = p.read_text(encoding="utf-8")
            m = re.search(r"import\s*\{([^}]*)\}\s*from\s*['\"](?:\.\./)+format['\"]", txt)
            if not m or "<script>" not in txt:
                continue
            si = txt.index("<script>")
            tpl, scr = txt[:si], txt[si:]
            for n in (x.strip() for x in m.group(1).split(",") if x.strip()):
                if not re.search(r"\b%s\b" % re.escape(n), tpl):
                    continue
                exposed = re.search(r"(?:^|[\s,{])%s\s*(\(\s*\)\s*\{|:)" % re.escape(n), scr)
                self.assertTrue(exposed,
                                f"{p.name} 的模板用了 {n}，但没把它挂成 computed/data/方法 —— "
                                f"options API 下模板拿不到 import 常量，渲染必崩")

    # ---- 构建产物完整性（CI 无 node，产物入库即可运行） ------------------------------
    def test_nav_single_source_covers_four_entry_points(self):
        """2026-10-08 批3：导航收敛为**单一真源** src/nav.js。

        此前导航在四处人工维护（App.vue NAV_GROUPS / router.js 路由表 /
        CommandPalette.vue navItems / ActionCenter.vue ROUTE_CN），新增页面要改四处，
        漏一处 = 功能页从某个入口静默消失（2026-10-06 重构批登记过该同步面；
        2026-10-08 B0 改名又在 PicksIntersectionView 漏出一处死链）。

        本测试的守卫强度**不降**：① 断言 nav.js 里有 picks 叶（三模型交集页在导航里）；
        ② 断言四个入口**全部 import nav.js** —— 任一处退回手写副本即红。"""
        nav = (SRC / "nav.js").read_text(encoding="utf-8")
        self.assertIn('name: "picks"', nav, "nav.js 缺 picks 叶（三模型交集页从导航消失）")
        self.assertTrue((SRC / "views" / "PicksIntersectionView.vue").exists())
        for rel, path in (
            ("App.vue", SRC / "App.vue"),
            ("router.js", SRC / "router.js"),
            ("components/CommandPalette.vue", SRC / "components" / "CommandPalette.vue"),
            ("components/ActionCenter.vue", SRC / "components" / "ActionCenter.vue"),
        ):
            text = path.read_text(encoding="utf-8")
            self.assertRegex(
                text, r"from\s+['\"]\.{1,2}/nav['\"]",
                f"{rel} 未 import nav.js ⇒ 导航又分叉成手写副本（改一处忘三处的老问题）")

    def test_built_assets_exist_and_no_inline_script(self):
        """构建产物必须在（看板由 FastAPI 直接服务 assets/），且 index.html 无内联脚本
        （CSP script-src 'self'，内联 = 白屏）。产物过期重建: cd frontend && npm run build。"""
        static = BASE / "app" / "web" / "static"
        self.assertTrue((static / "index.html").exists(), "app/web/static/index.html 不见了")
        html = (static / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("<script>", html, "构建产物含内联脚本 ⇒ CSP 下白屏")
        assets = list((static / "assets").glob("*.js")) if (static / "assets").exists() else []
        self.assertTrue(assets, "assets/*.js 不见了 —— 构建产物未提交或被清空")

    def test_built_assets_not_stale_vs_source(self):
        """2026-09-16 审查 ENG-01：改了 frontend/src 忘了 rebuild ⇒ 看板跑旧代码，
        而且没有任何测试会红（契约测试扫的是源码，跑的是产物）。这里钉新鲜度：
        源码里最新的 mtime 不得比构建产物 index.html 新 60 秒以上（60 秒容错覆盖
        git checkout 全量重写 mtime 的场景）。红 ⇒ cd frontend && npm run build。"""
        import time
        src_latest = max(p.stat().st_mtime for p in SRC.rglob("*") if p.is_file())
        built = (BASE / "app" / "web" / "static" / "index.html").stat().st_mtime
        self.assertLessEqual(
            src_latest - built, 60,
            "frontend/src 有比构建产物新的改动 —— 看板在跑旧代码。"
            "重建: cd frontend && npm run build，然后把 app/web/static/ 一并提交。")


    def test_two_benchmark_scales_are_labelled_differently(self):
        """两把基准尺的图例**必须不一样**（2026-09-20 B1 采纳轮，14 号三补二十八 §⑤）。

        回测/净值那张图的基准曲线自 2026-09-20 起是全收益指数 `H00906.CSI`（超额同尺），
        而模拟盘那张图仍对价格指数 `000906.SH`（`paper_nav.bench_nav` 本轮有意未换尺）。
        ⇒ 两处文案一旦"被统一"，就说明有人没读预注册 §5 就把两条腿并成一把尺了。
        （2026-10-06 分类细化：月频模拟盘图随内容迁到 PaperView.vue，钉点跟随。）
        """
        plan = (SRC / "views" / "PlanView.vue").read_text(encoding="utf-8")
        paper = (SRC / "views" / "PaperView.vue").read_text(encoding="utf-8")
        self.assertIn("全收益", plan, "回测基准曲线已换全收益尺，图例不能再写价格指数")
        self.assertNotIn("中证800价格指数", plan, "回测图例仍是价格指数 ⇒ 与 bench_nav 不同尺")
        self.assertIn("中证800价格指数", paper,
                      "模拟盘那张图的基准列仍是价格指数（本轮有意未换）⇒ 文案不许跟着改")


    def test_style_lookback_default_is_250(self):
        """2026-09-18 审查 P1-01：`frontend/src/api.js` 的 `style()` 默认 lookback 原为
        **60**，而 `store.js` 是无参调用 `api.style()` ⇒ 实际传了 60，把风格曲线从后端
        默认的 250 天**静默**截到 60 天（store.js 自己的注释就写明"传 60 会把风格曲线
        从 250 天静默缩到 60 天"，但默认值写反了）。看板上看不出来，只能靠钉默认值。"""
        s = (SRC / "api.js").read_text(encoding="utf-8")
        m = re.search(r"export\s+const\s+style\s*=\s*\(\s*lookback\s*=\s*(\d+)\s*\)", s)
        self.assertIsNotNone(m, "api.js 里找不到 style 的默认 lookback 参数")
        self.assertEqual(int(m.group(1)), 250,
                         "style() 默认 lookback 必须是 250（后端默认口径）—— "
                         "传 60 会让风格曲线静默缩短，是已经发生过的缺陷")

    def test_boot_retry_is_singleton_and_wake_throttled(self):
        """2026-09-18 审查 P2-05：失败重试 `setTimeout(boot, 30000)` 原无 clear ⇒ 连续
        失败叠加多个定时器；窗口聚焦原每次都 `boot()` 全量重发 8 个域（实测 2.4h 内每域
        约 1300 次请求）。钉两件事：重试定时器单例、聚焦刷新带节流。"""
        s = (SRC / "store.js").read_text(encoding="utf-8")
        self.assertIn("clearTimeout", s, "重试定时器必须是单例（连续失败会叠加）")
        self.assertIn("_lastWakeAt", s, "窗口聚焦刷新必须带节流，不能每次都全量 boot()")
        self.assertIn("60 * 1000", s, "聚焦节流窗口应为 60 秒")
        # 轮询降频：/api/paper 不得再是 15 秒（每请求 2 次 MAX(trade_date) + 实时行情）
        self.assertNotIn("LOADERS.paper(); }, 15 * 1000", s,
                         "/api/paper 轮询已降为 60 秒，别改回 15 秒")

    # ---- ⑱ 全账户页模型清单前后端同源（2026-10-06 审查补） ----------------------------
    def test_all_accounts_model_order_synced_with_backend(self):
        """全账户页的模型分区清单在两处人工维护：前端 acctCols.js 的 MODEL_ORDER 与
        后端 app/all_accounts.py 的 MODEL_SCHEDULE 键（SOURCES 决定账户归属）。
        后端加模型而前端忘了 ⇒ 新模型的卡片在门户里**静默不渲染**。钉死两侧集合一致。"""
        fe = (SRC / "acctCols.js").read_text(encoding="utf-8")
        m = re.search(r"MODEL_ORDER\s*=\s*\[([^\]]*)\]", fe)
        self.assertIsNotNone(m, "acctCols.js 里找不到 MODEL_ORDER")
        fe_models = set(re.findall(r'"([^"]+)"', m.group(1)))
        be = (BASE / "app" / "all_accounts.py").read_text(encoding="utf-8")
        bm = re.search(r"MODEL_SCHEDULE\s*=\s*\{(.*?)\n\}", be, re.S)
        self.assertIsNotNone(bm, "all_accounts.py 里找不到 MODEL_SCHEDULE")
        be_models = set(re.findall(r'^\s*"([^"]+)":\s*\[', bm.group(1), re.M))
        self.assertEqual(fe_models, be_models,
                         "前端 MODEL_ORDER 与后端 MODEL_SCHEDULE 模型清单不同步 —— "
                         "新增模型要三处同改：all_accounts.py(SOURCES+MODEL_SCHEDULE) + acctCols.js")

    # ---- ⑲ 问答覆盖全账户（2026-10-06 看板重构补缺口） --------------------------------
    def test_ask_engine_covers_all_accounts(self):
        """看板已是全账户枢纽，但问答引擎（本地规则 + LLM 智能体工具）此前只接
        NEW ECO 数据域——Final/W2 的账本问不进来。钉住双侧覆盖。"""
        s = (SRC / "intelligence" / "answers.js").read_text(encoding="utf-8")
        self.assertIn("answerAccounts", s, "问答引擎缺全账户意图函数")
        for kw in ("Final", "W2", "熔断", "心跳"):
            self.assertIn(kw, s, f"问答全账户意图缺关键词 {kw}")
        self.assertIn("全账户现在怎么样", s, "预设问题里要有全账户入口")
        llm = (BASE / "app" / "web" / "llm.py").read_text(encoding="utf-8")
        self.assertIn("get_all_accounts", llm, "LLM 智能体工具缺全账户检索")


if __name__ == "__main__":
    unittest.main()
