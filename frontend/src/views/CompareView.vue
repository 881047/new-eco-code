<!-- 跨盘面对比（/compare，总览组；2026-10-08 批6 新增）。
     用户痛点之一是"三盘面没有运行结果对比"。数据全部来自 /api/all_accounts 的
     accounts[]（已含 equity / pct / dd_pct / days / curve / capital / positions / extra）
     ⇒ **零后端改动**。

     风险指标（年化收益 / 年化波动 / 夏普）从各账户的 curve（[[date, 累计%]] 净值序列）
     计算 —— 数据全部来自真实账本，不引入任何硬编码。

     ⚠️ 口径必须如实（本页最容易被误读，故把假设全部写在页面上）：
     1. **无风险利率 r_f 显式可调**，默认 0，页面常驻显示当前取值；
     2. **年化按"实际观测频率"推断**（ppy = 观测数 / 年数），不硬编码 252 ——
        三盘面周期不同（NEW ECO 月频+周频 / Final 日频 / W2 周频），
        硬编码 252 会把周频/月频的波动放大数倍；
     3. **样本不足一律不输出数字**（n < 20 或跨度 < 0.25 年），显示「—（样本不足 n=K）」；
     4. **不设胜率列** —— 三盘面均无平仓对表，禁止用 pct>0 冒充胜率。 -->
<template>
  <div>
    <div class="page-title">跨盘面对比</div>

    <section class="card">
      <h2>三盘面运行结果对比
        <span class="muted">（数据源 /api/all_accounts · 只读聚合）</span>
      </h2>

      <!-- 口径横幅：本页最容易被误读，假设全部前置 -->
      <div class="banner-note" style="margin-bottom:12px">
        <b>口径不一致（务必先读）</b>：
        ① 三盘面周期不同 —— NEW ECO 月频盘 + 周频袖套 / Final 日频 / W2 周频锚定，
        故「年化收益 / 年化波动 / 夏普」的<b>横向可比性有限</b>，只宜看量级不宜比大小；
        ② 年化按<b>实际观测频率</b>推断（ppy = 观测数 ÷ 年数），不是硬编码 252；
        ③ 样本不足（观测数 &lt; {{ minObs }} 或跨度 &lt; {{ minYears }} 年）一律显示
        「—（样本不足）」，<b>宁可空、不可假</b>。
      </div>

      <div class="chips-row" style="margin-bottom:10px">
        <span class="blk-inline">无风险利率 r_f：</span>
        <input type="number" step="0.005" min="-0.05" max="0.2" v-model.number="rf"
               style="width:90px;margin-bottom:0" aria-label="无风险利率（年化）">
        <span class="muted">= {{ (rf * 100).toFixed(2) }}%（夏普分子用此值）</span>
        <span class="spacer" style="flex:1"></span>
        <button class="ghost mini" type="button" @click="byModel = !byModel">
          {{ byModel ? "切换：按账户" : "切换：按盘面聚合" }}
        </button>
      </div>

      <AsyncSection :state="st">
        <DataTable :columns="cols" :rows="rows" :filterable="false"
                   export-name="跨盘面对比" />
      </AsyncSection>

      <p class="muted" style="margin-top:10px">
        「观测 n」= curve 的相邻点收益个数；「跨度」= curve 首末日期之差。
        风险指标 = 由 curve 归一净值算日收益 → 年化收益 (nav末/nav首)^(1/年数)−1、
        年化波动 std(r)×√ppy、夏普 (年化收益 − r_f) ÷ 年化波动。
        三盘面均无平仓对表 ⇒ <b>本页不设胜率列</b>（禁止用累计收益为正冒充胜率）。
      </p>
    </section>
  </div>
</template>

<script>
import { store, ensure } from '../store'
import { money, pctTxt, pctCls } from '../acctCols'
import DataTable from '../components/DataTable.vue'
import AsyncSection from '../components/AsyncSection.vue'

/* 样本阈值：低于任一即不输出风险指标（避免用十几天数据年化出误导性数字） */
const MIN_OBS = 20;
const MIN_YEARS = 0.25;

export default {
  name: "CompareView",
  components: { DataTable, AsyncSection },
  data() {
    return { rf: 0, byModel: false, minObs: MIN_OBS, minYears: MIN_YEARS };
  },
  computed: {
    st() { return store.accounts },
    accounts() { return (store.accounts.data || {}).accounts || [] },
    rows() {
      return this.byModel ? this.modelRows() : this.accountRows();
    },
    cols() {
      return [
        { key: "name", label: this.byModel ? "盘面" : "账户" },
        { key: "model", label: "盘面键" },
        { key: "equity", label: "权益", num: true, render: (r) => "¥" + money(r.equity) },
        { key: "pct", label: "累计%", num: true,
          render: (r) => `<span class="${pctCls(r.pct)}">${pctTxt(r.pct)}</span>` },
        { key: "dd_pct", label: "回撤%", num: true,
          render: (r) => (r.dd_pct == null ? "—" : `<span class="neg">${Number(r.dd_pct).toFixed(2)}%</span>`) },
        { key: "n_pos", label: "持仓", num: true, render: (r) => (r.n_pos == null ? "—" : `${r.n_pos} 只`) },
        { key: "n_obs", label: "观测 n", num: true, render: (r) => (r.n_obs ? r.n_obs : "—") },
        { key: "span", label: "跨度", render: (r) => (r.span_days == null ? "—" : `${Math.round(r.span_days)} 天`) },
        { key: "ppy", label: "推断频率", num: true,
          render: (r) => (r.ppy == null ? "—" : `${r.ppy.toFixed(1)}/年`) },
        { key: "ann_ret", label: "年化收益", num: true,
          render: (r) => (r.ann_ret == null ? this.short(r) : pctTxt(r.ann_ret * 100)) },
        { key: "ann_vol", label: "年化波动", num: true,
          render: (r) => (r.ann_vol == null ? this.short(r) : `${(r.ann_vol * 100).toFixed(2)}%`) },
        { key: "sharpe", label: "夏普", num: true,
          render: (r) => (r.sharpe == null ? this.short(r) : r.sharpe.toFixed(2)) },
        { key: "state", label: "状态", render: (r) => r.state },
      ];
    },
  },
  methods: {
    money, pctTxt, pctCls,
    /* 样本不足时的统一文案（带 n，让用户看到差多少） */
    short(r) {
      if (r.n_obs == null) return "—（无净值序列）";
      return `—（样本不足 n=${r.n_obs}）`;
    },
    _risk(curve) {
      const pts = (curve || []).filter((p) => Array.isArray(p) && p.length >= 2 && p[1] != null);
      if (pts.length < 2) return { n_obs: pts.length, span_days: null };
      const nav = pts.map((p) => 1 + Number(p[1]) / 100);
      const rs = [];
      for (let i = 1; i < nav.length; i++) {
        if (nav[i - 1] > 0) rs.push(nav[i] / nav[i - 1] - 1);
      }
      const n = rs.length;
      const t0 = Date.parse(pts[0][0]);
      const t1 = Date.parse(pts[pts.length - 1][0]);
      if (!isFinite(t0) || !isFinite(t1)) return { n_obs: n, span_days: null };
      const span_days = (t1 - t0) / 86400000;
      const years = span_days / 365.25;
      const base = { n_obs: n, span_days };
      if (n < MIN_OBS || years < MIN_YEARS) return base;   // 不输出指标
      const ppy = n / years;
      const ann_ret = Math.pow(nav[nav.length - 1] / nav[0], 1 / years) - 1;
      const mean = rs.reduce((a, b) => a + b, 0) / n;
      const varr = rs.reduce((a, b) => a + (b - mean) ** 2, 0) / (n - 1);
      const ann_vol = Math.sqrt(varr) * Math.sqrt(ppy);
      return { ...base, ppy, ann_ret, ann_vol,
               sharpe: ann_vol > 0 ? (ann_ret - this.rf) / ann_vol : null };
    },
    /* 状态列：心跳（Final）/ 熔断（W2）/ 数据滞后（通用） */
    _state(a) {
      if (!a.ok) return "读取失败";
      if (a.stale) return "数据滞后";
      const c = (a.extra || {}).circuit || {};
      if (c.halted) return "熔断 halted";
      if (c.derisked) return "降险 derisked";
      if (c.cautious) return "谨慎 cautious";
      const hb = (a.extra || {}).heartbeat;
      if (hb) return `心跳 ${String(hb).slice(0, 16)}`;
      return "正常";
    },
    accountRows() {
      return this.accounts.map((a) => ({
        name: a.name, model: a.model, equity: a.equity, pct: a.pct, dd_pct: a.dd_pct,
        n_pos: (a.positions || []).length, state: this._state(a),
        ...this._risk(a.curve),
      }));
    },
    /* 按盘面聚合：权益求和、累计% 按权益加权、回撤取最差、持仓求和。
       风险指标**不聚合**（各账户周期/起点不同，聚合出的夏普没有意义）⇒ 显示"—（按账户看）"。 */
    modelRows() {
      const byModel = {};
      for (const a of this.accounts) {
        (byModel[a.model] = byModel[a.model] || []).push(a);
      }
      return Object.entries(byModel).map(([model, list]) => {
        const eq = list.reduce((s, a) => s + (a.equity || 0), 0);
        const cap = list.reduce((s, a) => s + (a.capital || 0), 0);
        const dds = list.map((a) => a.dd_pct).filter((v) => v != null);
        const nObs = list.reduce((s, a) => s + (((a.curve || []).length > 1) ? (a.curve.length - 1) : 0), 0);
        return {
          name: model, model, equity: eq,
          pct: cap > 0 ? (eq / cap - 1) * 100 : null,
          dd_pct: dds.length ? Math.max(...dds) : null,
          n_pos: list.reduce((s, a) => s + (a.positions || []).length, 0),
          n_obs: nObs, span_days: null, ppy: null, ann_ret: null, ann_vol: null, sharpe: null,
          state: `${list.length} 账户`,
        };
      });
    },
  },
  async mounted() { await ensure("accounts") },
}
</script>
