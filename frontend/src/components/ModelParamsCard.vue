<!-- 盘面参数配置卡（**只读快照**，2026-10-08 批6）。
     用户痛点之一是"三盘面都没有参数配置区"。本卡补齐，但**刻意只读**：
     - 用 .acc-row（标签 + 值）而非 input/select，视觉上就不是可编辑控件；
     - 标题固定带「只读快照 · 源 config.yaml」，NEW ECO 另给"要改去设置页"的链接；
     - 不做写回接口 —— 避免"前端改了后端没生效"的静默分叉（写回只有 /settings 页一条路径）。

     数据来源（三盘面不同，后端已具备，零后端改动）：
     - NEW ECO：`GET /api/settings` 返回的完整 config.yaml（仅 email.auth_code 已打码）
     - Final：账户 `extra.limits`（后端抠出的 4 个风控标量）+ `extra.halt_line`
     - W2：账户 `extra.circuit`（熔断三层）+ `extra.schedule`（排期）
     整组缺失时**保留行**并显示「—（后端未暴露）」—— 隐藏会被误读成"没有风控"。 -->
<template>
  <section class="card" style="margin-top:24px">
    <h2>参数配置
      <span class="muted">（只读快照 · 源 {{ sourceLabel }}；要改请改文件后重启）</span>
    </h2>
    <AsyncSection :state="state">
      <template v-if="groups.length">
        <div v-for="g in groups" :key="g.title">
          <h3 class="blk-h">{{ g.title }}</h3>
          <div class="acc-row" v-for="r in g.rows" :key="r.k" style="max-width:640px">
            <span>{{ r.k }}</span><b>{{ r.v }}</b>
          </div>
          <div class="chips-row" v-if="g.chips && g.chips.length">
            <span v-for="(c, i) in g.chips" :key="i" class="chip">{{ c }}</span>
          </div>
          <p class="muted" style="margin-top:6px" v-if="g.note">{{ g.note }}</p>
        </div>
      </template>
      <p v-else class="muted">参数不可读（后端未暴露该盘面配置）</p>
    </AsyncSection>
    <p class="muted" style="margin-top:10px">
      本卡为只读展示，不接受编辑。NEW ECO 的可改参数在
      <router-link :to="{ name: 'settings' }">设置页</router-link>；
      Final / W2 的参数在各自仓库的 config.yaml。
    </p>
  </section>
</template>

<script>
import { store, ensure } from '../store'
import AsyncSection from './AsyncSection.vue'

/* NEW ECO：config.yaml 的只读摘录（只列有决策含义的键，不铺全表）。
   点号路径支持嵌套（email.enabled）。 */
const NEWECO_GROUPS = [
  { title: "资金与风控", items: [
    ["capital", "本金（元）"], ["risk_profile", "风险档"],
    ["max_stocks", "最大持仓只数"], ["industry_max_weight", "单行业权重上限"],
    ["industry_floor_stocks", "单行业保底只数"], ["single_side_cost", "单边费率"],
    ["hysteresis_buffer", "迟滞缓冲"],
  ] },
  { title: "数据与评估", items: [
    ["data_start", "数据起点"], ["eval_start", "评估起点"],
    ["backtest_run", "回测批次"], ["benchmark", "基准指数"],
    ["bse_enabled", "北交所"], ["star_enabled", "科创板"],
  ] },
  { title: "股票池", items: [
    ["stock_candidate_top", "候选池上限"], ["stock_size_pctl", "市值分位"],
    ["stock_min_amt20", "最小 20 日均额"],
  ] },
  { title: "状态仓位（attack / neutral / defense）", items: [], dict: "state_stocks" },
  { title: "邮件", items: [
    ["email.enabled", "启用"], ["email.smtp_server", "SMTP 服务器"],
    ["email.smtp_port", "端口"], ["email.sender", "发件人"],
  ], note: "授权码已在后端打码（******），本卡不展示。" },
];

const FINAL_LIMIT_CN = {
  max_cash_util: "最大现金使用率", risk_per_trade: "单笔风险",
  daily_stop: "日停阈值", total_dd_halt: "总回撤熔断",
};

function dig(obj, path) {
  return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
}

/* 值格式化：null/undefined → 「—（后端未暴露）」；布尔 → 是/否；数字保留原样 */
function fmt(v) {
  if (v === null || v === undefined || v === "") return "—（后端未暴露）";
  if (typeof v === "boolean") return v ? "是" : "否";
  return String(v);
}

export default {
  name: "ModelParamsCard",
  components: { AsyncSection },
  props: {
    model: { type: String, required: true },
    /* 该盘面的账户（用于 Final/W2 从 extra 取参数） */
    accounts: { type: Array, default: () => [] },
  },
  computed: {
    sourceLabel() {
      return this.model === "NEW ECO" ? "config.yaml" : `${this.model} 账户 extra（本仓只读聚合）`;
    },
    /* settings 域只在 NEW ECO 用；其他盘面不依赖它 */
    state() {
      return this.model === "NEW ECO" ? store.settings : store.accounts;
    },
    groups() {
      if (this.model === "NEW ECO") return this._newecoGroups();
      if (this.model === "Final") return this._finalGroups();
      if (this.model === "W2") return this._w2Groups();
      return [];
    },
  },
  methods: {
    _newecoGroups() {
      const cfg = store.settings.data;
      if (!cfg) return [];
      return NEWECO_GROUPS.map((g) => ({
        title: g.title,
        rows: (g.items || []).map(([path, cn]) => ({ k: cn, v: fmt(dig(cfg, path)) })),
        chips: g.dict
          ? Object.entries(cfg[g.dict] || {}).map(([k, v]) => `${k} = ${fmt(v)}`)
          : [],
        note: g.note || "",
      }));
    },
    _finalGroups() {
      const a = this.accounts.find((x) => x.ok && x.extra) || this.accounts[0] || {};
      const ex = a.extra || {};
      const lim = ex.limits || {};
      const rows = Object.keys(FINAL_LIMIT_CN).map((k) => ({
        k: FINAL_LIMIT_CN[k],
        v: fmt(lim[k]),
      }));
      rows.push({ k: "熔断线（halt_line）", v: ex.halt_line == null ? "—（后端未暴露）" : "¥" + Number(ex.halt_line).toLocaleString("zh-CN") });
      return [{ title: "风控限额（extra.limits）", rows, chips: [], note: "" }];
    },
    _w2Groups() {
      const out = [];
      const accs = this.accounts.filter((x) => x.ok && x.extra);
      const circuits = accs.map((x) => ({ n: x.name, c: x.extra.circuit || {} }))
                           .filter((x) => Object.keys(x.c).length);
      out.push({
        title: "熔断三层（extra.circuit）",
        rows: [],
        chips: circuits.length
          ? circuits.map((x) => `${x.n}：${Object.entries(x.c).map(([k, v]) => `${k}=${v == null ? "—" : v}`).join(" ")}`)
          : [],
        note: circuits.length ? "" : "—（后端未暴露）",
      });
      const sch = (accs.find((x) => (x.extra.schedule || []).length) || {}).extra || {};
      out.push({
        title: "调仓排期（extra.schedule）",
        rows: [],
        chips: (sch.schedule || []).slice(0, 6).map((s) => `${s.iso} → ${s.anchor}`),
        note: (sch.schedule || []).length ? "锚定周调仓：每周最后一个开市日锚定 → 其后首个开市日执行。" : "—（后端未暴露）",
      });
      return out;
    },
  },
  async mounted() {
    /* 只有 NEW ECO 需要 settings 域；ensure 幂等（已加载则直接返回，不会重复请求） */
    if (this.model === "NEW ECO") await ensure("settings");
  },
}
</script>
