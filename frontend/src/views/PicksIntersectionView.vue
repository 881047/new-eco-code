<!-- 三模型选股交集页（2026-10-08 新增；2026-10-08 晚重构删重复区块）。
     数据 = /api/model_picks（app/model_picks.py 纯只读聚合）。
     2026-10-08 重构：删掉「各模型榜单前 20 预览」整块 —— 它与各盘面页的 PicksBoard
     完全重复（完整榜单本就在各盘面页），且其中两个 router-link 指向 B0 改名前的
     路由名 fterm/ecow2（已失效），判据 m === 'fterm' 亦恒假成死代码。
     口径注记保留并上移到本卡内（它是本页的核心解释，不是重复内容）。 -->
<template>
  <div>
    <div class="page-title">三模型选股交集</div>

    <section class="card">
      <h2>三模型选股 Top100 交集
        <span class="muted">（生成于 {{ gen || "—" }} · 各榜口径见下注记）</span>
      </h2>
      <AsyncSection :state="st">
        <KpiGrid :items="kpiItems" compact dom-id="picks-kpis" />
        <h3 class="blk-h">三模型同时点名（{{ triple.length }} 只）</h3>
        <DataTable v-if="triple.length"
                   :columns="tripleCols" :rows="triple" :filterable="false"
                   export-name="三模型交集" />
        <p v-else class="muted">
          当前无三模型共同入选——三套宇宙/频率不同（见下注记），空交集是正常读数而非故障。
        </p>
        <template v-for="(pv, pk) in pairs" :key="'p-' + pk">
          <h3 class="blk-h">{{ pk }}（{{ pv.n }} 只）</h3>
          <DataTable v-if="pv.codes.length"
                     :columns="pairCols(pk)" :rows="pv.codes" :filterable="false"
                     :export-name="'两两交集 ' + pk" />
          <p v-else class="muted">无交集</p>
        </template>
      </AsyncSection>

      <p class="muted" style="margin-top:12px">
        口径注记：NEW ECO = 月频模型的全市场日度推分（daily_score，18:00 档）；
        Final = dlv2 14 网集成的 EMA 平滑秩（日频，20:05 档）；W2 = B0 规则全序
        （周频锚点，子池 close≤20 元内按市值升序，sidecar 由 ECO-W2 仓规范代码离线产出并
        对照账本自校验）。三者宇宙与调选频率不同，交集是<b>强共识读数</b>：
        三套独立证据同时点名的股票；交集小乃至为空是结构性的，不构成故障。
        完整榜单见各盘面页。
      </p>
    </section>
  </div>
</template>

<script>
import { store, ensure } from '../store'
import { intersectCols, MODEL_ORDER } from '../acctCols'
import KpiGrid from '../components/KpiGrid.vue'
import DataTable from '../components/DataTable.vue'
import AsyncSection from '../components/AsyncSection.vue'

export default {
  name: "PicksIntersectionView",
  components: { KpiGrid, DataTable, AsyncSection },
  computed: {
    st() { return store.picks },
    payload() { return store.picks.data || {} },
    gen() { return this.payload.generated_at || null },
    models() { return this.payload.models || {} },
    inter() { return this.payload.intersection || {} },
    triple() { return this.inter.triple || [] },
    pairs() { return this.inter.pairs || {} },
    counts() { return this.inter.counts || {} },
    tripleCols() { return intersectCols(MODEL_ORDER) },
    pairCols() {
      return (pk) => intersectCols(String(pk).split(" ∩ "));
    },
    kpiItems() {
      const c = this.counts;
      return [
        { label: "三模型交集", value: `${c.triple ?? "—"} 只`,
          sub: "三套独立证据同时点名（强共识）", cls: (c.triple || 0) > 0 ? "pos" : "" },
        ...MODEL_ORDER.filter(m => this.models[m] && this.models[m].ok).map(m => ({
          label: `${m} 入榜`, value: `${c[m] ?? "—"} 只`,
          sub: (this.models[m] || {}).asof || "",
        })),
      ];
    },
  },
  methods: {},
  async mounted() { ensure("picks") },
}
</script>
