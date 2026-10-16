<template>
  <teleport to="body">
    <div class="overlay" v-if="open" @click.self="$emit('close')">
      <div class="palette" role="dialog" aria-label="命令面板">
        <input ref="inp" v-model="q" type="text" placeholder="跳转视图 / 区块，或输入 6 位代码、名称查股票…"
               @keydown.down.prevent="move(1)" @keydown.up.prevent="move(-1)"
               @keydown.enter="go" @keydown.esc.stop="$emit('close')">
        <div class="plist">
          <div v-for="(it, i) in items" :key="it.id" class="pitem" :class="{ sel: i === sel }"
               @mouseenter="sel = i" @click="goItem(it)">
            <!-- 2026-10-08 批2：三类标签改用 .chip 修饰符，去掉内联硬编码色
                 （原「股票」标签写死浅绿底+深绿字，暗色下刺眼） -->
            <span class="chip chip--accent" v-if="it.kind === 'view'">视图</span>
            <span class="chip chip--muted" v-else-if="it.kind === 'section'">区块</span>
            <span class="chip chip--ok" v-else>股票</span>
            <span>{{ it.label }}</span>
            <span class="muted" style="font-size:var(--fs-2)" v-if="it.sub">{{ it.sub }}</span>
            <span class="pk">{{ it.kind === 'stock' ? 'ENTER 查看命中' : 'ENTER 跳转' }}</span>
          </div>
          <div v-if="!items.length" class="muted" style="padding:12px 16px">没有匹配项</div>
        </div>
        <div class="muted" style="padding:8px 16px;border-top:1px solid var(--line);font-size:var(--fs-1)">
          ↑↓ 选择 · ENTER 确认 · ESC 关闭 ｜ 股票速查覆盖打分榜前 500 / 七账户持仓 / 方案 / 龙虎榜
        </div>
      </div>
    </div>
  </teleport>
</template>

<script>
/* Ctrl+K 命令面板：视图/区块跳转 + 股票速查（跨打分榜、两账户持仓、方案、龙虎榜、信号）。 */
import { store, ensure } from '../store'
/* 2026-10-08 批3：视图项改由 nav.js 派生（此前是本文件第二份手写表）。 */
import { PALETTE_VIEWS } from '../nav'

let poolLoaded = false;

export default {
  name: "CommandPalette",
  props: { open: { type: Boolean, default: false } },
  emits: ["close"],
  data() { return { q: "", sel: 0 } },
  computed: {
    navItems() {
      /* 2026-10-08 批3：改由 nav.js 的 PALETTE_VIEWS 派生（此前与 App.vue NAV_GROUPS
         各手写一份 15/16 项，两边标签已经出现过不一致）。 */
      return PALETTE_VIEWS;
    },
    sectionItems() {
      /* 各视图内可跳的区块锚点（拆分后多数功能页整页即功能，只留多区块页的锚点） */
      return [
        /* 2026-09-28 审查修复: id 必须与各视图 section 的实际 DOM id 一致 —— 原来带
           `s-` 前缀而 DOM 无前缀, querySelector 全部落空 ⇒ 17 个区块锚点点击"没反应" */
        ["actions", "今日行动中枢", "today"], ["brief", "今日简报", "today"],
        ["signal", "动态信号", "today"],
        ["nav", "回测净值曲线", "plans"], ["stockplan", "当期股票方案", "plans"],
        ["scores", "打分榜", "model"], ["imp", "因子重要性", "model"],
        ["cfg", "设置表单", "settings"],
        ["run", "手动运行", "settings"],
      ].map(([id, label, view]) => ({
        id, kind: "section", label: label + "（" + view + "）",
        route: { name: view, hash: "#" + id },
      }));
    },
    stockItems() {
      const q = this.q.trim().toLowerCase();
      if (!q || q.length < 2) return [];
      const out = [];
      const push = (code, name, where) => {
        if (out.length >= 8) return;
        if (out.some(x => x.code === code)) return;
        out.push({ id: "k-" + code + "-" + where, kind: "stock", code,
                   label: `${code} ${name || ""}`, sub: where, route: null });
      };
      const sc = (store.scorePool.data || {}).rows || [];
      for (const r of sc) {
        if (String(r.ts_code || "").toLowerCase().includes(q) || (r.name || "").toLowerCase().includes(q)) push(r.ts_code, r.name, "打分 " + (r.score ?? "--"));
      }
      const pp = ((store.paper.data || {}).positions || []);
      for (const r of pp) if (String(r.ts_code).toLowerCase().includes(q) || (r.name || "").toLowerCase().includes(q)) push(r.ts_code, r.name, "模拟盘持仓");
      const wp = ((store.weekly.data || {}).positions || []);
      for (const r of wp) if (String(r.ts_code).toLowerCase().includes(q)) push(r.ts_code, "", "袖套持仓");
      const pr = ((store.plan.data || {}).rows || []);
      for (const r of pr) if (String(r.ts_code).toLowerCase().includes(q) || (r.name || "").toLowerCase().includes(q)) push(r.ts_code, r.name, "方案");
      const lh = ((store.lhb.data || {}).rows || []);
      for (const r of lh) if (String(r.ts_code).toLowerCase().includes(q) || (r.name || "").toLowerCase().includes(q)) push(r.ts_code, r.name, "龙虎榜");
      return out;
    },
    items() {
      const q = this.q.trim().toLowerCase();
      const nav = this.navItems.filter(x => !q || x.label.toLowerCase().includes(q));
      const sec = this.sectionItems.filter(x => q && x.label.toLowerCase().includes(q));
      return [...nav, ...sec, ...this.stockItems].slice(0, 20);
    },
  },
  watch: {
    open(v) {
      if (v) {
        this.q = ""; this.sel = 0;
        this.$nextTick(() => this.$refs.inp && this.$refs.inp.focus());
        /* 股票速查首次打开时把大池拉起来（scorePool 独立域，500 只） */
        if (!poolLoaded) { poolLoaded = true; ensure("scorePool", "plan", "weekly"); }
      }
    },
    q() { this.sel = 0; },
  },
  methods: {
    move(d) { this.sel = Math.min(Math.max(this.sel + d, 0), this.items.length - 1); },
    go() { const it = this.items[this.sel]; if (it) this.goItem(it); },
    goItem(it) {
      if (it.kind === "stock") {
        /* 股票命中 → 跳个股详情页（2026-10-05）：详情页里有跨账户持仓/打分/龙虎榜；
           问 LLM 的入口仍在详情页与顶栏「问 NEW ECO」按钮。 */
        this.$emit("close");
        this.$router.push({ name: "stockDetail", params: { code: it.code } });
        return;
      }
      this.$emit("close");
      this.$router.push(it.route);
      if (it.route.hash) {
        this.$nextTick(() => setTimeout(() => {
          const el = document.querySelector(it.route.hash);
          if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
        }, 150));
      }
    },
  },
}
</script>
