<template>
  <div>
    <div class="page-title">龙虎榜与游资</div>

    <!-- 自 TodayView 拆出（2026-10-06 分类细化）：每日市场情报独立成页。 -->
    <section class="card" id="lhb">
      <h2>龙虎榜 <span class="muted">{{ lhbDate }}</span></h2>
      <AsyncSection :state="lh">
        <div class="grid2 lhb-grid" style="grid-template-columns:minmax(0,1.15fr) minmax(0,1fr)">
          <div style="min-width:0">
            <h3 class="blk-h">上榜个股（<span class="chip chip--accent">持仓</span> 高亮标注）</h3>
            <DataTable :columns="lhbCols" :rows="lhRows" :max-height="420" export-name="龙虎榜个股"
                       empty-text="暂无龙虎榜数据（每日更新后自动拉取）" />
          </div>
          <div style="min-width:0">
            <h3 class="blk-h">游资动向</h3>
            <DataTable :columns="hmCols" :rows="hmRows" :max-height="420" export-name="游资动向"
                       empty-text="暂无游资数据（同花顺口径逐日积累）" />
          </div>
        </div>
      </AsyncSection>
    </section>
  </div>
</template>

<script>
/* 龙虎榜与游资（2026-10-06 自 TodayView 拆出）：上榜个股（持仓高亮）+ 游资净买。
   数据域 /api/lhb（今日行动中枢的"持仓上榜"行动项跳转到本页）。 */
import { store, ensure } from '../store'
import { esc, fmtPct, fmtMoney, stockLink } from '../format'
import DataTable from '../components/DataTable.vue'
import AsyncSection from '../components/AsyncSection.vue'

export default {
  name: "LhbView",
  components: { DataTable, AsyncSection },
  computed: {
    lh() { return store.lhb },
    lhRows() { return ((store.lhb.data || {}).rows || []) },
    hmRows() { return ((store.lhb.data || {}).hot_money || []) },
    lhbDate() {
      const d = store.lhb.data;
      if (!d || !d.date) return "";
      return "（" + d.date + " · " + (d.source === "lhb_ths" ? "同花顺" : "东方财富") + "）";
    },
    lhbCols() {
      return [
        { key: "name", label: "名称", render: r => `${r.in_plan ? '<span class="chip chip--accent">持仓</span>' : ""}${stockLink(r.ts_code, r.name)}` },
        { key: "change", label: "涨跌幅", num: true, value: r => r.change, render: r => `<span class="${(r.change ?? 0) >= 0 ? "pos" : "neg"}">${fmtPct(r.change, 1)}</span>` },
        { key: "net_value", label: "净买入", num: true, value: r => r.net_value, render: r => `<span class="${(r.net_value ?? 0) >= 0 ? "pos" : "neg"}">${fmtMoney(r.net_value)}</span>` },
        { key: "org_net_value", label: "机构净买", num: true, value: r => r.org_net_value, render: r => `<span class="${(r.org_net_value ?? 0) >= 0 ? "pos" : "neg"}">${fmtMoney(r.org_net_value)}</span>` },
        { key: "hot_rank", label: "人气", num: true, value: r => r.hot_rank, render: r => esc(r.hot_rank ?? "--") },
        { key: "limit_reason", label: "上榜原因", render: r => { const x = esc(r.limit_reason); return `<span class="lhb-reason" title="${x}">${x}</span>`; } },
      ];
    },
    hmCols() {
      return [
        { key: "hm_name", label: "游资" },
        { key: "name", label: "标的", render: r => `${esc(r.name)} <span class="muted">${esc(r.thscode)}</span>` },
        { key: "net_value", label: "净买入", num: true, value: r => r.net_value, render: r => `<span class="${(r.net_value ?? 0) >= 0 ? "pos" : "neg"}">${fmtMoney(r.net_value)}</span>` },
        { key: "buying", label: "当日合计", num: true, value: r => r.buying, render: r => `<span class="${(r.buying ?? 0) >= 0 ? "pos" : "neg"}">${fmtMoney(r.buying)}</span>` },
      ];
    },
  },
  mounted() { ensure("lhb") },
}
</script>
