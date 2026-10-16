<template>
  <section class="card" id="brief">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">
      <h2 style="margin-bottom:0">📋 今日简报</h2>
      <span class="muted">{{ b.stamp }}</span>
    </div>
    <!-- 2026-09-28: lines 改为 {text, source} 对象(红线"每条输出带数据来源字段");
         {{ }} 插值自动转义, 来源小字只读展示 -->
    <div v-for="(l, i) in b.lines" :key="i" style="padding:3px 0;font-size:var(--fs-3)">
      {{ l.text }}<span v-if="l.source" class="muted" style="font-size:var(--fs-1)">（来源: {{ l.source }}）</span>
    </div>
  </section>
</template>

<script>
import { composeBrief } from '../intelligence/brief'

export default {
  name: "BriefCard",
  computed: { b() { return composeBrief() } },
}
</script>
