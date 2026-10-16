/* 三盘面配置（2026-10-08 批5 配置驱动统一）。
 *
 * 背景：FinalView / W2View / NewEcoHomeView 三个盘面页的**结构完全相同**
 * （骨架 → KPI → 净值 → 账户卡 → 模型信息 → 按账户循环的功能区块 → 选股榜 → 口径注记），
 * 差异只在文案、列定义、区块清单、KPI 口径。此前三份文件各写一遍，
 * 改一处（如加个区块）要改三处且容易漏。
 *
 * 现在：差异全部声明在本文件，`components/ModelPage.vue` 按配置渲染；
 * 三个视图退化为薄壳（各 1 个 <ModelPage :model="..." />）。
 *
 * ⚠️ MODEL_ORDER 仍留在 acctCols.js —— 契约测试
 * tests/test_web_static_contract.py::test_all_accounts_model_order_synced_with_backend
 * 从**那个文件**读它，不要搬到这里。
 */
import { money, thousand, posCols, tradeCols, decisionCols, alertCols,
         periodCols, runCols, pickBaseCols } from './acctCols'

/* ---------------------------------------------------------------- 盘面差异声明 */

export const MODEL_CONFIG = {
  "NEW ECO": {
    key: "NEW ECO",
    title: "NEW ECO",
    pageTitle: "NEW ECO · 盘面",
    subtitle: "月频老盘 + 周频袖套 · 每日 18:00 日链推分 / 月度全流程重训",
    domId: "neweco-kpis",
    /* 本盘面页自己的路由名（ModelPage 用它把"盘面页"从"子页入口"里排除掉） */
    pageRoute: "neweco",
    chartCaption: "各账户以本金归一 · 虚线为基准 · 点图例可隐藏",
    /* 账户卡：月频盘/周频袖套无盘面特有行、无卡尾区块 */
    accountCard: { extraRows: () => [], tailRows: () => [], body: null },
    afterAccounts: "subPages",
    meta: {
      subtitle: "月频模型工件元数据；周频袖套为规则臂",
      chipsLabel: "特征",
      rows: (meta) => [
        { k: "训练时间", v: meta.trained_at || "—", width: 640 },
        { k: "训练档", v: scheduleLineOf(meta), width: 640 },
      ],
      footer: (meta) => meta.factor_source || "",
    },
    picks: {
      title: "选股榜 Top100",
      subtitle: (meta) => `daily_score 最新交易日 · 全市场推分 · 数据日 ${meta.asof || "—"}`,
      exportName: "NEWECO选股榜Top100",
      emptyHint: "等待 18:00 日链推分",
      columns: () => [...pickBaseCols,
        { key: "score", label: "打分", num: true,
          render: (r) => (r.score == null ? "—" : Number(r.score).toFixed(2)) }],
      note: "榜单口径：daily_score 最新交易日的截面打分降序取前 100（score 为截面分位量纲 0-100）；",
      afterTable: null,
    },
    /* NEW ECO 的盘面级区块里没有按账户循环的功能区块（明细在 8 个子页） */
    blocks: [],
    accountHeader: (a) => `数据日 ${a.asof || "—"}`,
    kpi: ({ okAccounts, picks, helpers }) => {
      const eq = okAccounts.reduce((s, a) => s + (a.equity || 0), 0);
      const nPos = okAccounts.reduce((s, a) => s + ((a.positions || []).length), 0);
      const nOrders = okAccounts.reduce((s, a) => s + ((a.extra && a.extra.orders) || []).length, 0);
      const asof = (okAccounts[0] || {}).asof || "—";
      return [
        { label: "双账户合计权益", value: "¥" + helpers.money(eq),
          sub: `${okAccounts.length}/2 账户 · 数据日 ${asof}` },
        { label: "持仓合计", value: `${nPos} 只`, sub: "月频盘 + 周频袖套" },
        { label: "待撮合挂单", value: `${nOrders} 笔`, sub: nOrders ? "见各子页" : "当前无挂单" },
        { label: "选股榜", value: `${picks.length} 只`,
          sub: picks.asof ? `数据日 ${picks.asof}` : "等待推分",
          cls: picks.length ? "" : "neg" },
      ];
    },
    footerNote: `口径：月频盘为 09:31 即时成交（回测全收益尺），周频袖套为 19:45 健康档（实盘尺）——
      <b>两把尺口径不同，不可直接比大小</b>，故净值图分轨归一、基准线只对月频盘生效。
      明细见各子页；账户明细表为本仓账本的只读聚合。`,
  },

  "Final": {
    key: "Final",
    title: "Final",
    pageTitle: "Final · 双轨账户",
    subtitle: "dlv2 双轨 · t+1 开盘价撮合 · 每日 20:05 档落账",
    pageRoute: "final",
    domId: "final-kpis",
    chartCaption: "各轨以本金归一 · 点图例可隐藏某轨",
    accountCard: {
      extraRows: (a) => [{ k: "持仓", v: `${(a.positions || []).length} 只 · ${a.days} 个交易日` }],
      tailRows: (a) => {
        const hl = a.extra && a.extra.halt_line != null ? a.extra.halt_line : null;
        return hl == null ? [] : [{ k: "熔断线", v: "¥" + money(hl) }];
      },
      body: null,
    },
    afterAccounts: "limits",
    meta: {
      subtitle: "工件元数据 sidecar 离线提取，换版后需重跑提取脚本",
      chipsLabel: "特征",
      rows: (meta) => [
        { k: "训练工件", v: `dlv2_production.pt（${meta.n_nets} 网 rank-loss 换版 2026-10-08）`, width: 560 },
        { k: "训练时间", v: meta.trained_at || "—", width: 560 },
        { k: "全史样本", v: `${meta.n_samples == null ? "—" : thousand(meta.n_samples)} 行`, width: 560 },
        { k: "训练档", v: scheduleLineOf(meta), width: 560 },
      ],
      footer: () => "",
    },
    picks: {
      title: "选股榜 Top100",
      subtitle: (meta) => `dlv2 EMA 平滑秩 · 全截面 · 数据日 ${meta.asof || "—"}`,
      exportName: "Final选股榜Top100",
      emptyHint: "等待 20:05 档重建 EMA 状态（换版 2026-10-08 已重置）",
      columns: () => [...pickBaseCols,
        { key: "ema", label: "EMA 平滑秩", num: true,
          render: (r) => (r.ema == null ? "—" : Number(r.ema).toFixed(4)) }],
      note: "榜单口径：EMA 平滑秩是当日全截面预测的日频平滑（α=0.5），与双轨调仓同源；",
      afterTable: null,
    },
    accountHeader: (a) => `数据日 ${a.asof || "—"} · 心跳 ${(a.extra && a.extra.heartbeat) || "缺失"}`,
    blocks: [
      { kind: "table", title: (a) => `持仓明细（${(a.positions || []).length} 只 · 现价取本仓 daily 最新收盘）`,
        rows: (a) => a.positions, columns: posCols, export: "Final持仓",
        empty: "当前空仓（周五信号 → 次周一开盘撮合）" },
      { kind: "table", title: () => "挂单 / 待撮合",
        rows: (a) => a.extra && a.extra.orders, columns: tradeCols, export: "Final挂单",
        empty: "无挂单（信号日 20:05 档生成，次日开盘价撮合）" },
      { kind: "table", title: () => "成交记录（最近 30 笔）",
        rows: (a) => a.trades, columns: tradeCols, export: "Final成交", empty: "暂无成交" },
      { kind: "table", title: () => "决策审计（最近 15 条）",
        rows: (a) => a.extra && a.extra.decisions, columns: decisionCols, export: "Final决策审计",
        empty: "无决策记录" },
      { kind: "table", title: () => "本地告警（最近 20 条）",
        rows: (a) => a.extra && a.extra.alerts, columns: alertCols, export: "Final告警",
        empty: "无告警" },
    ],
    kpi: ({ okAccounts, helpers }) => {
      const eq = okAccounts.reduce((s, a) => s + (a.equity || 0), 0);
      const nOrders = okAccounts.reduce((s, a) => s + ((a.extra && a.extra.orders) || []).length, 0);
      const hb = (okAccounts.find((a) => a.id === "fterm-w2") || {}).extra?.heartbeat;
      const w2Acct = okAccounts.find((a) => a.id === "fterm-w2") || {};
      return [
        { label: "双轨合计权益", value: "¥" + helpers.money(eq),
          sub: `${okAccounts.length}/2 轨 · 数据日 ${w2Acct.asof || "—"}` },
        { label: "待撮合挂单", value: `${nOrders} 笔`,
          sub: nOrders ? "次一交易日开盘价撮合" : "当前无挂单" },
        { label: "运行心跳", value: hb ? String(hb).slice(0, 16) : "缺失", cls: hb ? "" : "neg",
          sub: hb ? "每日 20:05 档写入（w2 轨覆盖双轨）" : "20:05 档可能未跑" },
        { label: "熔断线（w2）",
          value: w2Acct.extra && w2Acct.extra.halt_line != null ? "¥" + helpers.money(w2Acct.extra.halt_line) : "—",
          sub: w2Acct.extra && w2Acct.extra.limits
            ? `total_dd_halt ${(w2Acct.extra.limits.total_dd_halt * 100).toFixed(0)}%` : "" },
      ];
    },
    footerNote: `口径：双轨信号共享（DL-v2 14 网集成 [5+5+2+2] + C_ema，训练损失 MSE+RankNet λ=1，
      2026-10-08 按判据链 #14 换版），仓位/风控/账本按轨隔离；
      周五调仓、EMA 日更；成交邮件当晚 21:15 逐轨送达。明细表为本仓对
      D:\\Final state 库的只读聚合。`,
  },

  "W2": {
    key: "W2",
    title: "W2",
    pageTitle: "W2 · 三账本",
    subtitle: "2 万周频 · B0 双臂锚定周调仓 · 每日 20:30 档落账",
    pageRoute: "w2",
    domId: "w2-kpis",
    chartCaption: "各账本以本金归一 · 点图例可隐藏",
    accountCard: { extraRows: () => [], tailRows: () => [], body: "circuit" },
    afterAccounts: null,
    meta: {
      subtitle: "生产纸面轨 = 规则臂 B0；LightGBM 属 M1 实验线",
      chipsLabel: "M1 实验线因子",
      rows: (meta) => [
        { k: "选股规则", v: "B0：子池（close≤20 元 ∧ amt20 门槛）内按流通市值升序 + 退出带", width: 640 },
        { k: "训练档", v: scheduleLineOf(meta), width: 640 },
      ],
      footer: (meta) => meta.factor_source || "",
    },
    picks: {
      title: "B0 选股榜 Top100",
      subtitle: (meta, ctx) => `锚点 ${meta.anchor || "—"} · 全序 ${ctx.fullOrderN} 只取前 100`,
      exportName: "W2选股榜Top100",
      emptyHint: "等待 ECOW2_picks_top100_daily 生成 sidecar",
      columns: () => [...pickBaseCols,
        { key: "close", label: "锚点收盘", num: true,
          render: (r) => (r.close == null ? "—" : Number(r.close).toFixed(2)) },
        { key: "total_mv", label: "总市值", num: true,
          render: (r) => (r.total_mv == null ? "—" : thousand(Math.round(r.total_mv))) }],
      note: "榜单口径：与 k 无关的 B0 全序（生产选股 = 该序头部 + 退出带）；",
      afterTable: (meta) => `自校验：${meta.verified
        ? "✓ head(6)/head(8) 与账本当期 picks 逐位一致"
        : "✗ sidecar 未通过账本自校验——读数不可信，等下次 20:45 档重建"}`,
    },
    accountHeader: (a) => `数据日 ${a.asof || "—"}`,
    blocks: [
      { kind: "table", title: (a) => `持仓明细（${(a.positions || []).length} 只）`,
        rows: (a) => a.positions, columns: posCols, export: "W2持仓", empty: "当前空仓" },
      { kind: "chips", title: () => "当期选股名单",
        sub: (a) => (latestPicks(a).length ? `锚点 ${latestAnchor(a)}` : ""),
        items: (a) => latestPicks(a), empty: "账本本期无 picks 记录" },
      { kind: "table", title: () => "成交记录（最近 30 笔）",
        rows: (a) => a.trades, columns: tradeCols, export: "W2成交",
        empty: "暂无成交（CB 账本无逐笔历史，属口径设计）" },
      { kind: "table", title: () => "逐期记录（最近 8 期）",
        rows: (a) => a.extra && a.extra.periods, columns: periodCols, export: "W2逐期",
        empty: "暂无逐期记录" },
      { kind: "table", title: () => "最近运行（最近 5 次）",
        rows: (a) => a.extra && a.extra.runs, columns: runCols, export: "W2运行",
        empty: "无运行记录" },
      { kind: "chips", label: "排期：", title: () => null,
        items: (a) => ((a.extra && a.extra.schedule) || []).slice(0, 6).map((s) => `${s.iso} → ${s.anchor}`),
        empty: null },
    ],
    kpi: ({ okAccounts, helpers }) => {
      const eq = okAccounts.reduce((s, a) => s + (a.equity || 0), 0);
      const cn = { halted: "halted（停交易层）", derisked: "derisked（降险层）",
                   cautious: "cautious（谨慎层）" };
      const asof = (okAccounts[0] || {}).asof || "—";
      const worst = worstCircuitOf(okAccounts);
      return [
        { label: "三账本合计权益", value: "¥" + helpers.money(eq),
          sub: `${okAccounts.length}/3 账本 · 数据日 ${asof}` },
        { label: "熔断最差态",
          value: worst ? (cn[worst] || worst) : "正常",
          cls: worst === "halted" ? "neg" : (worst ? "" : "pos"),
          sub: "A/B/C 三层，账户卡内可看各账本明细" },
        { label: "调仓节奏", value: "锚定周调仓",
          sub: "每周最后一个开市日锚定 → 其后首个开市日执行（20:30/20:35 档）" },
        { label: "生产臂", value: "B0 规则臂", sub: "M1 模型臂判否维持 B0（预注册判决）" },
      ];
    },
    footerNote: `口径：B0 双臂 = 市值升序 + 退出带（锚定周调仓，每周最后一个开市日锚定、其后首个开市日
      t1 执行）。
      熔断三层 A(halted)/B(derisked)/C(cautious) 的含义与处置见全账户日报的告警解释。`,
  },
};

/* ---------------------------------------------------------------- 共用小工具 */

/** 训练档位串（meta.schedule 数组 → 分号连接） */
export function scheduleLineOf(meta) {
  const s = meta && meta.schedule;
  return Array.isArray(s) ? s.join("；") : "";
}

/** W2 熔断最差态：halted > derisked > cautious > 正常（与账户告警级别同序） */
export function worstCircuitOf(accounts) {
  let worst = "";
  for (const a of accounts) {
    const c = (a.extra || {}).circuit || {};
    if (c.halted) return "halted";
    if (c.derisked) worst = worst || "derisked";
    if (c.cautious) worst = worst || "cautious";
  }
  return worst;
}

/** W2：账本最近一期的 pick_list / anchor（periods[0] 为最新） */
function latestPicks(a) {
  const ps = (a.extra && a.extra.periods) || [];
  return (ps[0] && ps[0].pick_list) || [];
}
function latestAnchor(a) {
  const ps = (a.extra && a.extra.periods) || [];
  return (ps[0] && ps[0].anchor) || "—";
}

/** 供测试/调试：配置里声明的盘面键清单 */
export const CONFIGURED_MODELS = Object.keys(MODEL_CONFIG);
