/* 格式化工具（自旧版 app.js 移植，口径不变） */

/* HTML 转义: 所有来自数据库/接口的字段渲染前必须过这里 */
export function esc(s) {
  if (s === null || s === undefined) return "";
  return String(s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
export function fmtPct(x, d = 2) {
  if (x === null || x === undefined || isNaN(x)) return "--";
  return (x * 100).toFixed(d) + "%";
}
export function fmtNum(x, d = 0) {
  if (x === null || x === undefined || isNaN(x)) return "--";
  return Number(x).toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d });
}
export function fmtMoney(x) {
  if (x === null || x === undefined || isNaN(x)) return "--";
  const abs = Math.abs(x);
  const w = abs >= 1e8 ? (abs / 1e8).toFixed(2) + "亿"
    : abs >= 1e4 ? (abs / 1e4).toFixed(0) + "万" : abs.toFixed(0);
  return (x < 0 ? "-" : "") + w;
}
export const STATE_CN = { attack: "进攻", neutral: "中性", defense: "防守" };
export const ACTION_CN = { sell: "止损卖出", sell_weak: "弱势替换卖出", buy_candidate: "候选买入", watch: "观察", reduce: "减仓", add: "加仓", warn: "风险预警" };
export const ACTION_CLS = { sell: "neg", sell_weak: "neg", reduce: "neg", warn: "neg", buy_candidate: "pos", add: "pos", watch: "" };

export function today8() {
  const d = new Date();
  return `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`;
}

export function daysBetween8(d8) {
  if (!d8 || String(d8).length !== 8) return null;
  const t = new Date(+String(d8).slice(0, 4), +String(d8).slice(4, 6) - 1, +String(d8).slice(6));
  return Math.floor((Date.now() - t.getTime()) / 86400000);
}

/* 个股详情链接（2026-10-05 全站联动）：所有股票代码经此渲染成可点链接。
   hash 路由下原生 <a href="#/stock/..."> 即可；文本部分必须先过 esc。
   DataTable 的排序/筛选/导出走 value||key，render 里放 <a> 不影响三者。 */
export function stockLink(code, name) {
  if (!code) return "--";
  const label = esc(String(name || code));
  return `<a class="stock-link" href="#/stock/${encodeURIComponent(String(code))}" title="查看个股详情">${label}</a>`;
}
