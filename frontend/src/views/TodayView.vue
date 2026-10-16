<template>
  <div>
    <div class="page-title">今日行动</div>
    <ActionCenter />
    <SleeveStatusBar />
    <section class="card" style="margin-top:24px">
      <h2>月频系统 KPI <span class="muted">{{ headerInfo }}</span></h2>
      <div class="muted" style="font-size:var(--fs-2);margin:2px 0 6px">择时与执行</div>
      <KpiGrid :items="kpisTiming" dom-id="kpis-timing" />
      <div class="muted" style="font-size:var(--fs-2);margin:10px 0 6px">回测基线（2019-07 起，不含 2018 崩盘段）</div>
      <KpiGrid :items="kpisBacktest" dom-id="kpis-backtest" />
      <div class="muted" style="margin:6px 0 0;font-size:var(--fs-2)">
        ⚠ 历史样本已按时点名称纳入已退市股（PIT：2017 年评分样本含 157 只后来退市股）。
        仍未反映的尾部：「买入后连续跌停直至摘牌」——摘牌后无日线，结转仓位按最后收盘价挂账、无法归零。
      </div>
    </section>
    <div class="grid2" style="margin-top:24px">
      <BriefCard />
      <section class="card" id="signal" style="margin-top:0">
        <h2>{{ signalTitle }} <span class="muted">{{ signalDate }}</span></h2>
        <AsyncSection :state="sg">
          <div v-if="!sigRows.length" class="muted">今日无信号 — 持仓体检通过，无需操作 ✓</div>
          <DataTable v-else :columns="sigCols" :rows="sigRows" :filterable="false" export-name="今日信号" />
          <template v-if="histRows.length">
            <h2 style="margin-top:16px">最近信号</h2>
            <DataTable :columns="histCols" :rows="histRows" :filterable="false" export-name="信号历史" />
          </template>
        </AsyncSection>
      </section>
    </div>
  </div>
</template>

<script>
import { store, ensure } from '../store'
import { esc, fmtPct, fmtNum, STATE_CN, ACTION_CN, ACTION_CLS, stockLink } from '../format'
import ActionCenter from '../components/ActionCenter.vue'
import SleeveStatusBar from '../components/SleeveStatusBar.vue'
import BriefCard from '../components/BriefCard.vue'
import KpiGrid from '../components/KpiGrid.vue'
import DataTable from '../components/DataTable.vue'
import AsyncSection from '../components/AsyncSection.vue'

export default {
  name: "TodayView",
  components: { ActionCenter, SleeveStatusBar, BriefCard, KpiGrid, DataTable, AsyncSection },
  computed: {
    o() { return store.overview },
    sg() { return store.signal },
    sigRows() { return ((store.signal.data || {}).rows || []) },
    histRows() {
      const hist = ((store.signal.data || {}).history || []);
      const recent = [...new Set(hist.map(r => r.signal_date))].slice(0, 5);
      return hist.filter(r => recent.includes(r.signal_date)).slice(0, 30);
    },
    headerInfo() {
      const o = this.o.data;
      if (!o) return "";
      return `｜ 数据截至 ${o.data_date || "--"} ｜ 方案日期 ${o.plan_date || "--"} ｜ 状态分 ${o.state_score ?? "--"}` +
        (o.backtest_pin_missed ? " ｜ ⚠ 指定基线缺失，当前展示最新 run" : "");
    },
    // 2026-09-24：动态信号可能停摆（`dynamic.enabled` 曾被关闭 ⇒ 信号停在 20260904、静默
    // 20 天）⇒ 标题与日期都必须显式提示"已过期"，否则"今日动态信号"会让人把 20 天前的
    // 旧信号当成今天的。判据 = 信号日期 vs 数据日期（overview.data_date），不需要新接口字段。
    sigStale() {
      const d = store.signal.data;
      const o = store.overview.data;
      return !!(d && d.date && o && o.data_date && d.date !== o.data_date);
    },
    staleDays() {
      const d = store.signal.data;
      const o = store.overview.data;
      if (!d || !d.date || !o || !o.data_date) return 0;
      const p = (s) => new Date(+s.slice(0, 4), +s.slice(4, 6) - 1, +s.slice(6, 8));
      return Math.max(0, Math.round((p(o.data_date) - p(d.date)) / 86400000));
    },
    signalTitle() { return this.sigStale ? "动态信号（已过期）" : "今日动态信号" },
    signalDate() {
      const d = store.signal.data;
      if (!d || !d.date) return "";
      return this.sigStale
        ? `（停在 ${d.date}，落后 ${this.staleDays} 天 · 数据已到 ${store.overview.data.data_date}）`
        : "（" + d.date + "）";
    },
    kpisTiming() {      const o = this.o.data;
      if (!o) return [];
      const bt = o.backtest || {};
      const tgt = bt.stocks_target;
      const tgtStr = tgt && typeof tgt === "object"
        ? `${tgt.attack ?? "?"}/${tgt.neutral ?? "?"}/${tgt.defense ?? "?"}` : (tgt ?? "--");
      const cap = bt.eff_cap_by_state;
      const capStr = cap && typeof cap === "object"
        ? `攻${fmtPct(cap.attack)}/中${fmtPct(cap.neutral)}/防${fmtPct(cap.defense)}` : "--";
      return [
        { label: "市场状态", value: esc(STATE_CN[o.state] || o.state), sub: `评分 ${o.state_score ?? "--"}` },
        /* 月频 regime 的建议仓位（现为 0.873 恒定）；周频袖套自己的敞口在袖套卡片里 */
        { label: "月频建议仓位", value: esc(fmtPct(o.equity_weight, 0)), sub: "月频系统（周频袖套见上）" },
        { label: "持仓只数 目标→实际", value: esc(bt.avg_stocks != null ? `${tgtStr} → ${bt.avg_stocks}` : "--"), sub: `满仓线口径 · 行业上限生效 ${capStr}` },
        { label: "股票预算闲置（满仓线）", value: esc(fmtPct(bt.avg_cash_idle)), sub: (bt.candidate_top ?? 0) > 0 ? `候选池 ${bt.candidate_top} 只向下补位` : "不补位（实证保护性持现）" },
      ];
    },
    kpisBacktest() {
      const o = this.o.data;
      if (!o) return [];
      const bt = o.backtest || {};
      /* 2026-09-28 审查(36补): KpiGrid 的 value/sub 都走 v-html ⇒ sub 里的动态插值
         与 value 同约定必须过 esc(当前全是后端数值无可注入面, 防的是未来补字段时复现
         PlanView 白屏类事故) */
      return [
        { label: "纯选股年化", value: esc(fmtPct(bt.ann_return)), sub: `基准 ${esc(fmtPct(bt.bench_ann))}` },
        { label: "超额年化", value: esc(fmtPct(bt.excess_ann)), sub: `月度胜率 ${esc(fmtPct(bt.win_rate, 0))}` },
        { label: "系统年化", value: esc(fmtPct(bt.sys_ann_return)), sub: "含仓位择时+现金" },
        { label: "最大回撤", value: esc(fmtPct(bt.max_drawdown)), sub: `月频 · 日频盯市 满仓${esc(fmtPct(bt.mdd_daily))}/系统${esc(fmtPct(bt.sys_mdd_daily))} · 夏普 ${esc(bt.sharpe ?? "--")}` },
        { label: "IC / ICIR", value: esc(bt.ic_mean ?? "--"), sub: `ICIR ${esc(bt.icir ?? "--")}` },
        { label: "回测区间", value: esc(`${(bt.start || "").slice(0, 4)}-${(bt.end || "").slice(0, 4)}`), sub: `${esc(bt.periods ?? "--")} 个月度` },
      ];
    },
    sigCols() {
      return [
        { key: "action", label: "动作", render: r => `<b class="${ACTION_CLS[r.action] || ""}">${esc(ACTION_CN[r.action] || r.action)}</b>` },
        { key: "name", label: "名称" },
        { key: "ts_code", label: "代码", value: r => r.ts_code, render: r => stockLink(r.ts_code, r.name) },
        { key: "reason", label: "原因" },
      ];
    },
    histCols() {
      return [
        { key: "signal_date", label: "日期" },
        { key: "action", label: "动作", render: r => `<span class="${ACTION_CLS[r.action] || ""}">${esc(ACTION_CN[r.action] || r.action)}</span>` },
        { key: "name", label: "名称" },
        { key: "reason", label: "原因" },
      ];
    },
  },
  mounted() { ensure("overview", "signal", "weeklyHealth") },
}
</script>
