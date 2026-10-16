/* 全账户视图的常量 / 格式化助手 / 6 组表格列配置。
   2026-10-06 自 AllAccountsView.vue 外移（列定义占原文件 ~70 行）。
   MODEL_ORDER / MODEL_NOTES 与 app/all_accounts.py 的 MODEL_ORDER/MODEL_NOTE 同源同序
   —— 前后端一致性由 tests/test_web_static_contract.py 的守卫测试钉死，改一头必须同步另一头。 */
import { esc, stockLink } from './format'
import { colors as themeColors, cssVar } from './charts'

/* 各账户曲线配色（与主题 chrome 色分离；基准线用主题灰）。
   2026-10-08 批2：原为模块级硬编码色表，与 tokens.css 双轨 ⇒ 改为读 --series-1..7。
   惰性求值（函数而非常量）是必须的：模块级常量只在首次 import 时读一次，
   主题切换后不会更新；本函数在 seriesForAccounts 内调用，随 store.theme 重算。 */
export function seriesColors() {
  return {
    "neweco-monthly": cssVar("--series-1"),
    "neweco-weekly": cssVar("--series-2"),
    "fterm-w2": cssVar("--series-3"),
    "fterm-t3": cssVar("--series-4"),
    "ecow2-k6": cssVar("--series-5"),
    "ecow2-k8": cssVar("--series-6"),
  };
}

/* 2026-10-08 三盘面改名：fterm→Final、ECO-W2→W2。
   注意三者不是同一件事：盘面键 "Final" / 仓库 D:\Final / 代码包名 fterm；
   盘面键 "W2" / 仓库 D:\ECO-W2。账户 id 保持原样（数据查找键）。 */
export const MODEL_ORDER = ["NEW ECO", "Final", "W2"];

/* 账户 → 该账户归属的功能页/模型页路由（门户总览的点行跳转单源，2026-10-06）。
   NEW ECO 双账户各有专属功能页；Final/W2 走盘面页（整盘面一页）。 */
const ACCOUNT_PAGE = {
  "neweco-monthly": { name: "paper" },
  "neweco-weekly": { name: "sleeve" },
};
export function accountRoute(a) {
  if (!a) return null;
  if (ACCOUNT_PAGE[a.id]) return ACCOUNT_PAGE[a.id];
  if (a.model === "Final") return { name: "final" };
  if (a.model === "W2") return { name: "w2" };
  return null;
}

export const MODEL_NOTES = {
  "NEW ECO": "月频盘 + 周频袖套 · 09:31 成交即时邮件 · 19:45 健康档",
  "Final": "dlv2 双轨 · t+1 开盘价撮合 · 每日 20:05 落账",
  "W2": "B0 双臂（锚定周调仓）· 每日 20:30 落账",
};

/* 账户列表 → 归一净值曲线 series（门户总览与 Final/W2 盘面页共用，2026-10-06 单源）。
   虚线基准只对带 extra.bench_curve 的账户生效（目前仅月频盘有）。 */
export function seriesForAccounts(list) {
  const c = themeColors();
  const palette = seriesColors();
  const series = [];
  (list || []).forEach(a => {
    if (!a.ok || !a.curve || !a.curve.length) return;
    series.push({ name: a.name, points: a.curve,
                  color: palette[a.id] || c.gray });
    const bench = a.extra && a.extra.bench_curve;
    if (bench && bench.length > 1) {
      series.push({ name: a.name + "·基准", points: bench, color: c.gray, dashed: true });
    }
  });
  return series;
}

/* 金额全精度（NEW ECO 全局 fmtMoney 是万/亿口径，账户页要精确到分） */
export function money(x) {
  if (x === null || x === undefined || isNaN(Number(x))) return "--";
  return Number(x).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
export function thousand(x) {
  if (x === null || x === undefined || isNaN(Number(x))) return "--";
  return Number(x).toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}
export function pctTxt(v) {
  if (v === null || v === undefined || isNaN(Number(v))) return "--";
  return (v > 0 ? "+" : "") + Number(v).toFixed(2) + "%";
}
export function pctCls(v) {
  if (v === null || v === undefined || !Number(v)) return "";
  return v > 0 ? "pos" : "neg";
}
export function sideTxt(side) {
  const s = String(side || "").toLowerCase();
  return s === "buy" ? "买入" : s === "sell" ? "卖出" : String(side || "—");
}
export function sideCls(side) {
  const s = String(side || "").toLowerCase();
  return s === "buy" ? "pos" : s === "sell" ? "neg" : "";
}

export const posCols = [
  { key: "code", label: "标的",
    render: r => stockLink(r.code, r.name)
                 + (r.stuck ? ' <span class="stuck-badge">卡</span>' : ""),
    value: r => r.code },
  { key: "shares", label: "数量", num: true, render: r => thousand(r.shares) },
  { key: "cost", label: "成本", num: true,
    render: r => (r.cost == null ? "—" : Number(r.cost).toFixed(3)) },
  { key: "last", label: "现价", num: true,
    render: r => (r.last == null ? "—" : Number(r.last).toFixed(3)) },
  { key: "value", label: "市值", num: true, render: r => money(r.value) },
];

export const tradeCols = [
  { key: "date", label: "日期", render: r => esc(r.date || "—") },
  { key: "code", label: "标的",
    render: r => stockLink(r.code, r.name), value: r => r.code },
  { key: "side", label: "方向", render: r => sideTxt(r.side), cellClass: r => sideCls(r.side) },
  { key: "shares", label: "数量", num: true, render: r => thousand(r.shares) },
  { key: "price", label: "价格", num: true,
    render: r => (r.price == null ? "—" : Number(r.price).toFixed(3)) },
  { key: "amount", label: "金额", num: true, render: r => money(r.amount) },
  { key: "reason", label: "原因", render: r => esc(r.reason || "") },
];

export const decisionCols = [
  { key: "date", label: "日期", render: r => esc(r.date || "—") },
  { key: "code", label: "标的", render: r => esc(r.code || "") },
  { key: "direction", label: "方向", render: r => esc(r.direction || "—") },
  { key: "approved", label: "通过",
    render: r => (r.approved == null ? "—"
                  : (r.approved ? '<span class="pos">✓</span>' : '<span class="neg">✗</span>')) },
  { key: "reason", label: "原因", render: r => esc(r.reason || "") },
];

export const periodCols = [
  { key: "anchor", label: "期", render: r => esc(r.anchor || "—") },
  { key: "t1", label: "T1", render: r => esc(r.t1 || "—") },
  { key: "nav_bop", label: "期初", num: true, render: r => money(r.nav_bop) },
  { key: "nav_post", label: "成交后", num: true, render: r => money(r.nav_post) },
  { key: "nav_eop", label: "期末", num: true, render: r => money(r.nav_eop) },
  { key: "n_pos", label: "持仓", num: true, render: r => (r.n_pos == null ? "—" : r.n_pos) },
  { key: "n_new", label: "新交易", num: true, render: r => (r.n_new == null ? "—" : r.n_new) },
  { key: "allow_buys", label: "可买",
    render: r => (r.allow_buys == null ? "—" : (r.allow_buys ? "是" : "否")) },
  { key: "circuit_status", label: "熔断态", render: r => esc(r.circuit_status || "—") },
];

export const runCols = [
  { key: "anchor", label: "期(anchor)", render: r => esc(r.anchor || "—") },
  { key: "t1", label: "T1", render: r => esc(r.t1 || "—") },
  { key: "status", label: "状态",
    render: r => esc(r.status || "—") + (r.reason ? ' <span class="muted">(' + esc(r.reason) + ")</span>" : "") },
  { key: "n_pos", label: "持仓", num: true, render: r => (r.n_pos == null ? "—" : r.n_pos) },
  { key: "ran_at", label: "运行于", render: r => esc(r.ran_at || "—") },
];

/* ---- 三模型选股榜（2026-10-08, /api/model_picks）----
   基础列 = 名次 + 标的(stockLink)；各模型分数语义不同，由各视图追加 metric 列。 */
export const pickBaseCols = [
  { key: "rank", label: "#", num: true, render: r => esc(String(r.rank ?? "—")) },
  { key: "code", label: "标的",
    render: r => stockLink(r.code, r.name), value: r => r.code },
];

/* 交集表列：每模型一列名次（缺榜=—），按 MODEL_ORDER 定列序。 */
export function intersectCols(models) {
  return [
    { key: "code", label: "标的",
      render: r => stockLink(r.code, r.name), value: r => r.code },
    ...models.map(m => ({
      key: "rank_" + m, label: m + " 名次", num: true,
      render: r => esc(String((r.ranks && r.ranks[m]) ?? "—")),
      value: r => (r.ranks && r.ranks[m]) ?? 999999,
    })),
  ];
}

export const alertCols = [
  { key: "ts", label: "时间", render: r => esc(r.ts || "") },
  { key: "level", label: "级别", render: r => esc(r.level || "") },
  { key: "code", label: "标的", render: r => esc(r.code || "") },
  { key: "message", label: "消息", render: r => esc(r.message || "") },
];
