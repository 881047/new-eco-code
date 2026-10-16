<!-- 选股榜卡片（三模型共用）。
     2026-10-08 抽出：FinalView/W2View 的「选股榜 Top100」区块同构（标题 + AsyncSection
     + DataTable + 空态文案 + 页脚口径注记 + 跳转 /#/picks 的链接），此前复制两份。
     列定义与注记由视图传入（Final=pickBaseCols+EMA 平滑秩；W2=pickBaseCols+锚点收盘+总市值），
     本组件不内建任何盘面专属列。 -->
<template>
  <section class="card" style="margin-top:24px">
    <h2>{{ title }} <span class="muted">（{{ subtitle }}）</span></h2>
    <AsyncSection :state="state">
      <template v-if="picks.length">
        <DataTable :columns="columns" :rows="picks" :export-name="exportName" />
        <!-- 表下补充（W2：sidecar 自校验 ✓/✗；Final：无） -->
        <slot name="after-table" />
      </template>
      <p v-else class="muted">榜单暂不可用：{{ error || emptyHint }}</p>
    </AsyncSection>
    <p class="muted" style="margin-top:8px">
      <slot name="footer" />
    </p>
  </section>
</template>

<script>
import DataTable from './DataTable.vue'
import AsyncSection from './AsyncSection.vue'

export default {
  name: "PicksBoard",
  components: { DataTable, AsyncSection },
  props: {
    state: { type: Object, required: true },
    picks: { type: Array, required: true },
    columns: { type: Array, required: true },
    title: { type: String, default: "选股榜 Top100" },
    subtitle: { type: String, default: "" },
    exportName: { type: String, default: "选股榜Top100" },
    error: { type: String, default: null },
    /* 榜单为空且无 error 时的说明（两个盘面的"等哪档任务"不同） */
    emptyHint: { type: String, default: "等待数据源生成" },
  },
}
</script>
