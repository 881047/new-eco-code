<!-- 三盘面统一渲染器（2026-10-08 批5 配置驱动）。
     差异全部声明在 src/models.js 的 MODEL_CONFIG[model]；本组件只负责"照配置渲染"。
     FinalView / W2View / NewEcoHomeView 三个视图因此退化为薄壳（各 1 个 <ModelPage>）。

     边界（重要）：本组件**不碰数据加载** —— ensure("accounts"/"picks") 留在各视图的
     mounted 里。若这里也调一次，会与视图双调，触发重复请求与重复注册轮询。 -->
<template>
  <ModelPageShell
    :title="cfg.title"
    :page-title="cfg.pageTitle"
    :subtitle="cfg.subtitle"
    :state="st"
    :kpi-items="kpiItems"
    :chart-option="chartOption"
    :chart-caption="cfg.chartCaption"
    :dom-id="cfg.domId"
    :accounts="accounts"
  >
    <!-- 账户卡：盘面特有行 + 卡尾区块（W2 熔断三层） -->
    <template #account="{ a }">
      <AccountCard :a="a"
                   :extra-rows="cfg.accountCard.extraRows(a)"
                   :tail-rows="cfg.accountCard.tailRows(a)">
        <!-- 卡尾区块：W2 的熔断三层 chips（v-if 写在槽内部——写在 <template #slot> 上不可靠） -->
        <template #body="{ a: acct }">
          <div class="chips-row"
               v-if="cfg.accountCard.body === 'circuit' && acct.extra && acct.extra.circuit">
            <span class="blk-inline">熔断：</span>
            <span v-for="(v, k) in acct.extra.circuit" :key="k" class="chip">{{ k }} = {{ v == null ? "—" : v }}</span>
          </div>
        </template>
      </AccountCard>
    </template>

    <!-- 账户栅格下方：风控参数 chips（Final）或子页入口（NEW ECO） -->
    <template #after-accounts>
      <template v-if="cfg.afterAccounts === 'limits'">
        <div class="chips-row" v-if="limitsChips.length">
          <span class="blk-inline">风控：</span>
          <span v-for="c in limitsChips" :key="c.k" class="chip">{{ c.k }} = {{ c.v }}</span>
        </div>
      </template>
      <template v-else-if="cfg.afterAccounts === 'subPages'">
        <h3 class="blk-h">盘面功能页</h3>
        <div class="chips-row">
          <router-link v-for="p in subPages" :key="p.name" class="chip chip--accent"
                       :to="{ name: p.name }">{{ p.ico }} {{ p.label }}</router-link>
        </div>
      </template>
    </template>

    <!-- 模型信息 -->
    <template #meta>
      <ModelMetaCard :state="st" :meta="modelMeta"
                     :subtitle="cfg.meta.subtitle" :chips-label="cfg.meta.chipsLabel">
        <template #rows="{ meta }">
          <div v-for="r in cfg.meta.rows(meta)" :key="r.k" class="acc-row" :style="{ maxWidth: r.width + 'px' }">
            <span>{{ r.k }}</span><b>{{ r.v }}</b>
          </div>
        </template>
        <template #footer="{ meta }">
          <p class="muted" style="margin-top:8px" v-if="cfg.meta.footer(meta)">{{ cfg.meta.footer(meta) }}</p>
        </template>
      </ModelMetaCard>

      <!-- 参数配置（只读快照）——与模型信息同区，补上"三盘面都没有参数区"的缺口 -->
      <ModelParamsCard :model="model" :accounts="accounts" />
    </template>

    <!-- 按账户循环的功能区块（配置声明，表格/chips 两种形态） -->
    <AsyncSection :state="st">
      <section class="card" style="margin-top:24px" v-for="a in okAccounts" :key="'d-' + a.id">
        <h2>{{ a.name }} <span class="muted">（{{ cfg.accountHeader(a) }}）</span></h2>
        <template v-for="(b, bi) in cfg.blocks" :key="'b-' + bi">
          <h3 class="blk-h" v-if="blockTitle(b, a)">
            {{ blockTitle(b, a) }}<span v-if="blockSub(b, a)" class="muted">（{{ blockSub(b, a) }}）</span>
          </h3>

          <template v-if="b.kind === 'table'">
            <DataTable v-if="blockRows(b, a).length"
                       :columns="b.columns" :rows="blockRows(b, a)" :filterable="false"
                       :export-name="b.export" :empty-text="b.empty" />
            <p v-else class="muted">{{ b.empty }}</p>
          </template>

          <template v-else-if="b.kind === 'chips'">
            <div v-if="blockRows(b, a).length" class="chips-row">
              <span v-if="b.label" class="blk-inline">{{ b.label }}</span>
              <span v-for="(c, ci) in blockRows(b, a)" :key="ci" class="chip">{{ c }}</span>
            </div>
            <p v-else-if="b.empty" class="muted">{{ b.empty }}</p>
          </template>
        </template>
      </section>
    </AsyncSection>

    <!-- 选股榜 -->
    <PicksBoard
      :state="pickSt"
      :picks="picks"
      :columns="pickCols"
      :title="cfg.picks.title"
      :subtitle="cfg.picks.subtitle(picksMeta, { fullOrderN })"
      :export-name="cfg.picks.exportName"
      :error="pickError"
      :empty-hint="cfg.picks.emptyHint"
    >
      <template #after-table>
        <p class="muted" style="margin-top:8px" v-if="afterTable">{{ afterTable }}</p>
      </template>
      <template #footer>
        {{ cfg.picks.note }}三模型交集视角见
        <router-link :to="{ name: 'picks' }">三模型选股交集</router-link>。
      </template>
    </PicksBoard>

    <template #footer>
      <p class="muted" style="margin-top:12px" v-html="cfg.footerNote"></p>
    </template>
  </ModelPageShell>
</template>

<script>
import { store } from '../store'
import { money, thousand, pctTxt, pctCls, seriesForAccounts } from '../acctCols'
import { allAccountsOption } from '../charts'
import { MODEL_CONFIG } from '../models'
import { PAGES } from '../nav'
import ModelPageShell from './ModelPageShell.vue'
import AccountCard from './AccountCard.vue'
import ModelMetaCard from './ModelMetaCard.vue'
import ModelParamsCard from './ModelParamsCard.vue'
import PicksBoard from './PicksBoard.vue'
import AsyncSection from './AsyncSection.vue'
import DataTable from './DataTable.vue'

export default {
  name: "ModelPage",
  components: { ModelPageShell, AccountCard, ModelMetaCard, ModelParamsCard,
                PicksBoard, AsyncSection, DataTable },
  props: {
    /* 盘面键，必须是 MODEL_CONFIG 的键（NEW ECO / Final / W2） */
    model: { type: String, required: true },
  },
  computed: {
    cfg() {
      const c = MODEL_CONFIG[this.model];
      if (!c) throw new Error(`ModelPage: 未知盘面 ${this.model}（MODEL_CONFIG 未声明）`);
      return c;
    },
    st() { return store.accounts },
    pickSt() { return store.picks },
    accounts() {
      return ((store.accounts.data || {}).accounts || []).filter((a) => a.model === this.model);
    },
    okAccounts() { return this.accounts.filter((a) => a.ok) },
    modelMeta() { return ((store.accounts.data || {}).model_meta || {})[this.model] || null },
    picksData() { return (store.picks.data || {}).models || {} },
    picks() { return (this.picksData[this.model] || {}).picks || [] },
    pickError() { return (this.picksData[this.model] || {}).error || null },
    picksMeta() { return this.picksData[this.model] || {} },
    fullOrderN() { return this.picksMeta.n_full_order ?? "—" },
    pickCols() { return this.cfg.picks.columns() },
    afterTable() { return this.cfg.picks.afterTable ? this.cfg.picks.afterTable(this.picksMeta) : null },
    chartOption() {
      return allAccountsOption({ series: seriesForAccounts(this.accounts) });
    },
    kpiItems() {
      return this.cfg.kpi({
        accounts: this.accounts,
        okAccounts: this.okAccounts,
        picks: this.picks,
        helpers: { money, thousand, pctTxt, pctCls },
      });
    },
    limitsChips() {
      const w2Acct = (this.okAccounts.find((a) => a.id === "fterm-w2") || {}).extra || {};
      return Object.entries(w2Acct.limits || {}).map(([k, v]) => ({ k, v }));
    },
    /* NEW ECO 子页入口：从 nav.js 取该组里除盘面页自身之外的项（单源，不手写清单） */
    subPages() {
      return PAGES.filter((p) => p.group === this.model && p.name !== this.cfg.pageRoute);
    },
    /* 该盘面的路由名（供 subPages 排除自身；也便于将来做"进入盘面页"跳转） */
    pageRouteName() { return this.cfg.pageRoute; },
  },
  methods: {
    money, thousand, pctTxt, pctCls,
    /* 区块标题/副标题/数据行都可能是函数（依赖账户 a），统一在这里求值 */
    blockTitle(b, a) { return typeof b.title === "function" ? b.title(a) : b.title; },
    blockSub(b, a) { return typeof b.sub === "function" ? b.sub(a) : (b.sub || ""); },
    blockRows(b, a) {
      const r = b.rows(a);
      return Array.isArray(r) ? r : [];
    },
  },
}
</script>
