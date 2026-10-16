/* 导航单一真源（2026-10-08 批3）。
 *
 * 背景：导航此前在**四处**人工维护 ——
 *   App.vue 的 NAV_GROUPS / router.js 的路由表 /
 *   CommandPalette.vue 的 navItems / ActionCenter.vue 的 ROUTE_CN。
 * 新增一个页面要改四处，漏一处 = 该页从某个入口静默消失
 * （2026-10-06 重构批已登记过这个同步面，2026-10-08 又因 B0 改名在
 * PicksIntersectionView 漏出一处死链，故本次收敛为单源）。
 *
 * 现在四处全部 import 本文件；新增/移动页面**只改这里一处**。
 * 守卫测试 tests/test_web_static_contract.py::test_nav_four_place_sync_includes_picks
 * 已同步改为「断言本文件含 picks 叶 + 四处均 import nav.js」。
 */

/* 侧栏/命令面板可见的功能页。view 用动态 import 保持路由级懒加载。
   group 的首次出现顺序即侧栏分组顺序。 */
export const PAGES = [
  // ---- 总览 ----
  { name: "accounts", path: "/accounts", group: "总览", ico: "🏦", label: "全账户总览",
    view: () => import("./views/AllAccountsView.vue") },
  { name: "today", path: "/today", group: "总览", ico: "🧭", label: "今日行动",
    view: () => import("./views/TodayView.vue") },
  { name: "picks", path: "/picks", group: "总览", ico: "🔗", label: "三模型选股交集",
    view: () => import("./views/PicksIntersectionView.vue") },
  /* 2026-10-08 批6：跨盘面运行结果对比（补"三盘面没有对比"的缺口，零后端改动） */
  { name: "compare", path: "/compare", group: "总览", ico: "⚖️", label: "跨盘面对比",
    view: () => import("./views/CompareView.vue") },
  // ---- NEW ECO（月频老盘）----
  /* 2026-10-08 批4：补上**盘面页**。此前 Final/W2 各有"一页装完盘面"的页，而 NEW ECO
     的盘面内容散在 8 个子页里 ⇒ 导航长期 8:1:1 不对称。本页只放三盘面共有的盘面级区块。 */
  { name: "neweco", path: "/neweco", group: "NEW ECO", ico: "🧬", label: "NEW ECO 盘面",
    view: () => import("./views/NewEcoHomeView.vue") },
  { name: "paper", path: "/paper", group: "NEW ECO", ico: "💼", label: "月频模拟盘",
    view: () => import("./views/PaperView.vue") },
  { name: "sleeve", path: "/sleeve", group: "NEW ECO", ico: "🧵", label: "周频袖套",
    view: () => import("./views/TradingView.vue") },
  { name: "plans", path: "/plans", group: "NEW ECO", ico: "🎯", label: "方案与净值",
    view: () => import("./views/PlanView.vue") },
  { name: "funds", path: "/funds", group: "NEW ECO", ico: "💰", label: "现金与基金",
    view: () => import("./views/FundsView.vue") },
  { name: "style", path: "/style", group: "NEW ECO", ico: "🎨", label: "风格读数",
    view: () => import("./views/StyleView.vue") },
  { name: "model", path: "/model", group: "NEW ECO", ico: "📊", label: "打分与因子",
    view: () => import("./views/DataView.vue") },
  { name: "llm", path: "/llm", group: "NEW ECO", ico: "🤖", label: "LLM 因子审读",
    view: () => import("./views/LlmReviewView.vue") },
  // ---- 市场情报 ----
  /* 2026-10-08 批4：龙虎榜是**市场层**数据（全市场龙虎榜/游资动向），与 NEW ECO 盘面
     无关，此前误挂在 NEW ECO 组下 ⇒ 上移独立成组。 */
  { name: "lhb", path: "/lhb", group: "市场", ico: "🐉", label: "龙虎榜与游资",
    view: () => import("./views/LhbView.vue") },
  // ---- Final（日频 2 万）----
  { name: "final", path: "/final", group: "Final", ico: "🛰", label: "双轨账户",
    paletteLabel: "Final 双轨账户", view: () => import("./views/FinalView.vue") },
  // ---- W2（周频 2 万）----
  { name: "w2", path: "/w2", group: "W2", ico: "⛓", label: "三账本",
    paletteLabel: "W2 三账本", view: () => import("./views/W2View.vue") },
  // ---- 运维 ----
  /* 2026-10-08 批4：系统状态（45 行单表）与邮件记录（39 行单表）信息密度都极低，
     合并为一页；两个原视图保留为可复用单元，由 OpsView 组合（区块 id #status/#notify
     不变，今日行动中枢的深链锚点继续可用）。旧路径 /status、/notify 走 redirect。 */
  { name: "ops", path: "/ops", group: "运维", ico: "🩺", label: "系统与邮件",
    view: () => import("./views/OpsView.vue") },
  { name: "settings", path: "/settings", group: "运维", ico: "⚙️", label: "设置",
    view: () => import("./views/SettingsView.vue") },
];

/* 不在导航里的路由（深链 landing 页）。
   个股详情由全站股票代码点击进入（#/stock/300621.SZ），故不进侧栏。 */
export const EXTRA_ROUTES = [
  { path: "/stock/:code", name: "stockDetail",
    view: () => import("./views/StockDetailView.vue") },
];

/* 旧路径兼容重定向（改 URL 必须留 redirect，否则旧书签跳空）。
   2026-10-08 累积：/trading（2026-10-06 改名）、/data（同日）、/fterm 与 /ecow2（B0 改名）。 */
export const REDIRECTS = [
  { path: "/", name: "accounts" },
  { path: "/trading", name: "sleeve" },
  { path: "/data", name: "model" },
  { path: "/fterm", name: "final" },
  { path: "/ecow2", name: "w2" },
  /* 2026-10-08 批4：状态页与邮件页合并为 /ops，旧路径继续可用 */
  { path: "/status", name: "ops" },
  { path: "/notify", name: "ops" },
];

/* 未匹配路径的兜底 */
export const FALLBACK = "accounts";

/* ---- 由 PAGES 派生的三个视图（此前各自手写一份）---- */

/** 侧栏分组：[{label, items:[{name, ico, label}]}]，组序 = PAGES 首现序 */
export const NAV_GROUPS = PAGES.reduce((groups, p) => {
  let g = groups.find((x) => x.label === p.group);
  if (!g) { g = { label: p.group, items: [] }; groups.push(g); }
  g.items.push({ name: p.name, ico: p.ico, label: p.label });
  return groups;
}, []);

/** 路由名 → 中文标签（ActionCenter 的行动项跳转文案用）。
    用 paletteLabel || label：跳转文案不受侧栏宽度约束，沿用原来的长名
    （"Final 双轨账户"/"W2 三账本"），避免本次重构改变既有文案。 */
export const ROUTE_CN = {
  ...Object.fromEntries(PAGES.map((p) => [p.name, p.paletteLabel || p.label])),
  ...Object.fromEntries(EXTRA_ROUTES.map((r) => [r.name, "个股详情"])),
};

/** 命令面板的视图项（kind=view）；id 约定 v-<name> */
export const PALETTE_VIEWS = PAGES.map((p) => ({
  id: `v-${p.name}`,
  kind: "view",
  label: p.paletteLabel || p.label,
  sub: p.group,
  route: { name: p.name },
}));

/** vue-router 路由表（懒加载 + 重定向 + 兜底） */
export function buildRoutes() {
  return [
    ...REDIRECTS.map((r) => ({ path: r.path, redirect: { name: r.name } })),
    ...PAGES.map((p) => ({ path: p.path, name: p.name, component: p.view })),
    ...EXTRA_ROUTES.map((r) => ({ path: r.path, name: r.name, component: r.view })),
    { path: "/:pathMatch(.*)*", redirect: { name: FALLBACK } },
  ];
}
