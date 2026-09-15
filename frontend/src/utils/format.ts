export const STATUS_META: Record<string, { label: string; type: 'info' | 'primary' | 'success' | 'warning' | 'danger' | 'gray' }> = {
  pending: { label: '等待执行', type: 'info' },
  running: { label: '执行中', type: 'primary' },
  succeeded: { label: '已完成', type: 'success' },
  partial: { label: '部分成功', type: 'warning' },
  failed: { label: '失败', type: 'danger' },
  canceled: { label: '已取消', type: 'gray' },
  paused: { label: '已暂停', type: 'warning' },
}

export const PUBLISH_META: Record<string, { label: string; type: string }> = {
  '': { label: '未发布', type: 'info' },
  publishing: { label: '发布中', type: 'primary' },
  published: { label: '已发布', type: 'success' },
  failed: { label: '发布失败', type: 'danger' },
  skipped: { label: '已跳过', type: 'info' },
}

export const STAGE_LABELS: Record<string, string> = {
  probe: '解析链接',
  download: '下载视频与字幕',
  subtitle: '字幕清洗与断句',
  translate: '翻译字幕',
  tts: '语音合成',
  align: '时间轴对齐与合成',
  metadata: '生成标题与话题',
  publish: '发布到抖音',
}

export function statusLabel(status: string): string {
  return STATUS_META[status]?.label || status || '-'
}

export function formatDuration(seconds: number): string {
  if (!seconds || seconds <= 0) return '-'
  const total = Math.round(seconds)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
  return `${m}:${String(s).padStart(2, '0')}`
}

export function formatDateTime(value?: string | null): string {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

export function formatRelative(value?: string | null): string {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const diff = Date.now() - date.getTime()
  const minute = 60_000
  if (diff < minute) return '刚刚'
  if (diff < 60 * minute) return `${Math.floor(diff / minute)} 分钟前`
  if (diff < 24 * 60 * minute) return `${Math.floor(diff / (60 * minute))} 小时前`
  if (diff < 30 * 24 * 60 * minute) return `${Math.floor(diff / (24 * 60 * minute))} 天前`
  return formatDateTime(value).slice(0, 10)
}

export function formatNumber(value?: number | null): string {
  if (value === null || value === undefined) return '-'
  return value.toLocaleString('zh-CN')
}

export function formatSize(mb?: number | null): string {
  if (!mb) return '0 MB'
  if (mb < 1024) return `${mb.toFixed(1)} MB`
  return `${(mb / 1024).toFixed(2)} GB`
}

export function elapsedText(start?: string | null, end?: string | null): string {
  if (!start) return '-'
  const from = new Date(start).getTime()
  const to = end ? new Date(end).getTime() : Date.now()
  if (Number.isNaN(from) || Number.isNaN(to)) return '-'
  const seconds = Math.max(0, Math.round((to - from) / 1000))
  if (seconds < 60) return `${seconds} 秒`
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  if (m < 60) return `${m} 分 ${s} 秒`
  return `${Math.floor(m / 60)} 小时 ${m % 60} 分`
}

export function shortUrl(url: string, max = 52): string {
  if (!url) return '-'
  return url.length > max ? `${url.slice(0, max - 1)}…` : url
}
