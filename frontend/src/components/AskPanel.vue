<template>
  <teleport to="body">
    <div class="ask-panel" v-if="open" role="dialog" aria-label="问 NEW ECO">
      <div class="ask-head">
        <b>💬 问 NEW ECO</b>
        <span class="muted" style="font-size:var(--fs-1)">{{ llmTag }}</span>
        <span style="flex:1"></span>
        <button class="ghost mini" type="button" @click="$emit('close')">✕</button>
      </div>
      <div class="ask-body" ref="body">
        <div v-for="(m, i) in msgs" :key="i" class="ask-msg" :class="{ q: m.role === 'q' }">
          <button v-if="m.role !== 'q' && m.text.length > 600 && !expanded.has(i)"
                  type="button" class="ghost mini muted" style="font-size:var(--fs-2)"
                  @click="expand(i)">
            ⬇ 回答较长（约 {{ m.text.length }} 字），点击展开全文
          </button>
          <div v-else class="md" style="white-space:pre-wrap;overflow-wrap:anywhere"
               v-html="renderMd(m.text)"></div>
          <div class="ask-src" v-if="m.sources && m.sources.length">来源：{{ m.sources.join("、") }}</div>
          <div class="ask-src" v-if="m.model">模型：{{ m.model }} ｜ LLM 生成，仅供研究参考，不构成投资建议</div>
        </div>
        <div v-if="busy" class="ask-msg thinking">
          <span class="tdots"><i></i><i></i><i></i></span>
          思考中… {{ thinkSec }}s
          <span class="ask-src" style="margin-top:2px">{{ thinkHint }}</span>
        </div>
      </div>
      <div class="ask-foot">
        <div class="chips">
          <button class="ghost mini" v-for="p in presets" :key="p" type="button" @click="ask(p)">{{ p }}</button>
        </div>
        <div style="display:flex;gap:8px">
          <input v-model="draft" type="text" placeholder="输入问题，回车发送…" style="margin-bottom:0"
                 @keydown.enter="ask(draft)">
          <button type="button" @click="ask(draft)" :disabled="busy">{{ busy ? "…" : "发送" }}</button>
        </div>
        <div class="muted" style="font-size:var(--fs-1);margin-top:6px" v-if="llmOk">
          <label style="cursor:pointer;margin-right:14px"><input type="checkbox" v-model="agentOn" style="width:auto;vertical-align:-2px"> 智能体检索（自主查数据，慢但深）</label>
          <label style="cursor:pointer"><input type="checkbox" v-model="researchOn" style="width:auto;vertical-align:-2px"> 研究模式（放开常识推理）</label>
          <span style="margin-left:10px">大模型模式，<a href="#" @click.prevent="useRules = true">切回本地规则</a></span>
        </div>
        <div class="muted" style="font-size:var(--fs-1);margin-top:6px" v-else>
          本地规则模式，秒出；配置 LLM 后可切
          <a href="#" @click.prevent="useRules = false">问大模型</a>
        </div>
      </div>
    </div>
  </teleport>
</template>

<script>
/* 问答面板：默认本地规则引擎（确定性、不外发）；LLM 配置好后可切换到 /api/llm/ask。 */
import { store, ensure, LOADERS } from '../store'
import { answer, PRESETS } from '../intelligence/answers'
import { llmAsk } from '../api'
import { esc } from '../format'

export default {
  name: "AskPanel",
  props: { open: { type: Boolean, default: false } },
  emits: ["close"],
  data() { return { msgs: [], draft: "", busy: false, useRules: false, agentOn: true, researchOn: false,
                   thinkSec: 0, _thinkTimer: null, expanded: new Set(), presets: PRESETS } },
  computed: {
    ls() { return store.llmStatus.data || {} },
    llmOk() { return !!this.ls.configured },
    llmTag() { return this.llmOk ? `LLM 已配置（${this.ls.model || "--"}）` : "本地规则模式（未配置 LLM）" },
    thinkHint() {
      if (this.llmOk && this.agentOn) return "智能体模式：正在自主检索系统数据（通常 10~60 秒）";
      if (this.llmOk) return "大模型生成中";
      return "本地规则检索中";
    },
  },
  mounted() {
    this._handler = (ev) => { store.askOpen = true; this.ask(ev.detail); };
    window.addEventListener("neweco-ask", this._handler);
    /* Esc 关闭（与命令面板同快捷键；palette 输入框的 Esc 带 .stop，不会冒泡到这里） */
    this._esc = (ev) => {
      if (ev.key === "Escape" && store.askOpen) this.$emit("close");
    };
    window.addEventListener("keydown", this._esc);
  },
  beforeUnmount() {
    window.removeEventListener("neweco-ask", this._handler);
    window.removeEventListener("keydown", this._esc);
  },
  watch: {
    open(v) {
      if (v) { ensure("llmStatus"); this.$nextTick(() => this.scrollBottom()); }
    },
    busy(v) {
      if (v) this.scrollBottom();
      else { clearInterval(this._thinkTimer); this._thinkTimer = null; }
    },
  },
  methods: {
    renderMd(text) {
      // 轻量 markdown 渲染: 先整体 esc, 再把白名单符号转标签（只引入自己的标签, 无注入面）
      let s = esc(text);
      s = s.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");          // **加粗**
      s = s.replace(/^#{1,4}\s*(.+)$/gm, "<b>$1</b>");            // ### 标题
      s = s.replace(/^[-*]\s+/gm, "• ");                          // - 列表
      s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");           // `code`
      return s;
    },
    expand(i) { this.expanded = new Set([...this.expanded, i]); },
    scrollBottom() {
      const el = this.$refs.body;
      if (el) el.scrollTop = el.scrollHeight;
    },
    async ask(text) {
      const q = (text || "").trim();
      if (!q || this.busy) return;
      this.draft = "";
      this.thinkSec = 0;
      /* 2026-09-28 审查修复(P2): 计时器清理统一走 finally —— 原清除只挂在 watch busy,
         而本地规则分支从不置 busy ⇒ 每次提问叠加一个永不清除的 setInterval(慢性泄漏,
         切回 LLM 模式后秒数还会 N 倍速乱跳) */
      this._thinkTimer = setInterval(() => (this.thinkSec += 1), 1000);
      try {
        this.msgs.push({ role: "q", text: q });
        this.scrollBottom();
        if (this.llmOk && !this.useRules) {
          this.busy = true;
          try {
            /* 2026-09-18 审查 Q-01：当前问题在上面已经 msgs.push 过了，直接 slice(-6)
               会把它一起带进 history —— 而后端拼的是 [system, *history, user(question)]
               且不去重 ⇒ 同一问题在上下文里出现两遍（双份 token，也违反 f44ee18a
               自钉的"history 不含当前问题"契约）。先摘掉末尾那条，再截 6。
               顺手剔掉失败占位：`LLM 调用失败：xxx` 以 assistant 身份进 history，
               等于让模型下一轮把自己的错误文本当成既成事实继续往下编。 */
            const hist = this.msgs.slice(0, -1).slice(-6)
              .map(m => ({ role: m.role === "q" ? "q" : "a",
                           text: String(m.text || "").replace(/\n\[（智能体检索 \d+ 次）\]$/, "") }))
              .filter(m => !(m.role === "a" && m.text.indexOf("LLM 调用失败：") === 0));
            const r = await llmAsk(q, this.agentOn, this.researchOn, hist);
            const tag = r.agent ? `（智能体检索 ${r.tool_calls} 次）` : "";
            this.msgs.push({ role: "a", text: r.answer + (tag ? `
[${tag}]` : ""), model: r.model });
          } catch (e) {
            this.msgs.push({ role: "a", text: "LLM 调用失败：" + (e.message || e) + "（可切回本地规则模式）" });
          } finally { this.busy = false; }
        } else {
          /* 2026-09-28 审查修复(P2): 规则分支原来裸奔 —— answer() 内部走 computeActions/
             深路径, 任何形状漂移即 TypeError 且无任何显示。与 LLM 分支同款失败占位。 */
          try {
            const r = await answer(q);
            if (r) this.msgs.push({ role: "a", text: r.text, sources: r.sources });
          } catch (e) {
            this.msgs.push({ role: "a", text: "规则引擎出错：" + (e.message || e) });
          }
          /* 问答用到的池子顺手刷新 */
          if (/\d{6}/.test(q)) LOADERS.scorePool();
        }
      } finally {
        if (this._thinkTimer) { clearInterval(this._thinkTimer); this._thinkTimer = null; }
      }
      this.scrollBottom();
    },
  },
}
</script>
