<template>
  <div>
    <div class="tbl-tools">
      <input v-if="filterable" v-model="filter" type="text" :placeholder="'筛选 ' + rows.length + ' 行…'">
      <span class="muted" v-if="filter">{{ shown.length }} / {{ rows.length }} 行</span>
      <span style="flex:1"></span>
      <button v-if="exportName" class="ghost mini" @click="exportCsv" type="button">导出 CSV</button>
    </div>
    <div class="scroll-box" :style="maxHeight ? { maxHeight: maxHeight + 'px' } : {}">
      <table>
        <thead>
          <tr>
            <th v-for="c in columns" :key="c.key" :class="{ num: c.num, sortable: c.sortable !== false }"
                @click="toggleSort(c)">
              {{ c.label }}<span v-if="sortKey === c.key">{{ sortAsc ? " ↑" : " ↓" }}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          <!-- 2026-10-08 批7 键盘可达：可点行此前是裸 <tr @click>，无 tabindex/role，
               键盘用户完全够不到（a11y 硬伤）。仅在 clickable 时把行变成 tab 停靠点，
               否则会给只读表格塞进大量无意义的 tab 位。
               ⚠️ @click 必须保持 $emit('row-click', r) —— 契约测试 ⑬ 钉死（逗号表达式会静默失效）。 -->
          <tr v-for="(r, i) in shown" :key="i" v-html="rowHtml(r)"
              :style="clickable ? { cursor: 'pointer' } : null"
              :tabindex="clickable ? 0 : null"
              :role="clickable ? 'button' : null"
              @click="$emit('row-click', r)"
              @keydown.enter="$emit('row-click', r)"
              @keydown.space.prevent="$emit('row-click', r)"></tr>
        </tbody>
      </table>
      <div v-if="!shown.length" class="muted" style="padding:10px 0">{{ emptyText }}</div>
    </div>
  </div>
</template>

<script>
/* 增强表格：点击表头排序 + 关键字筛选 + 导出 CSV（带 BOM，Excel 直开不乱码）。
   单元格由 column.render(row) 产出 HTML 字符串 —— 渲染函数内部必须先过 esc()。 */
import { esc } from '../format'

export default {
  name: "DataTable",
  emits: ['row-click'],
  props: {
    columns: { type: Array, required: true },
    rows: { type: Array, default: () => [] },
    maxHeight: { type: Number, default: 0 },
    exportName: { type: String, default: "" },
    emptyText: { type: String, default: "暂无数据" },
    filterable: { type: Boolean, default: true },
    initialSort: { type: String, default: "" },
    initialAsc: { type: Boolean, default: false },
    clickable: { type: Boolean, default: false },   // 只有真的监听 row-click 的表才显示手型
  },
  data() {
    return { filter: "", sortKey: this.initialSort, sortAsc: this.initialAsc };
  },
  computed: {
    shown() {
      let rows = this.rows;
      const f = this.filter.trim().toLowerCase();
      if (f) {
        rows = rows.filter(r => JSON.stringify(Object.values(r)).toLowerCase().includes(f));
      }
      if (this.sortKey) {
        const col = this.columns.find(c => c.key === this.sortKey);
        const get = col && col.value ? col.value : (r => r[this.sortKey]);
        const asc = this.sortAsc ? 1 : -1;
        rows = [...rows].sort((a, b) => {
          const va = get(a), vb = get(b);
          if (va === null || va === undefined) return 1;
          if (vb === null || vb === undefined) return -1;
          if (typeof va === "number" && typeof vb === "number") return (va - vb) * asc;
          return String(va).localeCompare(String(vb), "zh-CN") * asc;
        });
      }
      return rows;
    },
  },
  methods: {
    toggleSort(c) {
      if (c.sortable === false) return;
      if (this.sortKey === c.key) { this.sortAsc = !this.sortAsc; }
      else { this.sortKey = c.key; this.sortAsc = c.num === true ? false : true; }
    },
    rowHtml(r) {
      return this.columns.map(c => {
        const cls = c.num ? "num" : "";
        const html = c.render ? c.render(r) : esc(r[c.key]);
        return `<td class="${cls}${c.cellClass ? " " + c.cellClass(r) : ""}">${html}</td>`;
      }).join("");
    },
    exportCsv() {
      const cell = (v) => {
        const s = v === null || v === undefined ? "" : String(v);
        return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
      };
      const plain = (r) => this.columns.map(c => c.value ? c.value(r) : r[c.key]);
      const lines = [this.columns.map(c => cell(c.label)).join(","),
        ...this.shown.map(r => plain(r).map(cell).join(","))];
      const blob = new Blob(["\uFEFF" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      /* 2026-09-28: 文件名日期用本地时区(原 toISOString 在北京时间 0-8 点导出会写昨天) */
      const _d = new Date();
      const _d8 = `${_d.getFullYear()}${String(_d.getMonth() + 1).padStart(2, "0")}${String(_d.getDate()).padStart(2, "0")}`;
      a.download = `${this.exportName}_${_d8}.csv`;
      a.click();
      URL.revokeObjectURL(a.href);
    },
  },
}
</script>
