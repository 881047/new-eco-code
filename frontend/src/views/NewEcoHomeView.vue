<!-- NEW ECO 盘面页（2026-10-08 批4 新增，批5 配置驱动后已退化为薄壳）。
     全部渲染逻辑与文案在 src/models.js 的 MODEL_CONFIG["NEW ECO"]，由 components/ModelPage.vue 渲染。

     本页补的是**信息架构缺口**：此前 Final/W2 各有"一页装完盘面"的页，而 NEW ECO 的
     盘面内容散在 8 个子页里 ⇒ 导航长期 8:1:1 不对称。本页只放三盘面共有的盘面级区块
     + 子页入口；8 个子页是真实功能差异，保持独立路由。

     数据 = /api/all_accounts 的 accounts 域过滤 model==="NEW ECO"
          + /api/model_picks 的 models["NEW ECO"]（daily_score 最新日 Top100）
          + model_meta["NEW ECO"]（因子清单 + 档位）。 -->
<template>
  <ModelPage model="NEW ECO" />
</template>

<script>
import { ensure } from '../store'
import ModelPage from '../components/ModelPage.vue'

export default {
  name: "NewEcoHomeView",
  components: { ModelPage },
  async mounted() {
    ensure("accounts")
    ensure("picks")
  },
}
</script>
