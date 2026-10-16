<template>
  <div>
    <div class="page-title">LLM 因子审读</div>

    <!-- 自 DataView 拆出（2026-10-06 分类细化）：LLM 研究辅助独立成页。 -->
    <section class="card" id="llmrev">
      <h2>LLM 因子审读 <span class="muted">（研究辅助：经济含义 / 风险红旗 / 冗余提示 —— 判决仍归门禁与回测）</span></h2>
      <AsyncSection :state="rv">
        <template v-if="rvd && rvd.available">
          <div class="muted" style="margin-bottom:8px">审读时间 {{ rvd.generated_at }} ｜ 模型 {{ rvd.model }} ｜ 覆盖 {{ (rvd.reviews || []).length }} 个候选（状态 {{ rvd.scope }}） ｜ 产出脚本：scripts/llm_factor_review.py</div>
          <DataTable :columns="revCols" :rows="rvd.reviews || []" export-name="LLM因子审读" />
        </template>
        <div v-else class="muted">
          尚无审读结果 —— 在配置好 LLM 后运行 <code>python scripts/llm_factor_review.py</code> 生成
          （读 alpha_bank 候选表达式，LLM 逐条输出经济含义与风险红旗，结果落 data/llm/）。
        </div>
      </AsyncSection>
    </section>
  </div>
</template>

<script>
/* LLM 因子审读（2026-10-06 自 DataView 拆出）：离线因子审读产物展示（data/llm/）。
   配置住 .env（LLM_API_KEY 等），设置页可查状态。 */
import { store, ensure } from '../store'
import { esc } from '../format'
import DataTable from '../components/DataTable.vue'
import AsyncSection from '../components/AsyncSection.vue'

export default {
  name: "LlmReviewView",
  components: { DataTable, AsyncSection },
  computed: {
    rv() { return store.llmReview },
    rvd() { return store.llmReview.data },
    revCols() {
      return [
        { key: "name", label: "因子", render: r => `${esc(r.name)} <span class="muted">${esc(r.alpha_id || "")}</span>` },
        { key: "meaning", label: "经济含义", render: r => esc(r.meaning) },
        { key: "flags", label: "风险红旗", render: r => (r.flags || []).length ? `<span class="neg">${r.flags.map(esc).join("；")}</span>` : '<span class="muted">无</span>' },
        { key: "verdict", label: "建议关注", render: r => `<b class="${r.verdict === "high" ? "pos" : r.verdict === "low" ? "muted" : ""}">${esc(r.verdict || "--")}</b>` },
        { key: "note", label: "备注" },
      ];
    },
  },
  async mounted() { ensure("llmReview") },
}
</script>
