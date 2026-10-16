<template>
  <div>
    <div class="page-title">设置与运维</div>

    <section class="card" id="cfg">
      <h2>设置</h2>
      <form id="settingsForm" @submit.prevent="save">
        <div class="form-grid">
          <div><label>总资金（元）</label><input id="cfgCapital" type="number" step="10000" v-model="f.capital"></div>
          <div><label>风险偏好</label>
            <select id="cfgRisk" v-model="f.risk_profile">
              <option value="aggressive">进取型</option>
              <option value="balanced">均衡型</option>
              <option value="conservative">稳健型</option>
            </select></div>
          <div><label>股票最大只数</label><input id="cfgMaxStocks" type="number" min="3" max="50" v-model="f.max_stocks"></div>
          <div><label>单行业权重上限（0~1）</label><input id="cfgIndCap" type="number" step="0.05" min="0.1" max="1" v-model="f.industry_max_weight"><span class="muted" style="font-size:var(--fs-2)">实际生效 = max(输入, floor/持仓只数)——floor 未放开时可能高于输入值</span></div>
          <div><label>单边交易成本</label><input id="cfgCost" type="number" step="0.0005" min="0.0005" max="0.01" v-model="f.single_side_cost"></div>
        </div>
        <fieldset>
          <legend>邮件通知（QQ邮箱示例: smtp.qq.com / 465 / 授权码在 设置-账户-开启SMTP 获取）</legend>
          <div class="form-grid">
            <div><label><input type="checkbox" id="cfgMailOn" style="width:auto" v-model="f.mail_on"> 启用邮件推送</label></div>
            <!-- 配置表单不是登录表单：不加 autocomplete 会让浏览器弹"保存密码" -->
            <div><label>SMTP 服务器</label><input id="cfgSmtp" type="text" placeholder="smtp.qq.com" autocomplete="off" v-model="f.smtp"></div>
            <div><label>端口</label><input id="cfgPort" type="number" autocomplete="off" v-model="f.port"></div>
            <div><label>发件邮箱</label><input id="cfgSender" type="text" autocomplete="off" v-model="f.sender"></div>
            <div><label>SMTP 授权码</label><input id="cfgAuth" type="password" placeholder="已保存则不填" autocomplete="new-password" v-model="f.auth"></div>
            <div><label>收件邮箱（多个用逗号分隔）</label><input id="cfgRecip" type="text" autocomplete="off" v-model="f.recip"></div>
          </div>
        </fieldset>
        <fieldset>
          <legend>大模型（LLM，可选）—— key 只存 .env，不写 config.yaml</legend>
          <div class="form-grid">
            <div><label>API Key <span class="muted" style="font-size:var(--fs-2)">{{ ls.configured ? "（已保存，留空则不变）" : "（未设置）" }}</span></label>
              <input id="llmKey" type="password" :placeholder="ls.configured ? '已保存（留空则不变）' : 'sk-...'" autocomplete="new-password" v-model="lf.key"></div>
            <div><label>接口地址（OpenAI 兼容）</label>
              <input id="llmBaseUrl" type="text" placeholder="https://api.deepseek.com" autocomplete="off" v-model="lf.base_url"></div>
            <div><label>模型名</label>
              <input id="llmModel" type="text" placeholder="deepseek-chat" autocomplete="off" v-model="lf.model"></div>
          </div>
          <div style="margin-top:6px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <button type="button" @click="saveLlm">保存 LLM 配置</button>
            <button type="button" class="ghost" @click="testLlm" :disabled="llmTesting">{{ llmTesting ? "测试中…" : "测试连接" }}</button>
            <button type="button" class="ghost" @click="clearLlmKey">清除 Key</button>
            <span class="msg" style="min-height:0">{{ llmMsg }}</span>
          </div>
          <div class="muted" style="margin-top:6px;font-size:var(--fs-2)">
            当前状态：<b>{{ ls.configured ? "已配置" : "未配置" }}</b><template v-if="ls.base_url"> ｜ {{ ls.base_url }} ｜ {{ ls.model }}</template>
            ｜ 接口地址走**主机白名单**（api.deepseek.com / api.openai.com / 127.0.0.1 / localhost；
            本地代理与本地 vLLM 走 127.0.0.1），新增供应商需改 app/web/llm.py 的 _ALLOWED_LLM_HOSTS；
            保存后即时生效，无需重启。
            key 只写 .env（git 忽略），永不回传浏览器。
          </div>
        </fieldset>
        <button type="submit" style="margin-top:12px">保存设置</button>
        <span class="msg" id="settingsMsg">{{ settingsMsg }}</span>
      </form>
    </section>

    <section class="card" id="run" style="margin-top:24px">
      <h2>系统状态与手动运行</h2>
      <div style="margin-top:14px">
        <button id="btnDaily" type="button" @click="runDaily">立即更新数据</button>
        <button id="btnMonthly" type="button" @click="runMonthly">立即运行月度全流程（约30-60分钟）</button>
        <span class="msg" id="runMsg">{{ runMsg }}</span>
      </div>
    </section>
  </div>
</template>

<script>
import { store, ensure, LOADERS } from '../store'
import { saveSettings, runDaily as apiRunDaily, runMonthly as apiRunMonthly, llmSaveConfig, llmTest } from '../api'

export default {
  name: "SettingsView",
  data() {
    return {
      f: { capital: "", risk_profile: "balanced", max_stocks: "", industry_max_weight: "", single_side_cost: "",
           mail_on: false, smtp: "", port: 465, sender: "", auth: "", recip: "" },
      lf: { key: "", base_url: "", model: "" },
      llmMsg: "", llmTesting: false,
      settingsMsg: "", runMsg: "",
    };
  },
  computed: {
    ls() { return store.llmStatus.data || {} },
  },
  async mounted() {
    await ensure("settings", "llmStatus");
    const s = store.settings.data;
    if (s) {
      const e = s.email || {};
      this.f = {
        capital: s.capital, risk_profile: s.risk_profile, max_stocks: s.max_stocks,
        industry_max_weight: s.industry_max_weight, single_side_cost: s.single_side_cost,
        mail_on: !!e.enabled, smtp: e.smtp_server || "", port: e.smtp_port || 465,
        sender: e.sender || "", auth: "", recip: (e.recipients || []).join(","),
      };
      this.$nextTick(() => {
        const el = document.getElementById("cfgAuth");
        if (el) el.placeholder = e.auth_code ? "已保存（留空则不变）" : "未设置";
      });
    }
    const ls = store.llmStatus.data;
    if (ls) {
      this.lf.base_url = ls.base_url || "https://api.deepseek.com";
      this.lf.model = ls.model || "deepseek-chat";
    }
  },
  methods: {
    async saveLlm() {
      const body = { base_url: this.lf.base_url.trim(), model: this.lf.model.trim() };
      if (this.lf.key.trim()) body.api_key = this.lf.key.trim();
      try {
        await llmSaveConfig(body);
        this.lf.key = "";
        this.llmMsg = "已保存，即时生效 ✓";
        LOADERS.llmStatus();
      } catch (e) { this.llmMsg = "保存失败: " + (e.message || e); }
      setTimeout(() => (this.llmMsg = ""), 5000);
    },
    async clearLlmKey() {
      if (!confirm("确认清除已保存的 LLM API Key？（问答将退回本地规则模式）")) return;
      try {
        await llmSaveConfig({ clear_key: true });
        this.llmMsg = "已清除 ✓";
        LOADERS.llmStatus();
      } catch (e) { this.llmMsg = "清除失败: " + (e.message || e); }
      setTimeout(() => (this.llmMsg = ""), 5000);
    },
    async testLlm() {
      this.llmTesting = true;
      this.llmMsg = "测试中…";
      try {
        const r = await llmTest();
        this.llmMsg = `连接正常 ✓ ${r.model}（${r.latency_ms}ms）`;
      } catch (e) { this.llmMsg = "连接失败: " + (e.message || e); }
      this.llmTesting = false;
      setTimeout(() => (this.llmMsg = ""), 8000);
    },
    async save() {
      /* 2026-09-28 审查修复(P2): 空字符串字段不提交 —— 原 +"" 隐式变 0, 输入框误清空
         并保存会把 single_side_cost(后端合法区间含 0)静默存成 0 成本, 直接污染
         paper/回测口径; 其余 0 值虽被后端 400 拦下, 但语义也是"误清空"而非"改 0" */
      const num = v => (String(v).trim() === "" ? undefined : +v);
      const body = {
        capital: num(this.f.capital),
        risk_profile: this.f.risk_profile,
        max_stocks: num(this.f.max_stocks),
        industry_max_weight: num(this.f.industry_max_weight),
        single_side_cost: num(this.f.single_side_cost),
        email: {
          enabled: this.f.mail_on,
          smtp_server: this.f.smtp.trim(),
          smtp_port: num(this.f.port),
          sender: this.f.sender.trim(),
          auth_code: this.f.auth.trim(),
          recipients: this.f.recip.split(/[,，]/).map(s => s.trim()).filter(Boolean),
        },
      };
      for (const k of ["capital", "max_stocks", "industry_max_weight", "single_side_cost"]) {
        if (body[k] === undefined) delete body[k];   // 缺键 ⇒ 后端保持原值
      }
      /* 保存失败必须如实报错（服务端 400 校验拒绝时不得显示"已保存"） */
      try {
        await saveSettings(body);
        this.settingsMsg = "已保存 ✓";
        setTimeout(() => (this.settingsMsg = ""), 3000);
        LOADERS.overview();
      } catch (e) {
        this.settingsMsg = "保存失败: " + (e.message || "网络错误");
      }
    },
    async runDaily() {
      try {
        const r = await apiRunDaily();
        this.runMsg = r.msg || "已启动";
      } catch (e) { this.runMsg = "启动失败: " + (e.message || "网络错误"); }
      setTimeout(() => (this.runMsg = ""), 8000);
    },
    async runMonthly() {
      if (!confirm("月度全流程约需 30-60 分钟（含重训模型），确认运行？")) return;
      try {
        const r = await apiRunMonthly();
        this.runMsg = r.msg || "已启动";
      } catch (e) { this.runMsg = "启动失败: " + (e.message || "网络错误"); }
      setTimeout(() => (this.runMsg = ""), 8000);
    },
  },
}
</script>
