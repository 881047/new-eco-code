<template>
  <div v-if="!state.loaded" class="muted">加载中…</div>
  <div v-else-if="state.error" class="muted">
    取数失败：{{ state.error }}
    <button v-if="retryable" class="ghost mini" type="button" style="margin-left:8px"
            @click="$emit('retry')">重试</button>
  </div>
  <template v-else><slot /></template>
</template>

<script>
/* 「加载中 / 取数失败(可重试) / 正文」三态的统一包装（2026-10-06 去重：
   五个视图 13 处同构 v-if/v-else-if 块收敛到这里；渲染结构与原手写块逐字一致）。
   state = store 的 domain 对象（{ data, error, loaded }）；传 @retry 才显示重试按钮
   （未声明 emits ⇒ 监听器留在 $attrs，用它的存在性判断 retryable）。 */
export default {
  name: "AsyncSection",
  props: { state: { type: Object, required: true } },
  computed: {
    retryable() { return typeof this.$attrs.onRetry === "function" },
  },
}
</script>
