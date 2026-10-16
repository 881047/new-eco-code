<!-- 模型信息卡外壳（三模型共用）。
     2026-10-08 抽出：FinalView/W2View 的「模型信息」卡片结构同构（标题 + AsyncSection
     + 若干 acc-row + 因子 chips + 可选脚注），此前复制两份。
     两个盘面的行内容差异大（Final=训练工件/训练时间/全史样本/训练档；
     W2=选股规则/训练档 + factor_source 注记），故行区走 #rows 插槽由视图自定，
     本组件只钉住「骨架 + 因子 chips + 缺失态」这三处共性。 -->
<template>
  <section class="card" style="margin-top:24px">
    <h2>{{ title }} <span class="muted">（{{ subtitle }}）</span></h2>
    <AsyncSection :state="state">
      <template v-if="meta">
        <slot name="rows" :meta="meta" />
        <div class="chips-row" style="margin-top:10px">
          <span class="blk-inline">{{ chipsLabel }}（{{ (meta.factors || []).length }}）：</span>
          <span v-for="f in (meta.factors || [])" :key="f" class="chip">{{ f }}</span>
        </div>
        <slot name="footer" :meta="meta" />
      </template>
      <p v-else class="muted">模型元数据不可读</p>
    </AsyncSection>
  </section>
</template>

<script>
import AsyncSection from './AsyncSection.vue'

export default {
  name: "ModelMetaCard",
  components: { AsyncSection },
  props: {
    state: { type: Object, required: true },
    meta: { type: Object, default: null },
    title: { type: String, default: "模型信息" },
    subtitle: { type: String, default: "" },
    /* Final="特征"；W2="M1 实验线因子" */
    chipsLabel: { type: String, default: "特征" },
  },
}
</script>
