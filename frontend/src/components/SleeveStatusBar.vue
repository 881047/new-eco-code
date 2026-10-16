<template>
  <section class="card" id="sleeve" aria-label="周频袖套今日状态">
    <div class="sleeve-head" style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <span style="font-size:var(--fs-5);font-weight:600">周频袖套 · 今日</span>
      <span class="sleeve-chip" :class="s.kind" role="status" id="sleeveState">{{ s.text }}</span>
      <span class="muted" v-if="ops.next_exec">下个执行档 {{ ops.next_exec }}（锚点 {{ ops.next_anchor }}）</span>
    </div>
    <dl class="kpis" style="margin-top:12px" id="sleeveFacts">
      <div v-for="c in cells" :key="c[0]" class="kpi compact">
        <dt class="label" style="color:var(--muted);font-size:var(--fs-2)">{{ c[0] }}</dt>
        <dd style="font-size:var(--fs-5);font-weight:600;margin-top:2px;overflow-wrap:anywhere">{{ c[1] }}</dd>
      </div>
    </dl>
    <p class="muted" style="margin-top:12px;font-size:var(--fs-2);overflow-wrap:anywhere" id="sleeveNote">
      判据原文：{{ der || "（无）" }}<span v-if="!enabled"> ｜ 袖套未启用</span>
    </p>
  </section>
</template>

<script>
/* 首屏状态条（周频袖套 = 主策略，必须排在本视图所有区块之前）。
   可信度规则在 store.sleeveStatus()：只有真有成交/持仓才绿色，
   "空占位净值点"必须琥珀色等兜底 —— 不许把没建仓伪装成已执行。 */
import { store, sleeveStatus } from '../store'
import { fmtNum, fmtPct } from '../format'
import { HEALTH_ICN } from '../weeklyNarrative'

export default {
  name: "SleeveStatusBar",
  computed: {
    d() { return store.weekly.data || {} },
    ops() { return this.d.ops || {} },
    s() { return sleeveStatus() },
    der() { return this.s.der || "" },
    held() { return this.s.held },
    enabled() { return this.d.enabled },
    kpi_() { return this.d.kpi || {} },
    wh() { return store.weeklyHealth.data || {} },
    healthCell() {
      const h = this.wh;
      if (store.weeklyHealth.error) return "未知（取数失败）";
      if (!h.level) return "加载中";
      return `${HEALTH_ICN[h.level] || "⚪"} ${h.level}`;
    },
    icCell() {
      const h = this.wh;
      if (h.rolling_ic == null && h.frozen_ic == null) return "--";
      return `${h.rolling_ic ?? "--"}（基线 ${h.frozen_ic ?? "--"}）`;
    },
    cells() {
      const o = this.ops, k = this.kpi_, pl = this.d.plan || {}, ex = pl.expectation || {};
      const held = this.held;
      return [
        ["执行日 / 锚点", `${o.today || "--"} / ${o.next_anchor || "--"}`],
        /* "已持"取实时账本持仓 held（与状态芯片同源），不用方案快照的 n_held 冒充 */
        ["目标 · 已持(实时)", `${pl.n_target ?? "--"} · ${held}`],
        ["计划投入", pl.invested != null ? `¥${fmtNum(pl.invested, 0)} (${fmtPct(pl.invested_ratio, 0)})` : "--"],
        ["袖套净值", k.nav != null ? `¥${fmtNum(k.nav, 0)}` : "尚无成交"],
        ["诚实预期", ex.excess_pp != null ? `+${ex.excess_pp}pp / MDD ${ex.mdd_pct}%` : "--"],
        /* 2026-09-17 用户要求: 袖套 IC/ICIR 上今日总览（数据源 /api/weekly/health） */
        ["健康度", this.healthCell],
        ["滚动 IC（冻结基线）", this.icCell],
      ];
    },
  },
}
</script>
