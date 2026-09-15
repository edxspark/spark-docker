<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { settingsApi } from '@/api'
import type { RuntimeInfo, SettingsSectionMeta, TestResult, Voice } from '@/types'

type FieldType = 'text' | 'password' | 'number' | 'switch' | 'select' | 'textarea' | 'tags' | 'json'

interface FieldMeta {
  label: string
  type: FieldType
  help?: string
  options?: { label: string; value: any }[]
  min?: number
  max?: number
  step?: number
  filterable?: boolean
  allowCreate?: boolean
}

/** 配置项元数据：决定每个字段的中文标签、控件类型与提示文案 */
const FIELD_META: Record<string, FieldMeta> = {
  // 通用
  'general.default_voice': { label: '默认配音音色', type: 'select', filterable: true, help: '新建任务时的默认阿里云发音人。实际可用性与音色列表以阿里云控制台开通情况为准' },
  'general.max_concurrent_tasks': { label: '并发任务数', type: 'number', min: 1, max: 8, help: '同时执行的任务数量。过高会争抢带宽与 CPU，建议 1-2' },
  'general.keep_source_files': { label: '保留源文件', type: 'switch', help: '关闭后成片完成后删除下载的原视频以节省磁盘' },
  'general.log_retention_days': { label: '日志保留天数', type: 'number', min: 1, max: 365 },
  'general.auto_clean_failed': { label: '自动清理失败任务文件', type: 'switch' },

  // 翻译
  'translator.provider': {
    label: '翻译服务',
    type: 'select',
    options: [
      { label: 'DeepSeek（真实翻译）', value: 'deepseek' },
      { label: 'Mock（离线示例）', value: 'mock' },
    ],
    help: 'Mock 模式不调用外部接口，仅用规则生成占位译文，用于无密钥时验证整条流水线',
  },
  'translator.api_key': { label: 'DeepSeek API Key', type: 'password', help: '在 platform.deepseek.com 创建。展示为脱敏值，留空或保持脱敏不改动即保留原值' },
  'translator.base_url': { label: 'API 地址', type: 'text', help: '默认 https://api.deepseek.com，使用中转服务时修改此处' },
  'translator.model': {
    label: '模型',
    type: 'select',
    filterable: true,
    allowCreate: true,
    options: [
      { label: 'deepseek-chat（推荐，性价比高）', value: 'deepseek-chat' },
      { label: 'deepseek-reasoner（更慢更强）', value: 'deepseek-reasoner' },
    ],
  },
  'translator.temperature': { label: '温度', type: 'number', min: 0, max: 2, step: 0.1, help: 'DeepSeek 官方建议翻译类任务使用 1.3' },
  'translator.timeout': { label: '超时（秒）', type: 'number', min: 10, max: 600 },
  'translator.batch_size': { label: '每批条数', type: 'number', min: 1, max: 100, help: '一次提交给模型的字幕条数，过大可能触发长度限制' },
  'translator.context_size': { label: '上下文条数', type: 'number', min: 0, max: 20, help: '额外送入前后各 N 条字幕帮助模型理解语境，不参与翻译输出' },
  'translator.retry': { label: '重试次数', type: 'number', min: 0, max: 10 },
  'translator.style_prompt': { label: '翻译风格提示词', type: 'textarea', help: '直接影响译文的口语化程度与风格，可按需改写' },
  'translator.glossary': { label: '术语对照表', type: 'json', help: 'JSON 对象，例如 {"OpenAI":"OpenAI","Python":"Python"}，用于固定专有名词译法' },

  // 语音合成
  'tts.provider': {
    label: '语音合成服务',
    type: 'select',
    options: [
      { label: '阿里云智能语音交互（真实配音）', value: 'aliyun' },
      { label: 'Mock（离线静音占位）', value: 'mock' },
    ],
    help: 'Mock 会按文本长度生成等长静音音轨，用于验证对轴逻辑而无需消耗额度',
  },
  'tts.access_key_id': { label: 'AccessKey ID', type: 'password', help: '阿里云 RAM 用户的 AccessKey ID，建议使用只授予 ISI 权限的子账号' },
  'tts.access_key_secret': { label: 'AccessKey Secret', type: 'password' },
  'tts.app_key': { label: '项目 AppKey', type: 'password', help: '阿里云智能语音交互控制台创建项目后获得，语音合成必需' },
  'tts.token': { label: '手工 Token', type: 'password', help: '可选项。填写后直接使用该 Token；留空则由 AK/SK 自动签发并缓存 24 小时' },
  'tts.region': {
    label: '服务地域',
    type: 'select',
    options: [
      { label: '华东2（上海）', value: 'cn-shanghai' },
      { label: '华北2（北京）', value: 'cn-beijing' },
    ],
  },
  'tts.voice': { label: '发音人', type: 'select', options: [], filterable: true, help: '选项来自内置的常用发音人列表，实际可用性取决于控制台开通情况' },
  'tts.format': {
    label: '音频格式',
    type: 'select',
    options: [
      { label: 'mp3（推荐）', value: 'mp3' },
      { label: 'wav', value: 'wav' },
      { label: 'pcm', value: 'pcm' },
    ],
  },
  'tts.sample_rate': {
    label: '采样率',
    type: 'select',
    options: [
      { label: '8000 Hz', value: 8000 },
      { label: '16000 Hz', value: 16000 },
      { label: '24000 Hz', value: 24000 },
      { label: '48000 Hz', value: 48000 },
    ],
    help: '部分音色仅支持 8000/16000/24000，若合成报错请下调',
  },
  'tts.volume': { label: '音量', type: 'number', min: 0, max: 100 },
  'tts.speech_rate': { label: '语速', type: 'number', min: -500, max: 500, help: '范围 -500~500，正值更快。中文变长时可适当调高' },
  'tts.pitch_rate': { label: '语调', type: 'number', min: -500, max: 500, help: '范围 -500~500，正值更高' },
  'tts.gain_db': { label: '输出增益（dB）', type: 'number', min: -20, max: 20, step: 0.5 },
  'tts.max_chars_per_request': { label: '单请求字符上限', type: 'number', min: 50, max: 300, help: '阿里云单次请求上限 300 字符，超长文本会自动按标点切分后拼接' },
  'tts.concurrency': { label: '合成并发数', type: 'number', min: 1, max: 16, help: '过高可能触发限流（错误码 429）' },

  // 语音识别（无字幕兜底）
  'asr.provider': {
    label: '语音识别服务',
    type: 'select',
    options: [
      { label: '阿里云智能语音交互（真实识别）', value: 'aliyun' },
      { label: 'Mock（离线占位）', value: 'mock' },
    ],
    help: '凭证与「语音合成」共用同一个阿里云项目 AppKey，无需重复填写',
  },
  'asr.enabled': {
    label: '无字幕时自动识别',
    type: 'switch',
    help: '视频没有字幕时，自动用语音识别生成原文，再翻译、配音并替换原音轨。关闭则保留原声',
  },
  'asr.language': { label: '识别语言', type: 'text', help: '仅用于提示。实际语种由阿里云控制台该项目的识别模型决定——英文视频必须选择英文模型，否则识别结果为空' },
  'asr.max_chunk_seconds': { label: '单块时长上限（秒）', type: 'number', min: 5, max: 55, help: '阿里云单次请求音频不超过 60 秒，按静音边界切块，留出余量' },
  'asr.concurrency': { label: '识别并发数', type: 'number', min: 1, max: 8, help: '并发过高可能触发限流（错误码 40000005）' },
  'asr.silence_threshold_db': { label: '静音判定阈值（dB）', type: 'number', min: -60, max: -10, help: '低于该响度视为静音。背景音乐较响时可适当调高（如 -30）' },
  'asr.min_silence_seconds': { label: '最短静音时长（秒）', type: 'number', min: 0.1, max: 3, step: 0.05, help: '用于切分语音块，避免把单词切成两半' },
  'asr.remove_fillers': { label: '去除口语填充词', type: 'switch', help: '去掉「嗯」「呃」之类的词，译文更干净' },
  'asr.max_duration_minutes': {
    label: '识别时长上限（分钟）',
    type: 'number',
    min: 0,
    max: 600,
    help: '0 表示不限制。语音识别按音频时长计费，长视频建议设上限以避免意外开销',
  },

  // 下载
  'download.format': { label: 'yt-dlp 格式', type: 'text', help: 'yt-dlp 的 -f 表达式，默认优先 1080p 以内的 mp4 以兼容抖音' },
  'download.max_height': {
    label: '最大分辨率',
    type: 'select',
    options: [
      { label: '480p', value: 480 },
      { label: '720p', value: 720 },
      { label: '1080p', value: 1080 },
      { label: '1440p', value: 1440 },
      { label: '2160p（4K）', value: 2160 },
    ],
  },
  'download.subtitle_langs': { label: '字幕语言优先级', type: 'tags', help: '按顺序尝试，例如 en、en-US、en-GB。没有字幕时会跳过翻译与配音并保留原声' },
  'download.prefer_manual_subtitle': { label: '优先人工字幕', type: 'switch', help: '关闭则优先使用自动生成字幕' },
  'download.write_thumbnail': { label: '下载缩略图', type: 'switch', help: '缩略图会作为抖音封面候选' },
  'download.cookies_file': { label: 'Cookies 文件', type: 'text', help: 'YouTube 需要登录（如年龄限制视频）时，填写 cookies.txt 的绝对路径' },
  'download.proxy': { label: '代理地址', type: 'text', help: '例如 http://127.0.0.1:7890。中国大陆环境访问 YouTube 通常必须配置' },
  'download.js_runtime': {
    label: 'JavaScript 运行时',
    type: 'select',
    options: [
      { label: '自动探测（推荐）', value: '' },
      { label: 'deno', value: 'deno' },
      { label: 'node', value: 'node' },
      { label: 'bun', value: 'bun' },
      { label: 'quickjs', value: 'quickjs' },
    ],
    help: 'YouTube 提取需要 JS 运行时解算签名。留空则由系统自动探测（deno > node > bun > quickjs），本机通常用 node',
  },
  'download.retries': { label: '重试次数', type: 'number', min: 0, max: 20 },
  'download.sleep_interval': { label: '分片间隔（秒）', type: 'number', min: 0, max: 60, step: 0.5, help: '每个分片下载后等待，降低被限流的概率' },
  'download.rate_limit': { label: '限速', type: 'text', help: '例如 5M 表示限制为 5 MB/s，留空表示不限速' },

  // 发布
  'publish.provider': {
    label: '发布服务',
    type: 'select',
    options: [
      { label: '抖音（真实发布）', value: 'douyin' },
      { label: 'Mock（模拟发布）', value: 'mock' },
    ],
    help: 'Mock 不会打开浏览器，仅生成一条模拟发布记录',
  },
  'publish.headless': { label: '无头模式', type: 'switch', help: '开启后浏览器不显示窗口。首次调试建议关闭，以便观察页面状态' },
  'publish.auto_publish': { label: '自动发布', type: 'switch', help: '关闭后流水线只产出成片与文案，需在任务详情页手动确认发布' },
  'publish.title_max_len': { label: '标题最大长度', type: 'number', min: 1, max: 200, help: '抖音标题上限为 30 字，超出会被截断' },
  'publish.default_tags': { label: '默认话题标签', type: 'tags', help: '生成标题与话题失败时的兜底标签，不带 # 号' },
  'publish.schedule_offset_minutes': { label: '定时发布延迟（分钟）', type: 'number', min: 0, max: 20160, help: '0 表示立即发布；大于 0 表示处理完成后延迟 N 分钟以定时方式发布' },
  'publish.collect_url': { label: '回填作品链接', type: 'switch', help: '发布成功后尝试从作品管理页读取链接并保存' },
  'publish.concurrency': { label: '发布并发数', type: 'number', min: 1, max: 3, help: '同一账号建议串行（1），并发容易被风控' },
  'publish.timeout': { label: '上传超时（秒）', type: 'number', min: 60, max: 7200, help: '大文件上传需要更长时间，默认 600 秒' },

  // 成片合成
  'video.target_aspect': {
    label: '输出画面比例',
    type: 'select',
    options: [
      { label: '保持原比例', value: 'original' },
      { label: '竖屏 9:16（抖音推荐）', value: '9:16' },
      { label: '横屏 16:9', value: '16:9' },
    ],
    help: '转为 9:16 时主画面居中，两侧用放大模糊背景填充，避免黑边',
  },
  'video.burn_subtitles': { label: '烧录字幕', type: 'switch', help: '把字幕直接渲染进画面，抖音等平台更吃字幕' },
  'video.subtitle_mode': {
    label: '字幕内容',
    type: 'select',
    options: [
      { label: '中英双语（中文一行、英文一行）', value: 'bilingual' },
      { label: '仅中文', value: 'zh' },
      { label: '仅英文', value: 'en' },
    ],
    help: '双语时中文在上、英文在下',
  },
  'video.subtitle_font_size': {
    label: '字幕字号（1080p 基准）',
    type: 'number',
    min: 6,
    max: 120,
    help: '以 1080p 画面为基准的像素值，会按成片分辨率等比缩放：1080p 成片即该值本身，1080x1920 竖屏约为其 1.78 倍',
  },
  'video.subtitle_font_name': { label: '字幕字体', type: 'text', help: '留空自动选择系统中可用的中文字体。注意 macOS 的 PingFang SC 无法被 libass 加载，填写后可能触发字体回退' },
  'video.subtitle_margin_v': {
    label: '字幕边距（1080p 基准）',
    type: 'number',
    min: 0,
    max: 600,
    help: '距画面边缘的距离，同样按 1080p 基准等比缩放',
  },
  'video.subtitle_alignment': {
    label: '字幕位置',
    type: 'select',
    options: [
      { label: '底部居中', value: 'bottom' },
      { label: '画面中央', value: 'middle' },
      { label: '顶部居中', value: 'top' },
    ],
  },
  'video.subtitle_outline': { label: '字幕描边粗细', type: 'number', min: 0, max: 6, help: '描边能让字幕在浅色画面上也看得清' },
  'video.original_audio': {
    label: '原视频音轨',
    type: 'select',
    options: [
      { label: '完全去掉（人声与背景音乐都不要）', value: 'remove' },
      { label: '保留并压低音量', value: 'keep' },
    ],
    help: '默认完全去掉，成片只有 AI 配音。注意：若语音合成仍是 Mock（产出静音），成片将完全没有声音',
  },
  'video.bgm_volume': { label: '原音轨音量倍率', type: 'number', min: 0, max: 2, step: 0.02, help: '仅「保留并压低音量」时生效' },
  'video.voice_volume': { label: '配音音量倍率', type: 'number', min: 0, max: 4, step: 0.05 },
  'video.max_speedup': { label: '最大加速比', type: 'number', min: 1, max: 3, step: 0.05, help: '中文通常比英文短，但语速慢时会超出原时间窗，允许最多加速至此倍数，仍超长则截断' },
  'video.crf': { label: '画质 CRF', type: 'number', min: 0, max: 51, help: '越小画质越好、文件越大。18-23 为常用区间' },
  'video.preset': {
    label: '编码预设',
    type: 'select',
    options: ['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow'].map((v) => ({
      label: v,
      value: v,
    })),
    help: '越慢压缩率越高。medium 为平衡点，veryfast 可显著缩短渲染时间',
  },
}

const loading = ref(true)
const saving = ref(false)
const testing = ref<string | null>(null)
const activeTab = ref('general')
const sections = ref<SettingsSectionMeta[]>([])
const voices = ref<Voice[]>([])
const runtime = ref<RuntimeInfo | null>(null)
const paths = ref<Record<string, any> | null>(null)
const testResult = ref<{ section: string; result: TestResult } | null>(null)
const testDialog = ref(false)

/** 表单数据：每个分组的字段值 */
const form = reactive<Record<string, Record<string, any>>>({})
/** 原始快照，用于 diff 后只提交改动项 */
const pristine = ref<Record<string, Record<string, any>>>({})

const secretKeys = computed(() => {
  const map: Record<string, Set<string>> = {}
  for (const section of sections.value) {
    map[section.key] = new Set(section.secrets || [])
  }
  return map
})

function isSecret(section: string, key: string): boolean {
  return secretKeys.value[section]?.has(key) ?? false
}

function isMultiValue(value: any): boolean {
  return Array.isArray(value) || (typeof value === 'object' && value !== null)
}

function fieldMeta(section: string, key: string, value: any): FieldMeta {
  const known = FIELD_META[`${section}.${key}`]
  if (known) return known
  // 未登记的字段做类型推断兜底
  if (typeof value === 'boolean') return { label: key, type: 'switch' }
  if (typeof value === 'number') return { label: key, type: 'number' }
  if (Array.isArray(value) || (typeof value === 'object' && value !== null)) return { label: key, type: 'json' }
  return { label: key, type: 'text' }
}

function optionsFor(section: string, key: string, meta: FieldMeta) {
  if (`${section}.${key}` === 'tts.voice' || `${section}.${key}` === 'general.default_voice') {
    return voices.value.map((voice) => ({ label: voice.name, value: voice.id }))
  }
  return meta.options || []
}

function fieldsOf(section: string): string[] {
  const data = form[section] || {}
  return Object.keys(data).filter((key) => !key.endsWith('__set'))
}

function initialValue(section: string, key: string, value: any): any {
  if (isMultiValue(value)) return JSON.parse(JSON.stringify(value))
  return value
}

async function load() {
  loading.value = true
  try {
    const [data, voiceList] = await Promise.all([
      settingsApi.get(),
      settingsApi.voices().catch(() => [] as Voice[]),
    ])
    sections.value = data.sections
    voices.value = voiceList
    for (const section of data.sections) {
      const incoming = data.config[section.key] || {}
      const target: Record<string, any> = {}
      for (const [key, value] of Object.entries(incoming)) {
        target[key] = initialValue(section.key, key, value)
      }
      form[section.key] = target
    }
    pristine.value = JSON.parse(JSON.stringify(form))
    if (!sections.value.some((s) => s.key === activeTab.value)) {
      activeTab.value = sections.value[0]?.key || 'general'
    }
  } catch {
    /* 拦截器已提示 */
  } finally {
    loading.value = false
  }
}

async function loadRuntime() {
  try {
    const [rt, pathInfo] = await Promise.all([
      settingsApi.runtime(),
      settingsApi.paths().catch(() => null),
    ])
    runtime.value = rt
    paths.value = pathInfo
  } catch {
    /* 忽略 */
  }
}

function buildPatch(): Record<string, Record<string, any>> {
  const patch: Record<string, Record<string, any>> = {}
  for (const section of sections.value) {
    const key = section.key
    const current = form[key] || {}
    const before = pristine.value[key] || {}
    const changes: Record<string, any> = {}
    for (const [field, value] of Object.entries(current)) {
      if (field.endsWith('__set')) continue
      const previous = before[field]
      const changed = isMultiValue(value)
        ? JSON.stringify(value) !== JSON.stringify(previous)
        : value !== previous
      if (!changed) continue
      if (isSecret(key, field)) {
        const text = value == null ? '' : String(value)
        // 空串或仍是脱敏值 => 不提交，保留后端原值
        if (text === '' || /^\*+$/.test(text) || text.includes('********')) continue
      }
      // json 文本字段在提交前解析
      if (fieldMeta(key, field, previous).type === 'json' && typeof value === 'string') {
        try {
          changes[field] = JSON.parse(value)
        } catch {
          throw new Error(`${section.label} 的「${fieldMeta(key, field, previous).label}」不是合法 JSON`)
        }
      } else {
        changes[field] = value
      }
    }
    if (Object.keys(changes).length) patch[key] = changes
  }
  return patch
}

async function save() {
  let patch: Record<string, Record<string, any>>
  try {
    patch = buildPatch()
  } catch (error) {
    ElMessage.error((error as Error).message)
    return
  }
  if (!Object.keys(patch).length) {
    ElMessage.info('没有需要保存的修改')
    return
  }
  saving.value = true
  try {
    const data = await settingsApi.update(patch)
    sections.value = data.sections
    for (const section of data.sections) {
      const incoming = data.config[section.key] || {}
      const target: Record<string, any> = {}
      for (const [key, value] of Object.entries(incoming)) {
        target[key] = initialValue(section.key, key, value)
      }
      form[section.key] = target
    }
    pristine.value = JSON.parse(JSON.stringify(form))
    const changedSections = Object.keys(patch)
      .map((key) => sections.value.find((s) => s.key === key)?.label || key)
      .join('、')
    ElMessage.success(`已保存：${changedSections}`)
  } catch {
    /* 拦截器已提示 */
  } finally {
    saving.value = false
  }
}

async function resetSection() {
  const section = sections.value.find((s) => s.key === activeTab.value)
  if (!section) return
  try {
    await ElMessageBox.confirm(
      `将「${section.label}」的全部配置恢复为默认值，包括已保存的密钥。确定继续吗？`,
      '恢复默认配置',
      { type: 'warning', confirmButtonText: '恢复默认', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const result = await settingsApi.reset(section.key)
    ElMessage.success(result.message)
    await load()
  } catch {
    /* 拦截器已提示 */
  }
}

async function runTest() {
  const section = activeTab.value
  testing.value = section
  try {
    const result = await settingsApi.test(section)
    testResult.value = { section, result }
    testDialog.value = true
    if (result.ok) {
      ElMessage.success(result.message)
    } else {
      ElMessage.error(result.message)
    }
  } catch {
    /* 拦截器已提示 */
  } finally {
    testing.value = null
  }
}

/** 未登记的密钥字段标记：后端返回 xxx__set */
function secretSet(section: string, key: string): boolean {
  return Boolean(form[section]?.[`${key}__set`])
}

function fieldHelp(section: string, key: string, meta: FieldMeta): string {
  const parts: string[] = []
  if (meta.help) parts.push(meta.help)
  if (isSecret(section, key)) {
    parts.push(secretSet(section, key) ? '当前：已配置（留空表示不修改）' : '当前：未配置')
  }
  return parts.join('｜')
}

function runtimeTags() {
  const info = runtime.value
  if (!info) return []
  const jsRuntime = info.js_runtime
  return [
    { label: 'ffmpeg', ok: !!info.ffmpeg?.available, tip: info.ffmpeg?.path || '未检测到，请执行 brew install ffmpeg' },
    { label: 'ffprobe', ok: !!info.ffprobe?.available, tip: info.ffprobe?.path || '未检测到' },
    { label: 'yt-dlp', ok: !!info.yt_dlp?.available, tip: info.yt_dlp?.version || '未安装' },
    {
      label: `JS 运行时${jsRuntime?.name ? '（' + jsRuntime.name + '）' : ''}`,
      ok: !!jsRuntime?.available,
      tip:
        jsRuntime?.path ||
        '未找到 JavaScript 运行时，YouTube 提取需要它：brew install deno，或确保 node 在 PATH 中',
    },
    {
      label: 'TLS 伪装',
      ok: !!info.impersonation?.available,
      tip: info.impersonation?.note || '缺少 curl-cffi 时 YouTube 可能拒绝请求',
    },
    { label: 'EJS 求解器', ok: !!info.ejs?.available, tip: info.ejs?.note || '' },
    {
      label: 'playwright',
      ok: !!info.playwright?.available,
      tip: info.playwright?.available ? '已安装' : '未安装：pip install playwright && playwright install chromium',
    },
  ]
}

onMounted(async () => {
  await load()
  loadRuntime()
})
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="page-header">
      <div>
        <h2 class="page-title">系统配置</h2>
        <p class="page-subtitle">
          配置翻译、语音合成、下载与发布参数。所有密钥加密存储在本机数据库，接口只返回脱敏值。
        </p>
      </div>
      <div class="head-actions">
        <el-button :icon="'Refresh'" @click="load">重新读取</el-button>
        <el-button :icon="'MagicStick'" :loading="testing === activeTab" @click="runTest">
          连接测试
        </el-button>
        <el-button :icon="'RefreshLeft'" @click="resetSection">恢复当前分组默认</el-button>
        <el-button type="primary" :icon="'Check'" :loading="saving" @click="save">保存配置</el-button>
      </div>
    </div>

    <!-- 运行环境 -->
    <div class="panel">
      <div class="panel-title">运行环境自检</div>
      <div class="env-tags">
        <el-tooltip v-for="item in runtimeTags()" :key="item.label" :content="item.tip" placement="top">
          <el-tag :type="item.ok ? 'success' : 'danger'" effect="plain">
            {{ item.label }}：{{ item.ok ? '就绪' : '缺失' }}
          </el-tag>
        </el-tooltip>
        <el-tag v-if="runtime?.python" effect="plain">Python {{ runtime.python }}</el-tag>
      </div>
      <div v-if="runtime" class="muted env-tip">
        ffmpeg、yt-dlp 与 JavaScript 运行时是下载链路的必需项；TLS 伪装（curl-cffi）缺失时 YouTube
        会拒绝请求。缺任何一项都会在下载阶段失败，建议跑任务前先确认这里全绿。
      </div>
      <el-descriptions v-if="paths" :column="2" size="small" border style="margin-top: 14px">
        <el-descriptions-item label="数据根目录">
          <span class="mono">{{ paths.root }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="成片目录">
          <span class="mono">{{ paths.outputs }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="下载目录">
          <span class="mono">{{ paths.downloads }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="登录态目录">
          <span class="mono">{{ paths.auth }}</span>
        </el-descriptions-item>
      </el-descriptions>
    </div>

    <!-- 配置分组 -->
    <div class="panel">
      <el-tabs v-model="activeTab">
        <el-tab-pane v-for="section in sections" :key="section.key" :label="section.label" :name="section.key">
          <el-form label-width="170px" label-position="left" class="config-form">
            <el-form-item v-for="key in fieldsOf(section.key)" :key="key" :label="fieldMeta(section.key, key, form[section.key][key]).label">
              <!-- switch -->
              <el-switch
                v-if="fieldMeta(section.key, key, form[section.key][key]).type === 'switch'"
                v-model="form[section.key][key]"
              />

              <!-- number -->
              <el-input-number
                v-else-if="fieldMeta(section.key, key, form[section.key][key]).type === 'number'"
                v-model="form[section.key][key]"
                :min="fieldMeta(section.key, key, form[section.key][key]).min"
                :max="fieldMeta(section.key, key, form[section.key][key]).max"
                :step="fieldMeta(section.key, key, form[section.key][key]).step ?? 1"
                controls-position="right"
                style="width: 200px"
              />

              <!-- password（密钥） -->
              <el-input
                v-else-if="isSecret(section.key, key)"
                v-model="form[section.key][key]"
                type="password"
                show-password
                clearable
                style="width: 420px"
                :placeholder="secretSet(section.key, key) ? '已配置，留空表示不修改' : '尚未配置'"
              />

              <!-- select -->
              <el-select
                v-else-if="fieldMeta(section.key, key, form[section.key][key]).type === 'select'"
                v-model="form[section.key][key]"
                :filterable="fieldMeta(section.key, key, form[section.key][key]).filterable"
                :allow-create="fieldMeta(section.key, key, form[section.key][key]).allowCreate"
                default-first-option
                style="width: 420px"
              >
                <el-option
                  v-for="option in optionsFor(section.key, key, fieldMeta(section.key, key, form[section.key][key]))"
                  :key="String(option.value)"
                  :label="option.label"
                  :value="option.value"
                />
              </el-select>

              <!-- tags -->
              <el-select
                v-else-if="fieldMeta(section.key, key, form[section.key][key]).type === 'tags'"
                v-model="form[section.key][key]"
                multiple
                filterable
                allow-create
                default-first-option
                style="width: 520px"
                placeholder="输入后回车添加"
              />

              <!-- json -->
              <el-input
                v-else-if="fieldMeta(section.key, key, form[section.key][key]).type === 'json'"
                :model-value="typeof form[section.key][key] === 'string' ? form[section.key][key] : JSON.stringify(form[section.key][key], null, 2)"
                type="textarea"
                :rows="4"
                style="width: 620px"
                @update:model-value="(v: string) => (form[section.key][key] = v)"
              />

              <!-- textarea -->
              <el-input
                v-else-if="fieldMeta(section.key, key, form[section.key][key]).type === 'textarea'"
                v-model="form[section.key][key]"
                type="textarea"
                :rows="4"
                style="width: 620px"
              />

              <!-- text -->
              <el-input
                v-else
                v-model="form[section.key][key]"
                style="width: 520px"
                clearable
              />

              <div v-if="fieldHelp(section.key, key, fieldMeta(section.key, key, form[section.key][key]))" class="field-help muted">
                {{ fieldHelp(section.key, key, fieldMeta(section.key, key, form[section.key][key])) }}
              </div>
            </el-form-item>
          </el-form>

          <div class="section-actions">
            <el-button :icon="'MagicStick'" :loading="testing === section.key" @click="runTest">
              测试「{{ section.label }}」配置
            </el-button>
            <span class="muted" style="font-size: 12.5px">
              测试会发起一次真实调用（Mock 模式为本地模拟），用于确认密钥与网络是否可用
            </span>
          </div>
        </el-tab-pane>
      </el-tabs>
    </div>

    <div class="footer-bar">
      <span class="muted">
        修改后请点击「保存配置」。密钥以 Fernet 对称加密存储于 <span class="mono">data/secrets.key</span> 与本机数据库中。
      </span>
      <el-button type="primary" :icon="'Check'" :loading="saving" @click="save">保存配置</el-button>
    </div>

    <!-- 测试结果 -->
    <el-dialog v-model="testDialog" title="连接测试结果" width="620px">
      <template v-if="testResult">
        <el-alert
          :type="testResult.result.ok ? 'success' : 'error'"
          :closable="false"
          show-icon
          :title="testResult.result.message"
        />
        <el-descriptions
          v-if="Object.keys(testResult.result.detail || {}).length"
          :column="1"
          size="small"
          border
          style="margin-top: 14px"
        >
          <el-descriptions-item v-for="(value, key) in testResult.result.detail" :key="String(key)" :label="String(key)">
            <span class="mono">{{ typeof value === 'object' ? JSON.stringify(value) : value }}</span>
          </el-descriptions-item>
        </el-descriptions>
      </template>
      <template #footer>
        <el-button type="primary" @click="testDialog = false">知道了</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.head-actions {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

.env-tags {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.env-tip {
  margin-top: 10px;
  font-size: 12.5px;
}

.config-form {
  max-width: 900px;
  padding-top: 6px;
}

.field-help {
  font-size: 12px;
  line-height: 1.6;
  margin-top: 4px;
  flex-basis: 100%;
}

:deep(.el-form-item) {
  margin-bottom: 22px;
}

:deep(.el-form-item__content) {
  flex-wrap: wrap;
}

.section-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-top: 8px;
  border-top: 1px dashed var(--spark-border);
  margin-top: 8px;
}

.footer-bar {
  position: sticky;
  bottom: 0;
  margin-top: 16px;
  background: #fff;
  border: 1px solid var(--spark-border);
  border-radius: var(--spark-radius);
  box-shadow: 0 -4px 20px -8px rgba(15, 23, 42, 0.18);
  padding: 12px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  font-size: 12.5px;
  flex-wrap: wrap;
}
</style>
