<template>
  <div>
    <div class="page-title">月频模拟盘</div>

    <!-- ========== 月频模拟盘（自 TradingView 拆出，2026-10-06 分类细化） ========== -->
    <section class="card" id="paper">
      <h2>月频模拟盘 <span class="muted">（全自动模拟交易：盘后决策 → 次日开盘价成交 · 整手 · 滑点+成本 · 一字板顺延）</span></h2>
      <div v-if="paper.paused" class="banner-note" style="margin-bottom:10px">
        ⏸ 交易已暂停（paper.paused=true）：不按方案/信号挂单、不成交，存量挂单已作废；数据链与净值记账照常。恢复：把 config.yaml 的 paper.paused 改回 false。
      </div>
      <AsyncSection :state="paper">
        <div v-if="!pd.created_at" class="muted">模拟盘未初始化（每日更新时自动创建）</div>
        <template v-else>
        <KpiGrid :items="paperKpis" compact dom-id="paper-kpis" />
        <div class="grid2" style="margin-top:16px">
          <div style="min-width:0">
            <h3 class="blk-h">持仓</h3>
            <DataTable :columns="posCols" :rows="pd.positions" :filterable="false" export-name="模拟盘持仓"
                       empty-text="暂无持仓（待首个交易日执行）" />
          </div>
          <div style="min-width:0">
            <h3 class="blk-h">
              净值（拖选区间自动算收益<span v-if="paperRange">：当前 {{ paperRange }}</span>）
            </h3>
            <ChartLine :option="paperNavOpt" :height="300" with-range-return @range-return="onPaperRange" />
          </div>
        </div>
        <h2 style="margin-top:16px">待执行挂单</h2>
        <DataTable :columns="orderCols" :rows="pd.orders || []" :filterable="false" export-name="模拟盘挂单" empty-text="无待执行挂单" />
        <h2 style="margin-top:16px">最近成交</h2>
        <DataTable :columns="tradeCols" :rows="pd.trades" :filterable="false" export-name="模拟盘成交" empty-text="暂无成交" />
        </template>
      </AsyncSection>
    </section>
  </div>
</template>

<script>
/* 月频模拟盘（2026-10-06 自 TradingView 拆出——月频盘与周频袖套是两套独立系统，
   导航分类细化后各自成页）。数据域 /api/paper；实时口径与合并逻辑仍在后端。 */
import { store, ensure } from '../store'
import { esc, fmtPct, fmtNum, stockLink } from '../format'
import { navOption, rangeReturnTxt, colors } from '../charts'
import KpiGrid from '../components/KpiGrid.vue'
import DataTable from '../components/DataTable.vue'
import ChartLine from '../components/ChartLine.vue'
import AsyncSection from '../components/AsyncSection.vue'

export default {
  name: "PaperView",
  components: { KpiGrid, DataTable, ChartLine, AsyncSection },
  data() { return { paperRange: "" } },
  computed: {
    paper() { return store.paper },
    pd() { return store.paper.data || {} },
    paperKpis() {
      const d = this.pd;
      const base = (d.initial_capital !== null && d.initial_capital !== undefined)
        ? d.initial_capital : (d.nav.length ? d.nav[0].nav : 0);
      const pnl = d.total - base;
      /* KpiGrid 的 value/sub 走 v-html —— 动态字符串必须过 esc（审查 SEC-01） */
      return [
        { label: "总资产", value: "¥" + fmtNum(d.total, 2), sub: d.is_live ? ("盘中 " + esc(d.quoted_at)) : ("数据截至 " + esc(d.data_date || "--")) },
        { label: "可用现金", value: "¥" + fmtNum(d.cash, 2), sub: "含交易费用" },
        { label: "持仓市值", value: "¥" + fmtNum(d.market_value, 2), sub: `${d.positions.length} 只持仓` },
        { label: "累计盈亏", value: (pnl >= 0 ? "+" : "") + fmtNum(pnl, 2), cls: pnl >= 0 ? "pos" : "neg", sub: fmtPct(base ? pnl / base : 0) },
      ];
    },
    /* ---------- 图表（基准 = 价格指数，与回测图的全收益尺是两把尺，契约测试钉死） ---------- */
    paperNavOpt() {
      const d = this.pd;
      if (!d.nav || !d.nav.length) return null;
      return navOption({
        xs: d.nav.map(r => r.trade_date.slice(4, 6) + "-" + r.trade_date.slice(6)),
        legendData: ["模拟盘(总收益口径)", "中证800价格指数"],
        series: [
          { name: "模拟盘(总收益口径)", data: d.nav.map(r => r.nav_norm == null ? null : +(r.nav_norm * 100 - 100).toFixed(2)), color: colors().blue },
          { name: "中证800价格指数", data: d.nav.map(r => r.bench_norm ? +(r.bench_norm * 100 - 100).toFixed(2) : null), color: colors().gray, dashed: true, width: 1.5 },
        ],
      });
    },
    /* ---------- 表格列 ---------- */
    posCols() {
      const livePriceCell = (p) => {
        if (p.live_price === null || p.live_price === undefined) return esc(p.close ?? "--");
        const cls = (p.live_pct_chg ?? 0) >= 0 ? "pos" : "neg";
        const badge = (p.live_pct_chg === null || p.live_pct_chg === undefined) ? "" :
          ` <span class="${cls}">${p.live_pct_chg >= 0 ? "+" : ""}${p.live_pct_chg}%</span>`;
        return `${esc(p.live_price)}${badge}`;
      };
      return [
        { key: "name", label: "名称" },
        { key: "ts_code", label: "代码", value: r => r.ts_code, render: r => stockLink(r.ts_code, r.name) },
        { key: "shares", label: "持股", num: true, value: r => r.shares },
        { key: "live_price", label: "现价", num: true, render: livePriceCell, value: r => r.live_price ?? r.close },
        { key: "avg_cost", label: "成本", num: true, value: r => r.avg_cost, render: r => esc(r.avg_cost ?? "--") },
        { key: "pnl", label: "浮盈亏", num: true, value: r => r.pnl, render: r => `<span class="${r.pnl >= 0 ? "pos" : "neg"}">${r.pnl === null ? "--" : fmtNum(r.pnl, 2) + " (" + r.pnl_pct + "%)"}</span>` },
      ];
    },
    orderCols() {
      return [
        { key: "side", label: "方向", render: r => `<span class="${r.side === "buy" ? "pos" : "neg"}">${r.side === "buy" ? "买入" : "卖出"}</span>` },
        { key: "name", label: "名称" },
        { key: "shares", label: "股数", num: true, value: r => r.shares },
        { key: "reason", label: "原因" },
        { key: "created_at", label: "创建时间", render: r => `<span class="muted">${esc((r.created_at || "").slice(5, 16))}</span>` },
      ];
    },
    tradeCols() {
      return [
        { key: "exec_date", label: "日期" },
        { key: "side", label: "方向", render: r => `<span class="${r.side === "buy" ? "pos" : "neg"}">${r.side === "buy" ? "买入" : "卖出"}</span>` },
        { key: "name", label: "名称" },
        { key: "shares", label: "股数", num: true, value: r => r.shares },
        { key: "price", label: "价格", num: true, value: r => r.price },
        { key: "amount", label: "金额", num: true, value: r => r.amount, render: r => fmtNum(r.amount, 0) },
        { key: "reason", label: "原因" },
      ];
    },
  },
  async mounted() {
    await ensure("paper");
  },
  methods: {
    onPaperRange(r) { this.paperRange = rangeReturnTxt(r) },
  },
}
</script>
