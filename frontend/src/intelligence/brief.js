/* 智能简报：规则拼接的每日综合解读（非 LLM）。每个数字都带时点，结论连着来源口径。
   2026-09-28 审查(36补): lines 从纯文本改为 {text, source} —— 与 actions.js(每条 source)/
   answers.js(每条 sources) 对齐"每条输出带数据来源字段"的红线（此前本模块是三缺口之一）。 */
import { store } from '../store'
import { fmtPct, fmtNum, STATE_CN } from '../format'
import { sleeveStatus } from '../store'

export function composeBrief() {
  const lines = [];
  const o = store.overview.data;
  const w = store.weekly.data;
  const p = store.paper.data;
  const sg = store.signal.data;

  if (o) {
    const bt = o.backtest || {};
    lines.push({ text: `市场 ${STATE_CN[o.state] || o.state}（评分 ${o.state_score ?? "--"}），月频系统建议权益仓位 ${fmtPct(o.equity_weight, 0)}。` +
      `基线口径：纯选股年化 ${fmtPct(bt.ann_return)}（基准 ${fmtPct(bt.bench_ann)}），最大回撤 ${fmtPct(bt.max_drawdown)}（${(bt.start || "").slice(0, 4)}-${(bt.end || "").slice(0, 4)}，不含 2018 崩盘段）。`,
      source: "/api/overview（state/state_score/backtest metrics）" });
  }
  if (w) {
    const s = sleeveStatus();
    const k = w.kpi || {};
    const ops = w.ops || {};
    const isExec = ops.next_exec && ops.next_exec === ops.today;
    lines.push({ text: `周频袖套：${s.text}。持仓 ${k.n_positions ?? 0} 只，净值 ${k.nav != null ? "¥" + fmtNum(k.nav, 0) : "尚无成交"}` +
      `${k.total_return != null ? `（累计 ${fmtPct(k.total_return)}，基准年化 ${fmtPct(k.bench_ann)}）` : ""}。` +
      (isExec ? `今天是执行档（${ops.next_exec}）。` : `下个执行档 ${ops.next_exec || "--"}。`),
      source: "/api/weekly（kpi/ops）+ 账本 sleeveStatus" });
    const ex = (w.plan || {}).expectation;
    if (ex && ex.excess_pp != null) {
      lines.push({ text: `方案预期（${String(ex.basis || "").split(/[\s（(:：]/)[0] || "口径见方案"}, w=${ex.equity_weight ?? "?"}）：超额 +${ex.excess_pp}pp / MDD ${ex.mdd_pct}% —— 预期来自历史实测，不保证未来。`,
        source: `plan.expectation（${ex.source || "实测点口径"}）` });
    }
  }
  if (p && p.created_at) {
    const base = (p.initial_capital !== null && p.initial_capital !== undefined) ? p.initial_capital : (p.nav.length ? p.nav[0].nav : 0);
    const pnl = p.total - base;
    lines.push({ text: `月频模拟盘：总资产 ¥${fmtNum(p.total, 2)}，累计盈亏 ${pnl >= 0 ? "+" : ""}¥${fmtNum(pnl, 2)}（${fmtPct(base ? pnl / base : 0)}），` +
      `${(p.positions || []).length} 只持仓，${(p.orders || []).length} 笔挂单待执行（数据${p.is_live ? "盘中实时 " + p.quoted_at : "截至 " + (p.data_date || "--")}）。`,
      source: "/api/paper（paper_account/position/nav）" });
  }
  if (sg) {
    // 2026-09-24：信号停摆时（`dynamic.enabled` 曾被关闭 ⇒ 停在 20260904）**不能写成"今日"**，
    // 否则简报会把 20 天前的旧信号当成当天的。判据 = 信号日期 vs 数据日期。
    const stale = !!(sg.date && o && o.data_date && sg.date !== o.data_date);
    if (sg.rows && sg.rows.length) {
      lines.push({ text: `${stale ? "动态信号（已过期，停在 " + sg.date + "）" : "今日动态信号"} ${sg.rows.length} 条：` +
        sg.rows.slice(0, 4).map(r => `${r.name}（${r.ts_code}）`).join("、") +
        (sg.rows.length > 4 ? ` 等` : "") + ` —— 明细见动态信号卡。`,
        source: `/api/signal（signal 表 @ ${sg.date}）` });
    } else {
      lines.push({ text: stale
        ? `动态信号已过期（停在 ${sg.date}）—— 请检查 monitor 是否在跑。`
        : "今日无动态信号 —— 持仓体检通过，无需操作。",
        source: `/api/signal（signal 表 @ ${sg.date || "--"}）` });
    }
  }
  if (!lines.length) lines.push({ text: "数据加载中，稍候片刻……", source: null });
  return {
    lines,
    stamp: `生成于 ${new Date().toLocaleString("zh-CN", { hour12: false })} ｜ 规则拼接（非 LLM），逐条来源见行尾`,
  };
}
