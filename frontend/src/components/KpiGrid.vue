<template>
  <div class="kpis" :id="domId">
    <div v-for="k in items" :key="k.label" class="kpi" :class="{ compact: compact }">
      <div class="label">{{ k.label }}</div>
      <div class="value" :class="k.cls" :style="compact ? { fontSize: compactValue } : {}" v-html="k.value"></div>
      <div class="sub" v-html="k.sub"></div>
    </div>
  </div>
</template>

<script>
/* KPI 网格：items = [{label, value(html,调用方先esc), sub(html), cls}] */
import { esc } from '../format'

export default {
  name: "KpiGrid",
  props: {
    items: { type: Array, default: () => [] },
    compact: { type: Boolean, default: false },
    compactValue: { type: String, default: "18px" },
    domId: { type: String, default: "kpis" },
  },
  methods: {
    /* 暴露给父组件拼 HTML 时复用 */
    esc,
  },
}
</script>
