<template>
  <section class="card" id="actions">
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px">
      <h2 style="margin-bottom:0">今日行动 · {{ acts.length }} 项</h2>
      <span class="muted">按 P0&gt;P1&gt;P2 排序，点击跳转对应区块</span>
      <span style="flex:1"></span>
      <button class="ghost mini" type="button" @click="checkup" :disabled="checking">
        {{ checking ? "体检中…" : "⟳ 一键体检" }}
      </button>
    </div>

    <div v-if="!ready" class="muted">数据加载中，行动清单稍候自动出现……</div>
    <template v-else>
      <div v-if="!acts.length" class="muted" style="padding:8px 0">✓ 无待办 — 各项检查通过</div>
      <!-- 2026-10-08 批7 键盘可达：此前是裸 div@click，键盘完全够不到 -->
      <div v-for="(a, i) in acts" :key="i" class="act-item" role="button" tabindex="0"
           @click="jump(a)" @keydown.enter="jump(a)" @keydown.space.prevent="jump(a)">
        <span class="lvl" :class="a.level.toLowerCase()">{{ a.level }}</span>
        <div style="min-width:0">
          <div class="act-text">{{ a.text }}</div>
          <div class="act-sub" v-if="a.sub">{{ a.sub }}</div>
          <div class="act-sub" style="opacity:.7">来源：{{ a.source }}</div>
        </div>
        <span class="act-jump" v-if="a.route">→ {{ routeLabel(a.route) }}</span>
      </div>
    </template>
  </section>
</template>

<script>
import { store } from '../store'
import { computeActions, runCheckup } from '../intelligence/actions'
import { esc } from '../format'
/* 2026-10-08 批3：行动项跳转目标的中文标签改由 nav.js 派生（此前是本文件第三份手写表）。 */
import { ROUTE_CN } from '../nav'

export default {
  name: "ActionCenter",
  data() { return { checking: false } },
  computed: {
    acts() { return computeActions() },
    ready() { return store.bootedOnce },
  },
  methods: {
    async checkup() {
      this.checking = true;
      try { await runCheckup(); } finally { this.checking = false; }
    },
    routeLabel(r) { return ROUTE_CN[r.name] || r.name; },
    jump(a) {
      if (!a.route) return;
      this.$router.push(a.route);
      if (a.route.hash) {
        /* 等视图渲染完再滚到锚点 */
        this.$nextTick(() => setTimeout(() => {
          const el = document.querySelector(a.route.hash);
          if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
        }, 120));
      }
    },
  },
}
</script>
