/* 所有 /api 调用的唯一出口（契约测试用 getJSON 加 /api 前缀的正则从这里抽 URL） */

export async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(url + " " + r.status);
  return r.json();
}

async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Neweco-Client": "web" },
    body: JSON.stringify(body ?? {}),
  });
  let data = {};
  try { data = await r.json(); } catch (e) { /* 非 JSON 响应 */ }
  if (!r.ok) {
    const detail = data && data.detail ? data.detail : "HTTP " + r.status;
    throw new Error(detail);
  }
  return data;
}

/* ---------- 读接口（与后端路由一一对应） ---------- */
export const overview      = () => getJSON("/api/overview");
/* 2026-09-18 审查 P1-01: 默认 60 → 250。store.js 是无参调用，原默认值会把风格曲线
   从 250 天静默缩到 60 天（与 store.js 注释要求的后端默认口径不符）。 */
export const style         = (lookback = 250) => getJSON("/api/style?lookback=" + lookback);
export const signal        = () => getJSON("/api/signal");
export const lhb           = () => getJSON("/api/lhb");
export const paper         = () => getJSON("/api/paper");
export const nav           = () => getJSON("/api/nav");
export const plan          = () => getJSON("/api/plan");
export const scores        = (limit = 30) => getJSON("/api/scores?limit=" + limit);
export const importance    = () => getJSON("/api/importance");
export const status        = () => getJSON("/api/status");
export const notify        = () => getJSON("/api/notify");
export const settings      = () => getJSON("/api/settings");
export const weekly        = () => getJSON("/api/weekly");
export const weeklyHealth  = () => getJSON("/api/weekly/health");
export const weeklyAttrib  = () => getJSON("/api/weekly/attrib");
/* 全账户聚合（2026-10-05）：Final/W2 纸面账户只读接入，见 app/all_accounts.py */
export const allAccounts   = () => getJSON("/api/all_accounts");
/* 2026-10-08 三模型选股榜 Top100 + 交集（app/model_picks.py，纯只读） */
export const modelPicks    = () => getJSON("/api/model_picks");
/* 个股详情（2026-10-05）：query 参数设计（api 契约按 getJSON 抽 URL 与 OpenAPI 精确比对） */
export const stockDetail   = (code) => getJSON("/api/stock?code=" + encodeURIComponent(code));
export const llmStatus     = () => getJSON("/api/llm/status");
export const llmFactorReview = () => getJSON("/api/llm/factor_review");

/* ---------- 写接口 ---------- */
export const saveSettings  = (body) => postJSON("/api/settings", body);
export const runDaily      = () => postJSON("/api/run/daily");
export const runMonthly    = () => postJSON("/api/run/monthly");
export const llmAsk        = (question, agentMode = false, researchMode = false, history = []) =>
  postJSON("/api/llm/ask", { question, agent_mode: agentMode, research_mode: researchMode,
                             history: history.slice(-6) });
export const llmSaveConfig = (body) => postJSON("/api/llm/config", body);
export const llmTest       = () => postJSON("/api/llm/test", {});
export const kgPositionsStory = () => getJSON("/api/kg/positions_story");
