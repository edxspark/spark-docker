<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { taskApi } from '@/api'
import type { Task } from '@/types'
import MetricCard from '@/components/MetricCard.vue'
import StatusTag from '@/components/StatusTag.vue'
import { STATUS_META, elapsedText, formatDateTime, formatRelative, shortUrl } from '@/utils/format'

const router = useRouter()

const loading = ref(false)
const rows = ref<Task[]>([])
const total = ref(0)

const query = reactive({
  status: ['succeeded', 'partial', 'failed', 'canceled'] as string[],
  q: '',
  page: 1,
  page_size: 20,
  range: 'all' as 'today' | '7d' | '30d' | 'all',
})

const statusOptions = Object.entries(STATUS_META).map(([value, meta]) => ({ value, label: meta.label }))

const sourceLabel: Record<string, string> = {
  video: '单个视频',
  playlist: '合集',
  channel: '频道',
}

/** 时间范围只在当前页内过滤，界面上会明确标注，避免与分页语义冲突 */
const visibleRows = computed(() => {
  if (query.range === 'all') return rows.value
  const now = Date.now()
  const span = query.range === 'today' ? 24 * 3600 * 1000 : query.range === '7d' ? 7 * 24 * 3600 * 1000 : 30 * 24 * 3600 * 1000
  return rows.value.filter((row) => {
    const time = new Date(row.created_at).getTime()
    return Number.isFinite(time) && now - time <= span
  })
})

const summary = computed(() => {
  const list = visibleRows.value
  return {
    count: list.length,
    succeeded: list.filter((r) => r.status === 'succeeded').length,
    failed: list.filter((r) => r.status === 'failed').length,
    videos: list.reduce((sum, r) => sum + (r.total_items || 0), 0),
    doneVideos: list.reduce((sum, r) => sum + (r.done_items || 0), 0),
  }
})

/** 当前页记录的完成比例（0 表示没有视频，避免除零） */
const videoRate = computed(() => {
  const { videos, doneVideos } = summary.value
  return videos ? `${Math.round((doneVideos / videos) * 100)}%` : '0%'
})

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

function exportCsv() {
  const list = visibleRows.value
  if (!list.length) {
    ElMessage.warning('当前没有可导出的记录')
    return
  }
  const header = ['任务ID', '标题', '来源链接', '类型', '状态', '成功/总数', '创建时间', '结束时间', '耗时']
  const lines = list.map((row) =>
    [
      row.id,
      row.title || '',
      row.source_url,
      sourceLabel[row.source_type] || row.source_type,
      STATUS_META[row.status]?.label || row.status,
      `${row.done_items}/${row.total_items}`,
      formatDateTime(row.created_at),
      formatDateTime(row.finished_at),
      elapsedText(row.started_at, row.finished_at),
    ]
      .map((cell) => `"${String(cell).replace(/"/g, '""')}"`)
      .join(','),
  )
  const csv = `\uFEFF${[header.join(','), ...lines].join('\n')}`
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `搬运历史_${new Date().toISOString().slice(0, 10)}.csv`
  link.click()
  URL.revokeObjectURL(url)
  ElMessage.success(`已导出当前页 ${list.length} 条记录`)
}

async function retry(row: Task) {
  try {
    await taskApi.retry(row.id, true)
    ElMessage.success('已重新入队执行')
    load()
  } catch {
    /* 拦截器已提示 */
  }
}

onMounted(() => {
  load()
  timer = window.setInterval(load, 20000)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2 class="page-title">任务历史</h2>
        <p class="page-subtitle">回看已结束的搬运记录，检索、复跑已失败的任务，或导出记录做归档。</p>
      </div>
      <el-button :icon="'Download'" @click="exportCsv">导出 CSV（当前页）</el-button>
    </div>

    <div class="panel filter-panel">
      <el-select
        v-model="query.status"
        multiple
        collapse-tags
        collapse-tags-tooltip
        clearable
        placeholder="全部状态"
        style="width: 280px"
        @change="search"
      >
        <el-option v-for="item in statusOptions" :key="item.value" :label="item.label" :value="item.value" />
      </el-select>

      <el-input
        v-model="query.q"
        placeholder="搜索标题、链接或作者"
        clearable
        style="width: 280px"
        @keyup.enter="search"
        @clear="search"
      >
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>

      <el-radio-group v-model="query.range">
        <el-radio-button value="today">今天</el-radio-button>
        <el-radio-button value="7d">近 7 天</el-radio-button>
        <el-radio-button value="30d">近 30 天</el-radio-button>
        <el-radio-button value="all">全部</el-radio-button>
      </el-radio-group>

      <el-button type="primary" :icon="'Search'" @click="search">查询</el-button>
      <el-button :icon="'Refresh'" @click="load">刷新</el-button>
    </div>

    <el-alert
      v-if="query.range !== 'all'"
      type="info"
      :closable="false"
      show-icon
      style="margin-bottom: 16px"
      title="时间范围仅作用于当前页"
      description="分页与总数由后端按所选状态与关键词统计，时间范围是在本页数据上做的二次筛选。要查看更早的记录请翻页或调整状态筛选。"
    />

    <div class="grid-cards stat-grid">
      <MetricCard
        label="当前页记录"
        :value="summary.count"
        :hint="`共 ${total} 条历史任务`"
        icon="Files"
        color="var(--spark-primary)"
      />
      <MetricCard
        label="成功任务"
        :value="summary.succeeded"
        :hint="`当前页 ${summary.count} 条中通过`"
        icon="CircleCheck"
        color="#1f8a4c"
      />
      <MetricCard
        label="失败任务"
        :value="summary.failed"
        :hint="summary.failed ? '可逐条复跑' : '当前页没有失败记录'"
        icon="CircleClose"
        color="#e05c5c"
        :value-colored="summary.failed > 0"
      />
      <MetricCard
        label="视频完成数"
        :value="summary.doneVideos"
        :hint="`共 ${summary.videos} 个视频 · ${videoRate}`"
        icon="VideoCamera"
        color="#7f5af0"
      />
    </div>

    <div class="panel">
      <el-table v-loading="loading" :data="visibleRows" style="width: 100%" row-key="id">
        <el-table-column prop="id" label="ID" width="70" />

        <el-table-column label="任务" min-width="300">
          <template #default="{ row }">
            <el-link type="primary" :underline="false" @click="router.push(`/tasks/${row.id}`)">
              {{ row.title || shortUrl(row.source_url, 40) }}
            </el-link>
            <div class="muted mono cell-sub">{{ shortUrl(row.source_url, 66) }}</div>
          </template>
        </el-table-column>

        <el-table-column label="来源" width="100">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ sourceLabel[row.source_type] || row.source_type }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column label="条目" width="110">
          <template #default="{ row }">
            <span>成功 {{ row.done_items }} / {{ row.total_items }}</span>
            <div v-if="row.failed_items" class="err cell-sub">失败 {{ row.failed_items }}</div>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="106">
          <template #default="{ row }"><StatusTag :status="row.status" /></template>
        </el-table-column>

        <el-table-column label="耗时" width="120">
          <template #default="{ row }">
            <span class="muted">{{ elapsedText(row.started_at, row.finished_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="创建时间" width="150">
          <template #default="{ row }">
            <el-tooltip :content="formatDateTime(row.created_at)" placement="top">
              <span class="muted">{{ formatRelative(row.created_at) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="190" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="row.done_items > 0"
              link
              type="success"
              size="small"
              :icon="'VideoPlay'"
              @click="router.push(`/tasks/${row.id}?preview=1`)"
            >
              看成片
            </el-button>
            <el-button link type="primary" size="small" @click="router.push(`/tasks/${row.id}`)">
              详情
            </el-button>
            <el-button
              v-if="row.failed_items > 0 || ['failed', 'partial', 'canceled'].includes(row.status)"
              link
              type="warning"
              size="small"
              @click="retry(row)"
            >
              复跑
            </el-button>
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

      <el-empty
        v-if="!loading && !visibleRows.length"
        description="没有匹配的历史记录，试试放宽筛选条件"
        :image-size="90"
      />
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

.stat-grid {
  margin-bottom: 16px;
  /* 4 张卡：放得下就一行铺满（1440px 下每张 279px），放不下才换行。
     原先是 repeat(auto-fill, minmax(180px,1fr))——它先切出 6 列再放 4 张卡，
     空列不回收，于是卡片宽度只有 182px、右侧空掉 393px。 */
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
}

.cell-sub {
  font-size: 11.5px;
  margin-top: 2px;
}

.err {
  color: #e05c5c;
  font-size: 11.5px;
}

.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
</style>
