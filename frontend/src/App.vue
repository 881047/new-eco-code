<template>
  <div class="shell">
    <!-- 侧边栏 -->
    <aside class="sidebar">
      <div class="brand">
        <h1>资产看板</h1>
        <span class="badge" :class="state">{{ stateCn }}</span>
      </div>
      <nav class="nav">
        <template v-for="g in navGroups" :key="g.label">
          <span class="nav-group-label">{{ g.label }}</span>
          <router-link v-for="it in g.items" :key="it.name" :to="{ name: it.name }">
            <span class="nav-ico">{{ it.ico }}</span>{{ it.label }}
          </router-link>
        </template>
      </nav>
      <div class="side-foot">
        <span>数据截至 {{ dataDate || "--" }}</span>
        <button class="ghost mini" type="button" @click="toggleTheme" style="width:100%;margin:0">
          {{ store.theme === "dark" ? "☀️ 亮色模式" : "🌙 暗色模式" }}
        </button>
      </div>
    </aside>

    <!-- 主区 -->
    <div class="main">
      <div class="topbar">
        <!-- 主策略状态常驻顶栏：切到哪个视图都看得见 -->
        <span class="sleeve-chip" :class="sleeve.kind" role="status" @click="$router.push({ name: 'sleeve' })"
              style="cursor:pointer" title="周频袖套 · 今日（点击查看详情）">{{ sleeve.text }}</span>
        <span class="muted">周频袖套</span>
        <span class="spacer"></span>
        <button class="ghost mini" type="button" @click="store.askOpen = true" title="本地规则问答；配置 LLM 后可问大模型">💬 问 NEW ECO</button>
        <button class="ghost mini" type="button" @click="store.paletteOpen = true" title="Ctrl+K">🔍 快速跳转 ⌘K</button>
      </div>

      <!-- 取数失败横幅：宁可吵，不许静默空白；点名失败区块 + 自动重试 -->
      <div id="bootWarn" class="banner" v-show="failed.length">
        ⚠ 有 {{ failed.length }} 块没取到（{{ failed.join(" / ") }}）——
        服务可能在重启或数据未就绪；30 秒后自动重试（也可手动刷新页面）。
      </div>

      <div class="content">
        <!-- :key 用 path 而非 fullPath —— 哈希锚点跳转（视图内滚动）不重挂视图 -->
        <router-view :key="$route.path" />
      </div>
      <footer class="foot">本系统仅供个人学习研究使用, 所有内容由模型自动生成, 不构成投资建议。股市有风险, 入市需谨慎。</footer>
    </div>

    <CommandPalette :open="store.paletteOpen" @close="store.paletteOpen = false" />
    <AskPanel :open="store.askOpen" @close="store.askOpen = false" />
  </div>
</template>

<script>
import { store, sleeveStatus, applyTheme } from './store'
import CommandPalette from './components/CommandPalette.vue'
import AskPanel from './components/AskPanel.vue'
import { STATE_CN } from './format'
/* 2026-10-08 批3：侧栏分组改由 nav.js 派生（此前本文件与 router.js /
   CommandPalette.vue / ActionCenter.vue 四处人工维护，漏改一处即静默失效）。 */
import { NAV_GROUPS } from './nav'

export default {
  name: "App",
  components: { CommandPalette, AskPanel },
  data() { return { navGroups: NAV_GROUPS } },
  computed: {
    store() { return store },
    failed() { return store.failed },
    o() { return store.overview.data || {} },
    state() { return this.o.state || "neutral" },
    stateCn() { return STATE_CN[this.state] || this.state },
    dataDate() { return this.o.data_date },
    sleeve() { return sleeveStatus() },
  },
  mounted() {
    /* Ctrl+K / ⌘K 呼出命令面板；Ctrl+/ 呼出问答 */
    window.addEventListener("keydown", (ev) => {
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "k") {
        ev.preventDefault();
        store.paletteOpen = !store.paletteOpen;
      } else if ((ev.ctrlKey || ev.metaKey) && ev.key === "/") {
        ev.preventDefault();
        store.askOpen = !store.askOpen;
      }
    });
  },
  methods: {
    toggleTheme() { applyTheme(store.theme === "dark" ? "light" : "dark"); },
  },
}
</script>
