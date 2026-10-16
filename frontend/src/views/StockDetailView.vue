<template>
  <div>
    <div class="page-title">个股详情
      <span class="muted" v-if="d.known">：{{ d.name }}（{{ d.code }} · {{ d.industry || "—" }} · {{ d.market || "—" }}）</span>
      <span class="muted" v-else-if="st.loaded">{{ d.code }}（基础库无此票）</span>
    </div>

    <section class="card">
      <!-- 2026-10-08 批7：三态改用 AsyncSection 统一（此前本页与 TradingView 各手写一份，
           全站共 3 套 loading 呈现）。AsyncSection 的标记与原先手写块**逐字一致**，
           只是把"重试"按钮的显示条件交给 @retry 监听器是否存在。 -->
      <AsyncSection :state="st" @retry="load">
        <!-- 智能读数（纯规则结论，口径见下方各节） -->
        <div class="verdict" :class="d.tone" role="status">{{ d.verdict }}</div>

        <!-- 行情 KPI -->
        <KpiGrid :items="kpiItems" compact dom-id="stock-kpis" />

        <div class="grid2" style="margin-top:16px">
          <div style="min-width:0">
            <h3 class="blk-h">收盘价（近 {{ (d.kline || []).length }} 个交易日 · 拖选缩放）</h3>
            <ChartLine :option="klineOpt" :height="240" />
          </div>
          <div style="min-width:0">
            <h3 class="blk-h">日推分走势（score，全市场分位见读数行）</h3>
            <ChartLine :option="scoreOpt" :height="240" />
          </div>
        </div>

        <h3 class="blk-h">谁在持有（七账户矩阵 · 三个模型）</h3>
        <DataTable :columns="holdCols" :rows="d.holdings || []" :filterable="false"
                   export-name="个股跨账户持仓" empty-text="7 个账户当前均未持有该票" />

        <h3 class="blk-h">成交史（按账户）</h3>
        <template v-if="(d.trades_by_account || []).length">
          <details v-for="t in d.trades_by_account" :key="t.account_id" class="acc-detail">
            <summary>{{ t.account_name }} <span class="muted">{{ t.model }} · {{ (t.trades || []).length }} 笔</span></summary>
            <div class="acc-body">
              <DataTable :columns="acctTradeCols" :rows="t.trades" :filterable="false" />
            </div>
          </details>
        </template>
        <div v-else class="muted">七账户成交史中均无该票（月频盘当日成交另见「今日待办/成交邮件」）</div>

        <h3 class="blk-h">月频盘（NEW ECO 月频账户）</h3>
        <div class="muted" style="margin-bottom:8px" v-if="d.monthly && d.monthly.holding">
          当前持有：<b>{{ d.monthly.holding.shares }}</b> 股 · 成本 {{ d.monthly.holding.avg_cost }}
        </div>
        <div class="muted" style="margin-bottom:8px" v-else>月频盘当前未持有</div>
        <DataTable :columns="monthlyTradeCols" :rows="(d.monthly && d.monthly.trades) || []"
                   :filterable="false" export-name="月频盘成交史" empty-text="月频盘无该票成交史" />

        <h3 class="blk-h">龙虎榜历史</h3>
        <DataTable :columns="lhbCols" :rows="d.lhb || []" :filterable="false"
                   export-name="个股龙虎榜" empty-text="无龙虎榜记录" />

        <h3 class="blk-h">动态信号</h3>
        <DataTable :columns="sigCols" :rows="d.signals || []" :filterable="false"
                   export-name="个股信号" empty-text="无信号记录" />

        <div class="muted" style="margin-top:10px">
          最近更新 {{ updatedAt || "—" }} · 60 秒自动刷新（落账口径，非盘中实时行情）
        </div>
      </AsyncSection>
    </section>
  </div>
</template>

<script>
/* 个股详情页（2026-10-05）：全站股票代码点进来 landing 的地方。
   数据层 /api/stock?code=（app/stock_detail.py，纯只读 + 纯规则 verdict）。
   实时交互感：60s 轮询 + 路由参数变化即刷新；本页无任何写操作。 */
import { setSmartInterval } from '../store'
import { esc, fmtNum, fmtMoney } from '../format'
import { allAccountsOption, colors } from '../charts'
import KpiGrid from '../components/KpiGrid.vue'
import DataTable from '../components/DataTable.vue'
import ChartLine from '../components/ChartLine.vue'
import AsyncSection from '../components/AsyncSection.vue'
import { stockDetail } from '../api'

export default {
  name: "StockDetailView",
  components: { KpiGrid, DataTable, ChartLine, AsyncSection },
  data() {
    return { d: {}, loaded: false, error: null, updatedAt: null, _timer: null };
  },
  computed: {
    st() { return { loaded: this.loaded, error: this.error } },
    kpiItems() {
      const d = this.d || {};
      const l = d.latest || {};
      const b = d.basic_fields || {};
      const items = [
        { label: "最新收盘", value: l.close == null ? "--" : Number(l.close).toFixed(2),
          sub: `${l.trade_date || "—"} · ${l.pct_chg == null ? "" : (l.pct_chg > 0 ? "+" : "") + l.pct_chg + "%"}`,
          cls: l.pct_chg > 0 ? "pos" : l.pct_chg < 0 ? "neg" : "" },
        { label: "换手率", value: b.turnover_rate == null ? "--" : b.turnover_rate.toFixed(2) + "%",
          sub: "daily_basic 最新" },
        { label: "PE-TTM / PB", value: (b.pe_ttm == null ? "--" : b.pe_ttm.toFixed(1)) + " / " + (b.pb == null ? "--" : b.pb.toFixed(2)),
          sub: "亏损股 PE 为空属正常" },
        { label: "总市值", value: b.total_mv == null ? "--" : (b.total_mv / 1e4).toFixed(0) + " 亿",
          sub: b.circ_mv == null ? "" : "流通 " + (b.circ_mv / 1e4).toFixed(0) + " 亿" },
      ];
      if (d.daily_scores && d.daily_scores.length) {
        const s = d.daily_scores[0];
        items.push({
          label: "日推分",
          value: s.score == null ? "--" : Number(s.score).toFixed(1),
          sub: (this.d.score_rank ? `全市场第 ${this.d.score_rank}/${this.d.score_pool} 名 · ` : "")
               + (s.pred_ret == null ? "" : "预期 " + (s.pred_ret * 100).toFixed(2) + "%"),
          cls: s.score >= 90 ? "pos" : "",
        });
      }
      if (d.holdings && d.holdings.length) {
        items.push({ label: "跨账户持仓", value: `${d.holdings.length}/7`,
                     sub: d.holdings.map(h => h.account_name).join("、") });
      }
      return items;
    },
    klineOpt() {
      const k = (this.d.kline || []).slice();
      if (!k.length) return null;
      const c = colors();
      return allAccountsOption({
        series: [{ name: "收盘价", points: k.map(r => [r.date, r.close]), color: c.blue }],
        yFmt: " 元",
      });
    },
    scoreOpt() {
      const s = (this.d.daily_scores || []).slice().reverse();
      if (!s.length) return null;
      const c = colors();
      return allAccountsOption({
        series: [{ name: "日推分", points: s.map(r => [r.trade_date, r.score]), color: c.green }],
        yFmt: " 分",
      });
    },
    holdCols() {
      return [
        { key: "account_name", label: "账户" },
        { key: "model", label: "模型" },
        { key: "shares", label: "持股", num: true, value: r => r.shares, render: r => fmtNum(r.shares, 0) },
        { key: "cost", label: "成本", num: true,
          value: r => r.cost, render: r => (r.cost == null ? "--" : Number(r.cost).toFixed(3)) },
        { key: "last", label: "现价", num: true,
          value: r => r.last, render: r => (r.last == null ? "--" : Number(r.last).toFixed(3)) },
        { key: "value", label: "市值", num: true, value: r => r.value, render: r => fmtMoney(r.value) },
      ];
    },
    acctTradeCols() {
      return [
        { key: "date", label: "日期", render: r => esc(r.date || "—") },
        { key: "side", label: "方向", render: r => esc(r.side || "—") },
        { key: "shares", label: "数量", num: true, value: r => r.shares, render: r => fmtNum(r.shares, 0) },
        { key: "price", label: "价格", num: true,
          value: r => r.price, render: r => (r.price == null ? "--" : Number(r.price).toFixed(3)) },
        { key: "amount", label: "金额", num: true, value: r => r.amount, render: r => fmtMoney(r.amount) },
        { key: "reason", label: "原因", render: r => esc(r.reason || "") },
      ];
    },
    monthlyTradeCols() {
      return [
        { key: "exec_date", label: "日期", render: r => esc(r.exec_date || "—") },
        { key: "side", label: "方向",
          render: r => `<span class="${r.side === "buy" ? "pos" : "neg"}">${r.side === "buy" ? "买入" : "卖出"}</span>` },
        { key: "shares", label: "股数", num: true, value: r => r.shares, render: r => fmtNum(r.shares, 0) },
        { key: "price", label: "价格", num: true,
          value: r => r.price, render: r => (r.price == null ? "--" : Number(r.price).toFixed(3)) },
        { key: "amount", label: "金额", num: true, value: r => r.amount, render: r => fmtMoney(r.amount) },
        { key: "reason", label: "理由", render: r => esc(r.reason || "") },
      ];
    },
    lhbCols() {
      return [
        { key: "trade_date", label: "日期", render: r => esc(r.trade_date || "—") },
        { key: "net_buy_wan", label: "净买额", num: true, value: r => r.net_buy,
          render: r => `<span class="${(r.net_buy || 0) >= 0 ? "pos" : "neg"}">${esc(r.net_buy_wan || "--")}</span>` },
        { key: "lhb_amount", label: "上榜成交", num: true, value: r => r.lhb_amount,
          render: r => fmtMoney(r.lhb_amount == null ? null : r.lhb_amount) },
        { key: "turnover", label: "换手", num: true, value: r => r.turnover,
          render: r => (r.turnover == null ? "--" : Number(r.turnover).toFixed(2) + "%") },
        { key: "reason", label: "上榜原因", render: r => esc(r.reason || "") },
      ];
    },
    sigCols() {
      return [
        { key: "signal_date", label: "日期", render: r => esc(r.signal_date || "—") },
        { key: "action", label: "动作", render: r => esc(r.action || "—") },
        { key: "reason", label: "理由", render: r => esc(r.reason || "") },
      ];
    },
  },
  watch: {
    /* 同一组件被复用（/stock/A → /stock/B）时路由参数变了不会重挂载 ⇒ 显式刷新 */
    "$route.params.code"() { this.load(); },
  },
  async mounted() {
    await this.load();
    /* 实时交互感：60s 轮询（页面隐藏时由 setSmartInterval 自动暂停） */
    this._timer = setSmartInterval(() => this.load(true), 60 * 1000);
  },
  beforeUnmount() {
    if (this._timer) clearInterval(this._timer);
  },
  methods: {
    async load(silent) {
      const code = this.$route.params.code;
      if (!code) return;
      if (!silent) this.loaded = false;
      try {
        this.d = await stockDetail(code);
        this.error = null;
        this.updatedAt = new Date().toLocaleTimeString("zh-CN");
      } catch (e) {
        this.error = String(e && e.message || e);
      } finally {
        this.loaded = true;
      }
    },
  },
}
</script>

<style scoped>
/* 2026-10-08 批1：删除本文件重复定义的 .blk-h —— 全局 tokens.css 已定义同一规则，
   在 scoped 里再写一份会导致"改 tokens.css 不生效"（这正是本轮重构要修的根因之一）。 */
.verdict { border-left: 4px solid var(--accent); background: var(--card-bg);
           padding: var(--s2) var(--s3); border-radius: var(--r-sm); margin-bottom: 14px; font-weight: var(--fw-semibold); }
.verdict.warn { border-left-color: var(--amber); }
.verdict.bad { border-left-color: var(--up); }
.acc-detail { border: 1px solid var(--line); border-radius: var(--r-sm); margin-bottom: 10px; }
.acc-detail summary { cursor: pointer; padding: var(--s2) var(--s3); user-select: none; }
.acc-body { padding: var(--s1) var(--s3) var(--s3); border-top: 1px solid var(--line); }
</style>
