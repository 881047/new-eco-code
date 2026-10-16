<template>
  <div>
    <div class="page-title">全账户总览</div>

    <!-- ======== 总览（常驻）：KPI + 全局曲线 ======== -->
    <section class="card">
      <h2>总览 <span class="muted">（三模型 7 纸面账户 · 各账本落账口径，非盘中实时）</span></h2>
      <AsyncSection :state="st" @retry="retry">
        <KpiGrid :items="kpiItems" compact dom-id="all-accounts-kpis" />
        <h3 class="blk-h">全局净值曲线（各账户以本金归一 · 虚线 = 基准 · 点图例可隐藏某账户）</h3>
        <ChartLine :option="chartOption" :height="340" />
      </AsyncSection>
    </section>

    <!-- ======== 门户：三张模型卡（账户入口行 / 功能按钮 / 训练与因子） ======== -->
    <template v-for="m in models" :key="'hub-' + m.name">
      <section class="card" style="margin-top:24px">
        <div class="model-card-head">
          <span class="model-title">{{ m.name }}</span>
          <span class="muted">{{ m.note }}</span>
          <span class="model-sum">小计 <b>¥{{ money(m.subtotal) }}</b> ·
            {{ m.accounts.length }} 个账户 · <span :class="m.statusCls">{{ m.statusTxt }}</span></span>
        </div>

        <h3 class="blk-h">账户（点行进入该账户的功能页）</h3>
        <div class="acc-mini-list">
          <button v-for="a in m.accounts" :key="'mini-' + a.id" type="button"
                  class="acc-mini" :class="{ bad: !a.ok }" @click="openAccount(a)">
            <span class="acc-mini-name">{{ a.name }}<span v-if="a.stale" class="stale-badge">滞后</span></span>
            <span class="acc-mini-eq">¥{{ money(a.equity) }}</span>
            <span class="acc-mini-pct" :class="pctCls(a.pct)">{{ pctTxt(a.pct) }}</span>
            <span class="muted acc-mini-asof">{{ a.asof || "—" }}</span>
          </button>
        </div>

        <div class="chips-row" style="margin-top:12px">
          <router-link v-if="m.name === 'NEW ECO'" class="ghost mini btn-strong"
                       :to="{ name: 'paper' }">📊 月频模拟盘</router-link>
          <template v-if="m.name === 'NEW ECO'">
            <router-link class="ghost mini" :to="{ name: 'lhb' }">龙虎榜</router-link>
            <router-link class="ghost mini" :to="{ name: 'sleeve' }">周频袖套</router-link>
            <router-link class="ghost mini" :to="{ name: 'plans' }">策略与方案</router-link>
            <router-link class="ghost mini" :to="{ name: 'model', hash: '#imp' }">因子重要性</router-link>
          </template>
          <router-link v-else class="ghost mini btn-strong"
                       :to="modelRoute(m.name)">📊 进入盘面页</router-link>
        </div>

        <details class="meta-details">
          <summary>训练时间点与因子（{{ factorCount(m) }} 因子 · 上次训练 {{ trainedTxt(m) }}）</summary>
          <div class="meta-body">
            <h4>档位时间点</h4>
            <ul class="meta-ul">
              <li v-for="s in (m.meta.schedule || [])" :key="s">{{ s }}</li>
            </ul>
            <h4>因子清单 <span class="muted">{{ m.meta.factor_source ? "—— " + m.meta.factor_source : "" }}</span></h4>
            <div class="chips-row">
              <span v-for="f in factorChips(m)" :key="f.label" class="chip">{{ f.label }}</span>
              <span v-if="!factorChips(m).length" class="muted">因子清单不可读</span>
            </div>
            <div v-if="m.meta.error" class="muted">元数据读取失败：{{ m.meta.error }}</div>
          </div>
        </details>
      </section>
    </template>

    <p class="muted" style="margin-top:12px">
      纸面模拟数据，非投资建议。数据源：本仓账本 + D:\Final / D:\ECO-W2（全部只读）；
      邮件覆盖：月频盘 09:31 成交即时邮件，Final/W2/周频成交当晚 21:15 逐户邮件，
      全账户日报 21:15（按模型分节），盘前行动提醒并入 08:30「今日待办」。
    </p>
  </div>
</template>

<script>
/* 全账户总览门户（2026-10-06 看板重构：从"NEW-ECO 独属看板"改为全账户枢纽——
   本页退化为纯门户（KPI + 全局曲线 + 三模型入口卡），单模型完整视图拆到
   FinalView（/final）与 W2View（/w2）；NEW ECO 的功能页在本页卡片的
   按钮组直达。旧 ?model= 深链在 mounted 自动迁移到对应模型页。 */
import { store, ensure, refresh } from '../store'
import { money, pctTxt, pctCls, seriesForAccounts, MODEL_ORDER, MODEL_NOTES, accountRoute } from '../acctCols'

/* 2026-10-08 盘面改名前（fterm / ECO-W2）的旧深链与旧查询值，归一到新键。 */
const LEGACY_MODEL = { "fterm": "Final", "ECO-W2": "W2" };
import { allAccountsOption } from '../charts'
import KpiGrid from '../components/KpiGrid.vue'
import ChartLine from '../components/ChartLine.vue'
import AsyncSection from '../components/AsyncSection.vue'

export default {
  name: "AllAccountsView",
  components: { KpiGrid, ChartLine, AsyncSection },
  async mounted() {
    await ensure("accounts");
    /* 旧深链迁移：/#/accounts?model=Final|W2 → 对应盘面页（旧书签不断）。
       2026-10-08 改名：旧值 fterm/ECO-W2 也一并归一，避免老链接跳空。 */
    const raw = String(this.$route.query.model || "");
    const m = LEGACY_MODEL[raw] || raw;
    if (m && m !== "NEW ECO") {
      const r = accountRoute({ model: m });
      if (r) { this.$router.replace(r); return; }
    }
  },
  computed: {
    st() { return store.accounts },
    d() { return store.accounts.data || {} },
    accounts() { return this.d.accounts || [] },
    totals() { return this.d.totals || {} },
    kpiItems() {
      const t = this.totals;
      const items = [{
        label: "纸面账户总权益",
        value: "¥" + this.money(t.equity),
        sub: `${t.n_ok ?? 0}/${t.n_total ?? 0} 账户 · 生成于 ${this.d.generated_at || "—"}`,
      }];
      const byModel = t.by_model || {};
      Object.keys(byModel).forEach(m => {
        items.push({ label: m, value: "¥" + this.money(byModel[m].equity),
                     sub: `${byModel[m].n} 个账户` });
      });
      if ((t.n_total || 0) > (t.n_ok || 0)) {
        items.push({ label: "异常源", value: `${t.n_total - t.n_ok} 个`, cls: "neg",
                     sub: "对应卡片显示读取失败原因" });
      }
      return items;
    },
    chartOption() {
      return allAccountsOption({ series: seriesForAccounts(this.accounts) });
    },
    models() {
      const metaAll = this.d.model_meta || {};
      return MODEL_ORDER.map(name => {
        const accounts = this.accounts.filter(a => a.model === name);
        const bad = accounts.filter(a => !a.ok).length;
        const stale = accounts.filter(a => a.ok && a.stale).length;
        const subtotal = accounts.reduce((s, a) => s + (a.ok ? (a.equity || 0) : 0), 0);
        const statusCls = bad ? "neg" : stale ? "" : "pos";
        const statusTxt = bad ? `${bad} 个读取失败` : stale ? `${stale} 个数据滞后` : "全部正常";
        return { name, note: MODEL_NOTES[name] || "", accounts, subtotal,
                 statusCls, statusTxt, meta: metaAll[name] || {} };
      }).filter(m => m.accounts.length);
    },
  },
  methods: {
    money, pctTxt, pctCls,
    retry() { refresh("accounts"); },
    /* 盘面卡 → 该盘面的页面路由。模板里只能调 methods，故在此转出
       acctCols.accountRoute（映射表单源，避免视图里再硬编码一份盘面→路由）。 */
    modelRoute(name) {
      return accountRoute({ model: name }) || {};
    },
    /* 账户行 → 该账户归属的功能页（映射表在 acctCols.accountRoute，模型页/功能页单源） */
    openAccount(a) {
      const r = accountRoute(a);
      if (r) this.$router.push(r);
    },
    factorChips(m) {
      const meta = m.meta || {};
      if (meta.importance && meta.importance.length) {
        return meta.importance.map(x => ({
          label: `${x.factor} ${(Number(x.importance) * 100).toFixed(1)}%` }));
      }
      return (meta.factors || []).map(f => ({ label: f }));
    },
    factorCount(m) {
      const meta = m.meta || {};
      return (meta.factors || []).length;
    },
    trainedTxt(m) {
      return (m.meta && m.meta.trained_at) || "—";
    },
  },
}
</script>

<style scoped>
.model-card-head { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
.model-title { font-size:var(--fs-7); font-weight: 600; }
.model-sum { font-size:var(--fs-3); margin-left: auto; font-variant-numeric: tabular-nums; }
.acc-mini-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 8px; }
.acc-mini {
  display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; cursor: pointer;
  /* 2026-10-08 批2：去掉全部裸 fallback —— --line/--card-bg/--accent/--banner-line/--text
     在 tokens.css 均有定义；fallback 里的硬编码色只会在暗色下给出错色（深色边框被沿用）。 */
  border: 1px solid var(--line); border-radius: var(--r-chip); padding: 8px 12px;
  background: var(--card-bg); font: inherit; color: inherit; text-align: left;
}
.acc-mini:hover { border-color: var(--accent); }
.acc-mini.bad { border-color: var(--banner-line); }
.acc-mini-name { font-weight: var(--fw-semibold); }
.acc-mini-eq { margin-left: auto; font-variant-numeric: tabular-nums; }
.acc-mini-pct { font-variant-numeric: tabular-nums; }
.acc-mini-asof { font-size:var(--fs-2); }
.btn-strong { font-weight: var(--fw-semibold); }
.meta-details { border: 1px solid var(--line); border-radius: var(--r-chip); margin-top: 12px; }
.meta-details summary { cursor: pointer; padding: 8px 12px; font-size:var(--fs-3); user-select: none; }
.meta-body { padding: 4px 12px 12px; border-top: 1px solid var(--line); }
.meta-body h4 { font-size:var(--fs-3); color: var(--muted); margin: 10px 0 6px; }
.meta-ul { margin: 0; padding-left: 18px; font-size:var(--fs-3); color: var(--text); }
.meta-ul li { margin: 2px 0; }
</style>
