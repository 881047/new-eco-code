<template>
  <div>
    <div class="page-title">周频袖套</div>

    <!-- ========== 周频袖套（原"持仓与交易"视图拆出月频盘后独立成页，2026-10-06 分类细化；
       月频模拟盘见 PaperView）。契约测试钉着本文件的 storyError/拥挤度/row-click/基准尺文案。 ========== -->
    <section class="card" id="weekly">
      <h2>周频袖套 <span class="muted">（独立账本 · 与月频模拟盘物理隔离 · 09:32 执行 / 18:05 兜底 / 19:30 打分保鲜 / 19:45 健康）</span></h2>
      <AsyncSection :state="wk">
        <div class="muted" style="margin-bottom:8px" id="weekly-ops" v-html="opsHtml"></div>
        <div class="muted" style="margin-bottom:10px" id="weekly-health" v-html="healthHtml"></div>
        <KpiGrid :items="weeklyKpis" compact dom-id="weekly-kpis" />
        <div class="muted" style="margin-top:10px" id="crowd-risk" v-if="crowdRisk">
          🌡 候选池拥挤度: <b :class="crowdRisk.hotCount > 10 ? 'neg' : ''">{{ crowdRisk.hotCount }}</b> 只持仓有事件活跃记录
          <template v-if="crowdRisk.top">｜ 最高: {{ crowdRisk.top.ts_code }}（{{ (crowdRisk.top.crowd && typeof crowdRisk.top.crowd === 'object') ? crowdRisk.top.crowd.score : crowdRisk.top.crowd }} 分）</template>
          <b v-if="crowdRisk.nFailed" class="neg">｜ ⚠ {{ crowdRisk.nFailed }} 只取数失败，本卡仅统计剩下的部分</b>
          <span style="opacity:.7">（拥挤=被多家券商推荐/多事件共振/尾盘被追高——历史实证为反向信号, 高分股需留意回落风险）</span>
        </div>
        <div class="grid2" style="margin-top:16px">
          <div style="min-width:0">
            <h3 class="blk-h">持仓</h3>
            <div class="muted" style="margin-bottom:6px" v-if="(wd.positions || []).length">
              持仓 {{ wd.n_positions_total ?? (wd.positions || []).length }} 只<template v-if="wd.n_positions_total > (wd.positions || []).length">（显示前 {{ (wd.positions || []).length }}）</template>
              <template v-if="posSummary"> ｜ 前 {{ posSummary.n }} 只市值 <b>{{ posSummary.market_value }}</b> 元 ｜ 浮动盈亏 <b :class="posSummary.pnl >= 0 ? 'pos' : 'neg'">{{ posSummary.pnl >= 0 ? '+' : '' }}{{ posSummary.pnl }}</b> 元</template>
            </div>
            <DataTable :columns="wposCols" :rows="wd.positions || []" :filterable="false"
                       :max-height="560" export-name="袖套持仓" clickable
                       :empty-text="'暂无持仓（待首个交易日执行）'" @row-click="toggleStory" />
            <div v-if="storyCode" class="card" style="margin-top:8px;padding:12px 16px">
              <div style="display:flex;align-items:center;gap:8px">
                <b>{{ storyCode }} 事件流</b>
                <span class="muted" style="font-size:var(--fs-1)">{{ storyData ? storyData.summary : '' }}</span>
              </div>
              <div v-if="storyError" class="neg" style="padding:4px 0;cursor:pointer"
                   @click="loadStory(storyCode)">{{ storyError }}</div>
              <div v-else-if="!storyData" class="muted" style="padding:4px 0">
                加载中…
              </div>
              <div v-else-if="storyData.missing" class="muted" style="padding:4px 0">
                该股不在事件流返回列表内（接口只覆盖部分持仓）——这不代表它没有事件。
              </div>
              <div v-else-if="!storyData.events.length" class="muted" style="padding:4px 0">
                近 90 天无图谱事件（该股未进入券商/股东/研报事件覆盖）。
              </div>
              <div v-else v-for="(e, i) in storyData.events" :key="i"
                   style="padding:3px 0;font-size:var(--fs-3);border-bottom:1px solid var(--line)">
                <span class="muted">{{ e.date }}</span>
                <span :class="e.direction > 0 ? 'pos' : (e.direction < 0 ? 'neg' : 'muted')"
                      style="font-weight:600">{{ e.type }}</span>
                <span v-if="e.entity">｜{{ e.entity }}</span>
                <span class="muted" v-if="e.quote">｜{{ e.quote }}</span>
              </div>
            </div>
            <div v-if="!storyCode" class="muted" style="padding:6px 0;font-size:var(--fs-2)">
              点击任一持仓行查看其事件流（图谱叙事）。
            </div>
          </div>
          <div style="min-width:0">
            <h3 class="blk-h">
              袖套净值（拖选区间自动算收益<span v-if="weeklyRange">：当前 {{ weeklyRange }}</span>）
            </h3>
            <ChartLine :option="weeklyNavOpt" :height="300" with-range-return @range-return="onWeeklyRange" />
          </div>
        </div>

        <h2 style="margin-top:16px">最新方案 <span class="muted">{{ planDateTxt }}</span></h2>
        <div class="muted" style="margin-bottom:8px;overflow-wrap:anywhere" v-html="expectationHtml"></div>
        <DataTable :columns="wplanCols" :rows="planRows" :filterable="false" export-name="袖套方案"
                   empty-text="暂无方案（先跑 plan）" />

        <h2 style="margin-top:16px">损益归因 <span class="muted">（风格 / 行业 / 选股 / 现金 / 费用 —— 19:45 档刷新）</span></h2>
        <div class="muted" id="weekly-attrib">
          <template v-if="attrib.loaded && !attrib.error">
            <div v-if="!(attrib.data.lines || []).length">归因缓存尚未生成（19:45 档后可看）</div>
            <div v-for="(l, i) in (attrib.data.lines || [])" :key="i">{{ l }}</div>
          </template>
          <span v-else-if="attrib.error">取数失败：{{ attrib.error }}</span>
          <span v-else>加载中…</span>
        </div>

        <h2 style="margin-top:16px">最近成交</h2>
        <div class="muted" style="margin-bottom:6px" v-if="(wd.trades || []).length">
          累计成交 {{ wd.n_trades_total ?? (wd.trades || []).length }} 笔<template v-if="wd.n_trades_total > (wd.trades || []).length">（显示最近 {{ (wd.trades || []).length }} 笔）</template>
        </div>
        <DataTable :columns="wtradeCols" :rows="wd.trades || []" :filterable="false" export-name="袖套成交" empty-text="暂无成交" />
      </AsyncSection>
    </section>
  </div>
</template>

<script>
import { store, ensure } from '../store'
import { esc, fmtPct, fmtNum, stockLink } from '../format'
import { navOption, rangeReturnTxt } from '../charts'
import { opsLine, healthLine, expectationLines } from '../weeklyNarrative'
import KpiGrid from '../components/KpiGrid.vue'
import DataTable from '../components/DataTable.vue'
import ChartLine from '../components/ChartLine.vue'
import AsyncSection from '../components/AsyncSection.vue'

export default {
  name: "TradingView",
  components: { KpiGrid, DataTable, ChartLine, AsyncSection },
  data() { return { weeklyRange: "", storyCode: "", storyData: null, storyError: "" } },
  computed: {
    wk() { return store.weekly },
    wd() { return store.weekly.data || {} },
    attrib() { return store.weeklyAttrib },
    /* 三段叙事 HTML 的实现在 weeklyNarrative.js（2026-10-06 外移为纯函数） */
    opsHtml() { return opsLine(this.wd) },
    healthHtml() { return healthLine(this.wd, store.weeklyHealth) },
    expectationHtml() { return expectationLines(this.wd) },
    weeklyKpis() {
      const k = this.wd.kpi || {};
      return [
        { label: "袖套净值", value: k.nav != null ? "¥" + fmtNum(k.nav, 0) : "--", sub: k.last_mark ? ("盯市 " + esc(k.last_mark)) : "尚无成交" },
        { label: "累计收益", value: k.total_return != null ? fmtPct(k.total_return) : "--", sub: k.ann_return != null ? ("年化 " + fmtPct(k.ann_return)) : "不足 8 周不报年化" },
        { label: "超额", value: k.excess_pp != null ? ((k.excess_pp > 0 ? "+" : "") + k.excess_pp + "pp") : "--", sub: k.bench_ann != null ? ("基准年化 " + fmtPct(k.bench_ann)) : "样本不足" },
        { label: "持仓 / 成交", value: `${k.n_positions ?? 0} 只 / ${k.n_trades ?? 0} 笔`, sub: k.mdd != null ? ("回撤 " + fmtPct(k.mdd)) : (this.wd.enabled ? "" : "未启用") },
      ];
    },
    planRows() { return ((this.wd.plan || {}).rows || []) },
    posSummary() { return this.wd.positions_summary || null },
    crowdRisk() {
      const d = store.kgStory.data;
      if (!d || !d.positions) return null;
      /* 2026-09-28 审查修复(P1): 后端把拥挤分包装成对象 {anchor, score, refs}(app/kg_story.py),
         原代码把对象当数字比较/相减 ⇒ 恒 NaN ⇒ hotCount 恒 0、"最高分"渲染成 [object Object]
         —— 有拥挤风险的池子被渲染成无风险(假绿)。统一读 p.crowd?.score。 */
      const cs = p => (p && p.crowd && typeof p.crowd === "object") ? (p.crowd.score || 0) : (p.crowd || 0);
      const hot = d.positions.filter(p => cs(p) >= 5);
      const top = [...d.positions].sort((a, b) => cs(b) - cs(a))[0];
      /* 2026-09-18 审查 Q-02：取数失败的只数必须带出来。后端逐股失败若被静默吞掉，
         positions 会是空数组 ⇒ hotCount=0 ⇒ 这张卡把"没拿到数据"渲染成"没有拥挤风险"。
         失败与空必须同形可辨。 */
      return { hotCount: hot.length, top: top && cs(top) ? top : null,
               nFailed: d.n_failed || 0 };
    },
    /* 方案头的全部预期标注（实时口径 + 快照溯源 + 多重比较 + 样本外 + 月度） */
    planDateTxt() {
      const p = this.wd.plan;
      if (!p) return "";
      const shown = (p.rows || []).length;
      return `锚点 ${p.anchor} ｜ 构型 top${p.top_n}/带宽${p.band}× ｜ ` +
        `${p.n_target} 只(保留 ${p.n_held}) ｜ 投出 ¥${fmtNum(p.invested, 0)}(${fmtPct(p.invested_ratio, 0)})` +
        ` ｜ 买不起丢弃 ${p.dropped ? p.dropped.unaffordable : 0} 只` +
        (p.n_rows_total > shown ? ` ｜ 表只显示前 ${shown}/${p.n_rows_total} 只` : "");
    },
    /* ---------- 图表 ---------- */
    weeklyNavOpt() {
      const nav = this.wd.nav || [];
      if (!nav.length) return null;
      /* 2026-09-16 修复: navOption 的 Y 轴是百分比格式（为月频 nav_norm×100-100 序列设计），
         喂原始净值(≈101 万)会被渲染成 "+1009280.0%" 乱码轴。改喂「相对首日累计 %」——
         与图题/区间收益算法/月频图语义对齐；绝对净值在上方 KPI 卡展示。 */
      const base = nav[0].nav;
      if (!base) return null;
      return navOption({
        xs: nav.map(x => x.date),
        legendData: ["净值(累计%)"],
        series: [{ name: "净值(累计%)",
                   data: nav.map(x => x.nav == null ? null : +((x.nav / base - 1) * 100).toFixed(2)),
                   color: colors().blue }],
      });
    },
    /* ---------- 表格列 ---------- */
    wposCols() {
      /* 2026-09-17 用户反馈: 补名称与实时盈亏（live_price 盘中优先, 回退 last_px） */
      const pxCell = (r) => {
        if (r.live_price != null) {
          const cls = (r.live_pct_chg ?? 0) >= 0 ? "pos" : "neg";
          const tag = r.live_pct_chg != null ? ` <span class="${cls}">${r.live_pct_chg >= 0 ? "+" : ""}${r.live_pct_chg}%</span>` : "";
          return `${esc(r.live_price)}${tag}`;
        }
        return r.last_px != null ? Number(r.last_px).toFixed(3) : "--";
      };
      return [
        { key: "name", label: "名称", render: r => stockLink(r.ts_code, r.name), sortable: true },
        { key: "shares", label: "股数", num: true, value: r => r.shares, render: r => fmtNum(r.shares, 0) },
        { key: "cost", label: "成本", num: true, value: r => r.cost, render: r => r.cost != null ? Number(r.cost).toFixed(3) : "--" },
        { key: "last_px", label: "最新价", num: true, value: r => r.live_price ?? r.last_px, render: pxCell },
        { key: "pnl", label: "盈亏额", num: true, value: r => r.pnl,
          render: r => r.pnl == null ? "--" : `<span class="${r.pnl >= 0 ? "pos" : "neg"}">${r.pnl >= 0 ? "+" : ""}${fmtNum(r.pnl, 2)}</span>` },
        { key: "pnl_pct", label: "盈亏%", num: true, value: r => r.pnl_pct,
          render: r => r.pnl_pct == null ? "--" : `<span class="${r.pnl_pct >= 0 ? "pos" : "neg"}">${r.pnl_pct >= 0 ? "+" : ""}${r.pnl_pct}%</span>` },
      ];
    },
    wplanCols() {
      return [
        { key: "ts_code", label: "代码", value: r => r.ts_code, render: r => stockLink(r.ts_code, r.name) },
        { key: "name", label: "名称" },
        { key: "rank", label: "排名", num: true, value: r => r.rank },
        { key: "score", label: "分", num: true, value: r => +r.score, render: r => r.score == null ? "--" : Number(r.score).toFixed(4) },
        { key: "shares", label: "股数", num: true, value: r => r.shares },
        { key: "close", label: "收盘", num: true, value: r => r.close },
        { key: "weight", label: "权重", num: true, value: r => r.weight, render: r => fmtPct(r.weight, 1) },
        { key: "held", label: "状态", render: r => r.held ? "保留" : "新入" },
      ];
    },
    wtradeCols() {
      return [
        { key: "date", label: "日期" },
        { key: "side", label: "方向", render: r => `<span class="${r.side === "buy" ? "pos" : "neg"}">${r.side === "buy" ? "买入" : "卖出"}</span>` },
        { key: "ts_code", label: "代码", value: r => r.ts_code, render: r => stockLink(r.ts_code, r.name) },
        { key: "shares", label: "股数", num: true, value: r => r.shares, render: r => fmtNum(r.shares, 0) },
        { key: "price", label: "价格", num: true, value: r => +r.price, render: r => r.price == null ? "--" : Number(r.price).toFixed(3) },
        { key: "fee", label: "费用", num: true, value: r => +r.fee, render: r => r.fee == null ? "--" : Number(r.fee).toFixed(2) },
      ];
    },
  },
  async mounted() {
    await ensure("weekly", "weeklyHealth", "weeklyAttrib", "kgStory");
  },
  methods: {
    toggleStory(row) {
      const code = row && row.ts_code ? row.ts_code : (row && row[0]);
      if (!code) return;
      if (this.storyCode === code) {
        this.storyCode = ""; this.storyData = null; this.storyError = ""; return;
      }
      this.storyCode = code;
      this.storyData = null;
      this.storyError = "";
      const d = store.kgStory.data;
      const pos = d && d.positions ? d.positions.find(x => x.ts_code === code) : null;
      if (pos && pos.events) { this.storyData = pos; return; }
      /* 缓存无 → 懒加载（单股 story 查询） */
      this.loadStory(code);
    },
    loadStory(code) {
      /* 失败保留成"失败"（可点重试），不塌成"无事件" */
      this.storyData = null;
      this.storyError = "";
      import('../kg_story_client').then(m => m.fetchStory(code)).then(s => {
        if (this.storyCode !== code) return;      // 期间用户已切走
        this.storyData = s;
      }).catch(e => {
        if (this.storyCode !== code) return;
        this.storyError = "取数失败：" + (e && e.message ? e.message : e) + "（点此重试）";
      });
    },
    /* dataZoom 拖选 → 显示区间收益（绑定内聚在 ChartLine 的 with-range-return prop；
       COR-03 的"加载后补绑"随生命周期内聚消失：数据到达 → option 变化 → 重渲染时自绑） */
    onWeeklyRange(r) { this.weeklyRange = rangeReturnTxt(r) },
  },
}
</script>
