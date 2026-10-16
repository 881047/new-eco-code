<!-- 三模型盘面页骨架（Final / W2 / NEW ECO 共用）。
     2026-10-08 抽出：FinalView 与 W2View 的首屏卡片结构同构
     （page-title → h2 盘面名 → KpiGrid → 净值图 → 账户栅格），此前复制两份。

     边界（重要）：本组件**不碰数据加载**——ensure("accounts"/"picks") 留在各视图的
     mounted 里。骨架若也调一次会与视图双调，触发重复请求与轮询注册两次。 -->
<template>
  <div>
    <div class="page-title">{{ pageTitle || title }}</div>

    <!-- 首屏卡：盘面名 + KPI + 净值 + 账户栅格 -->
    <section class="card">
      <h2>{{ title }} <span class="muted">（{{ subtitle }}）</span></h2>
      <AsyncSection :state="state">
        <KpiGrid :items="kpiItems" compact :dom-id="domId" />
        <h3 class="blk-h">净值（{{ chartCaption }}）</h3>
        <ChartLine
          :option="chartOption"
          :height="chartHeight"
          :with-range-return="withRangeReturn"
          @range-return="$emit('range-return', $event)"
        />
        <h3 class="blk-h">账户</h3>
        <AccountCardGrid :accounts="accounts">
          <template #card="{ a }">
            <slot name="account" :a="a" />
          </template>
        </AccountCardGrid>
        <!-- 账户栅格下方的盘面特有区（Final：风控 chips；W2：无） -->
        <slot name="after-accounts" />
      </AsyncSection>
    </section>

    <!-- 模型信息卡（各盘面行内容差异大，整块由视图提供） -->
    <slot name="meta" />

    <!-- 主体：按账户循环的功能区块（持仓/挂单/成交/逐期/决策审计...） -->
    <slot />

    <!-- 页脚口径注记 -->
    <slot name="footer" />
  </div>
</template>

<script>
import KpiGrid from './KpiGrid.vue'
import ChartLine from './ChartLine.vue'
import AsyncSection from './AsyncSection.vue'
import AccountCardGrid from './AccountCardGrid.vue'

export default {
  name: "ModelPageShell",
  components: { KpiGrid, ChartLine, AsyncSection, AccountCardGrid },
  props: {
    title: { type: String, required: true },
    /* 页头标题。多数盘面与卡片标题一致；Final 页头是「Final · 双轨账户」而卡内是「Final」 */
    pageTitle: { type: String, default: "" },
    subtitle: { type: String, default: "" },
    /* store 域对象（如 store.accounts），交给 AsyncSection 渲染三态 */
    state: { type: Object, required: true },
    kpiItems: { type: Array, default: () => [] },
    chartOption: { type: Object, default: null },
    chartHeight: { type: Number, default: 300 },
    chartCaption: { type: String, default: "各账户以本金归一 · 点图例可隐藏" },
    /* KpiGrid 的 DOM 锚点 id（命令面板/截图定位用，需盘面唯一） */
    domId: { type: String, default: "" },
    /* 净值图拖选：NEW ECO 两页早已开启，Final/W2 本次一并补上（B4） */
    withRangeReturn: { type: Boolean, default: true },
    accounts: { type: Array, default: () => [] },
  },
  emits: ["range-return"],
}
</script>
