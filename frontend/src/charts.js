/* 图表 option 构造器（统一读 store.theme 配色）。净值曲线统一带：
   dataZoom（缩放）+ 十字光标 + 拖选区间自动计算区间涨跌幅。 */
import { store } from './store'

/* 2026-10-08 批2 色板单源化：此前 charts.js 自己维护**第二套色值**（与 tokens.css 双轨），
   暗色靠 `dark ? A : B` 三元手工分支 —— 改 tokens.css 不改这里，图与 UI 就会不同色。
   现改为读 CSS 变量的**实时计算值**：暗色由 html[data-theme] 自动生效，无需分支。
   注意：必须先读一次 store.theme 建立响应式依赖，否则主题切换时调用方的 computed
   不会重算，图表会停留在旧配色（这是本文件此前依赖 store.theme 的真实原因）。 */
export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function colors() {
  void store.theme;   // 建立响应式依赖（主题切换 → 调用方 computed 重算 → 重新读取变量）
  return {
    text: cssVar("--chart-axis"),
    split: cssVar("--chart-split"),
    blue: cssVar("--chart-blue"),
    red: cssVar("--chart-red"),
    gray: cssVar("--chart-gray"),
    amber: cssVar("--chart-amber"),
    green: cssVar("--chart-green"),
    tooltipBg: cssVar("--chart-tooltip-bg"),
  };
}

const baseGrid = { left: 50, right: 20, top: 34, bottom: 56 };

function axis() {
  const c = colors();
  return {
    axisLabel: { fontSize: 10, color: c.text },
    axisLine: { lineStyle: { color: c.split } },
    splitLine: { lineStyle: { color: c.split } },
  };
}

function pctFormatter() {
  return (v) => (typeof v === "number" ? v.toFixed(1) + "%" : v);
}

/* 通用净值对比曲线。rows: [{x, series:[{name, data(已换算成百分比涨幅), color, dashed}]}]
   拖选 dataZoom 后在副标题位显示区间收益（首尾值差）。 */
export function navOption({ xs, series, legendData }) {
  const c = colors();
  const a = axis();
  return {
    tooltip: { trigger: "axis", axisPointer: { type: "cross" }, backgroundColor: c.tooltipBg,
               borderColor: c.split, textStyle: { color: c.text } },
    legend: { data: legendData, top: 0, textStyle: { color: c.text, fontSize: 11 } },
    grid: baseGrid,
    xAxis: { type: "category", data: xs, ...a, axisLabel: { ...a.axisLabel, interval: Math.max(0, Math.floor(xs.length / 12)) } },
    yAxis: { type: "value", scale: true, ...a, axisLabel: { ...a.axisLabel, formatter: pctFormatter() } },
    dataZoom: [
      { type: "inside", start: 0, end: 100 },
      { type: "slider", height: 18, bottom: 8, borderColor: c.split,
        textStyle: { color: c.text, fontSize: 9 } },
    ],
    series: series.map(s => ({
      name: s.name, type: "line", showSymbol: false, data: s.data,
      lineStyle: { width: s.width || 2, color: s.color, type: s.dashed ? "dashed" : "solid" },
      itemStyle: { color: s.color },
    })),
  };
}

/* 区间收益计算（dataZoom 事件回调里用）：返回 {start, end, chg} 或 null */
export function rangeReturn(chart, seriesIndex = 0) {
  if (!chart) return null;
  const opt = chart.getOption();
  if (!opt || !opt.dataZoom || !opt.dataZoom[0]) return null;
  const dz = opt.dataZoom[0];
  const ser = opt.series[seriesIndex];
  if (!ser || !ser.data || !ser.data.length) return null;
  const n = ser.data.length;
  const i0 = Math.floor((dz.start ?? 0) / 100 * (n - 1));
  const i1 = Math.ceil((dz.end ?? 100) / 100 * (n - 1));
  const v0 = ser.data[i0], v1 = ser.data[i1];
  if (typeof v0 !== "number" || typeof v1 !== "number") return null;
  /* 序列本身是"相对首日涨幅%"，区间收益 = (1+v1/100)/(1+v0/100)-1 */
  const chg = ((1 + v1 / 100) / (1 + v0 / 100) - 1) * 100;
  return { start: (opt.xAxis[0].data || [])[i0], end: (opt.xAxis[0].data || [])[i1], chg };
}

/* 区间收益的统一文案（ChartLine @range-return 的消费方共用，2026-10-06 去重） */
export function rangeReturnTxt(r) {
  return r ? `${r.start} ~ ${r.end}：${r.chg >= 0 ? "+" : ""}${r.chg.toFixed(2)}%` : "";
}

/* 风格读数：池子/中证800 相对曲线（基准线 y=1） */
export function styleOption(xs, values) {
  const c = colors();
  const a = axis();
  return {
    tooltip: { trigger: "axis", backgroundColor: c.tooltipBg, borderColor: c.split, textStyle: { color: c.text } },
    grid: { left: 46, right: 16, top: 10, bottom: 24 },
    xAxis: { type: "category", data: xs, ...a },
    yAxis: { type: "value", scale: true, ...a, axisLabel: { ...a.axisLabel, formatter: (v) => ((v - 1) * 100).toFixed(0) + "%" } },
    series: [{
      name: "池子 / 中证800", type: "line", showSymbol: false, data: values,
      lineStyle: { width: 2, color: c.blue }, itemStyle: { color: c.blue },
      markLine: { silent: true, symbol: "none", data: [{ yAxis: 1 }],
                  lineStyle: { color: c.gray, type: "dashed" }, label: { show: false } },
    }],
  };
}

/* 因子重要性横向条形图 */
export function importanceOption(rows) {
  const c = colors();
  const a = axis();
  const rev = [...rows].reverse();
  return {
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, backgroundColor: c.tooltipBg, borderColor: c.split, textStyle: { color: c.text } },
    grid: { left: 90, right: 40, top: 10, bottom: 24 },
    xAxis: { type: "value", ...a, axisLabel: { ...a.axisLabel, formatter: (v) => (v * 100).toFixed(1) + "%" } },
    yAxis: { type: "category", data: rev.map(r => r.factor), ...a },
    series: [{ type: "bar", data: rev.map(r => +(r.importance * 100).toFixed(2)),
               itemStyle: { color: c.blue }, barMaxWidth: 18 }],
  };
}

/* 全账户聚合曲线（2026-10-05，自 D:\account-board 迁入）：各账户以各自本金归一、
   日期范围不同 ⇒ 用 time 轴而不是 navOption 的 category 轴。
   series: [{name, points: [[date, pct], ...], color, dashed}]，points 已是百分比涨幅。 */
/* yFmt（可选）：Y 轴后缀。缺省 "%"；个股详情收盘价传 " 元"、日推分传 " 分"，
   否则 Y 轴会把价格误标成百分比（2026-10-05 个股详情页实测）。 */
export function allAccountsOption({ series, yFmt = "%" }) {
  const c = colors();
  const a = axis();
  return {
    tooltip: { trigger: "axis", axisPointer: { type: "cross" }, backgroundColor: c.tooltipBg,
               borderColor: c.split, textStyle: { color: c.text },
               valueFormatter: (v) => (typeof v === "number" ? v.toFixed(2) + (yFmt || "") : v) },
    legend: { data: series.map(s => s.name), top: 0, textStyle: { color: c.text, fontSize: 11 } },
    grid: baseGrid,
    xAxis: { type: "time", ...a },
    yAxis: { type: "value", scale: true, ...a,
             axisLabel: { ...a.axisLabel,
                          formatter: yFmt === "%" ? pctFormatter()
                            : (v) => (typeof v === "number" ? v.toFixed(2) + yFmt : v) } },
    dataZoom: [
      { type: "inside", start: 0, end: 100 },
      { type: "slider", height: 18, bottom: 8, borderColor: c.split,
        textStyle: { color: c.text, fontSize: 9 } },
    ],
    series: series.map(s => ({
      name: s.name, type: "line", showSymbol: s.points.length < 8, symbolSize: 5,
      data: s.points, connectNulls: true,
      lineStyle: { width: s.dashed ? 1.2 : 2, color: s.color,
                   type: s.dashed ? "dashed" : "solid", opacity: s.dashed ? 0.85 : 1 },
      itemStyle: { color: s.color },
    })),
  };
}
