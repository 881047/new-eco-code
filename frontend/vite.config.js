import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 构建产物直接进 app/web/static/（FastAPI StaticFiles(html=True) 挂在 "/"）。
// emptyOutDir 清掉旧版 app.js/echarts.min.js/index.html —— 三者都在 git 里，可回溯。
// CSP 是 script-src 'self'：Vite 默认构建不注入内联脚本，构建后 CI 侧会校验这一点。
export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: '../app/web/static',
    // 2026-09-17: **不清空输出目录** —— 清空会删掉旧哈希 chunk, 用户开着旧标签页时
    // 切视图触发懒加载 404 ⇒ 整页卡死("无法正常使用")。保留旧产物: index.html 永远
    // 指向最新哈希, 旧文件只是闲置; 偶尔手动清一次即可。
    emptyOutDir: false,
  },
  server: {
    port: 5173,
    proxy: {
      // 开发模式(vite dev)时把 /api 代理到本机 FastAPI，免 CORS。
      // 2026-09-22：端口不再写死 —— 读同一个 NEWECO_PORT（默认 8787，与
      // app/config.py DEFAULT_SERVER_PORT 一致，由 tests/test_port_single_source.py 钉住）。
      // ⚠️ 本文件**只影响 dev server / 构建期**，不进产物：
      //    `frontend/src/api.js` 全走相对路径 `/api/...`，生产时静态文件由同一个
      //    FastAPI 服务端出 ⇒ **改后端端口不需要重建前端**，只需 `npm run dev` 时带上
      //    同一个环境变量。
      '/api': `http://127.0.0.1:${process.env.NEWECO_PORT || 8787}`,
    },
  },
})
