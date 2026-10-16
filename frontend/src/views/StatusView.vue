<template>
  <div>
    <!-- embedded：被 OpsView 包住时由外层出页头，本组件不再重复 -->
    <div class="page-title" v-if="!embedded">系统状态</div>

    <!-- 自 DataView 拆出（2026-10-06 分类细化）：数据水位/更新状态独立成页。 -->
    <section class="card" id="status">
      <h2>系统状态 <span class="muted">（「增量」单位随表不同：日线族=天数，其余=行数）</span></h2>
      <AsyncSection :state="sta">
        <DataTable :columns="statusCols" :rows="stRows" export-name="系统状态" :filterable="false" />
        <div class="muted" style="margin-top:4px;font-size:var(--fs-2)">
          无缺口时为 0（数据完整，非缺失）。注意：<b>factor</b>=月度全量窗口日、<b>daily_factor</b>=日频水位，
          双标签并存属正常口径差异，非数据落后。
        </div>
      </AsyncSection>
    </section>
  </div>
</template>

<script>
/* 系统状态（2026-10-06 自 DataView 拆出）：/api/status 各表水位与增量。
   今日行动中枢的"数据滞后"行动项跳转到本页。 */
import { store, ensure } from '../store'
import { fmtNum } from '../format'
import DataTable from '../components/DataTable.vue'
import AsyncSection from '../components/AsyncSection.vue'

export default {
  name: "StatusView",
  components: { DataTable, AsyncSection },
  /* 2026-10-08 批4：/ops 把本页与 NotifyView 合并成一页，由 OpsView 出页头 */
  props: { embedded: { type: Boolean, default: false } },
  computed: {
    sta() { return store.status },
    stRows() { return (store.status.data || {}).rows || [] },
    statusCols() {
      return [
        { key: "name", label: "数据项" },
        { key: "last_date", label: "最新日期" },
        { key: "updated_at", label: "更新时间" },
        { key: "rows", label: "本次增量", num: true, value: r => r.rows,
          render: r => `${(r.rows == null || r.rows === 0) && !r.last_date ? "—" : fmtNum(r.rows) + (r.unit === "day" ? " 天" : " 行")}` },
      ];
    },
  },
  async mounted() { ensure("status") },
}
</script>
