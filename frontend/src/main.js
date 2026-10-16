import { createApp } from 'vue'
import App from './App.vue'
import { router } from './router'
import { boot, startPolling, store } from './store'
import './styles/tokens.css'

/* 恢复本地主题偏好（无记录时跟随系统）——只设 DOM 属性，不回写 localStorage：
   让"跟随系统"在用户明确切换主题前一直生效 */
document.documentElement.dataset.theme = store.theme;

const app = createApp(App);
app.use(router);
app.mount("#app");

/* 全局兜底：任何未捕获异常都进 console 且不拖垮其它区块（旧版"一处抛错打死全页"的教训） */
app.config.errorHandler = (err) => { console.error("全局异常", err); };

boot();
startPolling();
