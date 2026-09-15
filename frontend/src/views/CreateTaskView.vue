<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
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
  // 默认手动发布；这个值最终以「系统配置 → 发布 → 自动发布」为准
  autoPublish: false,
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

/** 发布策略由系统配置统一决定，创建页只做展示，避免两处各改一次 */
const publishPolicyHint = computed(() =>
  form.autoPublish
    ? '自动发布已在「系统配置 → 发布」中开启：任务跑完会直接上传抖音。'
    : '默认手动发布：任务跑完只产出成片与文案，在任务详情页点「立即发布」才会上传抖音。',
)

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

/** 解析结果摘要卡：单视频展示封面与时长，合集/频道展示条目规模 */
const probeSummary = computed(() => {
  const data = probe.value
  if (!data) return null
  const first = selectedEntries.value[0] || data.entries[0] || null
  return {
    kind: data.source_type === 'video' ? '单个视频' : data.source_type === 'channel' ? '频道' : '合集',
    title: data.title || first?.title || '未命名',
    author: data.author || first?.author || '未知作者',
    total: data.total,
    duration: formatDuration(selectedDuration.value),
    thumbnail: first?.thumbnail || '',
  }
})

/** 解析成功后把结果滚进视野，避免用户以为「点了没反应」 */
const resultAnchor = ref<HTMLDivElement | null>(null)

async function handleProbe(explicit?: string) {
  const target = (explicit ?? url.value).trim()
  if (!target) {
    ElMessage.warning('请粘贴 YouTube 视频、合集或频道链接')
    return
  }
  if (probing.value) return
  url.value = target
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
      // 合集/频道：先弹列表让用户确认要搬运哪些条目，不直接建任务
      pickerVisible.value = true
      ElMessage.success(`解析到 ${result.total} 个视频，请先确认要搬运的条目`)
    } else {
      ElMessage.success('解析成功，已获取视频信息')
    }
    await nextTick()
    resultAnchor.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    probing.value = false
  }
}

/** 从剪贴板一键粘贴：读不到权限时不报错，交给用户手动粘贴 */
async function pasteFromClipboard() {
  try {
    const text = (await navigator.clipboard.readText()).trim()
    if (!text) {
      ElMessage.warning('剪贴板是空的')
      return
    }
    url.value = text
  } catch {
    ElMessage.info('浏览器未授予剪贴板读取权限，请手动粘贴（⌘V）')
  }
}

function openSettings(section: string) {
  router.push(`/settings?section=${section}`)
}

async function loadDefaults() {
  try {
    const [data, voiceList] = await Promise.all([
      settingsApi.get(),
      settingsApi.voices().catch(() => [] as Voice[]),
    ])
    voices.value = voiceList
    form.voice = data.config.tts?.voice || voiceList[0]?.id || ''
    form.autoPublish = data.config.publish?.auto_publish ?? false
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
      <ol class="step-rail">
        <li :class="['step-pill', step === 0 ? 'is-current' : 'is-done']">
          <span class="step-index">
            <el-icon v-if="step > 0"><Check /></el-icon>
            <template v-else>1</template>
          </span>
          粘贴链接
        </li>
        <li class="step-arrow"><el-icon><ArrowRight /></el-icon></li>
        <li :class="['step-pill', step === 1 ? 'is-current' : 'is-todo']">
          <span class="step-index">2</span>
          确认与选项
        </li>
      </ol>
    </div>

    <!-- 第一步：粘贴链接 -->
    <div class="panel paste-panel">
      <div class="paste-grid">
        <div class="paste-main">
          <label class="field-label" for="source-url">YouTube 链接</label>
          <el-input
            id="source-url"
            v-model="url"
            size="large"
            clearable
            placeholder="https://www.youtube.com/watch?v=..."
            @keyup.enter="handleProbe()"
          >
            <template #prepend><el-icon><Link /></el-icon></template>
            <template #append>
              <el-button :icon="'DocumentCopy'" @click="pasteFromClipboard">粘贴</el-button>
            </template>
          </el-input>

          <div class="paste-actions">
            <el-button
              type="primary"
              size="large"
              :icon="'Search'"
              :loading="probing"
              @click="handleProbe()"
            >
              解析链接
            </el-button>
            <el-button v-if="probe" size="large" :icon="'RefreshLeft'" @click="reset">重新输入</el-button>
          </div>
        </div>

        <aside class="paste-aside">
          <div v-if="!probe">
            <div class="aside-title">支持哪些链接</div>
            <ul class="link-kinds">
              <li><span class="kind">单个视频</span><code>youtube.com/watch?v=…</code></li>
              <li><span class="kind">合集</span><code>youtube.com/playlist?list=…</code></li>
              <li><span class="kind">频道</span><code>youtube.com/@handle</code></li>
            </ul>
            <p class="aside-foot">合集与频道会先弹出条目列表，勾选后才建立任务。</p>
          </div>

          <div v-else ref="resultAnchor" class="summary-card">
            <el-image v-if="probeSummary?.thumbnail" :src="probeSummary.thumbnail" fit="cover" class="summary-cover" lazy>
              <template #error>
                <div class="summary-cover fallback"><el-icon><Picture /></el-icon></div>
              </template>
            </el-image>
            <div class="summary-body">
              <el-tag size="small" effect="plain" type="success">{{ probeSummary?.kind }}</el-tag>
              <div class="summary-title">{{ probeSummary?.title }}</div>
              <div class="summary-meta muted">
                {{ probeSummary?.author }} · {{ probeSummary?.total }} 个视频 · {{ probeSummary?.duration }}
              </div>
            </div>
          </div>
        </aside>
      </div>
    </div>

    <!-- 第二步：搬运范围 -->
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
      <!-- 第二步：搬运范围 -->
      <div class="panel">
        <div class="panel-head">
          <div>
            <div class="panel-title">搬运范围</div>
            <div class="panel-sub muted">任务只会包含选中的条目，之后仍可在任务详情页取消个别条目</div>
          </div>
          <div class="range-facts">
            <span class="fact"><strong>{{ willProcessCount }}</strong> 个视频</span>
            <span class="fact-divider"></span>
            <span class="fact">预计原片 {{ formatDuration(selectedDuration) }}</span>
          </div>
        </div>

        <div v-if="isPlaylist" class="range-row">
          <el-tag effect="plain" type="success">{{ selectionHint }}</el-tag>
          <el-button size="small" :icon="'Edit'" @click="openPicker">重新选择</el-button>
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

      <!-- 第三步：处理与发布选项（两栏：左侧成片，右侧发布） -->
      <div class="option-columns">
        <div class="panel option-col">
          <div class="panel-head">
            <div>
              <div class="panel-title">成片与配音</div>
              <div class="panel-sub muted">留空即跟随「系统配置」</div>
            </div>
            <el-button text size="small" :icon="'Setting'" @click="openSettings('video')">系统配置</el-button>
          </div>

          <el-form label-position="top" class="option-form">
            <el-form-item label="任务名称">
              <el-input v-model="form.title" placeholder="便于在列表中识别，默认为视频/合集标题" maxlength="120" show-word-limit />
            </el-form-item>

            <el-form-item label="配音音色">
              <el-select v-model="form.voice" filterable placeholder="选择发音人" style="width: 100%">
                <el-option v-for="voice in voices" :key="voice.id" :label="voice.name" :value="voice.id">
                  <span>{{ voice.name }}</span>
                  <span class="muted" style="float: right; font-size: 12px">{{ voice.scene }}</span>
                </el-option>
              </el-select>
            </el-form-item>

            <el-form-item label="画面比例">
              <el-radio-group v-model="form.targetAspect">
                <el-radio-button value="original">保持原比例</el-radio-button>
                <el-radio-button value="9:16">竖屏 9:16</el-radio-button>
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
            </el-form-item>

            <el-form-item label="原视频音轨">
              <el-radio-group v-model="form.originalAudio">
                <el-radio value="remove">完全去掉（只保留 AI 配音）</el-radio>
                <el-radio value="keep">保留并压低音量</el-radio>
              </el-radio-group>
            </el-form-item>
          </el-form>
        </div>

        <div class="panel option-col">
          <div class="panel-head">
            <div>
              <div class="panel-title">发布到抖音</div>
              <div class="panel-sub muted">标题与话题由系统自动生成</div>
            </div>
            <el-button text size="small" :icon="'Setting'" @click="openSettings('publish')">系统配置</el-button>
          </div>

          <el-form label-position="top" class="option-form">
            <el-form-item label="发布方式">
              <div class="publish-row">
                <el-tag :type="form.autoPublish ? 'success' : 'info'" effect="plain" size="small">
                  {{ form.autoPublish ? '自动发布已开启' : '手动发布（默认）' }}
                </el-tag>
                <el-radio-group v-if="form.autoPublish" v-model="form.publishMode" size="small">
                  <el-radio-button value="immediate">立即</el-radio-button>
                  <el-radio-button value="scheduled">定时</el-radio-button>
                </el-radio-group>
              </div>
              <div v-if="form.autoPublish && form.publishMode === 'scheduled'" class="volume-row">
                <span class="muted">延迟</span>
                <el-input-number v-model="form.scheduleOffset" :min="5" :max="20160" controls-position="right" />
                <span class="muted">分钟后发布</span>
              </div>
              <div class="muted inline-help">{{ publishPolicyHint }}</div>
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
                :rows="4"
                maxlength="1000"
                show-word-limit
                placeholder="将作为抖音作品的正文描述；留空则只填标题与话题"
              />
            </el-form-item>
          </el-form>
        </div>
      </div>

      <div class="submit-bar">
        <div class="submit-summary">
          <strong>{{ willProcessCount }}</strong> 个视频
          <span class="muted">· 原片 {{ formatDuration(selectedDuration) }}</span>
          <span class="muted">
            · {{ form.autoPublish ? (form.publishMode === 'scheduled' ? `${form.scheduleOffset} 分钟后定时发布` : '结束后自动发布') : '仅产出成片，待手动发布' }}
          </span>
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
/* ---------- 步骤指示：两枚药丸 + 箭头，取代原来挤在右上角的 el-steps ---------- */
.step-rail {
  display: flex;
  align-items: center;
  gap: 8px;
  list-style: none;
  margin: 4px 0 0;
  padding: 0;
}

.step-pill {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 14px 6px 8px;
  border-radius: 999px;
  border: 1px solid var(--spark-border);
  background: #fff;
  color: var(--spark-text-2);
  font-size: 13px;
  white-space: nowrap;
}

.step-index {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  font-size: 12px;
  font-weight: 600;
  background: #eef1f6;
  color: #7b8390;
  flex: none;
}

.step-pill.is-current {
  border-color: var(--spark-primary);
  background: var(--spark-primary-9);
  color: var(--spark-primary);
  font-weight: 600;
}

.step-pill.is-current .step-index {
  background: var(--spark-primary);
  color: #fff;
}

.step-pill.is-done .step-index {
  background: #e8f5ec;
  color: #1f8a4c;
}

.step-arrow {
  color: #c3c9d4;
  display: grid;
  place-items: center;
}

/* ---------- 第一步：粘贴链接（输入区 + 右侧说明/结果卡） ---------- */
.paste-panel {
  padding: 20px;
}

.paste-grid {
  display: grid;
  /* 右侧第二栏给上限宽度：宽屏（1920）不会把输入框挤成 860px 而右侧拉出 507px，
     窄屏（<1080）由下面的媒体查询堆叠成一列。 */
  grid-template-columns: minmax(0, 1.6fr) minmax(300px, 420px);
  gap: 22px;
  align-items: start;
}

/* 追加在输入框里的「粘贴」按钮降级为中性按钮，不与「解析链接」抢主次 */
.paste-main :deep(.el-input-group__append) {
  background: #f5f7fa;
  border-color: var(--spark-border);
  color: var(--spark-text-2);
  box-shadow: none;
  padding: 0 4px;
}

.paste-main :deep(.el-input-group__append .el-button) {
  color: var(--spark-primary);
  font-weight: 500;
}

.field-label {
  display: block;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--spark-text-2);
  margin-bottom: 8px;
}

.paste-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 14px;
  flex-wrap: wrap;
}

.paste-aside {
  border-left: 1px solid var(--spark-border);
  padding-left: 22px;
  min-height: 96px;
}

.aside-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--spark-text-2);
  margin-bottom: 10px;
}

.link-kinds {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.link-kinds li {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}

.link-kinds .kind {
  flex: none;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--spark-primary-9);
  color: var(--spark-primary);
  font-size: 11.5px;
}

.link-kinds code {
  font-family: 'SF Mono', Menlo, Consolas, monospace;
  font-size: 11.5px;
  color: var(--spark-text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.aside-foot {
  margin: 12px 0 0;
  font-size: 11.5px;
  color: var(--spark-text-2);
  line-height: 1.6;
}

/* 解析结果摘要卡 */
.summary-card {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}

.summary-cover {
  width: 112px;
  height: 63px;
  border-radius: 8px;
  flex: none;
  display: block;
}

.summary-cover.fallback {
  display: grid;
  place-items: center;
  background: #f0f2f5;
  color: #a8b0bd;
}

.summary-body {
  min-width: 0;
}

.summary-title {
  font-weight: 600;
  font-size: 13.5px;
  line-height: 1.45;
  margin: 6px 0 4px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.summary-meta {
  font-size: 11.5px;
  line-height: 1.6;
}

/* ---------- 面板头部：标题 + 说明 + 右侧事实 ---------- */
.panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}

.panel-head .panel-title {
  margin: 0 0 4px;
}

.panel-sub {
  font-size: 12px;
  line-height: 1.5;
}

.range-facts {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 13px;
  color: var(--spark-text-2);
  white-space: nowrap;
}

.range-facts strong {
  color: var(--spark-primary);
  font-size: 15px;
}

.fact-divider {
  width: 1px;
  height: 14px;
  background: var(--spark-border);
}

/* ---------- 选项区：两栏，避免单列长表单一路滚到底 ---------- */
.option-columns {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: 16px;
  margin-top: 16px;
}

.option-col {
  display: flex;
  flex-direction: column;
}

.option-form {
  flex: 1;
}

.option-form :deep(.el-form-item) {
  margin-bottom: 20px;
}

.option-form :deep(.el-form-item:last-child) {
  margin-bottom: 0;
}

.option-form :deep(.el-form-item__label) {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--spark-text-2);
  padding-bottom: 6px;
  line-height: 1.4;
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
  gap: 14px;
  flex-wrap: wrap;
}

.volume-row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  margin-top: 10px;
}

.inline-help {
  font-size: 12px;
  margin-top: 6px;
  display: block;
  line-height: 1.6;
}

.submit-bar {
  position: sticky;
  bottom: 0;
  z-index: 5;
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

.submit-summary {
  font-size: 13px;
  color: var(--spark-text);
}

.submit-summary strong {
  font-size: 16px;
  color: var(--spark-primary);
  margin-right: 2px;
}

.submit-actions {
  display: flex;
  gap: 10px;
}

/* 窄屏：说明卡移到输入框下方，两栏选项合并成一栏 */
@media (max-width: 1080px) {
  .paste-grid,
  .option-columns {
    grid-template-columns: minmax(0, 1fr);
  }

  .paste-aside {
    border-left: none;
    border-top: 1px solid var(--spark-border);
    padding: 16px 0 0;
    min-height: 0;
  }
}

@media (max-width: 680px) {
  .step-rail {
    display: none;
  }

  .paste-actions .el-button {
    flex: 1;
  }

  .submit-bar {
    position: static;
  }

  .submit-actions {
    width: 100%;
  }

  .submit-actions .el-button {
    flex: 1;
  }
}
</style>
