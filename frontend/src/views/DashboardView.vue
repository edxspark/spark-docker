<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as echarts from 'echarts'
import { settingsApi, statsApi, taskApi } from '@/api'
import type { RuntimeInfo, StatsOverview, Task } from '@/types'
import StatusTag from '@/components/StatusTag.vue'
import {
  formatDateTime,
  formatNumber,
  formatRelative,
  formatSize,
  shortUrl,
} from '@/utils/format'

const router = useRouter()
const loading = ref(true)
const overview = ref<StatsOverview | null>(null)
const runtime = ref<RuntimeInfo | null>(null)

const cancelingAll = ref(false)
const quickUrl = ref('')
const quickProbing = ref(false)
const quickCreating = ref(false)
const quickResult = ref<{
  source_type: string
  title: string
  author: string
  total: number
} | null>(null)

const chartEl = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
let timer: number | null = null

const cards = computed(() => {
  const data = overview.value
  return [
    {
      key: 'total',
      label: '任务总数',
      value: formatNumber(data?.total_tasks),
      hint: `执行中 ${data?.running_tasks ?? 0}`,
      icon: 'Files',
      color: '#3b6ef5',
    },
    {
      key: 'succeeded',
      label: '成功任务',
      value: formatNumber(data?.succeeded_tasks),
      hint: `失败 ${data?.failed_tasks ?? 0}`,
      icon: 'CircleCheck',
      color: '#1f8a4c',
    },
    {
      key: 'videos',
      label: '处理视频',
      value: formatNumber(data?.total_videos),
      hint: `已发布 ${data?.published_videos ?? 0}`,
      icon: 'VideoCamera',
      color: '#7f5af0',
    },
    {
      key: 'sentences',
      label: '翻译句数',
      value: formatNumber(data?.total_sentences),
      hint: `${formatNumber(data?.total_tokens)} tokens`,
      icon: 'ChatLineSquare',
      color: '#e08a1e',
    },
    {
      key: 'characters',
      label: '配音字符',
      value: formatNumber(data?.total_characters),
      hint: '阿里云语音',
      icon: 'Microphone',
      color: '#0f9ba8',
    },
    {
      key: 'disk',
      label: '数据占用',
      value: formatSize(data?.disk_usage_mb),
      hint: '含下载与成片',
      icon: 'Coin',
      color: '#8a94a6',
    },
  ]
})

const envItems = computed(() => {
  const info = runtime.value
  if (!info) return []
  return [
    { label: 'ffmpeg', ok: !!info.ffmpeg?.available, tip: info.ffmpeg?.path || '未检测到，请执行 brew install ffmpeg' },
    { label: 'ffprobe', ok: !!info.ffprobe?.available, tip: info.ffprobe?.path || '未检测到' },
    { label: 'yt-dlp', ok: !!info.yt_dlp?.available, tip: info.yt_dlp?.version || '未安装' },
    { label: 'playwright', ok: !!info.playwright?.available, tip: info.playwright?.available ? '已安装（发布需要）' : '未安装，无法真实发布到抖音' },
  ]
})

const blockers = computed(() => envItems.value.filter((item) => !item.ok))

async function load() {
  loading.value = true
  try {
    const [stats, rt] = await Promise.all([
      statsApi.overview(),
      settingsApi.runtime().catch(() => null),
    ])
    overview.value = stats
    runtime.value = rt
  } finally {
    loading.value = false
  }
  renderChart()
}

function renderChart() {
  const daily = overview.value?.daily || []
  if (!chartEl.value) return
  if (!chart) {
    chart = echarts.init(chartEl.value)
  }
  chart.setOption({
    grid: { left: 40, right: 18, top: 32, bottom: 28 },
    tooltip: { trigger: 'axis' },
    legend: { data: ['新建', '成功', '失败'], right: 0, top: 0, itemWidth: 12, itemHeight: 8, textStyle: { fontSize: 12 } },
    xAxis: {
      type: 'category',
      data: daily.map((d) => d.date.slice(5)),
      axisLine: { lineStyle: { color: '#dcdfe6' } },
      axisLabel: { color: '#8a94a6', fontSize: 11 },
    },
    yAxis: {
      type: 'value',
      minInterval: 1,
      splitLine: { lineStyle: { color: '#f0f2f5' } },
      axisLabel: { color: '#8a94a6', fontSize: 11 },
    },
    series: [
      {
        name: '新建',
        type: 'line',
        smooth: true,
        symbolSize: 6,
        data: daily.map((d) => d.created),
        itemStyle: { color: '#3b6ef5' },
        areaStyle: { color: 'rgba(59,110,245,0.10)' },
      },
      {
        name: '成功',
        type: 'line',
        smooth: true,
        symbolSize: 6,
        data: daily.map((d) => d.succeeded),
        itemStyle: { color: '#1f8a4c' },
      },
      {
        name: '失败',
        type: 'line',
        smooth: true,
        symbolSize: 6,
        data: daily.map((d) => d.failed),
        itemStyle: { color: '#e05c5c' },
      },
    ],
  })
}

function resizeChart() {
  chart?.resize()
}

async function handleCancelAll() {
  let targets: Task[] = []
  let totalActive = 0
  try {
    const data = await taskApi.list({ status: 'pending,running,paused', page: 1, page_size: 100 })
    targets = data.items
    totalActive = data.total
  } catch {
    return
  }
  if (!totalActive) {
    ElMessage.info('当前没有需要取消的任务')
    await load()
    return
  }
  const running = targets.filter((t) => t.status === 'running').length
  const preview = targets
    .slice(0, 5)
    .map((t) => `· #${t.id} ${t.title || shortUrl(t.source_url, 28)}`)
    .join('\n')
  const more = totalActive > 5 ? `\n… 另有 ${totalActive - 5} 个` : ''
  try {
    await ElMessageBox.confirm(
      `将取消 ${totalActive} 个未结束的任务${running ? `（其中 ${running} 个正在执行）` : ''}：\n\n${preview}${more}\n\n` +
        '正在执行的任务会在当前阶段结束后停止；已完成的成片与字幕不会被删除。',
      '取消所有任务',
      { type: 'warning', confirmButtonText: '全部取消', cancelButtonText: '返回', customClass: 'cancel-all-confirm' },
    )
  } catch {
    return
  }
  cancelingAll.value = true
  try {
    const result = await taskApi.cancelAll(true)
    ElMessage.success(result.message)
    await load()
  } catch {
    /* 拦截器已提示 */
  } finally {
    cancelingAll.value = false
  }
}

async function handleProbe() {
  const url = quickUrl.value.trim()
  if (!url) {
    ElMessage.warning('请先粘贴 YouTube 视频或合集链接')
    return
  }
  quickProbing.value = true
  quickResult.value = null
  try {
    const result = await taskApi.probe(url)
    quickResult.value = {
      source_type: result.source_type,
      title: result.title,
      author: result.author,
      total: result.total,
    }
    ElMessage.success(`解析成功：${result.total} 个视频`)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    quickProbing.value = false
  }
}

async function handleQuickCreate() {
  const url = quickUrl.value.trim()
  if (!url) return
  quickCreating.value = true
  try {
    const task = await taskApi.create({ url, auto_start: true })
    ElMessage.success(`任务已创建（${task.total_items} 个视频），正在后台执行`)
    quickUrl.value = ''
    quickResult.value = null
    router.push(`/tasks/${task.id}`)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    quickCreating.value = false
  }
}

watch(() => overview.value?.daily, renderChart)

onMounted(() => {
  load().catch(() => undefined)
  window.addEventListener('resize', resizeChart)
  // 工作台有执行中任务时自动刷新
  timer = window.setInterval(() => {
    if ((overview.value?.running_tasks ?? 0) > 0) {
      load().catch(() => undefined)
    }
  }, 15000)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resizeChart)
  if (timer) window.clearInterval(timer)
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="page-header">
      <div>
        <h2 class="page-title">工作台</h2>
        <p class="page-subtitle">
          YouTube 视频 → 下载 → 翻译 → 配音 → 发布抖音，全流程自动化。粘贴链接即可开始。
        </p>
      </div>
      <div class="head-actions">
        <el-button
          v-if="(overview?.running_tasks ?? 0) > 0"
          :icon="'CircleClose'"
          :loading="cancelingAll"
          @click="handleCancelAll"
        >
          全部取消（{{ overview?.running_tasks }}）
        </el-button>
        <el-button type="primary" :icon="'Plus'" @click="router.push('/create')">新建搬运任务</el-button>
      </div>
    </div>

    <!-- 快捷开始 -->
    <div class="panel quick-panel">
      <div class="quick-row">
        <el-input
          v-model="quickUrl"
          size="large"
          clearable
          placeholder="粘贴 YouTube 视频链接或合集/频道链接，例如 https://www.youtube.com/watch?v=xxx 或 https://www.youtube.com/playlist?list=xxx"
          @keyup.enter="handleProbe"
        >
          <template #prepend>
            <el-icon><Link /></el-icon>
          </template>
        </el-input>
        <el-button size="large" :loading="quickProbing" @click="handleProbe">解析</el-button>
        <el-button size="large" type="primary" :loading="quickCreating" @click="handleQuickCreate">
          一键搬运
        </el-button>
      </div>

      <div v-if="quickResult" class="quick-result">
        <el-tag size="small" type="success" effect="plain">
          {{ quickResult.source_type === 'video' ? '单个视频' : quickResult.source_type === 'channel' ? '频道' : '合集' }}
        </el-tag>
        <span class="quick-title">{{ quickResult.title || '未命名' }}</span>
        <span class="muted">· {{ quickResult.author || '未知作者' }}</span>
        <span class="muted">· 共 {{ quickResult.total }} 个视频</span>
      </div>
    </div>

    <!-- 环境告警 -->
    <el-alert
      v-if="blockers.length"
      class="env-alert"
      type="warning"
      show-icon
      :closable="false"
      title="运行环境不完整，部分功能不可用"
    >
      <div style="line-height: 1.9">
        <div v-for="item in blockers" :key="item.label">
          <strong>{{ item.label }}</strong>：{{ item.tip }}
        </div>
      </div>
    </el-alert>

    <!-- 指标卡 -->
    <div class="grid-cards metric-grid">
      <div v-for="card in cards" :key="card.key" class="panel metric-card">
        <div class="metric-icon" :style="{ background: `${card.color}1a`, color: card.color }">
          <el-icon :size="18"><component :is="card.icon" /></el-icon>
        </div>
        <div class="metric-body">
          <div class="metric-label">{{ card.label }}</div>
          <div class="metric-value">{{ card.value }}</div>
          <div class="metric-hint muted">{{ card.hint }}</div>
        </div>
      </div>
    </div>

    <div class="two-col">
      <div class="panel">
        <div class="panel-title">
          <span>近 14 天任务趋势</span>
        </div>
        <div ref="chartEl" class="chart"></div>
      </div>

      <div class="panel">
        <div class="panel-title">
          <span>运行环境</span>
          <el-button text size="small" :icon="'Refresh'" @click="load">刷新</el-button>
        </div>
        <div v-if="envItems.length" class="env-list">
          <div v-for="item in envItems" :key="item.label" class="env-row">
            <span class="env-label">{{ item.label }}</span>
            <span class="env-tip muted">{{ item.tip }}</span>
            <el-tag :type="item.ok ? 'success' : 'danger'" size="small" effect="plain">
              {{ item.ok ? '就绪' : '缺失' }}
            </el-tag>
          </div>
        </div>
        <el-empty v-else description="环境信息加载失败" :image-size="70" />
        <div class="data-dir muted mono" v-if="runtime?.data_dir">
          数据目录：{{ runtime.data_dir }}
        </div>
      </div>
    </div>

    <!-- 最近任务 -->
    <div class="panel">
      <div class="panel-title">
        <span>最近任务</span>
        <el-button text size="small" @click="router.push('/tasks')">查看全部</el-button>
      </div>

      <el-table
        v-if="overview?.recent_tasks?.length"
        :data="overview.recent_tasks"
        style="width: 100%"
        @row-click="(row: any) => router.push(`/tasks/${row.id}`)"
      >
        <el-table-column prop="id" label="ID" width="72" />
        <el-table-column label="标题" min-width="260" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="cell-title">{{ row.title || shortUrl(row.source_url) }}</div>
            <div class="muted mono cell-sub">{{ shortUrl(row.source_url, 60) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="进度" width="150">
          <template #default="{ row }">
            <el-progress
              :percentage="Math.round(row.progress || 0)"
              :stroke-width="8"
              :status="row.status === 'failed' ? 'exception' : row.status === 'succeeded' ? 'success' : undefined"
            />
            <div class="muted cell-sub">{{ row.done_items }}/{{ row.total_items }} 个视频</div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }"><StatusTag :status="row.status" /></template>
        </el-table-column>
        <el-table-column label="创建时间" width="130">
          <template #default="{ row }">
            <el-tooltip :content="formatDateTime(row.created_at)" placement="top">
              <span class="muted">{{ formatRelative(row.created_at) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
      </el-table>

      <el-empty v-else description="还没有任务，粘贴一个 YouTube 链接开始吧" :image-size="90" />
    </div>
  </div>
</template>

<style scoped>
.head-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.quick-panel {
  margin-bottom: 16px;
}

.quick-row {
  display: flex;
  gap: 10px;
  align-items: center;
}

.quick-result {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  flex-wrap: wrap;
}

.quick-title {
  font-weight: 600;
}

.env-alert {
  margin-bottom: 16px;
}

.metric-grid {
  margin-bottom: 16px;
  /* 单行排布：6 张卡放得下时永远排成一行（1440px 屏幕下正好 6 列一行铺满，
    auto-fit + 1fr 会把余量摊给现有卡片，因此右侧不像 auto-fill 那样空一条）。
    下限 182px 是保证卡片内文案不被压缩的下限值：
    182px 卡片 = 16×2 内边距 + 34px 图标 + 12px 间距 + 102px 文案区。 */
  grid-template-columns: repeat(auto-fit, minmax(182px, 1fr));
}

.metric-card {
  /* 图标在左、文案在右（水平对齐），图标垂直居中于三行文案 */
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  /* 高度交给网格行（同一行等高），文案再长也不会单独长高 */
}

.metric-icon {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  flex: none;
}

.metric-body {
  /* 窄列里允许收缩；配合下面三行的 nowrap+ellipsis 保证不换行 */
  min-width: 0;
  flex: 1;
}

.metric-label {
  font-size: 12.5px;
  line-height: 1.35;
  color: var(--spark-text-2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.metric-value {
  font-size: 21px;
  font-weight: 650;
  line-height: 1.3;
  letter-spacing: -0.5px;
  /* 大数（如 1,234,567）不换行，保持一行高度一致 */
  white-space: nowrap;
}

.metric-hint {
  font-size: 11.5px;
  line-height: 1.35;
  /* 提示文案固定一行：过长省略，卡片高度不受文案长度影响 */
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.two-col {
  display: grid;
  grid-template-columns: 1.6fr 1fr;
  gap: 16px;
  margin-bottom: 16px;
  /* 两侧面板等高：高度由较高的那张决定，两张都拉满，顶边/底边对齐；
     窄屏堆叠成一列时每一行同样等高（行高取内容较高的那张）。 */
  align-items: stretch;
  grid-auto-rows: 1fr;
}

/* 两块面板都是「标题 + 内容」，内容自适应撑满剩余高度并垂直居中，
   这样图表区与运行环境区的视觉重心一致，不会一张上重一张下重 */
.two-col > .panel {
  display: flex;
  flex-direction: column;
  margin-top: 0;
}

.two-col > .panel > .panel-title {
  flex: none;
}

@media (max-width: 1080px) {
  .two-col {
    grid-template-columns: 1fr;
  }
}

.chart {
  flex: 1;
  min-height: 260px;
}

.env-list {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 10px;
  /* 撑满标题以下的剩余高度：与左侧图表区等高，行距自动均分 */
  flex: 1;
  min-height: 0;
}

.env-row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
}

.env-label {
  width: 78px;
  flex: none;
  font-weight: 500;
}

.env-tip {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}

.data-dir {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px dashed var(--spark-border);
  font-size: 11.5px;
  word-break: break-all;
}

.cell-title {
  font-weight: 500;
}

.cell-sub {
  font-size: 11.5px;
  margin-top: 2px;
}

:deep(.el-table__row) {
  cursor: pointer;
}
</style>
