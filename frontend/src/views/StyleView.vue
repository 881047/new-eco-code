<template>
  <div>
    <div class="page-title">风格读数</div>

    <!-- 自 PlanView 拆出（2026-10-06 分类细化）：池子 vs 中证800 的风格/体制描述性读数。
         契约钉点：模板使用 STATE_CN 必须挂实例（⑧ 白屏事故）；描述文案不许写成买卖信号。 -->
    <section class="card" id="style">
      <h2>风格读数 · 池子（等权全A） vs 中证800 <span class="muted">{{ styleDate }}</span></h2>
      <AsyncSection :state="st">
        <template v-if="sd">
          <div v-if="!sd.available" class="muted">{{ sd.reason || "暂无数据" }}</div>
          <template v-else>
            <KpiGrid :items="styleKpis" compact dom-id="style-kpis" />
            <div v-if="sd.diverged" class="banner-note" style="margin-top:6px;font-size:var(--fs-2)">
              ⚠ 池子分支与生产线基准状态不一致（池子 <b>{{ STATE_CN[sd.pool_state] || sd.pool_state }}</b> /
              中证800 <b>{{ STATE_CN[sd.bench_state] || sd.bench_state || "?" }}</b>，基准状态日 {{ sd.bench_state_date || "--" }}）：
              生产的均线/回撤/波动算在中证800 上，此刻与持仓的参照系不同步。恒定仓位后该状态已不再影响调仓，
              仅影响 ETF 候选域（builder.py 按 attack 决定是否扫股票型基金）。
            </div>
            <ChartLine :option="styleOpt" :height="200" />
            <div class="muted" style="margin-top:6px;font-size:var(--fs-2)">
              池子 = 主板+创业板等权全A，持仓的真实参照系。曲线 &gt;1 表示池子跑赢中证800。
              <b>本块是描述性读数，不构成买卖信号</b> —— 2026-09-10 事件研究显示该信号无预测力，故不驱动任何动作；
              这也是 regime 保持恒定仓位、不再按状态择时的依据。
            </div>
          </template>
        </template>
      </AsyncSection>
    </section>
  </div>
</template>

<script>
/* 风格读数（2026-10-06 自 PlanView 拆出）：池子/中证800 相对强弱 + 体制状态，纯描述。 */
import { store, ensure } from '../store'
import { esc, fmtPct, STATE_CN } from '../format'
import { styleOption } from '../charts'
import KpiGrid from '../components/KpiGrid.vue'
import ChartLine from '../components/ChartLine.vue'
import AsyncSection from '../components/AsyncSection.vue'

export default {
  name: "StyleView",
  components: { KpiGrid, ChartLine, AsyncSection },
  computed: {
    /* 模板里要用 `STATE_CN[...]`。options API 下 **import 的常量不会进模板作用域**
       （编译产物是 `_ctx.STATE_CN`），必须挂到实例上 —— 2026-09-18 白屏事故的钉点。 */
    STATE_CN() { return STATE_CN },
    st() { return store.style },
    sd() { return store.style.data },
    styleDate() { const d = this.sd; return d && d.as_of ? "（" + d.as_of + "）" : "" },
    styleOpt() {
      const d = this.sd;
      if (!d || !d.available) return null;
      const xs = (d.series || []).map(r => r.d.slice(4, 6) + "-" + r.d.slice(6));
      return styleOption(xs, (d.series || []).map(r => r.v));
    },
    styleKpis() {
      const d = this.sd;
      if (!d) return [];
      const relCell = (v) => (v === null || v === undefined) ? "--"
        : `<span class="${v >= 0 ? "pos" : "neg"}">${v >= 0 ? "+" : ""}${fmtPct(v)}</span>`;
      const badge = (s) => (s === null || s === undefined) ? "--"
        : `<span class="badge ${esc(s)}" style="font-size:var(--fs-2);padding:2px 10px">${esc(STATE_CN[s] || s)}</span>`;
      return [
        { label: "近 20 日相对", value: relCell(d.rel_20) },
        { label: "近 60 日相对", value: relCell(d.rel_60) },
        { label: "近 250 日相对", value: relCell(d.rel_250) },
        { label: "风格判读", value: `<b>${esc(d.bias)}</b>` },
        { label: "池子状态", value: badge(d.pool_state) + `<span class="muted" style="font-size:var(--fs-2)"> 分 ${esc(d.pool_score ?? "--")}</span>` },
        { label: "池子自身回撤（250日）", value: `<span class="${(d.pool_drawdown ?? 0) >= 0 ? "pos" : "neg"}">${fmtPct(d.pool_drawdown)}</span>` },
        { label: "生产线基准状态", value: badge(d.bench_state) },
      ];
    },
  },
  async mounted() { ensure("style") },
}
</script>
