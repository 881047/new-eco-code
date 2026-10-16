/* 本地问答引擎：规则意图匹配 + 现有接口取数，答案附来源。不接外部 LLM。
   与 AskPanel 配合：LLM 未配置时全部走这里；配置后可一键转问大模型。 */
import { store, ensure, sleeveStatus } from '../store'
import { fmtPct, fmtNum, fmtMoney, STATE_CN, ACTION_CN } from '../format'
import { computeActions } from './actions'

const PRESETS = [
  "今天该做什么？",
  "全账户现在怎么样？",
  "数据新鲜吗？",
  "600519 现在怎么样？",
  "为什么卖它？",
  "风控/健康度如何？",
  "最近赚了还是亏了？",
]

function findStock(q) {
  /* 支持 6 位代码片段或名称子串 */
  const m = q.match(/\d{6}/);
  const code = m ? m[0] : null;
  const nameHit = (r) => code ? String(r.ts_code || "").startsWith(code) : (r.name && q.includes(r.name));
  return { code, nameHit };
}

async function answerStock(q) {
  await ensure("scorePool", "paper", "weekly", "plan", "lhb", "signal", "accounts");
  const { code, nameHit } = findStock(q);
  if (!code && !q.replace(/[?？。！\s]/g, "")) return null;
  const parts = [];
  const sources = [];
  const sc = (store.scorePool.data || {}).rows || [];
  const hit = sc.find(nameHit);
  if (hit) {
    parts.push(`打分榜：${hit.name}（${hit.ts_code}）第 ${sc.indexOf(hit) + 1} 名，打分 ${hit.score}，预测超额 ${fmtPct(hit.pred_ret)}，最新价 ${hit.close ?? "--"}（${(store.scores.data || {}).date || "--"}）。`);
    sources.push("/api/scores");
  }
  const pp = ((store.paper.data || {}).positions || []).find(nameHit);
  if (pp) {
    parts.push(`月频模拟盘持仓：${pp.shares} 股，成本 ${pp.avg_cost}，浮盈亏 ${fmtNum(pp.pnl, 2)}（${pp.pnl_pct}%）。`);
    sources.push("/api/paper");
  }
  const wp = ((store.weekly.data || {}).positions || []).find(nameHit);
  if (wp) {
    parts.push(`周频袖套持仓：${fmtNum(wp.shares, 0)} 股，成本 ${wp.cost != null ? Number(wp.cost).toFixed(3) : "--"}，最新价 ${wp.last_px != null ? Number(wp.last_px).toFixed(3) : "--"}。`);
    sources.push("/api/weekly");
  }
  const planRows = (((store.plan.data || {}).rows) || []).filter(r => r.plan_type === "stock" && nameHit(r));
  if (planRows.length) {
    parts.push(`当期方案在列：建议 ${planRows[0].shares} 股，权重 ${fmtPct(planRows[0].weight)}，理由：${planRows[0].reason}。`);
    sources.push("/api/plan");
  }
  const lhbRows = ((store.lhb.data || {}).rows || []).find(nameHit);
  if (lhbRows) {
    /* limit_reason 可为 null（无上榜原因）—— 不能把字面 null 甩给用户 */
    const reason = lhbRows.limit_reason ? `，原因：${lhbRows.limit_reason}` : "";
    parts.push(`龙虎榜：净买入 ${fmtMoney(lhbRows.net_value)}，人气 ${lhbRows.hot_rank ?? "--"}${reason}。`);
    sources.push("/api/lhb");
  }
  const sgAll = ((store.signal.data || {}).history || []).filter(nameHit).slice(0, 3);
  if (sgAll.length) {
    parts.push(`近期信号：` + sgAll.map(r => `${r.signal_date} ${ACTION_CN[r.action] || r.action}（${r.reason}）`).join("；"));
    sources.push("/api/signal");
  }
  /* 跨账户持仓（2026-10-06 问答补缺口）：Final 双轨 + W2 三账本的持仓命中 */
  const accs = (store.accounts.data || {}).accounts || [];
  for (const a of accs) {
    const hit2 = (a.positions || []).find((r) => code
      ? String(r.code || "").startsWith(code)
      : (r.name && q.includes(r.name)));
    if (hit2) {
      parts.push(`${a.name}（${a.model}）持仓：${hit2.shares} 股，成本 ${hit2.cost != null ? Number(hit2.cost).toFixed(3) : "--"}，现价 ${hit2.last != null ? Number(hit2.last).toFixed(3) : "--"}，市值 ¥${fmtNum(hit2.value, 0)}。`);
      sources.push("/api/all_accounts");
    }
  }
    if (!parts.length) {
    return { text: `在打分榜前 500、七个账户持仓、当期方案、龙虎榜与近期信号里都没查到${code ? "代码 " + code : "「" + q + "」"}。它可能不在候选域内（未上市满 60 日/停牌/ST/板块/市值/流动性六层过滤之外），或今天没进榜。`, sources: [] };
    }
  return { text: parts.join("\n"), sources };
}

/* 全账户问答（2026-10-06 问答补缺口）：三模型 7 纸面账户总览 + 逐户要点 + 异常清单。
   数据 = /api/all_accounts（与看板全账户总览同源，只读）；异常判定与
   accounts_mail.assess 的前四类对齐（读取失败/数据滞后/熔断线/心跳），纯前端推导。 */
async function answerAccounts(q) {
  await ensure("accounts", "paper");
  const all = (store.accounts.data || {}).accounts || [];
  if (!all.length) return { text: "全账户聚合数据加载中或不可用（稍后再问，或看板「全账户总览」页）。", sources: ["/api/all_accounts"] };
  /* 2026-10-08 盘面改名：Final/W2 为新键，同时保留 fterm/eco-w2 旧写法以便旧问法仍命中。
     必须加词界 \b：否则 "w2" 会误命中 "ecow2" 等含 w2 的文本，且 Final 账户名里也带 Final。 */
  const wantModel = (() => {
    if (/\b(final|fterm)\b/i.test(q)) return "Final";
    if (/\b(w2|eco-?w2)\b/i.test(q)) return "W2";
    return null;
  })();
  const list = wantModel ? all.filter(a => a.model === wantModel) : all;
  const gen = (store.accounts.data || {}).generated_at || "--";
  const lines = [`全账户（3 模型 ${all.length} 纸面账户 · 聚合于 ${gen}${wantModel ? `，已过滤模型 ${wantModel}` : ""}）：`];
  const anomalies = [];
  for (const a of list) {
    if (!a.ok) {
      lines.push(`✗ ${a.name}：读取失败（${a.error || "未知错误"}）`);
      anomalies.push(`${a.name} 读取失败`);
      continue;
    }
    let line = `· ${a.name}：¥${fmtNum(a.equity, 0)}（自建仓 ${a.pct == null ? "--" : (a.pct > 0 ? "+" : "") + a.pct.toFixed(2) + "%"}，回撤 ${a.dd_pct == null ? "--" : a.dd_pct.toFixed(2) + "%"}）`;
    const ex = a.extra || {};
    const tags = [];
    if (a.id === "neweco-monthly" && store.paper.data && store.paper.data.paused) tags.push("⛔ 交易总闸暂停");
    if (String(a.id || "").startsWith("fterm-")) {
      const n = (ex.orders || []).length;
      tags.push(`挂单 ${n} 笔`);
      if (a.id === "fterm-w2") tags.push(ex.heartbeat ? "心跳正常" : "⚠ 心跳缺失（20:05 档可能未跑）");
      if (ex.halt_line != null && a.equity != null) {
        const dist = (a.equity / ex.halt_line - 1) * 100;
        tags.push(`距熔断线 ${dist >= 0 ? "+" : ""}${dist.toFixed(1)}%`);
        if (dist <= 0) anomalies.push(`${a.name} 触及熔断线`);
      }
    }
    if (String(a.id || "").startsWith("ecow2-")) {
      const c = ex.circuit || {};
      const w = c.halted ? "halted（停交易层）" : c.derisked ? "derisked（降险层）" : c.cautious ? "cautious（谨慎层）" : "正常";
      tags.push(`熔断：${w}`);
      if (c.halted) anomalies.push(`${a.name} 熔断 halted`);
      else if (c.derisked) anomalies.push(`${a.name} derisked`);
    }
    if (a.stale) { tags.push("⚠ 数据滞后"); anomalies.push(`${a.name} 数据滞后`); }
    lines.push(line + (tags.length ? " ｜ " + tags.join(" ｜ ") : ""));
  }
  const tail = anomalies.length
    ? `\n⚠ 异常 ${anomalies.length} 项：${anomalies.join("；")}。含义与处置见每晚 21:15 全账户日报的告警解释（同因同日只发一次）。`
    : "\n全部账户无异常。每天 21:15 有全账户日报邮件；异常出现时会按账户单独发信。";
  return {
    text: lines.join("\n") + tail + (wantModel ? "" : "\n问单个模型可带关键词：Final / W2（旧写法 fterm / ECO-W2 仍可命中）。"),
    sources: ["/api/all_accounts"],
  };
}

export async function answer(question) {
  const q = (question || "").trim();
  if (!q) return null;

  /* 1) 今天该做什么 */
  if (/该做什么|做什么|怎么办|行动|待办|今日/.test(q) && !/\d{6}/.test(q)) {
    const acts = computeActions();
    if (!acts.length) return { text: "各项检查都通过，今天没有待办。", sources: ["行动中枢规则"] };
    return {
      text: "按优先级：\n" + acts.map(a => `[${a.level}] ${a.text}${a.sub ? " —— " + a.sub : ""}`).join("\n"),
      sources: ["行动中枢规则", ...new Set(acts.map(a => a.source))],
    };
  }
  /* 1.5) 全账户/其他账户（2026-10-06 问答补缺口：看板已是全账户枢纽，
     Final 双轨与 W2 三账本此前完全不在问答覆盖里）。放得靠前——
     这些关键词不应被 盈亏/股票查询 意图截胡。 */
  if (/全账户|六账户|6\s*个账户|其他账户|final|fterm|w2|eco-?w2|双轨|三账本|熔断|心跳/i.test(q)) {
    return await answerAccounts(q);
  }
  /* 2) 数据新鲜度 */
  if (/数据|新鲜|更新|落后/.test(q)) {
    await ensure("status");
    const rows = (store.status.data || {}).rows || [];
    const key = ["daily", "lhb_ths", "factor", "daily_factor"].map(n => rows.find(r => r.name === n)).filter(Boolean);
    if (!key.length) return { text: "数据状态表为空。", sources: ["/api/status"] };
    return {
      text: key.map(r => `${r.name}：最新 ${r.last_date ?? "--"}，更新于 ${r.updated_at ?? "--"}（本次增量 ${r.rows ?? 0} ${r.unit === "day" ? "天" : "行"}）`).join("\n"),
      sources: ["/api/status"],
    };
  }
  /* 3) 为什么卖 */
  if (/为什么|为啥/.test(q)) {
    await ensure("signal");
    const { nameHit } = findStock(q);
    const hist = ((store.signal.data || {}).history || []);
    const hit = (/为什么卖|为啥卖/.test(q) && !/\d{6}/.test(q) && !q.includes("它"))
      ? hist.filter(r => r.action === "sell" || r.action === "sell_weak").slice(0, 5)
      : hist.filter(nameHit).slice(0, 5);
    if (hit.length) {
      return { text: hit.map(r => `${r.signal_date} ${r.name}（${r.ts_code}）${ACTION_CN[r.action] || r.action}：${r.reason}`).join("\n"), sources: ["/api/signal"] };
    }
    return { text: "近期信号历史里没找到对应的卖出记录；若指当前持仓的止损逻辑，规则是：动态信号模块按止损线/弱势天数/回撤阈值判定（config.yaml 的 dynamic 段），触发后次日开盘执行。", sources: ["/api/signal"] };
  }
  /* 4) 风控/健康 */
  if (/风控|健康|停买|halt/.test(q)) {
    await ensure("weeklyHealth", "weekly");
    const h = store.weeklyHealth.data;
    const w = store.weekly.data;
    const halt = w && w.kpi && w.kpi.halt;
    const lines = [];
    if (h) lines.push(`健康度 ${h.level}：滚动 IC ${h.rolling_ic ?? "--"}（冻结基线 ${h.frozen_ic ?? "--"}），连续为负 ${h.neg_streak ?? 0} 锚点，换手 ${h.turnover_est ?? "--"}，投入率 ${fmtPct(h.deployed_ratio)}${(h.reasons || []).length ? "；⚠ " + h.reasons.join("；") : ""}`);
    if (halt && halt.active) lines.push(`⛔ 风控停买中（自 ${halt.since}）：${halt.reason} —— 只卖不买，人工 resume 才解除`);
    if (!lines.length) lines.push("健康度数据加载中或不可用。");
    return { text: lines.join("\n"), sources: ["/api/weekly/health", "/api/weekly"] };
  }
  /* 5) 盈亏 */
  if (/赚|亏|盈亏|收益|净值|表现/.test(q)) {
    await ensure("paper", "weekly", "accounts");
    const p = store.paper.data;
    const w = store.weekly.data;
    const lines = [];
    if (p && p.created_at) {
      const base = (p.initial_capital !== null && p.initial_capital !== undefined) ? p.initial_capital : (p.nav.length ? p.nav[0].nav : 0);
      const pnl = p.total - base;
      lines.push(`月频模拟盘：总资产 ¥${fmtNum(p.total, 2)}，累计 ${pnl >= 0 ? "+" : ""}¥${fmtNum(pnl, 2)}（${fmtPct(base ? pnl / base : 0)}）`);
    }
    if (w && w.kpi) lines.push(`周频袖套：净值 ${w.kpi.nav != null ? "¥" + fmtNum(w.kpi.nav, 0) : "尚无成交"}，累计 ${fmtPct(w.kpi.total_return)}（基准年化 ${fmtPct(w.kpi.bench_ann)}，超额 ${w.kpi.excess_pp != null ? w.kpi.excess_pp + "pp" : "--"}）`);
    /* 其余 5 账户（2026-10-06 问答补缺口）：口径=自建仓累计，非当日盈亏——诚实标注 */
    const others = ((store.accounts.data || {}).accounts || []).filter(a => a.model !== "NEW ECO");
    if (others.length) {
      lines.push("其余账户（口径=自建仓累计，非当日）："
        + others.map(a => `${a.name} ¥${fmtNum(a.equity, 0)}（${a.pct == null ? "--" : (a.pct > 0 ? "+" : "") + a.pct.toFixed(2) + "%"}）`).join(" ｜ "));
    }
    if (!lines.length) lines.push("账户都还没有数据（未初始化）。");
    return { text: lines.join("\n"), sources: ["/api/paper", "/api/weekly", "/api/all_accounts"] };
  }
  /* 6) 股票查询（代码优先） */
  if (/\d{6}/.test(q) || /怎么样|什么情况|查/.test(q)) {
    const r = await answerStock(q);
    if (r) return r;
  }
  /* 兜底 */
  return {
    text: "我能回答这些（全部本地规则，不外发数据）：\n· 「今天该做什么」— 行动中枢摘要\n· 「全账户现在怎么样」— 三模型 7 账户总览（Final/W2 含挂单/心跳/熔断）\n· 「数据新鲜吗」— 各表更新状态\n· 「600519 现在怎么样」— 打分/七账户持仓/方案/龙虎榜/信号命中\n· 「为什么卖出」— 信号历史与原因\n· 「风控/健康度如何」— 周频健康与停买状态\n· 「最近赚了还是亏了」— 全部账户盈亏\n配置 LLM 后可问我更开放的问题。",
    sources: [],
  };
}

export { PRESETS };
