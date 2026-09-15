<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { taskApi } from '@/api'
import { formatDuration, formatSize } from '@/utils/format'

interface PreviewItem {
  id: number
  title?: string
  title_zh?: string
  duration?: number
  output_path?: string
  cover_path?: string
  stats?: Record<string, any>
}

const props = defineProps<{
  modelValue: boolean
  taskId: number | null
  item: PreviewItem | null
}>()

const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

const videoEl = ref<HTMLVideoElement | null>(null)
const loadError = ref('')

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const hasOutput = computed(() => Boolean(props.taskId && props.item?.output_path))

/** inline=1 让浏览器内嵌播放而不是触发下载 */
const videoUrl = computed(() =>
  hasOutput.value
    ? `${taskApi.fileUrl(props.taskId as number, props.item!.id, 'output')}&inline=1`
    : '',
)

const posterUrl = computed(() =>
  props.taskId && props.item?.cover_path
    ? taskApi.fileUrl(props.taskId, props.item.id, 'cover')
    : undefined,
)

const downloadUrl = computed(() =>
  hasOutput.value ? taskApi.fileUrl(props.taskId as number, props.item!.id, 'output') : '',
)

const title = computed(
  () => props.item?.title_zh || props.item?.title || (props.item ? `条目 ${props.item.id}` : ''),
)

const outputStats = computed(() => (props.item?.stats?.output ?? {}) as Record<string, any>)

const metaParts = computed(() => {
  const stats = outputStats.value
  const parts: string[] = []
  if (stats.resolution) parts.push(String(stats.resolution))
  const duration = stats.duration || props.item?.duration
  if (duration) parts.push(formatDuration(Number(duration)))
  if (stats.size_mb) parts.push(formatSize(Number(stats.size_mb)))
  parts.push(stats.dubbed ? '含 AI 配音' : '原声')
  return parts
})

/**
 * 关闭时必须暂停并释放 src。
 * 否则对话框关掉后声音还在播，且浏览器会继续占用这个文件的连接。
 */
function releaseVideo() {
  const el = videoEl.value
  if (!el) return
  try {
    el.pause()
    el.removeAttribute('src')
    el.load()
  } catch {
    /* 忽略：元素可能已被卸载 */
  }
}

watch(visible, async (open) => {
  if (open) {
    loadError.value = ''
    await nextTick()
    // 自动播放可能被浏览器拦截，失败不提示——用户点一下即可
    videoEl.value?.play?.().catch(() => undefined)
  } else {
    releaseVideo()
  }
})

function handleError() {
  loadError.value = '视频播放失败。文件可能已被清理，或在最新一次运行中被重新生成，请刷新页面重试。'
}

function openInNewTab() {
  if (videoUrl.value) window.open(videoUrl.value, '_blank', 'noopener')
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="title || '成片预览'"
    width="min(1000px, 92vw)"
    top="5vh"
    destroy-on-close
    class="preview-dialog"
    @closed="releaseVideo"
  >
    <div v-if="hasOutput" class="preview-body">
      <div class="player-wrap">
        <video
          ref="videoEl"
          class="player"
          controls
          playsinline
          preload="metadata"
          :src="videoUrl"
          :poster="posterUrl"
          @error="handleError"
        />
      </div>

      <el-alert
        v-if="loadError"
        type="error"
        :closable="false"
        show-icon
        :title="loadError"
        style="margin-top: 12px"
      />

      <div class="meta-row">
        <el-tag v-for="part in metaParts" :key="part" size="small" effect="plain">{{ part }}</el-tag>
        <span class="muted mono out-path">{{ item?.output_path }}</span>
      </div>
    </div>

    <el-empty v-else description="该条目还没有成片" :image-size="90" />

    <template #footer>
      <div class="footer-row">
        <span class="muted hint">拖动进度条可直接跳转；大文件按需分段加载，不会一次性占满内存</span>
        <div class="footer-actions">
          <el-button :icon="'TopRight'" :disabled="!hasOutput" @click="openInNewTab">新窗口打开</el-button>
          <a v-if="hasOutput" :href="downloadUrl">
            <el-button type="primary" :icon="'Download'">下载成片</el-button>
          </a>
          <el-button @click="visible = false">关闭</el-button>
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.preview-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.player-wrap {
  background: #000;
  border-radius: 8px;
  overflow: hidden;
  display: flex;
  justify-content: center;
}

.player {
  width: 100%;
  max-height: 62vh;
  display: block;
  background: #000;
}

.meta-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.out-path {
  font-size: 11.5px;
  word-break: break-all;
}

.footer-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.hint {
  font-size: 12px;
  text-align: left;
}

.footer-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}
</style>
