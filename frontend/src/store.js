/* 全局数据层：每个接口一个 domain（data/error/loaded），视图按需 ensure，轮询只刷已加载域。
   boot 语义与旧版一致：失败进 failed 数组 → 横幅点名 → 30 秒自动重试。 */
import { reactive } from 'vue'
import * as api from './api'

function domain() { return { data: null, error: null, loaded: false, loading: false } }

/* 2026-09-18 审查 P2-05: 失败重试的 setTimeout 原无 clear，连续失败会叠加多个定时器；
   窗口聚焦原每次都 boot() 全量重发 8 个域（实测 2.4h 内每域约 1300 次请求）。
   这里改为：重试定时器单例 + 聚焦刷新带 60 秒节流。 */
let _retryTimer = null;
let _lastWakeAt = 0;

function initTheme() {
  const saved = localStorage.getItem("neweco-theme");
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export const store = reactive({
  overview: domain(), style: domain(), signal: domain(), lhb: domain(),
  paper: domain(), nav: domain(), plan: domain(), scores: domain(), scorePool: domain(),
  importance: domain(), status: domain(), notify: domain(), settings: domain(),
  weekly: domain(), weeklyHealth: domain(), weeklyAttrib: domain(),
  accounts: domain(),
  picks: domain(),
  llmStatus: domain(), llmReview: domain(), kgStory: domain(),
  theme: initTheme(),
  failed: [],          // boot 失败的区块名（横幅 bootWarn 的数据源）
  paletteOpen: false,
  askOpen: false,
  bootedOnce: false,
})

export function applyTheme(t) {
  store.theme = t;
  localStorage.setItem("neweco-theme", t);
  document.documentElement.dataset.theme = t;
}

async function run(key, fn) {
  const d = store[key];
  d.loading = true;
  try {
    d.data = await fn();
    d.error = null;
    d.loaded = true;
  } catch (e) {
    d.error = String(e && e.message || e);
  } finally {
    d.loading = false;
  }
  return d;
}

export const LOADERS = {
  overview:      () => run("overview", api.overview),
  /* lookback 走后端默认 250 —— 与旧版看板同口径（rel_* 读数虽用全历史，但 series 曲线长度=lookback，
     传 60 会把风格曲线从 250 天静默缩到 60 天，2026-09-16 审查 COR-01） */
  style:         () => run("style", () => api.style()),
  signal:        () => run("signal", api.signal),
  lhb:           () => run("lhb", api.lhb),
  paper:         () => run("paper", api.paper),
  nav:           () => run("nav", api.nav),
  plan:          () => run("plan", api.plan),
  scores:        () => run("scores", () => api.scores(30)),
  /* 大池(500 只)独立于视图用的 30 只池 —— 命令面板/问答用它速查,
     不许覆盖 scores 域(否则打分榜视图会突然显示 500 行) */
  scorePool:     () => run("scorePool", () => api.scores(500)),
  importance:    () => run("importance", api.importance),
  status:        () => run("status", api.status),
  notify:        () => run("notify", api.notify),
  settings:      () => run("settings", api.settings),
  weekly:        () => run("weekly", api.weekly),
  weeklyHealth:  () => run("weeklyHealth", api.weeklyHealth),
  weeklyAttrib:  () => run("weeklyAttrib", api.weeklyAttrib),
  accounts:      () => run("accounts", api.allAccounts),
  picks:         () => run("picks", api.modelPicks),
  llmStatus:     () => run("llmStatus", api.llmStatus),
  kgStory:       () => run("kgStory", api.kgPositionsStory),
  llmReview:     () => run("llmReview", api.llmFactorReview),
}

/* boot：只拉"今日行动中枢"必需的域（比旧版首屏 14 连发快得多），其余进视图时 ensure */
export async function boot() {
  const jobs = [LOADERS.overview(), LOADERS.style(), LOADERS.signal(), LOADERS.lhb(),
                LOADERS.paper(), LOADERS.weekly(), LOADERS.status(), LOADERS.llmStatus()];
  const names = ["overview", "style", "signal", "lhb", "paper", "weekly", "status", "llm"];
  const rs = await Promise.allSettled(jobs);
  const failed = [];
  rs.forEach((r, i) => {
    if (r.status === "rejected" || (r.value && r.value.error)) {
      failed.push(names[i]);
    }
  });
  store.failed = failed;
  store.bootedOnce = true;
  /* 失败也要能读：横幅点名 + 30 秒自动重试，成功后横幅自动消失（语义与旧版一致） */
  if (_retryTimer) clearTimeout(_retryTimer);
  _retryTimer = failed.length ? setTimeout(boot, 30000) : null;
}

/* 视图进入时确保所需域已加载（已加载则不重复拉，轮询会保持新鲜） */
export async function ensure(...keys) {
  const jobs = keys.map(k => {
    const d = store[k];
    if (!d || (d.loaded && !d.error)) return null;
    const loader = LOADERS[k];
    return loader ? loader() : null;
  }).filter(Boolean);
  await Promise.allSettled(jobs);
}

/* 视图进入时强制刷新（用户切回来想看的就是新的） */
export async function refresh(...keys) {
  const jobs = keys.map(k => LOADERS[k] ? LOADERS[k]() : null).filter(Boolean);
  await Promise.allSettled(jobs);
}

/* ---------- 轮询（自旧版 setSmartInterval 移植） ---------- */
export function setSmartInterval(fn, ms) {
  return setInterval(() => {
    if (document.hidden) return;
    try {
      const r = fn();
      if (r && typeof r.catch === "function") r.catch(() => {});
    } catch (e) { /* 瞬时失败静默 */ }
  }, ms);
}

/* 只轮询已加载过的域：没进过的视图不产生后台流量（列表内全部已加载域都刷，不只第一个） */
function pollIfLoaded(...keys) {
  for (const k of keys) {
    if (store[k] && store[k].loaded) LOADERS[k]();
  }
}

export function startPolling() {
  setSmartInterval(() => pollIfLoaded("overview", "style", "signal", "lhb"), 5 * 60 * 1000);
  /* 2026-09-28 审查补 scorePool: 打分日频推新, 原"打开过一次就整场不刷" ⇒ 命令面板/
     问答的打分读数全天用早上那份, 与 scores 主视图口径分叉 */
  setSmartInterval(() => pollIfLoaded("scorePool"), 5 * 60 * 1000);
  /* 15s → 60s：/api/paper 每请求要 2 次 MAX(trade_date) + 一次实时行情拉取，
     15 秒轮询对账户页毫无必要（持仓分钟级不变），却占了大头请求量。 */
  setSmartInterval(() => { if (store.paper.loaded) LOADERS.paper(); }, 60 * 1000);
  setSmartInterval(() => { if (store.weekly.loaded) LOADERS.weekly(); }, 60 * 1000);
  /* 全账户聚合: 15s 服务端缓存 + 只在外部账本落账时变化, 60s 足够 */
  setSmartInterval(() => { if (store.accounts.loaded) LOADERS.accounts(); }, 60 * 1000);
  /* 三模型选股榜: 日频数据, 5 分钟足够（服务端另有 60s 缓存） */
  setSmartInterval(() => pollIfLoaded("picks"), 5 * 60 * 1000);
  setSmartInterval(() => pollIfLoaded("weeklyHealth"), 10 * 60 * 1000);
  setSmartInterval(() => pollIfLoaded("weeklyAttrib"), 30 * 60 * 1000);
  setSmartInterval(() => pollIfLoaded("status", "notify"), 10 * 60 * 1000);
  /* 后台隐藏 tab 暂停轮询, 回到前台立即刷新一次（旧版语义） */
  document.addEventListener("visibilitychange", () => {
    if (document.hidden || !store.bootedOnce) return;
    const now = Date.now();
    if (now - _lastWakeAt < 60 * 1000) return;   // 60 秒内反复切窗不再全量重拉
    _lastWakeAt = now;
    refresh("overview", "style", "signal", "lhb", "paper", "weekly", "status", "llmStatus");
  });
}

/* ---------- 派生读数（行动中枢/简报/问答共用） ---------- */
export function sleeveStatus() {
  const d = store.weekly.data;
  /* 2026-09-28 审查修复(P2): 取数失败与"还在加载"必须可辨 —— 原 data 为 null 一律
     显示"状态加载中…", weekly 域故障时顶栏芯片永远"加载中"(失败与空同形的假绿族) */
  if (store.weekly.error) return { kind: "bad", text: "袖套状态取数失败" };
  if (!d) return { kind: "wait", text: "状态加载中…" };
  const k = d.kpi || {};
  const o = d.ops || {};
  /* 判据原文照抄，但去掉 markdown 强调符（doctor 的文案里有 **加粗**，直出会显示成星号） */
  const der = String(o.exec_detail || "").replace(/\*\*/g, "");
  const held = Number(k.n_positions ?? 0);
  const traded = Number(k.n_trades ?? 0);
  let kind, text;
  if (held > 0 || traded > 0) {
    kind = "ok"; text = `已成交 ${traded} 笔 · 持仓 ${held} 只`;
  } else if (der.indexOf("空占位") >= 0 || der.indexOf("等 18:05") >= 0) {
    kind = "wait"; text = "等 18:05 晚间兜底（今日尚无成交）";
  } else if (der.indexOf("已落账") >= 0) {
    kind = "ok"; text = "执行档已落账";
  } else if (o.exec_ok_today === false) {
    kind = "bad"; text = "⚠ 执行档未落账";
  } else {
    kind = "wait"; text = der || "非执行日 / 尚未到窗口";
  }
  return { kind, text, der, held, traded, ops: o, kpi: k };
}
