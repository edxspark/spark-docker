import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/dashboard' },
    {
      path: '/dashboard',
      name: 'dashboard',
      component: () => import('@/views/DashboardView.vue'),
      meta: { title: '工作台', icon: 'Odometer' },
    },
    {
      path: '/create',
      name: 'create',
      component: () => import('@/views/CreateTaskView.vue'),
      meta: { title: '新建搬运', icon: 'MagicStick' },
    },
    {
      path: '/plans',
      name: 'plans',
      component: () => import('@/views/PlanListView.vue'),
      meta: { title: '搬运计划', icon: 'Calendar' },
    },
    {
      path: '/tasks',
      name: 'tasks',
      component: () => import('@/views/TaskListView.vue'),
      meta: { title: '搬运管理', icon: 'List' },
    },
    {
      path: '/tasks/:id',
      name: 'task-detail',
      component: () => import('@/views/TaskDetailView.vue'),
      meta: { title: '任务详情', hidden: true },
    },
    {
      path: '/history',
      name: 'history',
      component: () => import('@/views/HistoryView.vue'),
      meta: { title: '搬运历史', icon: 'Clock' },
    },
    {
      path: '/douyin',
      name: 'douyin',
      component: () => import('@/views/DouyinView.vue'),
      meta: { title: '抖音账号', icon: 'Promotion' },
    },
    {
      path: '/settings',
      name: 'settings',
      component: () => import('@/views/SettingsView.vue'),
      meta: { title: '系统配置', icon: 'Setting' },
    },
  ],
})

router.afterEach((to) => {
  const title = (to.meta?.title as string) || ''
  document.title = title ? `${title} · Spark 视频搬运工作台` : 'Spark 视频搬运工作台'
})

export default router
