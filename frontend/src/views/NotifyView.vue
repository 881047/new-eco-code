<template>
  <div>
    <!-- embedded：被 OpsView 包住时由外层出页头，本组件不再重复 -->
    <div class="page-title" v-if="!embedded">邮件记录</div>

    <!-- 自 DataView 拆出（2026-10-06 分类细化）：SMTP 发送记录独立成页。 -->
    <section class="card" id="notify">
      <h2>邮件发送记录 <span class="muted">（最近 20 封；失败会同时出现在今日行动）</span></h2>
      <AsyncSection :state="nt">
        <DataTable :columns="notifyCols" :rows="ntRows" export-name="邮件记录" empty-text="暂无发送记录" />
      </AsyncSection>
    </section>
  </div>
</template>

<script>
/* 邮件记录（2026-10-06 自 DataView 拆出）：/api/notify 最近发送记录与失败详情。 */
import { store, ensure } from '../store'
import { esc } from '../format'
import DataTable from '../components/DataTable.vue'
import AsyncSection from '../components/AsyncSection.vue'

export default {
  name: "NotifyView",
  components: { DataTable, AsyncSection },
  /* 2026-10-08 批4：/ops 把本页与 StatusView 合并成一页，由 OpsView 出页头 */
  props: { embedded: { type: Boolean, default: false } },
  computed: {
    nt() { return store.notify },
    ntRows() { return (store.notify.data || {}).rows || [] },
    notifyCols() {
      return [
        { key: "created_at", label: "时间" },
        { key: "tag", label: "类型" },
        { key: "ok", label: "结果", value: r => r.ok ? 1 : 0, render: r => `<span class="${r.ok ? "pos" : "neg"}">${r.ok ? "成功" : "失败"}</span>` },
        { key: "error", label: "错误", render: r => `<span style="color:var(--up)">${esc(r.error ?? "")}</span>` },
      ];
    },
  },
  async mounted() { ensure("notify") },
}
</script>
