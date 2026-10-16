<template>
  <div>
    <div class="page-title">打分与因子</div>

    <!-- 模型读数页（2026-10-06 分类细化：原"模型与数据"拆分——LLM 审读 → LlmReviewView，
         系统状态 → StatusView，邮件记录 → NotifyView；本页保留打分榜 + 因子重要性）。 -->
    <div class="grid2">
      <section class="card" id="scores" style="margin-top:0">
        <h2>模型打分榜 Top 30 <span class="muted">{{ scoreDate }}</span></h2>
        <AsyncSection :state="sc">
          <DataTable :columns="scoreCols" :rows="scRows" export-name="打分榜" />
        </AsyncSection>
      </section>
      <section class="card" id="imp" style="margin-top:0">
        <h2>因子重要性（最新一期模型）</h2>
        <AsyncSection :state="im">
          <ChartLine :option="impOpt" :height="440" />
        </AsyncSection>
      </section>
    </div>
  </div>
</template>

<script>
/* 打分与因子（2026-10-06 自原 DataView 拆分）：月度模型打分榜 + 当期因子重要性。
   打分榜的股票代码列全站联动（stockLink → 个股详情页）。 */
import { store, ensure } from '../store'
import { esc, fmtPct, stockLink } from '../format'
import { importanceOption } from '../charts'
import DataTable from '../components/DataTable.vue'
import ChartLine from '../components/ChartLine.vue'
import AsyncSection from '../components/AsyncSection.vue'

export default {
  name: "DataView",
  components: { DataTable, ChartLine, AsyncSection },
  computed: {
    sc() { return store.scores },
    im() { return store.importance },
    scoreDate() { const d = store.scores.data; return d && d.date ? "（" + d.date + "）" : "" },
    scRows() { return (store.scores.data || {}).rows || [] },
    impOpt() {
      const rows = (store.importance.data || {}).rows || [];
      if (!rows.length) return null;
      return importanceOption(rows);
    },
    scoreCols() {
      return [
        { key: "name", label: "名称" },
        { key: "ts_code", label: "代码", value: r => r.ts_code, render: r => stockLink(r.ts_code, r.name) },
        { key: "industry", label: "行业" },
        { key: "score", label: "打分", num: true, value: r => +r.score, render: r => `<b>${esc(r.score)}</b>` },
        { key: "pred_ret", label: "预测超额", num: true, value: r => r.pred_ret, render: r => `<span class="${r.pred_ret >= 0 ? "pos" : "neg"}">${fmtPct(r.pred_ret)}</span>` },
        { key: "pct_chg", label: "今日涨跌", num: true, value: r => r.pct_chg, render: r => `<span class="${(r.pct_chg ?? 0) >= 0 ? "pos" : "neg"}">${r.pct_chg == null ? "--" : r.pct_chg + "%"}</span>` },
        { key: "close", label: "最新价", num: true, value: r => r.close },
      ];
    },
  },
  async mounted() { ensure("scores", "importance") },
}
</script>
