<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { settingsApi, taskApi } from '@/api'
import CollectionPicker from '@/components/CollectionPicker.vue'
import type { CreateTaskPayload, ProbeEntry, ProbeResponse, TaskOptions, Voice } from '@/types'
import { formatDuration, formatNumber, shortUrl } from '@/utils/format'

const router = useRouter()

const step = ref(0)
const url = ref('')
const probing = ref(false)
const creating = ref(false)
const probe = ref<ProbeResponse | null>(null)

/** 合集/频道弹窗：必须先勾选确认，才允许创建任务 */
const pickerVisible = ref(false)
const selectionConfirmed = ref(false)
const selectedIds = ref<string[]>([])
/** 最近一次解析成功的链接，用于判断用户是否改动了输入 */
const probedUrl = ref('')

const voices = ref<Voice[]>([])
const defaultsLoaded = ref(false)

const form = reactive({
  title: '',
  voice: '',
  autoPublish: true,
  publishMode: 'immediate' as 'immediate' | 'scheduled',
  scheduleOffset: 60,
  targetAspect: 'original' as 'original' | '9:16' | '16:9',
  burnSubtitles: true,
  originalAudio: 'remove' as 'remove' | 'keep',
  subtitleMode: 'bilingual' as 'bilingual' | 'zh' | 'en',
  bgmVolume: 0.12,
  description: '',
  tags: [] as string[],
})

const isPlaylist = computed(() => (probe.value?.total ?? 0) > 1)
const entries = computed(() => probe.value?.entries || [])

/** 勾选后的条目（单视频恒为该视频本身） */
const selectedEntries = computed(() => {
  if (!probe.value) return [] as ProbeEntry[]
  if (!isPlaylist.value) return entries.value
  if (selectedIds.value.includes('all')) return entries.value
  const wanted = new Set(selectedIds.value)
  return entries.value.filter((e) => wanted.has(e.video_id))
})

const willProcessCount = computed(() => selectedEntries.value.length)
const selectedDuration = computed(() => selectedEntries.value.reduce((sum, e) => sum + (e.duration || 0), 0))
const selectionReady = computed(() => !isPlaylist.value || selectionConfirmed.value)
const previewEntries = computed(() => selectedEntries.value.slice(0, 50))
const selectionHint = computed(() => {
  if (!isPlaylist.value) return ''
  if (!selectionConfirmed.value) return '尚未确认搬运范围'
  if (selectedEntries.value.length === entries.value.length) return `已确认全部 ${entries.value.length} 个视频`
  return `已从 ${entries.value.length} 个视频中勾选 ${selectedEntries.value.length} 个`
})

async function loadDefaults() {
  try {
    const [data, voiceList] = await Promise.all([
      settingsApi.get(),
      settingsApi.voices().catch(() => [] as Voice[]),
    ])
    voices.value = voiceList
    form.voice = data.config.tts?.voice || voiceList[0]?.id || ''
    form.autoPublish = data.config.publish?.auto_publish ?? true
    form.targetAspect = (data.config.video?.target_aspect as any) || 'original'
    form.burnSubtitles = data.config.video?.burn_subtitles ?? true
    form.originalAudio = (data.config.video?.original_audio as 'remove' | 'keep') ?? 'remove'
    form.subtitleMode = (data.config.video?.subtitle_mode as 'bilingual' | 'zh' | 'en') ?? 'bilingual'
    form.bgmVolume = data.config.video?.bgm_volume ?? 0.12
    form.tags = [...(data.config.publish?.default_tags || [])]
    const offset = data.config.publish?.schedule_offset_minutes || 0
    if (offset > 0) {
      form.publishMode = 'scheduled'
      form.scheduleOffset = offset
    }
  } catch {
    /* 默认值加载失败不阻塞操作 */
  } finally {
    defaultsLoaded.value = true
  }
}

async function handleProbe() {
  const target = url.value.trim()
  if (!target) {
    ElMessage.warning('请粘贴 YouTube 视频、合集或频道链接')
    return
  }
  probing.value = true
  try {
    const result = await taskApi.probe(target)
    probe.value = result
    probedUrl.value = target
    form.title = result.title || ''
    if (result.entries[0]?.author && !form.title) form.title = result.entries[0].author
    selectedIds.value = []
    selectionConfirmed.value = false
    step.value = 1
    if (result.total > 1) {
      // 合集/频道：先弹列表让用户确认要搬运哪些视频，不直接建任务
      pickerVisible.value = true
      ElMessage.success(`解析到 ${result.total} 个视频，请先确认要搬运的条目`)
    } else {
      ElMessage.success('解析成功，已获取视频信息')
    }
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    probing.value = false
  }
}

function openPicker() {
  pickerVisible.value = true
}

function handlePickerConfirm(videoIds: string[]) {
  selectedIds.value = [...videoIds]
  selectionConfirmed.value = true
  pickerVisible.value = false
  const count = videoIds.includes('all') ? entries.value.length : videoIds.length
  ElMessage.success(`已确认搬运 ${count} 个视频`)
}

function buildOptions(): TaskOptions {
  const options: TaskOptions = {
    auto_publish: form.autoPublish,
    target_aspect: form.targetAspect,
    burn_subtitles: form.burnSubtitles,
    original_audio: form.originalAudio,
    subtitle_mode: form.subtitleMode,
    bgm_volume: form.bgmVolume,
  }
  if (form.voice) options.voice = form.voice
  if (form.publishMode === 'scheduled') {
    options.schedule_offset_minutes = form.scheduleOffset
  } else {
    options.schedule_offset_minutes = 0
  }
  if (form.description.trim()) options.description = form.description.trim()
  if (form.tags.length) options.tags = [...form.tags]
  return options
}

async function handleCreate(autoStart: boolean) {
  if (!probe.value) return
  if (!selectionReady.value) {
    ElMessage.warning('这是合集链接，请先在列表中确认要搬运的视频')
    openPicker()
    return
  }
  if (!willProcessCount.value) {
    ElMessage.warning('至少选择 1 个视频')
    openPicker()
    return
  }
  creating.value = true
  try {
    const payload: CreateTaskPayload = {
      url: url.value.trim(),
      title: form.title.trim(),
      auto_start: autoStart,
      options: buildOptions(),
    }
    if (isPlaylist.value) {
      payload.selected_video_ids = [...selectedIds.value]
    }
    const task = await taskApi.create(payload)
    ElMessage.success(
      autoStart
        ? `任务已创建并开始执行（${task.total_items} 个视频）`
        : `任务已创建并保存为待执行（${task.total_items} 个视频）`,
    )
    router.push(`/tasks/${task.id}`)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    creating.value = false
  }
}

function reset() {
  step.value = 0
  probe.value = null
  url.value = ''
  selectedIds.value = []
  selectionConfirmed.value = false
  pickerVisible.value = false
}

// 链接被改动后，之前的勾选结果不再对应当前输入，必须重新解析确认
watch(url, () => {
  if (!selectionConfirmed.value) return
  if (url.value.trim() !== probedUrl.value) {
    selectionConfirmed.value = false
    selectedIds.value = []
  }
})

onMounted(() => {
  loadDefaults()
})
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2 class="page-title">新建搬运任务</h2>
        <p class="page-subtitle">
          粘贴 YouTube 链接，系统会自动完成下载 → 英译中 → AI 配音 → 字幕合成 → 发布抖音。
        </p>
      </div>
      <el-steps :active="step" simple style="max-width: 420px">
        <el-step title="粘贴链接" />
        <el-step title="确认与选项" />
      </el-steps>
    </div>

    <!-- Step 0 -->
    <div class="panel">
      <div class="panel-title">第一步 · 粘贴链接</div>
      <el-input
        v-model="url"
        size="large"
        clearable
        placeholder="https://www.youtube.com/watch?v=... 或 https://www.youtube.com/playlist?list=..."
        @keyup.enter="handleProbe"
      >
        <template #prepend><el-icon><Link /></el-icon></template>
      </el-input>

      <div class="row-actions">
        <el-button type="primary" size="large" :loading="probing" :icon="'Search'" @click="handleProbe">
          解析链接
        </el-button>
        <el-button v-if="probe" size="large" @click="reset">重新输入</el-button>
        <span class="muted tip">
          支持：单个视频、合集（playlist）、频道（@handle 或 /channel/）链接
        </span>
      </div>

      <div v-if="probe" class="probe-summary">
        <el-tag effect="plain" type="success">
          {{ probe.source_type === 'video' ? '单个视频' : probe.source_type === 'channel' ? '频道' : '合集' }}
        </el-tag>
        <span class="summary-title">{{ probe.title || '未命名' }}</span>
        <span class="muted">· {{ probe.author || '未知作者' }}</span>
        <span class="muted">· 共 {{ probe.total }} 个视频</span>
      </div>
    </div>

    <!-- Step 1 -->
    <template v-if="probe">
      <div v-if="isPlaylist && !selectionReady" class="panel picker-gate">
        <div class="gate-icon"><el-icon><WarningFilled /></el-icon></div>
        <div class="gate-body">
          <div class="gate-title">这是{{ probe.source_type === 'channel' ? '频道' : '合集' }}链接（共 {{ probe.total }} 个视频）</div>
          <p class="muted gate-desc">
            为避免一次性创建大量任务，请先在合集列表中勾选要搬运的视频，确认后才会建立任务。
          </p>
        </div>
        <el-button type="primary" size="large" :icon="'List'" @click="openPicker">打开合集列表选择</el-button>
      </div>

      <template v-else>
      <div class="panel">
        <div class="panel-title">
          <span>第二步 · 搬运范围</span>
          <span class="muted">
            将处理 {{ willProcessCount }} 个视频，预计原片总时长 {{ formatDuration(selectedDuration) }}
          </span>
        </div>

        <div v-if="isPlaylist" class="range-row">
          <el-tag effect="plain" type="success">{{ selectionHint }}</el-tag>
          <el-button size="small" :icon="'Edit'" @click="openPicker">重新选择</el-button>
          <span class="muted tip">任务只会包含你勾选的条目，之后仍可在任务详情页取消个别条目</span>
        </div>

        <el-table :data="previewEntries" max-height="320" style="width: 100%">
          <el-table-column type="index" label="#" width="56" />
          <el-table-column label="封面" width="96">
            <template #default="{ row }">
              <el-image :src="row.thumbnail" fit="cover" class="thumb" lazy>
                <template #error>
                  <div class="thumb-fallback"><el-icon><Picture /></el-icon></div>
                </template>
              </el-image>
            </template>
          </el-table-column>
          <el-table-column label="标题" min-width="320" show-overflow-tooltip>
            <template #default="{ row }">
              <div>{{ row.title || row.video_id }}</div>
              <div class="muted mono" style="font-size: 11.5px">{{ row.video_id }}</div>
            </template>
          </el-table-column>
          <el-table-column label="时长" width="100">
            <template #default="{ row }">{{ formatDuration(row.duration) }}</template>
          </el-table-column>
          <el-table-column label="作者" width="160" show-overflow-tooltip>
            <template #default="{ row }"><span class="muted">{{ row.author || '-' }}</span></template>
          </el-table-column>
        </el-table>
        <div v-if="willProcessCount > previewEntries.length" class="muted more-tip">
          仅预览前 {{ previewEntries.length }} 个，实际将处理 {{ willProcessCount }} 个视频
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">
          <span>第三步 · 处理与发布选项</span>
          <span class="muted">留空即跟随「系统配置」中的默认值</span>
        </div>

        <el-form label-width="150px" label-position="left">
          <el-form-item label="任务名称">
            <el-input v-model="form.title" placeholder="便于在列表中识别，默认为视频/合集标题" maxlength="120" show-word-limit />
          </el-form-item>

          <el-form-item label="配音音色">
            <el-select v-model="form.voice" filterable placeholder="选择阿里云发音人" style="width: 320px">
              <el-option v-for="voice in voices" :key="voice.id" :label="voice.name" :value="voice.id">
                <span>{{ voice.name }}</span>
                <span class="muted" style="float: right; font-size: 12px">{{ voice.scene }}</span>
              </el-option>
            </el-select>
            <span class="inline-help muted">Speech Rate / Pitch 等细项在「系统配置 → 语音合成」中调整</span>
          </el-form-item>

          <el-form-item label="画面比例">
            <el-radio-group v-model="form.targetAspect">
              <el-radio-button value="original">保持原比例</el-radio-button>
              <el-radio-button value="9:16">竖屏 9:16（抖音）</el-radio-button>
              <el-radio-button value="16:9">横屏 16:9</el-radio-button>
            </el-radio-group>
          </el-form-item>

          <el-form-item label="字幕">
            <div class="switch-row">
              <el-switch v-model="form.burnSubtitles" active-text="烧录字幕" />
              <el-radio-group v-model="form.subtitleMode" :disabled="!form.burnSubtitles">
                <el-radio-button value="bilingual">中英双语</el-radio-button>
                <el-radio-button value="zh">仅中文</el-radio-button>
                <el-radio-button value="en">仅英文</el-radio-button>
              </el-radio-group>
            </div>
            <div class="muted inline-help">
              字号、边距与位置在「系统配置 → 成片合成」中调整（以 1080p 为基准等比缩放）
            </div>
          </el-form-item>

          <el-form-item label="原视频音轨">
            <el-radio-group v-model="form.originalAudio">
              <el-radio value="remove">完全去掉（只保留 AI 配音）</el-radio>
              <el-radio value="keep">保留并压低音量</el-radio>
            </el-radio-group>
            <div class="muted inline-help">
              默认完全去掉原声（人声与背景音乐都不保留）
            </div>
          </el-form-item>

          <el-form-item label="发布设置">
            <div class="publish-row">
              <el-switch v-model="form.autoPublish" active-text="处理完成后自动发布到抖音" />
              <el-radio-group v-if="form.autoPublish" v-model="form.publishMode">
                <el-radio value="immediate">立即发布</el-radio>
                <el-radio value="scheduled">定时发布</el-radio>
              </el-radio-group>
              <div v-if="form.autoPublish && form.publishMode === 'scheduled'" class="volume-row">
                <span class="muted">延迟</span>
                <el-input-number v-model="form.scheduleOffset" :min="5" :max="20160" controls-position="right" />
                <span class="muted">分钟后发布（最多 14 天）</span>
              </div>
            </div>
            <div v-if="!form.autoPublish" class="muted inline-help">
              关闭后只产出成片与文案，可在任务详情页确认后手动发布
            </div>
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
            <el-input
              v-model="form.description"
              type="textarea"
              :rows="3"
              maxlength="1000"
              show-word-limit
              placeholder="将作为抖音作品的正文描述；留空则只填标题与话题"
            />
          </el-form-item>
        </el-form>
      </div>

      <div class="submit-bar">
        <div class="muted">
          将处理 <strong>{{ willProcessCount }}</strong> 个视频
          <template v-if="form.autoPublish">
            ｜结束后{{ form.publishMode === 'scheduled' ? `${form.scheduleOffset} 分钟后定时` : '立即' }}发布到抖音
          </template>
          <template v-else>｜仅产出成片，不自动发布</template>
        </div>
        <div class="submit-actions">
          <el-button size="large" :loading="creating" @click="handleCreate(false)">仅创建（不执行）</el-button>
          <el-button type="primary" size="large" :loading="creating" @click="handleCreate(true)">
            创建并开始执行
          </el-button>
        </div>
      </div>
      </template>
    </template>

    <CollectionPicker
      v-model="pickerVisible"
      :title="probe?.title || ''"
      :author="probe?.author || ''"
      :source-type="probe?.source_type || 'playlist'"
      :entries="entries"
      @confirm="handlePickerConfirm"
    />
  </div>
</template>

<style scoped>
.row-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 14px;
  flex-wrap: wrap;
}

.tip {
  font-size: 12.5px;
}

.probe-summary {
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px dashed var(--spark-border);
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.summary-title {
  font-weight: 600;
}

.range-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}

.picker-gate {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  border-color: #f0d9a8;
  background: linear-gradient(0deg, #fffdf6, #fffdf6);
}

.gate-icon {
  width: 40px;
  height: 40px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  font-size: 20px;
  color: #d97706;
  background: #fdf3e0;
  flex: none;
}

.gate-body {
  flex: 1 1 260px;
  min-width: 240px;
}

.gate-title {
  font-weight: 600;
  margin-bottom: 4px;
}

.gate-desc {
  margin: 0;
  font-size: 13px;
}

.range-label {
  color: var(--spark-text-2);
  font-size: 13px;
}

.thumb {
  width: 72px;
  height: 42px;
  border-radius: 6px;
  display: block;
}

.thumb-fallback {
  width: 72px;
  height: 42px;
  border-radius: 6px;
  background: #f0f2f5;
  display: grid;
  place-items: center;
  color: #a8b0bd;
}

.more-tip {
  margin-top: 10px;
  font-size: 12.5px;
}

.switch-row,
.publish-row {
  display: flex;
  align-items: center;
  gap: 18px;
  flex-wrap: wrap;
}

.volume-row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
}

.inline-help {
  font-size: 12px;
  margin-top: 6px;
  display: block;
}

.submit-bar {
  position: sticky;
  bottom: 0;
  margin-top: 16px;
  background: #fff;
  border: 1px solid var(--spark-border);
  border-radius: var(--spark-radius);
  box-shadow: 0 -4px 20px -8px rgba(15, 23, 42, 0.18);
  padding: 14px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.submit-actions {
  display: flex;
  gap: 10px;
}
</style>
