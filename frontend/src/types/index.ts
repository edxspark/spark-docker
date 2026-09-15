// 与后端 API 对应的类型定义

export type TaskStatus =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'partial'
  | 'failed'
  | 'canceled'
  | 'paused'

export type StageKey =
  | 'probe'
  | 'download'
  | 'subtitle'
  | 'translate'
  | 'tts'
  | 'align'
  | 'metadata'
  | 'publish'

export interface TaskItem {
  id: number
  task_id: number
  idx: number
  video_id: string
  url: string
  title: string
  title_zh: string
  author: string
  duration: number
  thumbnail: string
  status: TaskStatus
  stage: string
  progress: number
  message: string
  error: string
  video_path: string
  subtitle_source_path: string
  subtitle_zh_path: string
  dubbed_audio_path: string
  output_path: string
  cover_path: string
  publish_status: string
  publish_url: string
  publish_error: string
  published_at: string | null
  stats: Record<string, any>
  tags: string[]
  created_at: string
  updated_at: string
}

export interface Task {
  id: number
  title: string
  source_url: string
  source_type: 'video' | 'playlist' | 'channel' | string
  source_id: string
  author: string
  status: TaskStatus
  stage: string
  progress: number
  message: string
  error: string
  total_items: number
  done_items: number
  failed_items: number
  options: Record<string, any>
  created_at: string
  updated_at: string
  started_at: string | null
  finished_at: string | null
}

export interface TaskDetail extends Task {
  items: TaskItem[]
}

export interface TaskLog {
  id: number
  task_id: number
  item_id: number | null
  level: 'info' | 'warning' | 'error' | string
  stage: string
  message: string
  created_at: string
}

export interface Page<T> {
  total: number
  page: number
  page_size: number
  items: T[]
}

export interface StageMeta {
  key: StageKey | string
  label: string
}

export interface PipelineMeta {
  stages: StageMeta[]
  statuses: string[]
}

export interface ProbeEntry {
  video_id: string
  url: string
  title: string
  author: string
  duration: number
  thumbnail: string
}

export interface ProbeResponse {
  source_type: string
  title: string
  author: string
  source_id: string
  total: number
  entries: ProbeEntry[]
}

export interface TaskOptions {
  voice?: string | null
  auto_publish?: boolean | null
  schedule_offset_minutes?: number | null
  schedule_at?: string | null
  target_aspect?: 'original' | '9:16' | '16:9' | null
  burn_subtitles?: boolean | null
  keep_bgm?: boolean | null
  bgm_volume?: number | null
  description?: string | null
  tags?: string[] | null
}

export interface CreateTaskPayload {
  url: string
  title?: string
  max_items?: number | null
  start_index?: number
  options?: TaskOptions
  auto_start?: boolean
}

export interface Voice {
  id: string
  name: string
  gender: string
  scene: string
}

export interface SettingsSectionMeta {
  key: string
  label: string
  secrets: string[]
  defaults: Record<string, any>
}

export interface SettingsResponse {
  config: Record<string, Record<string, any>>
  sections: SettingsSectionMeta[]
}

export interface TestResult {
  ok: boolean
  message: string
  detail: Record<string, any>
}

export interface RuntimeInfo {
  ffmpeg: { available: boolean; path: string }
  ffprobe: { available: boolean; path: string }
  data_dir: string
  python: string
  yt_dlp?: { available: boolean; version: string }
  playwright?: { available: boolean }
}

export interface StatsOverview {
  total_tasks: number
  running_tasks: number
  succeeded_tasks: number
  failed_tasks: number
  total_videos: number
  published_videos: number
  total_sentences: number
  total_characters: number
  total_tokens: number
  disk_usage_mb: number
  recent_tasks: Task[]
  daily: { date: string; created: number; succeeded: number; failed: number }[]
}

export interface DouyinAccount {
  id: number
  name: string
  nickname: string
  status: string
  is_default: boolean
  storage_state_path: string
  storage_state_exists: boolean
  last_login_at: string | null
  last_check_at: string | null
  provider: string
  login_session: {
    status: string
    message: string
    qrcode_ready: boolean
    running: boolean
    started_at: string | null
    finished_at: string | null
  }
}

export interface WsEvent {
  type: 'snapshot' | 'task.updated' | 'item.updated' | 'log' | 'stage' | 'task.finished' | 'task.created' | 'task.requeued' | 'ping'
  task_id: number
  payload: Record<string, any>
  ts?: string
}
