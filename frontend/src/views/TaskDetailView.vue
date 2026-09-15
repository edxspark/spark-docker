<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { taskApi } from '@/api'
import { useTaskSocket } from '@/composables/useTaskSocket'
import type { TaskItem } from '@/types'
import StatusTag from '@/components/StatusTag.vue'
import StageProgress from '@/components/StageProgress.vue'
import VideoPreviewDialog from '@/components/VideoPreviewDialog.vue'
import {
  STAGE_LABELS,
  copyText,
  elapsedText,
  formatDateTime,
  formatDuration,
  formatNumber,
  formatSize,
  shortUrl,
} from '@/utils/format'

const route = useRoute()
const router = useRouter()

const taskId = computed(() => {
  const raw = Number(route.params.id)
  return Number.isFinite(raw) ? raw : null
})

const notFound = ref(false)
const autoScroll = ref(true)
const logBox = ref<HTMLDivElement | null>(null)
const expanded = ref<number[]>([])
const publishingId = ref<number | null>(null)
const previewOpen = ref(false)
const previewItem = ref<TaskItem | null>(null)

const { detail, logs, connected, reload } = useTaskSocket(taskId, {
  onUpdate: (event) => {
    if (event.type === 'task.finished') {
      // 任务结束时做一次校准，确保统计数字准确
      reload().catch(() => undefined)
    }
  },
})

const sourceLabel = computed(() => {
  const map: Record<string, string> = { video: '单个视频', playlist: '合集', channel: '频道' }
  const type = detail.value?.source_type || ''
  return map[type] || type || '-'
})

const progressStatus = computed(() => {
  const status = detail.value?.status
  if (status === 'failed') return 'exception'
  if (status === 'succeeded') return 'success'
  if (status === 'canceled') return 'warning'
  return undefined
})

const canCancel = computed(() => ['pending', 'running'].includes(detail.value?.status || ''))

/** 已完成成片、等待人工确认发布的条目（默认手动发布流程的待办） */
const pendingPublishItems = computed(
  () =>
    (detail.value?.items || []).filter(
      (item) => item.output_path && ['', 'pending', 'skipped'].includes(item.publish_status || ''),
    ),
)

const canRetry = computed(() => {
  if (!detail.value) return false
  return (
    !['running', 'pending'].includes(detail.value.status) &&
    (detail.value.failed_items > 0 ||
      ['failed', 'partial', 'canceled'].includes(detail.value.status))
  )
})

const stepSummary = computed(() => {
  const data = detail.value
  if (!data) return []
  return [
    { label: '开始时间', value: formatDateTime(data.started_at) },
    { label: '结束时间', value: formatDateTime(data.finished_at) },
    { label: '总耗时', value: elapsedText(data.started_at, data.finished_at) },
    { label: '创建时间', value: formatDateTime(data.created_at) },
  ]
})

function stageText(item: TaskItem): string {
  if (item.status === 'succeeded') return '已完成'
  if (item.status === 'failed') return `失败于「${STAGE_LABELS[item.stage] || item.stage || '未知阶段'}」`
  if (item.status === 'canceled') return '已取消'
  return STAGE_LABELS[item.stage] || '等待执行'
}

function statsRows(item: TaskItem): { label: string; value: string }[] {
  const stats = (item.stats || {}) as Record<string, any>
  const rows: { label: string; value: string }[] = []
  const subtitle = stats.subtitle_source
  if (subtitle) {
    const kindLabel =
      subtitle.kind === 'manual'
        ? '人工字幕'
        : subtitle.kind === 'auto'
          ? '自动字幕'
          : subtitle.kind === 'asr'
            ? '语音识别（无字幕兜底）'
            : '未知'
    rows.push({ label: '原字幕', value: `${kindLabel}，${subtitle.cues ?? 0} 条` })
  }
  const asr = stats.asr
  if (asr) {
    rows.push({
      label: '语音识别',
      value: `${asr.provider === 'mock' ? 'Mock' : '阿里云'}，${asr.segments ?? 0} 句，` +
        `${formatNumber(asr.characters)} 字符，耗时 ${Math.round(asr.elapsed_seconds || 0)} 秒`,
    })
  }
  if (stats.asr_error) rows.push({ label: '识别失败', value: String(stats.asr_error).slice(0, 120) })
  if (stats.sentences) rows.push({ label: '断句后', value: `${stats.sentences} 句` })
  const translate = stats.translate
  if (translate) {
    rows.push({
      label: '翻译',
      value: `${translate.provider === 'mock' ? 'Mock' : 'DeepSeek'}，${translate.sentences ?? 0} 句，` +
        `${formatNumber(translate.tokens)} tokens`,
    })
    if (translate.source_chars) {
      rows.push({ label: '文本量', value: `原文 ${translate.source_chars} 字符 → 译文 ${translate.target_chars} 字符` })
    }
  }
  const tts = stats.tts
  if (tts) {
    rows.push({
      label: '配音',
      value: `${tts.provider === 'mock' ? 'Mock' : '阿里云'} · 音色 ${tts.voice || '-'} · ` +
        `${tts.segments ?? 0} 段 / ${formatNumber(tts.characters)} 字符` +
        (tts.cached_segments ? `（复用 ${tts.cached_segments} 段）` : ''),
    })
  }
  const output = stats.output
  if (output) {
    rows.push({
      label: '成片',
      value: `${output.resolution || '-'} · ${formatSize(output.size_mb)} · ` +
        `${output.dubbed ? 'AI 配音' : '原声'} · 时长 ${formatDuration(output.duration)}`,
    })
  }
  if (stats.resolution) rows.push({ label: '原片分辨率', value: String(stats.resolution) })
  if (stats.no_subtitle) {
    rows.push({
      label: '注意',
      value: '该视频没有字幕，语音识别也未能生成内容，成片已保留原声（未配音、未加中文字幕）',
    })
  }
  return rows
}

function scrollLogsToBottom() {
  if (!autoScroll.value || !logBox.value) return
  nextTick(() => {
    if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight
  })
}

watch(() => logs.value.length, scrollLogsToBottom)

async function handleCancel() {
  if (!taskId.value) return
  try {
    const result = await taskApi.cancel(taskId.value)
    ElMessage.success(result.message)
  } catch {
    /* 拦截器已提示 */
  }
}

async function handleRetry(onlyFailed: boolean) {
  if (!taskId.value) return
  try {
    await taskApi.retry(taskId.value, onlyFailed)
    ElMessage.success('已重新入队执行')
    reload().catch(() => undefined)
  } catch {
    /* 拦截器已提示 */
  }
}

async function handleDelete() {
  if (!taskId.value || !detail.value) return
  try {
    await ElMessageBox.confirm(`确定删除任务 #${taskId.value} 吗？`, '删除任务', {
      type: 'warning',
      confirmButtonText: '删除记录',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  let removeFiles = false
  try {
    await ElMessageBox.confirm('是否同时删除磁盘上的下载文件、成片与字幕？此操作不可恢复。', '同时清理文件？', {
      type: 'warning',
      confirmButtonText: '同时删除文件',
      cancelButtonText: '仅删除记录',
    })
    removeFiles = true
  } catch {
    removeFiles = false
  }
  try {
    const result = await taskApi.remove(taskId.value, removeFiles)
    ElMessage.success(result.message)
    router.push('/tasks')
  } catch {
    /* 拦截器已提示 */
  }
}

function openPreview(item: TaskItem) {
  if (!item.output_path) {
    ElMessage.warning('该条目还没有成片')
    return
  }
  previewItem.value = item
  previewOpen.value = true
}

async function handlePublish(item: TaskItem, republish = false) {
  if (!taskId.value) return

  if (republish) {
    // 抖音不会用新上传替换旧作品，重发必然多出一个作品，必须先讲清楚
    try {
      await ElMessageBox.confirm(
        '重新发布会向抖音再上传一个「新作品」，旧的已发布作品不会自动删除，' +
          '需要你到创作者中心手动删除旧的那条。\n\n' +
          '确认要重新发布吗？',
        '重新发布到抖音',
        { type: 'warning', confirmButtonText: '重新发布', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
  }

  publishingId.value = item.id
  try {
    const updated = await taskApi.publishItem(taskId.value, item.id, { republish })
    ElMessage.success(
      `${republish ? '重新发布' : '发布'}成功${updated.publish_url ? '：' + updated.publish_url : ''}`,
    )
    reload().catch(() => undefined)
  } catch {
    /* 拦截器已提示 */
  } finally {
    publishingId.value = null
  }
}

/** 近几次发布尝试，用于展示「重新发布」的结果 */
function publishHistory(item: TaskItem): Record<string, any>[] {
  const history = (item.stats?.publish_history ?? []) as Record<string, any>[]
  return [...history].reverse()
}

function truncateLog(message: string, max = 60): string {
  return message.length > max ? `${message.slice(0, max - 1)}…` : message
}

/**
 * 条目封面的候选地址，按「本地优先」排序。
 *
 * 之前直接用 row.thumbnail（i.ytimg.com 外链）：本机网络访问不到 YouTube 图床时，
 * 图片永远加载不出来，条目上就只剩一个灰色占位图标——而封面其实早就随任务下载到本地
 * （data/covers/<task>/<video>.jpg，实测约 370KB）。现在优先走后端本地文件接口，
 * 外链只作兜底。
 */
function thumbSources(item: TaskItem): string[] {
  if (taskId.value == null) return ['']
  const sources: string[] = []
  // 1) 下载时落盘的 YouTube 缩略图（16:9，与列表里的 88x50 槽位同形状）
  sources.push(`${taskApi.fileUrl(taskId.value, item.id, 'thumbnail')}&inline=1`)
  // 2) 成片抽帧的竖版封面（会裁切，但至少是本地的）
  if (item.cover_path) {
    sources.push(`${taskApi.fileUrl(taskId.value, item.id, 'cover')}&inline=1`)
  }
  // 3) 最后才用 YouTube 外链（本机无代理时不可达）
  if (item.thumbnail) sources.push(item.thumbnail)
  return sources
}

/** 每个条目当前用到第几个候选地址；前一个加载失败就降级到下一个 */
const thumbAttempt = ref<Record<number, number>>({})

function handleThumbError(item: TaskItem) {
  const used = thumbAttempt.value[item.id] ?? 0
  if (used + 1 < thumbSources(item).length) {
    thumbAttempt.value = { ...thumbAttempt.value, [item.id]: used + 1 }
  }
  // 候选都失败时不再自增，由 el-image 的 error 插槽显示占位图标
}

/** 复制任务来源链接（YouTube 原视频/合集链接） */
async function copySourceLink() {
  const url = detail.value?.source_url
  if (!url) {
    ElMessage.warning('该任务没有来源链接')
    return
  }
  const ok = await copyText(url)
  if (ok) ElMessage.success('已复制来源链接')
  else ElMessage.error('复制失败，请手动选中链接复制')
}

/** 复制某个条目对应的原视频链接 */
async function copyItemLink(item: TaskItem) {
  const url = item.url || (item.video_id ? `https://www.youtube.com/watch?v=${item.video_id}` : '')
  if (!url) {
    ElMessage.warning('该条目没有原视频链接')
    return
  }
  const ok = await copyText(url)
  if (ok) ElMessage.success('已复制原视频链接')
  else ElMessage.error('复制失败，请手动选中链接复制')
}

onMounted(async () => {
  try {
    await reload()
  } catch {
    notFound.value = true
    return
  }
  // 从列表页「看成片」跳转过来时自动打开预览
  if (route.query.preview) {
    const first = detail.value?.items.find((item) => item.output_path)
    if (first) openPreview(first)
  }
})
</script>

<template>
  <div class="page">
    <el-empty v-if="notFound" description="任务不存在或已被删除">
      <el-button type="primary" @click="router.push('/tasks')">返回任务列表</el-button>
    </el-empty>

    <template v-else-if="detail">
      <!-- 头部 -->
      <div class="page-header">
        <div class="head-left">
          <el-button text :icon="'ArrowLeft'" @click="router.push('/tasks')">返回</el-button>
          <div>
            <h2 class="page-title">
              #{{ detail.id }} {{ detail.title || '未命名任务' }}
            </h2>
            <div class="head-meta">
              <StatusTag :status="detail.status" />
              <el-tag size="small" effect="plain">{{ sourceLabel }}</el-tag>
              <span class="muted">{{ detail.author || '未知作者' }}</span>
              <a
                class="mono muted src-link"
                :href="detail.source_url"
                target="_blank"
                rel="noopener"
              >{{ shortUrl(detail.source_url, 56) }}</a>
              <el-button
                v-if="detail.source_url"
                link
                type="primary"
                size="small"
                :icon="'CopyDocument'"
                @click="copySourceLink"
              >
                复制链接
              </el-button>
            </div>
          </div>
        </div>

        <div class="head-actions">
          <el-tag :type="connected ? 'success' : 'info'" size="small" effect="plain">
            {{ connected ? '实时连接中' : '连接断开，重连中…' }}
          </el-tag>
          <el-button v-if="canCancel" type="warning" plain :icon="'VideoPause'" @click="handleCancel">
            取消任务
          </el-button>
          <template v-if="canRetry">
            <el-button type="primary" plain :icon="'RefreshRight'" @click="handleRetry(true)">
              重试失败项
            </el-button>
            <el-button plain @click="handleRetry(false)">全部重跑</el-button>
          </template>
          <el-button type="danger" plain :icon="'Delete'" @click="handleDelete">删除任务</el-button>
        </div>
      </div>

      <!-- 总览 -->
      <div class="panel">
        <div class="overview-top">
          <div class="overview-progress">
            <div class="progress-label">
              <span>总进度</span>
              <span class="mono">{{ Math.round(detail.progress || 0) }}%</span>
            </div>
            <el-progress
              :percentage="Math.round(detail.progress || 0)"
              :status="progressStatus"
              :stroke-width="12"
            />
            <div class="muted progress-msg">
              {{ detail.message || '—' }}
            </div>
          </div>

          <div class="overview-nums">
            <div class="num">
              <div class="num-value">{{ detail.total_items }}</div>
              <div class="num-label muted">总视频</div>
            </div>
            <div class="num">
              <div class="num-value ok">{{ detail.done_items }}</div>
              <div class="num-label muted">成功</div>
            </div>
            <div class="num">
              <div class="num-value" :class="{ err: detail.failed_items > 0 }">{{ detail.failed_items }}</div>
              <div class="num-label muted">失败</div>
            </div>
          </div>
        </div>

        <div class="overview-stage">
          <StageProgress :stage="detail.stage" :status="detail.status" />
        </div>

        <el-descriptions :column="4" size="small" border class="overview-desc">
          <el-descriptions-item v-for="row in stepSummary" :key="row.label" :label="row.label">
            {{ row.value }}
          </el-descriptions-item>
        </el-descriptions>

        <el-alert
          v-if="pendingPublishItems.length"
          type="warning"
          :closable="false"
          show-icon
          style="margin-top: 12px"
          title="成片已就绪，等待人工确认发布"
        >
          <div>
            本任务 {{ pendingPublishItems.length }} 个条目尚未发布（默认手动发布，不会自动上传抖音）。
            在下方条目列表点「立即发布」即可上传；想全自动发布请到「系统配置 → 发布」打开「自动发布」。
          </div>
        </el-alert>

        <el-alert
          v-if="detail.status === 'failed' && detail.error"
          type="error"
          :closable="false"
          show-icon
          title="任务级错误"
          style="margin-top: 12px"
        >
          <pre class="err-pre">{{ detail.error }}</pre>
        </el-alert>
      </div>

      <!-- 条目列表 -->
      <div class="panel">
        <div class="panel-title">
          <span>视频条目（{{ detail.items.length }}）</span>
          <el-button text size="small" @click="expanded = expanded.length ? [] : detail.items.map((i) => i.id)">
            {{ expanded.length ? '收起全部' : '展开全部' }}
          </el-button>
        </div>

        <el-table :data="detail.items" row-key="id" style="width: 100%" :expand-row-keys="expanded">
          <el-table-column type="expand">
            <template #default="{ row }">
              <div class="expand-body">
                <div class="expand-col">
                  <div class="expand-title">处理明细</div>
                  <el-descriptions v-if="statsRows(row).length" :column="1" size="small" border>
                    <el-descriptions-item
                      v-for="stat in statsRows(row)"
                      :key="stat.label"
                      :label="stat.label"
                    >
                      {{ stat.value }}
                    </el-descriptions-item>
                  </el-descriptions>
                  <div v-else class="muted">暂无处理明细</div>

                  <div v-if="publishHistory(row).length" class="expand-error">
                    <div class="expand-title">发布记录（最近 {{ publishHistory(row).length }} 次）</div>
                    <el-timeline class="publish-history">
                      <el-timeline-item
                        v-for="(entry, index) in publishHistory(row)"
                        :key="index"
                        :type="entry.success ? 'success' : 'danger'"
                        size="small"
                        :timestamp="String(entry.at || '').replace('T', ' ')"
                      >
                        <span>
                          {{ entry.kind === 'republish' ? '重新发布' : entry.kind === 'dry_run' ? '干跑自检' : '首次发布' }}
                          · {{ entry.success ? '成功' : '失败' }}
                        </span>
                        <div class="muted history-msg">{{ entry.message }}</div>
                        <a
                          v-if="entry.url"
                          :href="entry.url"
                          target="_blank"
                          rel="noopener"
                          class="pub-link"
                        >{{ entry.url }}</a>
                      </el-timeline-item>
                    </el-timeline>
                  </div>

                  <div v-if="row.error" class="expand-error">
                    <div class="expand-title" style="color: #e05c5c">错误信息</div>
                    <pre class="err-pre">{{ row.error }}</pre>
                  </div>
                </div>

                <div class="expand-col">
                  <button v-if="row.output_path" class="preview-cta" @click="openPreview(row)">
                    <el-icon><VideoPlay /></el-icon> 查看成片视频
                  </button>

                  <div class="expand-title">产物下载</div>
                  <div class="artifacts">
                    <a
                      v-if="row.output_path"
                      class="artifact"
                      :href="taskApi.fileUrl(detail.id, row.id, 'output')"
                    >
                      <el-icon><VideoCamera /></el-icon> 成片（已配音合成）
                    </a>
                    <a
                      v-if="row.cover_path"
                      class="artifact"
                      :href="taskApi.fileUrl(detail.id, row.id, 'cover')"
                    >
                      <el-icon><Picture /></el-icon> 封面
                    </a>
                    <a
                      v-if="row.subtitle_zh_path"
                      class="artifact"
                      :href="taskApi.fileUrl(detail.id, row.id, 'subtitle_zh')"
                    >
                      <el-icon><Document /></el-icon> 中文字幕
                    </a>
                    <a
                      v-if="row.subtitle_source_path"
                      class="artifact"
                      :href="taskApi.fileUrl(detail.id, row.id, 'subtitle_source')"
                    >
                      <el-icon><Document /></el-icon> 原文字幕
                    </a>
                    <a
                      v-if="row.dubbed_audio_path"
                      class="artifact"
                      :href="taskApi.fileUrl(detail.id, row.id, 'audio')"
                    >
                      <el-icon><Headset /></el-icon> 配音音轨
                    </a>
                    <a
                      v-if="row.video_path"
                      class="artifact"
                      :href="taskApi.fileUrl(detail.id, row.id, 'video')"
                    >
                      <el-icon><Download /></el-icon> 原视频文件
                    </a>
                    <div v-if="!row.output_path" class="muted" style="font-size: 12.5px">
                      成片尚未生成
                    </div>
                  </div>
                </div>
              </div>
            </template>
          </el-table-column>

          <el-table-column type="index" label="#" width="56" />

          <el-table-column label="视频" min-width="320">
            <template #default="{ row }">
              <div class="item-row">
                <div
                  class="thumb-box"
                  :class="{ clickable: !!row.output_path }"
                  @click="row.output_path && openPreview(row)"
                >
                  <el-image
                    :src="thumbSources(row)[thumbAttempt[row.id] ?? 0]"
                    fit="cover"
                    class="item-thumb"
                    @error="handleThumbError(row)"
                  >
                    <template #error>
                      <div class="thumb-fallback"><el-icon><Picture /></el-icon></div>
                    </template>
                  </el-image>
                  <div v-if="row.output_path" class="thumb-play"><el-icon><VideoPlay /></el-icon></div>
                </div>
                <div class="item-text">
                  <div class="item-title">{{ row.title_zh || row.title || row.video_id }}</div>
                  <div v-if="row.title_zh" class="muted item-sub">{{ truncateLog(row.title, 70) }}</div>
                  <div class="muted mono item-sub">{{ formatDuration(row.duration) }} · {{ row.video_id }}</div>
                  <div v-if="row.tags?.length" class="item-tags">
                    <el-tag v-for="tag in row.tags" :key="tag" size="small" effect="plain" type="info">
                      #{{ tag }}
                    </el-tag>
                  </div>
                </div>
              </div>
            </template>
          </el-table-column>

          <el-table-column label="状态" width="150">
            <template #default="{ row }">
              <StatusTag :status="row.status" />
              <div class="muted item-sub">{{ stageText(row) }}</div>
            </template>
          </el-table-column>

          <el-table-column label="进度" width="140">
            <template #default="{ row }">
              <el-progress
                :percentage="Math.round(row.progress || 0)"
                :stroke-width="6"
                :status="row.status === 'failed' ? 'exception' : row.status === 'succeeded' ? 'success' : undefined"
              />
            </template>
          </el-table-column>

          <el-table-column label="发布" width="150">
            <template #default="{ row }">
              <StatusTag :status="row.publish_status || ''" kind="publish" />
              <div v-if="row.publish_url" class="item-sub">
                <a :href="row.publish_url" target="_blank" rel="noopener" class="pub-link">查看作品 ↗</a>
              </div>
              <div v-else-if="row.publish_error" class="muted item-sub" :title="row.publish_error">
                {{ truncateLog(row.publish_error, 24) }}
              </div>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="280" fixed="right">
            <template #default="{ row }">
              <el-button
                v-if="row.output_path"
                link
                type="success"
                size="small"
                :icon="'VideoPlay'"
                @click="openPreview(row)"
              >
                查看成片
              </el-button>
              <el-button
                v-if="row.output_path && row.publish_status !== 'published' && row.publish_status !== 'publishing'"
                link
                type="primary"
                size="small"
                :loading="publishingId === row.id"
                @click="handlePublish(row)"
              >
                立即发布
              </el-button>
              <el-button
                v-if="
                  row.output_path &&
                  ['published', 'failed'].includes(row.publish_status || '') &&
                  row.publish_status !== 'publishing'
                "
                link
                type="warning"
                size="small"
                :icon="'RefreshRight'"
                :loading="publishingId === row.id"
                @click="handlePublish(row, true)"
              >
                重新发布
              </el-button>
              <a v-if="row.output_path" :href="taskApi.fileUrl(detail.id, row.id, 'output')">
                <el-button link type="primary" size="small">下载成片</el-button>
              </a>
              <el-button
                v-if="row.url || row.video_id"
                link
                type="primary"
                size="small"
                title="复制该视频的原始链接"
                @click="copyItemLink(row)"
              >
                复制链接
              </el-button>
              <el-button
                link
                size="small"
                @click="expanded = expanded.includes(row.id) ? expanded.filter((x) => x !== row.id) : [...expanded, row.id]"
              >
                明细
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <VideoPreviewDialog v-model="previewOpen" :task-id="taskId" :item="previewItem" />

      <!-- 日志 -->
      <div class="panel">
        <div class="panel-title">
          <span>执行日志（{{ logs.length }}）</span>
          <div class="log-actions">
            <el-switch v-model="autoScroll" size="small" active-text="自动滚动" />
            <el-button text size="small" @click="logs.splice(0, logs.length)">清空显示</el-button>
          </div>
        </div>

        <div ref="logBox" class="log-console">
          <div v-if="!logs.length" class="muted">暂无日志</div>
          <div v-for="log in logs" :key="log.id" class="log-line" :class="log.level">
            <span class="ts">{{ formatDateTime(log.created_at).slice(11) }}</span>
            <span class="msg">
              <template v-if="log.stage">[{{ STAGE_LABELS[log.stage] || log.stage }}] </template>
              {{ log.message }}
            </span>
          </div>
        </div>
      </div>
    </template>

    <div v-else class="panel" v-loading="true" style="height: 200px" />
  </div>
</template>

<style scoped>
.head-left {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

.head-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 6px;
}

.src-link:hover {
  color: var(--spark-primary);
  text-decoration: underline;
}

.head-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.overview-top {
  display: flex;
  gap: 28px;
  align-items: center;
  flex-wrap: wrap;
}

.overview-progress {
  flex: 1;
  min-width: 280px;
}

.progress-label {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  margin-bottom: 6px;
}

.progress-msg {
  font-size: 12.5px;
  margin-top: 8px;
}

.overview-nums {
  display: flex;
  gap: 24px;
}

.num {
  text-align: center;
}

.num-value {
  font-size: 24px;
  font-weight: 650;
  letter-spacing: -0.5px;
}

.num-value.ok {
  color: #1f8a4c;
}

.num-value.err {
  color: #e05c5c;
}

.num-label {
  font-size: 12px;
}

.overview-stage {
  margin-top: 16px;
}

.overview-desc {
  margin-top: 16px;
}

.err-pre {
  margin: 6px 0 0;
  white-space: pre-wrap;
  word-break: break-all;
  font-size: 12px;
  font-family: 'SF Mono', Menlo, Consolas, monospace;
  max-height: 220px;
  overflow: auto;
}

.expand-body {
  display: grid;
  grid-template-columns: 1.5fr 1fr;
  gap: 22px;
  padding: 8px 16px 16px 48px;
}

@media (max-width: 1000px) {
  .expand-body {
    grid-template-columns: 1fr;
  }
}

.expand-title {
  font-size: 12.5px;
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--spark-text-2);
}

.expand-error {
  margin-top: 14px;
}

.artifacts {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.artifact {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: 13px;
  color: var(--spark-primary);
  padding: 7px 12px;
  border: 1px solid #e2e9fb;
  border-radius: 8px;
  background: #f7faff;
  transition: all 0.15s ease;
}

.artifact:hover {
  background: #edf3ff;
  border-color: #c9d9fb;
}

.item-row {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}

.thumb-box {
  position: relative;
  flex: none;
  border-radius: 6px;
  overflow: hidden;
  line-height: 0;
}

.thumb-box.clickable {
  cursor: pointer;
}

.thumb-play {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  color: #fff;
  font-size: 22px;
  background: rgba(0, 0, 0, 0.32);
  opacity: 0;
  transition: opacity 0.15s ease;
}

.thumb-box.clickable:hover .thumb-play {
  opacity: 1;
}

.publish-history {
  padding-left: 2px;
  margin-top: 4px;
}

.history-msg {
  font-size: 11.5px;
  margin-top: 2px;
  word-break: break-all;
}

.preview-cta {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 14px;
  padding: 8px 14px;
  font-size: 13px;
  color: #fff;
  background: linear-gradient(135deg, #1f8a4c, #2fae66);
  border: none;
  border-radius: 8px;
  cursor: pointer;
  font-family: inherit;
}

.preview-cta:hover {
  filter: brightness(1.06);
}

.item-thumb {
  width: 88px;
  height: 50px;
  border-radius: 6px;
  flex: none;
  /* 兜底用的是成片抽帧的竖版封面，contain + 底色保证不把画面主体裁掉 */
  background: #eef1f6;
  overflow: hidden;
}

.item-thumb :deep(img) {
  object-fit: contain;
}

.thumb-fallback {
  width: 88px;
  height: 50px;
  border-radius: 6px;
  background: #f0f2f5;
  display: grid;
  place-items: center;
  color: #a8b0bd;
}

.item-text {
  min-width: 0;
}

.item-title {
  font-weight: 500;
  line-height: 1.45;
}

.item-sub {
  font-size: 11.5px;
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 420px;
}

.item-tags {
  margin-top: 5px;
  display: flex;
  gap: 5px;
  flex-wrap: wrap;
}

.pub-link {
  color: var(--spark-primary);
  font-size: 11.5px;
}

.pub-link:hover {
  text-decoration: underline;
}

.log-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}
</style>
