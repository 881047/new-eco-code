<template>
  <div ref="el" :style="{ height: height + 'px' }"></div>
</template>

<script>
/* echarts 封装：初始化/自适应/主题切换重绘。option 由调用方经 charts.js 构造（统一主题色）。 */
import * as echarts from 'echarts/core'
import { LineChart, BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, MarkLineComponent, TitleComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { store } from '../store'
import { rangeReturn } from '../charts'

echarts.use([LineChart, BarChart, GridComponent, TooltipComponent, LegendComponent,
             DataZoomComponent, MarkLineComponent, TitleComponent, CanvasRenderer])

export default {
  name: "ChartLine",
  props: {
    option: { type: Object, default: null },
    height: { type: Number, default: 280 },
    /* with-range-return：开 dataZoom 拖选 → emit('range-return', {start,end,chg}|null)。
       2026-10-06 自 TradingView/PlanView 的手工 bind 内聚到这里 —— 图表位于 v-if 分支内
       的"mounted 时数据未到 ⇒ 绑定静默跳过"（COR-03）随生命周期消失：绑定在每次
       render() 后幂等重建（数据到达 ⇒ option 变化 ⇒ 重渲染 ⇒ 重绑）。 */
    withRangeReturn: { type: Boolean, default: false },
  },
  emits: ["range-return"],
  data() { return { chart: null, ro: null } },
  watch: {
    option: { deep: false, handler() { this.render() } },
    /* 主题切换 → 换配色重绘（charts.js 的构造器读 store.theme） */
    'store.theme'() { this.render(true) },
  },
  mounted() {
    this.chart = echarts.init(this.$refs.el);
    this.ro = new ResizeObserver(() => this.chart && this.chart.resize());
    this.ro.observe(this.$refs.el);
    this.render();
  },
  beforeUnmount() {
    if (this.ro) this.ro.disconnect();
    if (this.chart) this.chart.dispose();
  },
  methods: {
    render(force) {
      if (!this.chart) return;
      if (!this.option) { this.chart.clear(); this._fp = null; return; }
      /* 指纹去重：轮询会整对象替换 option，但 nav 只在 EOD 变 —— 数据没变就不重绘，
         否则用户在图上的缩放/拖选每 15 秒被清一次（旧版 app.js 的 paperNavKey 同法）。
         JSON.stringify 会丢函数(formatter)，但函数从不变化，数据变化仍会改变指纹。 */
      const fp = JSON.stringify(this.option);
      if (!force && fp === this._fp) return;
      this._fp = fp;
      /* ECharts 增量 setOption 不会撤销旧的 dataZoom/series —— 统一直接重设 */
      this.chart.clear();
      this.chart.setOption(this.option);
      if (force) this.chart.resize();
      if (this.withRangeReturn) this._bindZoom();
    },
    _bindZoom() {
      if (!this.chart) return;
      this.chart.off("datazoom").on("datazoom", () => {
        this.$emit("range-return", rangeReturn(this.chart));
      });
    },
  },
  computed: {
    store() { return store },
  },
}
</script>
