<template>
  <div>
    <div class="page-title">现金与基金</div>

    <!-- 自 PlanView 拆出（2026-10-06 分类细化）：现金管理/场内备选/场外推荐三块。
         数据域仍是 /api/plan（plan_type 分区：repo/etf/otc）。 -->
    <div class="grid2">
      <section class="card" id="cash" style="margin-top:0">
        <h2>现金管理 · 逆回购</h2>
        <template v-if="repo.length">
          <div v-for="r in repo" :key="r.ts_code">
            <div class="kpi" style="margin-bottom:8px">
              <div class="label">剩余现金管理</div>
              <div class="value" style="font-size:var(--fs-6)">{{ r.reason }}</div>
            </div>
          </div>
        </template>
        <span v-else class="muted">暂无方案</span>
        <h2 style="margin-top:18px">场内基金备选（ETF/LOF/封基）</h2>
        <DataTable :columns="etfCols" :rows="etfs" :filterable="false" export-name="ETF备选" empty-text="暂无数据" />
      </section>
      <section class="card" id="otc" style="margin-top:0">
        <h2>场外基金推荐（支付宝 / 天天基金申购）</h2>
        <DataTable :columns="otcCols" :rows="otc" :filterable="false" export-name="场外基金" empty-text="暂无数据（每月更新排行快照后生成）" />
      </section>
    </div>

    <p class="muted" style="margin-top:12px">
      逆回购方案与基金备选由月度链的 plan 生成；ETF 候选域受市场状态影响
      （builder 按 attack 决定是否扫股票型基金，见「风格读数」页）。
    </p>
  </div>
</template>

<script>
/* 现金与基金（2026-10-06 自 PlanView 拆出）：逆回购 + 场内基金备选 + 场外基金推荐。 */
import { store, ensure } from '../store'
import DataTable from '../components/DataTable.vue'

export default {
  name: "FundsView",
  components: { DataTable },
  computed: {
    rows() { return (store.plan.data || {}).rows || [] },
    byType() {
      const m = {};
      this.rows.forEach(r => { (m[r.plan_type] = m[r.plan_type] || []).push(r); });
      return m;
    },
    etfs() { return this.byType.etf || [] },
    otc() { return (this.byType.otc || []).map(r => ({ ...r, cat: (r.name || "").split("]")[0].replace("[", ""), nm: (r.name || "").split("]")[1] || "" })) },
    repo() { return this.byType.repo || [] },
    etfCols() {
      return [
        { key: "name", label: "名称" },
        { key: "ts_code", label: "代码" },
        { key: "fund_type", label: "类型" },
        { key: "reason", label: "理由" },
      ];
    },
    otcCols() {
      return [
        { key: "cat", label: "类别" },
        { key: "ts_code", label: "代码" },
        { key: "nm", label: "名称" },
        { key: "reason", label: "理由" },
      ];
    },
  },
  async mounted() { ensure("plan") },
}
</script>
