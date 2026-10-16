<template>
  <div>
    <div class="page-title">方案与净值</div>

    <!-- 2026-10-06 分类细化拆分：现金与基金 → FundsView，风格读数 → StyleView；
         本页保留回测净值曲线 + 当期股票方案。 -->
    <section class="card" id="nav">
      <h2>回测净值曲线（月度, 2017 至今）
        <span class="muted" v-if="navRange">｜ 拖选区间：{{ navRange }}</span>
      </h2>
      <AsyncSection :state="nv">
        <ChartLine :option="navOpt" :height="380" with-range-return @range-return="onNavRange" />
      </AsyncSection>
    </section>

    <section class="card" id="stockplan" style="margin-top:24px">
      <h2>当期股票方案 <span class="muted">{{ planDate }}</span></h2>
      <AsyncSection :state="pl">
        <DataTable :columns="stockCols" :rows="stocks" :filterable="false" export-name="股票方案" empty-text="暂无方案" />
        <div class="muted" style="margin-top:6px" v-if="metaReason">{{ metaReason }}</div>
      </AsyncSection>
    </section>
  </div>
</template>

<script>
import { store, ensure } from '../store'
import { fmtPct } from '../format'
import { navOption, rangeReturnTxt, colors } from '../charts'
import DataTable from '../components/DataTable.vue'
import ChartLine from '../components/ChartLine.vue'
import AsyncSection from '../components/AsyncSection.vue'

export default {
  name: "PlanView",
  components: { DataTable, ChartLine, AsyncSection },
  data() { return { navRange: "" } },
  computed: {
    nv() { return store.nav },
    pl() { return store.plan },
    rows() { return (store.plan.data || {}).rows || [] },
    byType() {
      const m = {};
      this.rows.forEach(r => { (m[r.plan_type] = m[r.plan_type] || []).push(r); });
      return m;
    },
    stocks() { return this.byType.stock || [] },
    metaReason() { const m = this.byType.meta || []; return m.length ? m[0].reason : "" },
    planDate() { const d = store.plan.data; return d && d.date ? "（" + d.date + "）" : "" },
    navOpt() {
      const d = store.nav.data;
      const rows = (d || {}).rows || [];
      if (!rows.length) return null;
      return navOption({
        xs: rows.map(r => r.trade_date.slice(0, 4) + "-" + r.trade_date.slice(4, 6) + "-" + r.trade_date.slice(6)),
        // 基准曲线 = **全收益**指数(2026-09-20 B1: config.benchmark_nav_code=H00906.CSI)。
        // 模拟盘那张图(TradingView.vue)仍是**价格指数** —— 两把尺并存是有意的，别"统一"。
        legendData: ["纯选股组合", "完整系统", "中证800全收益(H00906)"],
        series: [
          { name: "纯选股组合", data: rows.map(r => r.nav == null ? null : +(r.nav * 100 - 100).toFixed(2)), color: colors().blue },
          { name: "完整系统", data: rows.map(r => r.sys_nav == null ? null : +(r.sys_nav * 100 - 100).toFixed(2)), color: colors().red },
          { name: "中证800全收益(H00906)", data: rows.map(r => r.bench_nav == null ? null : +(r.bench_nav * 100 - 100).toFixed(2)), color: colors().gray, dashed: true, width: 1.5 },
        ],
      });
    },
    stockCols() {
      return [
        { key: "name", label: "名称" },
        { key: "ts_code", label: "代码" },
        { key: "industry", label: "行业" },
        { key: "shares", label: "建议股数", num: true, value: r => r.shares },
        { key: "weight", label: "权重", num: true, value: r => r.weight, render: r => fmtPct(r.weight) },
        { key: "reason", label: "理由" },
      ];
    },
  },
  async mounted() {
    await ensure("nav", "plan");
  },
  methods: {
    /* dataZoom 拖选 → 区间收益文案（绑定逻辑内聚在 ChartLine，COR-03 的"加载后补绑"
       问题随之消失：数据到达 → option 变化 → ChartLine 重渲染时自绑） */
    onNavRange(r) { this.navRange = rangeReturnTxt(r) },
  },
}
</script>
