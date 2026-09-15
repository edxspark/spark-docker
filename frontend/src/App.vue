<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { settingsApi } from '@/api'
import type { RuntimeInfo } from '@/types'

const route = useRoute()
const collapsed = ref(false)
const runtime = ref<RuntimeInfo | null>(null)

const navItems = [
  { path: '/dashboard', title: '工作台', icon: 'Odometer' },
  { path: '/create', title: '新建搬运', icon: 'MagicStick' },
  { path: '/tasks', title: '搬运管理', icon: 'List' },
  { path: '/history', title: '任务历史', icon: 'Clock' },
  { path: '/douyin', title: '抖音账号', icon: 'Promotion' },
  { path: '/settings', title: '系统配置', icon: 'Setting' },
]

const activePath = computed(() => {
  if (route.path.startsWith('/tasks/')) return '/tasks'
  return route.path
})

const envWarnings = computed(() => {
  const list: string[] = []
  if (runtime.value && !runtime.value.ffmpeg?.available) list.push('未检测到 ffmpeg')
  if (runtime.value && runtime.value.yt_dlp && !runtime.value.yt_dlp.available) list.push('未安装 yt-dlp')
  if (runtime.value?.playwright && !runtime.value.playwright.available) list.push('未安装 playwright')
  return list
})

onMounted(async () => {
  try {
    runtime.value = await settingsApi.runtime()
  } catch {
    runtime.value = null
  }
})
</script>

<template>
  <el-container class="app-shell">
    <el-aside :width="collapsed ? '64px' : '220px'" class="app-aside">
      <div class="brand">
        <div class="brand-logo">S</div>
        <transition name="fade">
          <div v-if="!collapsed" class="brand-text">
            <div class="brand-name">Spark 搬运工作台</div>
            <div class="brand-sub">YouTube → 抖音</div>
          </div>
        </transition>
      </div>

      <el-menu :default-active="activePath" :collapse="collapsed" router class="app-menu">
        <el-menu-item v-for="item in navItems" :key="item.path" :index="item.path">
          <el-icon><component :is="item.icon" /></el-icon>
          <template #title>{{ item.title }}</template>
        </el-menu-item>
      </el-menu>

      <div class="aside-footer">
        <el-tooltip v-if="envWarnings.length" :content="envWarnings.join('；')" placement="right">
          <el-tag type="warning" size="small" effect="dark">
            <el-icon><WarningFilled /></el-icon>
            <span v-if="!collapsed" style="margin-left: 4px">环境待完善</span>
          </el-tag>
        </el-tooltip>
      </div>
    </el-aside>

    <el-container>
      <el-header class="app-header">
        <el-button text :icon="collapsed ? 'Expand' : 'Fold'" @click="collapsed = !collapsed" />
        <div class="header-title">{{ route.meta?.title || '' }}</div>
        <div class="header-right">
          <el-tag v-if="runtime?.ffmpeg?.available" size="small" type="success" effect="plain">ffmpeg 就绪</el-tag>
          <el-tag v-else size="small" type="danger" effect="plain">ffmpeg 缺失</el-tag>
          <el-tag size="small" effect="plain">yt-dlp {{ runtime?.yt_dlp?.version || '-' }}</el-tag>
        </div>
      </el-header>

      <el-main class="app-main">
        <router-view v-slot="{ Component }">
          <transition name="fade-page" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.app-shell {
  height: 100vh;
}

.app-aside {
  background: #10141c;
  display: flex;
  flex-direction: column;
  transition: width 0.2s ease;
  overflow: hidden;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 18px 16px 14px;
  color: #fff;
}

.brand-logo {
  width: 32px;
  height: 32px;
  flex: none;
  border-radius: 9px;
  background: linear-gradient(135deg, #3b6ef5, #7f5af0);
  display: grid;
  place-items: center;
  font-weight: 700;
  font-size: 17px;
}

.brand-name {
  font-size: 14px;
  font-weight: 600;
  white-space: nowrap;
}

.brand-sub {
  font-size: 11px;
  color: #8b93a5;
  white-space: nowrap;
}

.app-menu {
  flex: 1;
  border-right: none;
  background: transparent;
  --el-menu-bg-color: transparent;
  --el-menu-text-color: #b6bdcb;
  --el-menu-hover-bg-color: rgba(255, 255, 255, 0.06);
  --el-menu-active-color: #ffffff;
}

.app-menu :deep(.el-menu-item.is-active) {
  background: linear-gradient(90deg, rgba(59, 110, 245, 0.9), rgba(59, 110, 245, 0.35));
  border-radius: 8px;
  margin: 0 8px;
}

.app-menu :deep(.el-menu-item) {
  height: 44px;
  border-radius: 8px;
  margin: 2px 8px;
}

.aside-footer {
  padding: 12px 16px 18px;
}

.app-header {
  background: #fff;
  border-bottom: 1px solid var(--spark-border);
  display: flex;
  align-items: center;
  gap: 12px;
  height: 56px;
}

.header-title {
  font-size: 15px;
  font-weight: 600;
}

.header-right {
  margin-left: auto;
  display: flex;
  gap: 8px;
  align-items: center;
}

.app-main {
  padding: 0;
  background: var(--spark-bg);
  overflow-y: auto;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.18s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

.fade-page-enter-active,
.fade-page-leave-active {
  transition: opacity 0.16s ease, transform 0.16s ease;
}

.fade-page-enter-from {
  opacity: 0;
  transform: translateY(6px);
}

.fade-page-leave-to {
  opacity: 0;
}
</style>
