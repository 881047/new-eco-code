import { createRouter, createWebHashHistory } from 'vue-router'
import { buildRoutes } from './nav'

/* hash 路由：FastAPI StaticFiles(html=True) 只服务 "/"，history 模式深链会 404；
   hash 模式零服务端配合，且行内锚点（视图内区块跳转）走 route.hash。

   2026-10-06 看板重构：从"NEW-ECO 独属"改为**全账户枢纽**——导航主轴 = 账户归属
   （总览 / NEW ECO / Final / W2 / 运维）。

   2026-10-08 批3：路由表与重定向**全部由 nav.js 派生**（此前本文件与 App.vue /
   CommandPalette.vue / ActionCenter.vue 四处人工维护，漏改一处即页面从某入口静默消失）。
   新增或移动页面只改 nav.js。 */
export const router = createRouter({
  history: createWebHashHistory(),
  routes: buildRoutes(),
  scrollBehavior(to, from, savedPosition) {
    if (to.hash) return { el: to.hash, top: 70, behavior: "smooth" };
    if (savedPosition) return savedPosition;
    return { top: 0 };
  },
})
