<!-- 账户卡栅格（三模型共用）。
     2026-10-08 抽出：.acc-grid 容器在 FinalView/W2View 各写一份，
     连同 v-for 与 :key 一起复制。栅格参数沿用 tokens.css 的 .acc-grid。 -->
<template>
  <div class="acc-grid">
    <template v-for="a in accounts" :key="a.id">
      <slot name="card" :a="a">
        <!-- 未传 #card 插槽时退回默认卡片（无盘面特有行） -->
        <AccountCard :a="a" />
      </slot>
    </template>
  </div>
</template>

<script>
import AccountCard from './AccountCard.vue'

export default {
  name: "AccountCardGrid",
  components: { AccountCard },
  props: {
    accounts: { type: Array, required: true },
  },
}
</script>
