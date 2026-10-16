/* 周频袖套的三段叙事 HTML（ops 运维行 / health 健康度行 / expectation 预期标注）。
   2026-10-06 自 TradingView.vue 外移为纯函数（入参显式，组件里只剩薄 computed 壳）。
   ⚠️ 输出全部经 v-html 直出：动态字符串必须过 esc（审查 SEC-01）；契约测试钉着
   `_shortSrc(ex.basis)` / `expectation_at_plan_time` / `snapSrc` / "取数失败" 等字符串，
   改写时不得丢。 */
import { esc, fmtPct } from './format'

export const HEALTH_ICN = { healthy: "🟢", warning: "🟡", degrading: "🟠", dead: "🔴" }

export function opsLine(wd) {
  const o = wd.ops || {};
  if (!o.next_exec) return "";
  const todayIsExec = o.next_exec === o.today;
  /* 今天不是执行日时不能显示"已落账 ✓" —— 非执行日直接显示判据原文（旧版教训） */
  const todayTxt = todayIsExec
    ? (o.exec_ok_today ? "已落账 ✓" : "⚠ 未落账(看门狗会告警)")
    : esc(o.exec_detail || "非执行日");
  return `🗓 下个执行档 <b>${esc(o.next_exec)}</b>（锚点 ${esc(o.next_anchor)}）` +
    `${todayIsExec ? "　＝今天" : ""}　｜ 今日执行档: ${todayTxt}` +
    (wd.ops_error ? `　｜ 状态取数失败: ${esc(wd.ops_error)}` : "");
}

export function healthLine(wd, healthState) {
  const k = wd.kpi || {};
  if (k.halt && k.halt.active) {
    return `<span style="color:var(--warn)">⛔ 风控停买中(自 ${esc(k.halt.since)})：${esc(k.halt.reason)} —— 只卖不买，人工 resume 才解除</span>`;
  }
  const h = healthState.data;
  if (healthState.error) return `⚪ 健康度未知｜${esc(healthState.error)}`;
  if (!h) return "健康度加载中…";
  const icon = HEALTH_ICN[h.level] || "⚪";
  const rs = (h.reasons || []).length ? " ｜ ⚠ " + esc(h.reasons.join("；")) : "";
  return `${icon} 健康度 ${esc(h.level)}｜滚动 IC ${esc(h.rolling_ic ?? "--")}` +
    `(冻结基线 ${esc(h.frozen_ic ?? "--")})｜连续为负 ${esc(h.neg_streak ?? 0)} 锚点｜换手 ${esc(h.turnover_est ?? "--")}` +
    `｜投入率 ${fmtPct(h.deployed_ratio)}${rs}` + (h.note ? `｜${esc(h.note)}` : "");
}

export function expectationLines(wd) {
  const p = wd.plan;
  if (!p) return "";
  const ex = p.expectation || {};
  const out = [];
  if (ex.excess_pp != null) {
    /* 预期必须连着敞口与来源一起显示；快照（方案生成时口径）不同时必须写出来 */
    const _shortSrc = (v) => String(v || "").split(/[\s（(:：]/)[0] || "";
    const exSrc = _shortSrc(ex.basis) || _shortSrc(p.expectation_source) || "同窗口 491 期";
    const snapSrc = _shortSrc((p.expectation_at_plan_time || {}).basis);
    let line = `诚实预期(${esc(exSrc)}, w=${ex.equity_weight != null ? ex.equity_weight : "?"}) ` +
      `超额 +${ex.excess_pp}pp / MDD ${ex.mdd_pct}%`;
    if (snapSrc && snapSrc !== exSrc) line += ` ｜ 方案生成时按 ${esc(snapSrc)}`;
    out.push(line);
    const mul = ex.multiplicity || p.expectation_multiplicity;
    if (mul) out.push(`⚠ 多重比较后不显著(DSR N=7: ${mul.dsr_n7[0]}/${mul.dsr_n7[1]}，N=25: ${mul.dsr_n25[0]}/${mul.dsr_n25[1]}，门槛 0.95)`);
    const oos = ex.oos_24m || p.expectation_oos;
    if (oos) {
      const atW = oos.measured_at_weight != null && ex.equity_weight != null &&
        oos.measured_at_weight !== ex.equity_weight ? `；且只在 w=${oos.measured_at_weight} 测过` : "";
      out.push(`⚠ 样本外 ${esc(oos.window)} 仅 +${oos.excess_pp[0]}~${oos.excess_pp[1]}pp` +
        `(t≈${oos.t_hac[0]}~${oos.t_hac[1]}, 校正后 p=${oos.p_corrected} ⇒ 与 0 不可区分${atW})`);
    }
    const mon = ex.monthly || p.expectation_monthly;
    if (mon && mon.full && mon.oos) {
      out.push(`📅 月度(${esc(mon.measured_by)} 测于 w=${mon.measured_at_weight}) 全史月均 ` +
        `${mon.full.mean_pp[0]}~${mon.full.mean_pp[1]}pp(命中 ${Math.round(100 * mon.full.hit[0])}%)` +
        ` / 留出集月均 ${mon.oos.mean_pp[0]}~${mon.oos.mean_pp[1]}pp(命中 ${Math.round(100 * mon.oos.hit[0])}%)`);
    }
  }
  return out.map(l => `<div style="padding:1px 0">${l}</div>`).join("");
}
