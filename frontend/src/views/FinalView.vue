<!-- Final 盘面页（2026-10-08 批5 配置驱动后已退化为薄壳）。
     全部渲染逻辑与文案在 src/models.js 的 MODEL_CONFIG["Final"]，由 components/ModelPage.vue 渲染。
     本文件只保留两件视图级职责：① 声明盘面键；② 数据加载（ensure 必须留在视图，
     不能进 ModelPage —— 否则与视图双调、重复注册轮询）。

     数据 = /api/all_accounts 的 accounts 域过滤 model==="Final"
          + /api/model_picks 的 models.Final（EMA 平滑秩 Top100）+ model_meta（因子清单）。 -->
<template>
  <ModelPage model="Final" />
</template>

<script>
import { ensure } from '../store'
import ModelPage from '../components/ModelPage.vue'

export default {
  name: "FinalView",
  components: { ModelPage },
  async mounted() {
    ensure("accounts")
    ensure("picks")
  },
}
</script>
