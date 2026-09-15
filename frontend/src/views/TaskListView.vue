<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { taskApi } from '@/api'
import type { Task } from '@/types'
import StatusTag from '@/components/StatusTag.vue'
import StageProgress from '@/components/StageProgress.vue'
import { STATUS_META, elapsedText, formatDateTime, formatRelative, shortUrl } from '@/utils/format'

const router = useRouter()

const loading = ref(false)
const rows = ref<Task[]>([])
const total = ref(0)

const query = reactive({
  status: [] as string[],
  q: '',
  page: 1,
  page_size: 20,
})

const statusOptions = Object.entries(STATUS_META).map(([value, meta]) => ({
  value,
  label: meta.label,
}))

const sourceLabel: Record<string, string> = {
  video: '单个视频',
  playlist: '合集',
  channel: '频道',
}

const hasActive = computed(() => rows.value.some((row) => row.status === 'running' || row.status === 'pending'))
let timer: number | null = null

async function load() {
  loading.value = true
  try {
    const data = await taskApi.list({
      status: query.status.length ? query.status.join(',') : undefined,
      q: query.q.trim() || undefined,
      page: query.page,
      page_size: query.page_size,
    })
    rows.value = data.items
    total.value = data.total
  } catch {
    rows.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function search() {
  query.page = 1
  load()
}

function resetFilters() {
  query.status = []
  query.q = ''
  query.page = 1
  load()
}

function open(row: Task) {
  router.push(`/tasks/${row.id}`)
}

async function cancel(row: Task) {
  try {
    const result = await taskApi.cancel(row.id)
    ElMessage.success(result.message)
    load()
  } catch {
    /* 拦截器已提示 */
  }
}

async function retry(row: Task) {
  try {
    await taskApi.retry(row.id, true)
    ElMessage.success('已重新入队，正在继续执行（已完成的阶段会自动跳过）')
    load()
  } catch {
    /* 拦截器已提示 */
  }
}

async function remove(row: Task) {
  try {
    await ElMessageBox.confirm(
      `确定删除任务 #${row.id}「${row.title || shortUrl(row.source_url, 40)}」吗？`,
      '删除任务',
      { type: 'warning', confirmButtonText: '删除记录', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  let removeFiles = false
  try {
    await ElMessageBox.confirm(
      '是否同时删除磁盘上的下载文件、成片与字幕？此操作不可恢复。',
      '同时清理文件？',
      { type: 'warning', confirmButtonText: '同时删除文件', cancelButtonText: '仅删除记录' },
    )
    removeFiles = true
  } catch {
    removeFiles = false
  }
  try {
    const result = await taskApi.remove(row.id, removeFiles)
    ElMessage.success(result.message)
    load()
  } catch {
    /* 拦截器已提示 */
  }
}

onMounted(() => {
  load()
  timer = window.setInterval(() => {
    if (hasActive.value) load()
  }, 5000)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2 class="page-title">搬运管理</h2>
        <p class="page-subtitle">查看所有任务进度、重试失败项、管理产物与发布结果。</p>
      </div>
      <el-button type="primary" :icon="'Plus'" @click="router.push('/create')">新建搬运任务</el-button>
    </div>

    <div class="panel filter-panel">
      <el-select
        v-model="query.status"
        multiple
        collapse-tags
        collapse-tags-tooltip
        clearable
        placeholder="全部状态"
        style="width: 260px"
        @change="search"
      >
        <el-option v-for="item in statusOptions" :key="item.value" :label="item.label" :value="item.value" />
      </el-select>

      <el-input
        v-model="query.q"
        placeholder="搜索标题、链接或作者"
        clearable
        style="width: 300px"
        @keyup.enter="search"
        @clear="search"
      >
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>

      <el-button type="primary" :icon="'Search'" @click="search">查询</el-button>
      <el-button :icon="'RefreshLeft'" @click="resetFilters">重置</el-button>

      <div class="filter-right">
        <el-tag v-if="hasActive" type="primary" effect="plain" size="small">
          <el-icon class="is-loading"><Loading /></el-icon> 有任务执行中，自动刷新
        </el-tag>
        <el-button text :icon="'Refresh'" @click="load">刷新</el-button>
      </div>
    </div>

    <div class="panel">
      <el-table
        v-loading="loading"
        :data="rows"
        style="width: 100%"
        row-key="id"
        @row-click="open"
      >
        <el-table-column prop="id" label="ID" width="70" />

        <el-table-column label="任务" min-width="300">
          <template #default="{ row }">
            <div class="cell-title">{{ row.title || '未命名任务' }}</div>
            <div class="muted mono cell-sub">{{ shortUrl(row.source_url, 66) }}</div>
          </template>
        </el-table-column>

        <el-table-column label="来源" width="100">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ sourceLabel[row.source_type] || row.source_type }}</el-tag>
            <div class="muted cell-sub">{{ row.author || '-' }}</div>
          </template>
        </el-table-column>

        <el-table-column label="当前阶段" width="180">
          <template #default="{ row }">
            <StageProgress v-if="row.status === 'running'" :stage="row.stage" :status="row.status" compact />
            <span v-else class="muted">-</span>
            <div class="muted cell-sub ellipsis">{{ row.message || '' }}</div>
          </template>
        </el-table-column>

        <el-table-column label="进度" width="170">
          <template #default="{ row }">
            <el-progress
              :percentage="Math.round(row.progress || 0)"
              :stroke-width="8"
              :status="row.status === 'failed' ? 'exception' : row.status === 'succeeded' ? 'success' : undefined"
            />
            <div class="muted cell-sub">
              {{ row.done_items }}/{{ row.total_items }} 成功
              <span v-if="row.failed_items">· 失败 {{ row.failed_items }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="106">
          <template #default="{ row }"><StatusTag :status="row.status" /></template>
        </el-table-column>

        <el-table-column label="耗时" width="110">
          <template #default="{ row }">
            <span class="muted">{{ elapsedText(row.started_at, row.finished_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="创建时间" width="130">
          <template #default="{ row }">
            <el-tooltip :content="formatDateTime(row.created_at)" placement="top">
              <span class="muted">{{ formatRelative(row.created_at) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="196" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click.stop="open(row)">详情</el-button>
            <el-button
              v-if="row.status === 'pending' || row.status === 'running'"
              link
              type="warning"
              size="small"
              @click.stop="cancel(row)"
            >
              取消
            </el-button>
            <el-button
              v-else-if="row.status !== 'succeeded'"
              link
              type="primary"
              size="small"
              @click.stop="retry(row)"
            >
              重试
            </el-button>
            <el-button link type="danger" size="small" @click.stop="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pager">
        <el-pagination
          v-model:current-page="query.page"
          v-model:page-size="query.page_size"
          :page-sizes="[10, 20, 50, 100]"
          :total="total"
          layout="total, sizes, prev, pager, next, jumper"
          background
          @current-change="load"
          @size-change="search"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.filter-panel {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 16px;
  padding: 14px 18px;
}

.filter-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 10px;
}

.cell-title {
  font-weight: 500;
}

.cell-sub {
  font-size: 11.5px;
  margin-top: 2px;
}

.ellipsis {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 168px;
}

.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}

:deep(.el-table__row) {
  cursor: pointer;
}
</style>
