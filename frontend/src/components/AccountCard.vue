<!-- 单账户卡（三模型共用）。
     2026-10-08 自 FinalView/W2View 抽出：此前两处账户卡模板**逐字重复 25 行**，
     改一处忘另一处 = 两个盘面的账户卡静默不一致。
     差异通过 props + 插槽表达，视觉类全部沿用 tokens.css 的全局账户卡类
     （.acc-card/.acc-head/.acc-name/.model-chip/.acc-eq/.acc-pct/.acc-row...），
     本组件不写 scoped 覆盖——改样式请改 tokens.css 一处。 -->
<template>
  <div class="acc-card" :class="{ bad: !a.ok }">
    <div class="acc-head">
      <span class="acc-name">{{ a.name }}<span v-if="a.stale" class="stale-badge">数据滞后</span></span>
      <span class="model-chip">{{ a.model }}</span>
    </div>
    <template v-if="a.ok">
      <div class="acc-eq">¥{{ money(a.equity) }}</div>
      <div class="acc-pct" :class="pctCls(a.pct)">
        {{ pctTxt(a.pct) }}<span class="muted"> 自建仓 · 回撤 {{ ddTxt }}</span>
      </div>
      <div class="acc-row"><span>本金</span><b>¥{{ money(a.capital) }}</b></div>
      <div class="acc-row"><span>现金 / 持仓市值</span><b>¥{{ money(a.cash) }} / ¥{{ money(a.pos_value) }}</b></div>
      <!-- 盘面特有行插在「现金/持仓市值」与「数据日」之间（Final：持仓只数·交易日） -->
      <div v-for="r in extraRows" :key="r.k" class="acc-row">
        <span>{{ r.k }}</span><b>{{ r.v }}</b>
      </div>
      <div class="acc-row"><span>数据日</span><b>{{ a.asof || "—" }}</b></div>
      <!-- 盘面特有行插在「数据日」之后（Final：熔断线） -->
      <div v-for="r in tailRows" :key="'t-' + r.k" class="acc-row">
        <span>{{ r.k }}</span><b>{{ r.v }}</b>
      </div>
      <!-- 卡尾自由区（W2：熔断三层 chips；Final：无） -->
      <slot name="body" :a="a" />
    </template>
    <div v-else class="muted">读取失败：{{ a.error }}</div>
  </div>
</template>

<script>
import { money, pctTxt, pctCls } from '../acctCols'

export default {
  name: "AccountCard",
  props: {
    a: { type: Object, required: true },
    /* [{k, v}]：插在「现金/持仓市值」之后 */
    extraRows: { type: Array, default: () => [] },
    /* [{k, v}]：插在「数据日」之后 */
    tailRows: { type: Array, default: () => [] },
  },
  computed: {
    ddTxt() { return this.a.dd_pct == null ? "—" : this.a.dd_pct.toFixed(2) + "%" },
  },
  methods: { money, pctTxt, pctCls },
}
</script>
