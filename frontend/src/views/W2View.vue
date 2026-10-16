<!-- W2 盘面页（2026-10-08 批5 配置驱动后已退化为薄壳）。
     全部渲染逻辑与文案在 src/models.js 的 MODEL_CONFIG["W2"]，由 components/ModelPage.vue 渲染。

     数据 = /api/all_accounts 的 accounts 域过滤 model==="W2"
          + /api/model_picks 的 models["W2"]（B0 规则全序 Top100 sidecar，由 ECO-W2 仓
          规范代码离线产出并自校验；本仓不 import 该仓代码）。 -->
<template>
  <ModelPage model="W2" />
</template>

<script>
import { ensure } from '../store'
import ModelPage from '../components/ModelPage.vue'

export default {
  name: "W2View",
  components: { ModelPage },
  async mounted() {
    ensure("accounts")
    ensure("picks")
  },
}
</script>
