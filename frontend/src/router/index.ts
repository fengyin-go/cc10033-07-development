import { createRouter, createWebHistory } from 'vue-router'

import FactIngestView from '@/views/FactIngestView.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 固定路径必须排在 /:moduleKey 之前，否则会被动态路由吞掉。
    { path: '/fact_ingest', name: 'fact_ingest', component: FactIngestView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
