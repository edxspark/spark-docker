<script setup lang="ts">
/**
 * 搬运计划：把「什么时候搬什么」预先存下来，到点自动创建任务。
 *
 * 页面结构：
 * - 顶部状态条：调度器状态 + 计划统计（有多少个会自动跑、下一个是谁）
 * - 计划列表：调度、下次执行、上次结果、产出任务、启停/立即运行/编辑/删除
 * - 编辑弹窗：链接、搬运范围、调度方式、任务选项（音色/发布/画面）
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { planApi, settingsApi, taskApi } from '@/api'
import type { PlanRecord, PlanRunResult, Task, Voice } from '@/types'
import { formatDateTime, formatRelative } from '@/utils/format'

const router = useRouter()

const loading = ref(false)
const rows = ref<PlanRecord[]>([])
const total = ref(0)
const voices = ref<Voice[]>([])
const meta = ref<Awaited<ReturnType<typeof planApi.meta>> | null>(null)

const query = reactive({ q: '', enabled: '' as '' | 'true' | 'false', page: 1, page_size: 20 })

const editing = ref<PlanRecord | null>(null)
const dialogVisible = ref(false)
const saving = ref(false)
const runningId = ref<number | null>(null)
const probingId = ref<number | null>(null)

const historyVisible = ref(false)
const historyPlan = ref<PlanRecord | null>(null)
const historyRows = ref<Task[]>([])
const historyTotal = ref(0)
const historyLoading = ref(false)
const historyPage = ref(1)

/** 计划编辑表单 */
const form = reactive({
  name: '',
  source_url: '',
  source_type: 'video',
  author: '',
  scheduleType: 'manual' as 'manual' | 'interval' | 'daily' | 'weekly' | 'once',
  minutes: 360,
  times: ['08:00'] as string[],
  weekdays: [0] as number[],
  onceAt: '',
  enabled: true,
  autoStart: true,
  selectionMode: 'latest' as 'all' | 'first_n' | 'latest' | 'range' | 'selected',
  count: 1,
  start: 1,
  end: 0,
  ignoreUploaded: true,
  voice: '',
  autoPublish: false,
  targetAspect: 'original' as 'original' | '9:16' | '16:9',
  tags: [] as string[],
  description: '',
})

const activeCount = computed(() => rows.value.filter((row) => row.enabled).length)
const nextPlan = computed(() => {
  const list = rows.value
    .filter((row) => row.enabled && row.next_run_at_local)
    .sort((a, b) => a.next_run_at_local.localeCompare(b.next_run_at_local))
  return list[0] || null
})

function statusType(row: PlanRecord) {
  if (!row.enabled) return 'info'
  if (row.status === 'error') return 'danger'
  if (row.status === 'running') return 'primary'
  return 'success'
}

function statusText(row: PlanRecord) {
  if (!row.enabled) return '已暂停'
  if (row.status === 'error') return '上次失败'
  if (row.status === 'running') return '正在执行'
  return '待命'
}

function selectionText(row: PlanRecord): string {
  const selection = row.selection || {}
  const mode = String(selection.mode || 'all')
  const modeLabel: Record<string, string> = {
    all: '全部',
    first_n: `前 ${selection.count ?? 1} 个`,
    latest: `最新 ${selection.count ?? 1} 个`,
    range: `第 ${selection.start ?? 1} 起${selection.end ? ` 到第 ${selection.end}` : ' 往后'}`,
    selected: `指定 ${(selection.selected_video_ids || []).length} 个`,
  }
  const extra = selection.ignore_uploaded === false ? '（含已搬运过的）' : '（跳过已搬运）'
  return (modeLabel[mode] || mode) + extra
}

async function load() {
  loading.value = true
  try {
    const data = await planApi.list({
      q: query.q.trim() || undefined,
      enabled: query.enabled === '' ? undefined : query.enabled === 'true',
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
  try {
    meta.value = await planApi.meta()
  } catch {
    meta.value = null
  }
}

function search() {
  query.page = 1
  load()
}

function resetFilters() {
  query.q = ''
  query.enabled = ''
  query.page = 1
  load()
}

/** 新建：按默认值打开弹窗；编辑：回填已有配置 */
function openDialog(row?: PlanRecord) {
  editing.value = row || null
  const schedule = row?.schedule || {}
  const selection = row?.selection || {}
  const options = row?.options || {}

  form.name = row?.name || ''
  form.source_url = row?.source_url || ''
  form.source_type = row?.source_type || 'video'
  form.author = row?.author || ''
  form.scheduleType = (schedule.type as typeof form.scheduleType) || 'manual'
  form.minutes = Number(schedule.minutes || 360)
  form.times = Array.isArray(schedule.times) && schedule.times.length ? [...schedule.times] : ['08:00']
  form.weekdays = Array.isArray(schedule.weekdays) && schedule.weekdays.length ? [...schedule.weekdays] : [0]
  form.onceAt = schedule.at ? toLocalInput(String(schedule.at)) : defaultOnceAt()
  form.enabled = row?.enabled ?? true
  form.autoStart = row?.auto_start ?? true
  form.selectionMode = (selection.mode as typeof form.selectionMode) || 'latest'
  form.count = Number(selection.count || 1)
  form.start = Number(selection.start || 1)
  form.end = Number(selection.end || 0)
  form.ignoreUploaded = selection.ignore_uploaded !== false
  form.voice = String(options.voice || '')
  form.autoPublish = Boolean(options.auto_publish)
  form.targetAspect = (options.target_aspect as typeof form.targetAspect) || 'original'
  form.tags = Array.isArray(options.tags) ? [...options.tags] : []
  form.description = String(options.description || '')

  if (!form.tags.length && !row) {
    settingsApi
      .get()
      .then((data) => {
        form.tags = [...(data.config.publish?.default_tags || [])]
        form.autoPublish = data.config.publish?.auto_publish ?? false
        form.voice = form.voice || String(data.config.tts?.voice || '')
      })
      .catch(() => undefined)
  }
  dialogVisible.value = true
}

/** 定时时间用本地时间输入，提交时转成本地无时区 ISO（后端按本地时间理解） */
function toLocalInput(iso: string): string {
  const date = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`)
  if (Number.isNaN(date.getTime())) return defaultOnceAt()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function defaultOnceAt(): string {
  const date = new Date(Date.now() + 30 * 60 * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function buildPayload() {
  const schedule: Record<string, any> = { type: form.scheduleType }
  if (form.scheduleType === 'interval') schedule.minutes = Number(form.minutes)
  if (form.scheduleType === 'daily') schedule.times = form.times.filter(Boolean)
  if (form.scheduleType === 'weekly') {
    schedule.times = form.times.filter(Boolean)
    schedule.weekdays = [...form.weekdays]
  }
  if (form.scheduleType === 'once') schedule.at = form.onceAt

  const options: Record<string, any> = {
    target_aspect: form.targetAspect,
    auto_publish: form.autoPublish,
  }
  if (form.voice) options.voice = form.voice
  if (form.tags.length) options.tags = [...form.tags]
  if (form.description.trim()) options.description = form.description.trim()

  return {
    name: form.name.trim(),
    source_url: form.source_url.trim(),
    source_type: form.source_type,
    author: form.author,
    schedule,
    enabled: form.enabled,
    auto_start: form.autoStart,
    options,
    selection: {
      mode: form.selectionMode,
      count: Number(form.count),
      start: Number(form.start),
      end: Number(form.end),
      ignore_uploaded: form.ignoreUploaded,
    },
  }
}

async function save() {
  if (!form.source_url.trim()) {
    ElMessage.warning('请填写视频/合集/频道链接')
    return
  }
  if (form.scheduleType === 'once' && !form.onceAt) {
    ElMessage.warning('请选择执行时间')
    return
  }
  saving.value = true
  try {
    const payload = buildPayload()
    if (editing.value) {
      await planApi.update(editing.value.id, payload)
      ElMessage.success('计划已更新')
    } else {
      await planApi.create(payload)
      ElMessage.success('计划已创建')
    }
    dialogVisible.value = false
    await load()
  } catch {
    /* 拦截器已提示 */
  } finally {
    saving.value = false
  }
}

async function toggle(row: PlanRecord) {
  try {
    const updated = await planApi.toggle(row.id, !row.enabled)
    ElMessage.success(updated.last_message || (updated.enabled ? '已启用' : '已暂停'))
    await load()
  } catch {
    /* 拦截器已提示 */
  }
}

async function runNow(row: PlanRecord) {
  const automatic = row.schedule?.type && row.schedule.type !== 'manual'
  try {
    await ElMessageBox.confirm(
      `立即执行计划「${row.name}」？\n\n` +
        `搬运范围：${selectionText(row)}\n` +
        (automatic ? '本次手动执行不会改变已排好的定时计划。' : ''),
      '立即运行',
      { type: 'info', confirmButtonText: '开始执行', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  runningId.value = row.id
  try {
    const result = await planApi.run(row.id, {})
    applyRunResult(row, result)
    await load()
  } catch {
    /* 拦截器已提示 */
  } finally {
    runningId.value = null
  }
}

/** 只看这次会搬哪些，不建任务 */
async function probeNow(row: PlanRecord) {
  probingId.value = row.id
  try {
    const result = await planApi.run(row.id, { probe_only: true })
    const lines = result.candidates
      .slice(0, 10)
      .map((item) => `${item.taken ? '· 已搬过' : '· 待搬运'} ${item.title || item.video_id}`)
      .join('\n')
    await ElMessageBox.alert(
      `${result.message}\n\n${lines}${result.candidates.length > 10 ? '\n…' : ''}`,
      `计划「${row.name}」本次会搬运的条目`,
      { dangerouslyUseHTMLString: false, confirmButtonText: '知道了' },
    )
  } catch {
    /* 拦截器已提示 */
  } finally {
    probingId.value = null
  }
}

function applyRunResult(row: PlanRecord, result: PlanRunResult) {
  if (result.task_id) {
    ElMessage.success(result.message)
    router.push(`/tasks/${result.task_id}`)
  } else {
    ElMessage.info(result.message || '本次没有需要搬运的新视频')
  }
}

async function remove(row: PlanRecord) {
  try {
    await ElMessageBox.confirm(
      `确定删除计划「${row.name}」吗？\n\n已由该计划创建的任务与文件不会被删除。`,
      '删除计划',
      { type: 'warning', confirmButtonText: '删除计划', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const result = await planApi.remove(row.id)
    ElMessage.success(result.message)
    await load()
  } catch {
    /* 拦截器已提示 */
  }
}

async function openHistory(row: PlanRecord) {
  historyPlan.value = row
  historyPage.value = 1
  historyVisible.value = true
  await loadHistory()
}

async function loadHistory() {
  if (!historyPlan.value) return
  historyLoading.value = true
  try {
    const data = await planApi.history(historyPlan.value.id, { page: historyPage.value, page_size: 10 })
    historyRows.value = data.items
    historyTotal.value = data.total
  } catch {
    historyRows.value = []
  } finally {
    historyLoading.value = false
  }
}

let timer: number | null = null

onMounted(async () => {
  await load()
  try {
    voices.value = await settingsApi.voices().catch(() => [])
  } catch {
    voices.value = []
  }
  // 计划的状态与下次执行时间会随调度器变化，页面停留期间定期刷新
  timer = window.setInterval(() => {
    if (document.visibilityState === 'visible') load()
  }, 15000)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
  void taskApi
})
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2 class="page-title">搬运计划</h2>
        <p class="page-subtitle">
          把要搬的链接与时间预先排好，到点自动创建任务并执行；也可以随时手动跑一次。
        </p>
      </div>
      <div class="head-actions">
        <el-tag v-if="meta" :type="meta.scheduler.running ? 'success' : 'info'" effect="plain">
          调度器{{ meta.scheduler.running ? '运行中' : '未启动' }}
          <span class="muted">· 每 {{ meta.scheduler.tick_seconds }} 秒检查</span>
        </el-tag>
        <el-button :icon="'Refresh'" @click="load">刷新</el-button>
        <el-button type="primary" :icon="'Plus'" @click="openDialog()">新建计划</el-button>
      </div>
    </div>

    <div class="panel filter-panel">
      <el-input
        v-model="query.q"
        placeholder="搜索计划名称或链接"
        clearable
        style="width: 280px"
        @keyup.enter="search"
        @clear="search"
      >
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
      <el-select v-model="query.enabled" placeholder="全部状态" clearable style="width: 140px" @change="search">
        <el-option label="启用中" value="true" />
        <el-option label="已暂停" value="false" />
      </el-select>
      <el-button type="primary" :icon="'Search'" @click="search">查询</el-button>
      <el-button :icon="'RefreshLeft'" @click="resetFilters">重置</el-button>

      <div class="filter-right">
        <span class="muted">
          本页 {{ activeCount }}/{{ rows.length }} 个启用
          <template v-if="nextPlan">
            ｜下一次：{{ nextPlan.name }} {{ nextPlan.next_run_at_local || '-' }}
          </template>
        </span>
      </div>
    </div>

    <div class="panel">
      <el-table v-loading="loading" :data="rows" style="width: 100%" row-key="id">
        <el-table-column prop="id" label="ID" width="64" />

        <el-table-column label="计划" min-width="240">
          <template #default="{ row }">
            <div class="cell-title">{{ row.name || '(未命名计划)' }}</div>
            <div class="muted mono cell-sub">{{ row.source_url }}</div>
            <div class="muted cell-sub">
              搬运范围：{{ selectionText(row) }}
              <span v-if="!row.auto_start" class="tag-inline">仅创建不执行</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="调度" width="180">
          <template #default="{ row }">
            <div>{{ row.schedule_text }}</div>
            <div class="muted cell-sub">
              下次：{{ row.enabled ? row.next_run_at_local || '-' : '已暂停' }}
            </div>
          </template>
        </el-table-column>

        <el-table-column label="上次执行" width="200">
          <template #default="{ row }">
            <div class="muted cell-sub">
              {{ row.run_count ? `${row.last_run_at_local || '-'}（第 ${row.run_count} 次）` : '还没执行过' }}
            </div>
            <div class="muted cell-sub ellipsis" :title="row.last_error || row.last_message">
              {{ row.last_error ? `失败：${row.last_error.slice(0, 40)}` : row.last_message || '-' }}
            </div>
            <el-button v-if="row.last_task_id" link type="primary" size="small" @click="router.push(`/tasks/${row.last_task_id}`)">
              查看任务 #{{ row.last_task_id }}
            </el-button>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="statusType(row)" effect="plain" size="small">{{ statusText(row) }}</el-tag>
            <div class="muted cell-sub">已跑 {{ row.run_count }} 次</div>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="280" fixed="right">
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              size="small"
              :icon="'VideoPlay'"
              :loading="runningId === row.id"
              @click="runNow(row)"
            >
              立即运行
            </el-button>
            <el-button
              link
              size="small"
              :loading="probingId === row.id"
              @click="probeNow(row)"
            >
              预演
            </el-button>
            <el-button link size="small" @click="openHistory(row)">记录</el-button>
            <el-button link size="small" @click="openDialog(row)">编辑</el-button>
            <el-button link :type="row.enabled ? 'warning' : 'success'" size="small" @click="toggle(row)">
              {{ row.enabled ? '暂停' : '启用' }}
            </el-button>
            <el-button link type="danger" size="small" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pager">
        <el-pagination
          v-model:current-page="query.page"
          v-model:page-size="query.page_size"
          :page-sizes="[10, 20, 50]"
          :total="total"
          layout="total, sizes, prev, pager, next"
          background
          @current-change="load"
          @size-change="search"
        />
      </div>
    </div>

    <!-- 新建 / 编辑 -->
    <el-dialog
      v-model="dialogVisible"
      :title="editing ? `编辑计划 #${editing.id}` : '新建搬运计划'"
      width="760px"
      top="6vh"
      append-to-body
    >
      <el-form label-width="120px" label-position="left">
        <el-form-item label="计划名称">
          <el-input v-model="form.name" placeholder="例如：每天搬 YC 最新一条" maxlength="120" show-word-limit />
        </el-form-item>

        <el-form-item label="链接">
          <el-input v-model="form.source_url" placeholder="https://www.youtube.com/@handle 或 /playlist?list=..." />
          <div class="muted inline-help">
            支持单视频、合集、频道。频道链接配合「最新 N 个」最实用：新视频会自动被搬走
          </div>
        </el-form-item>

        <el-form-item label="搬运范围">
          <div class="range-block">
            <el-radio-group v-model="form.selectionMode">
              <el-radio-button value="latest">最新 N 个</el-radio-button>
              <el-radio-button value="first_n">前 N 个</el-radio-button>
              <el-radio-button value="all">全部</el-radio-button>
              <el-radio-button value="range">序号区间</el-radio-button>
            </el-radio-group>
            <div class="range-row">
              <template v-if="form.selectionMode === 'latest' || form.selectionMode === 'first_n'">
                <span class="muted">数量</span>
                <el-input-number v-model="form.count" :min="1" :max="200" controls-position="right" />
              </template>
              <template v-else-if="form.selectionMode === 'range'">
                <span class="muted">从第</span>
                <el-input-number v-model="form.start" :min="1" controls-position="right" />
                <span class="muted">到第</span>
                <el-input-number v-model="form.end" :min="0" controls-position="right" />
                <span class="muted">个（0 表示一直到最后一个）</span>
              </template>
              <el-switch v-model="form.ignoreUploaded" active-text="跳过已搬运过的视频" />
            </div>
            <div class="muted inline-help">
              「跳过已搬运过的」让持续更新的频道不会重复搬同一条；关掉则每次都重新搬
            </div>
          </div>
        </el-form-item>

        <el-form-item label="运行方式">
          <el-radio-group v-model="form.scheduleType">
            <el-radio-button value="manual">仅手动</el-radio-button>
            <el-radio-button value="interval">按间隔</el-radio-button>
            <el-radio-button value="daily">每天定时</el-radio-button>
            <el-radio-button value="weekly">每周定时</el-radio-button>
            <el-radio-button value="once">定时一次</el-radio-button>
          </el-radio-group>

          <div class="range-row">
            <template v-if="form.scheduleType === 'interval'">
              <span class="muted">每</span>
              <el-input-number v-model="form.minutes" :min="5" :max="43200" :step="30" controls-position="right" />
              <span class="muted">分钟执行一次</span>
            </template>

            <template v-else-if="form.scheduleType === 'daily'">
              <span class="muted">每天</span>
              <el-time-select
                v-for="(_, index) in form.times"
                :key="`d${index}`"
                v-model="form.times[index]"
                start="00:00"
                step="00:15"
                end="23:45"
                style="width: 132px"
              />
              <el-button link type="primary" :icon="'Plus'" @click="form.times.push('20:00')">加一个时间</el-button>
              <el-button v-if="form.times.length > 1" link type="danger" @click="form.times.pop()">去掉</el-button>
            </template>

            <template v-else-if="form.scheduleType === 'weekly'">
              <el-checkbox-group v-model="form.weekdays">
                <el-checkbox-button v-for="(day, index) in meta?.weekdays || []" :key="day" :value="index">
                  {{ day }}
                </el-checkbox-button>
              </el-checkbox-group>
              <el-time-select v-model="form.times[0]" start="00:00" step="00:15" end="23:45" style="width: 132px" />
            </template>

            <template v-else-if="form.scheduleType === 'once'">
              <span class="muted">执行时间</span>
              <el-date-picker
                v-model="form.onceAt"
                type="datetime"
                placeholder="选择日期与时间"
                format="YYYY-MM-DD HH:mm"
                value-format="YYYY-MM-DDTHH:mm"
                style="width: 220px"
              />
            </template>

            <span v-else class="muted">不自动执行，只在计划页手动点「立即运行」</span>
          </div>
          <div class="muted inline-help">
            时间按本机时区（{{ meta?.server_timezone || '本地' }}）计算；调度器每 {{ meta?.scheduler.tick_seconds || 20 }} 秒检查一次
          </div>
        </el-form-item>

        <el-form-item label="执行设置">
          <div class="range-row">
            <el-switch v-model="form.enabled" active-text="启用该计划" />
            <el-switch v-model="form.autoStart" active-text="创建后立即执行" />
            <el-switch v-model="form.autoPublish" active-text="处理完自动发布抖音" />
          </div>
          <div class="muted inline-help">关闭「立即执行」则到点只建任务、留在待执行队列</div>
        </el-form-item>

        <el-form-item label="配音音色">
          <el-select v-model="form.voice" filterable clearable placeholder="跟随系统配置" style="width: 280px">
            <el-option v-for="voice in voices" :key="voice.id" :label="voice.name" :value="voice.id" />
          </el-select>
        </el-form-item>

        <el-form-item label="画面比例">
          <el-radio-group v-model="form.targetAspect">
            <el-radio-button value="original">保持原比例</el-radio-button>
            <el-radio-button value="9:16">竖屏 9:16</el-radio-button>
            <el-radio-button value="16:9">横屏 16:9</el-radio-button>
          </el-radio-group>
        </el-form-item>

        <el-form-item label="话题标签">
          <el-select
            v-model="form.tags"
            multiple
            filterable
            allow-create
            default-first-option
            placeholder="输入后回车添加，不带 # 号"
            style="width: 100%"
          />
        </el-form-item>

        <el-form-item label="作品简介">
          <el-input v-model="form.description" type="textarea" :rows="2" maxlength="1000" show-word-limit />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">
          {{ editing ? '保存修改' : '创建计划' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 执行记录 -->
    <el-drawer v-model="historyVisible" :title="`执行记录 · ${historyPlan?.name || ''}`" size="620px">
      <el-table v-loading="historyLoading" :data="historyRows" style="width: 100%">
        <el-table-column prop="id" label="任务" width="80" />
        <el-table-column label="标题" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.title || '未命名' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="160">
          <template #default="{ row }">
            <span class="muted">{{ formatDateTime(row.created_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="" width="80">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="router.push(`/tasks/${row.id}`)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination
          v-model:current-page="historyPage"
          :page-size="10"
          :total="historyTotal"
          layout="total, prev, pager, next"
          background
          @current-change="loadHistory"
        />
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.head-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

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
  font-size: 12.5px;
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
  max-width: 190px;
}

.tag-inline {
  margin-left: 8px;
  color: var(--spark-primary);
}

.range-block {
  width: 100%;
}

.range-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 10px;
}

.inline-help {
  font-size: 12px;
  margin-top: 6px;
  display: block;
  line-height: 1.6;
}

.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
</style>
