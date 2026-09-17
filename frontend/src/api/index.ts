import axios from 'axios'
import { ElMessage } from 'element-plus'
import type {
  CancelAllResult,
  CreateTaskPayload,
  DouyinAccount,
  Page,
  PipelineMeta,
  ProbeResponse,
  RuntimeInfo,
  SettingsResponse,
  StatsOverview,
  Task,
  TaskDetail,
  TaskItem,
  TaskLog,
  TestResult,
  Voice,
} from '@/types'

export const http = axios.create({
  baseURL: '/api',
  timeout: 300000, // 探测合集、发布等操作可能较慢
})

http.interceptors.response.use(
  (response) => response,
  (error) => {
    const detail = error?.response?.data?.detail
    const message =
      typeof detail === 'string'
        ? detail
        : detail
          ? JSON.stringify(detail)
          : error?.message || '请求失败'
    // 允许调用方通过 config.silent 关闭全局提示
    if (!error?.config?.silent) {
      ElMessage.error(message)
    }
    return Promise.reject(new Error(message))
  },
)

// ---------------------------------------------------------------- 任务

export const taskApi = {
  meta: () => http.get<PipelineMeta>('/tasks/meta').then((r) => r.data),

  probe: (url: string) =>
    http.post<ProbeResponse>('/tasks/probe', { url }, { silent: true } as any).then((r) => r.data),

  create: (payload: CreateTaskPayload) =>
    http.post<TaskDetail>('/tasks', payload).then((r) => r.data),

  list: (params: {
    status?: string
    q?: string
    page?: number
    page_size?: number
  }) => http.get<Page<Task>>('/tasks', { params }).then((r) => r.data),

  detail: (id: number) => http.get<TaskDetail>(`/tasks/${id}`).then((r) => r.data),

  logs: (id: number, params?: { item_id?: number; limit?: number }) =>
    http.get<TaskLog[]>(`/tasks/${id}/logs`, { params }).then((r) => r.data),

  cancel: (id: number) => http.post<{ message: string }>(`/tasks/${id}/cancel`).then((r) => r.data),

  /** 取消所有未结束的任务。includePaused=false 时保留已暂停的任务 */
  cancelAll: (includePaused = true) =>
    http
      .post<CancelAllResult>('/tasks/cancel-all', null, { params: { include_paused: includePaused } })
      .then((r) => r.data),

  /** 仅取「未结束任务」的总数，用于按钮角标（page_size=1 只为了拿 total） */
  activeCount: () =>
    http
      .get<Page<Task>>('/tasks', { params: { status: 'pending,running,paused', page_size: 1 } })
      .then((r) => r.data.total),

  retry: (id: number, onlyFailed = true) =>
    http
      .post<TaskDetail>(`/tasks/${id}/retry`, null, { params: { only_failed: onlyFailed } })
      .then((r) => r.data),

  remove: (id: number, removeFiles = false) =>
    http
      .delete<{ message: string }>(`/tasks/${id}`, { params: { remove_files: removeFiles } })
      .then((r) => r.data),

  /** 发布条目。republish=true 用于「重新发布」已发布过的条目（会再上传一个新作品） */
  publishItem: (taskId: number, itemId: number, options?: { republish?: boolean; dryRun?: boolean }) =>
    http
      .post<TaskItem>(`/tasks/${taskId}/items/${itemId}/publish`, {
        republish: options?.republish ?? false,
        dry_run: options?.dryRun ?? false,
      })
      .then((r) => r.data),

  fileUrl: (taskId: number, itemId: number, kind: string) =>
    `/api/tasks/${taskId}/items/${itemId}/file?kind=${kind}`,

  wsUrl: (taskId: number) => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${protocol}//${window.location.host}/api/tasks/${taskId}/ws`
  },
}

// ---------------------------------------------------------------- 配置

export const settingsApi = {
  get: () => http.get<SettingsResponse>('/settings').then((r) => r.data),

  update: (patch: Record<string, Record<string, any>>) =>
    http.put<SettingsResponse>('/settings', patch).then((r) => r.data),

  reset: (section?: string) =>
    http.post<{ message: string }>('/settings/reset', null, { params: { section } }).then((r) => r.data),

  voices: () => http.get<{ voices: Voice[] }>('/settings/voices').then((r) => r.data.voices),

  runtime: () => http.get<RuntimeInfo>('/settings/runtime').then((r) => r.data),

  paths: () => http.get<Record<string, any>>('/settings/paths').then((r) => r.data),

  test: (section: string) =>
    http.post<TestResult>(`/settings/test/${section}`, null, { timeout: 180000 }).then((r) => r.data),

  /** 预览「统一开头语」的开头画面（返回 data URI，可在保存前先看效果） */
  introCardPreview: (intro: Record<string, any>, force = false) =>
    http
      .post<{
        ok: boolean
        renderer: string
        width: number
        height: number
        image: string
        message: string
      }>('/settings/intro/card', { intro, force }, { timeout: 120000 })
      .then((r) => r.data),
}

// ---------------------------------------------------------------- 抖音

export const douyinApi = {
  account: () => http.get<DouyinAccount>('/douyin/account').then((r) => r.data),

  login: () => http.post<{ message: string }>('/douyin/login').then((r) => r.data),

  loginStatus: () => http.get<Record<string, any>>('/douyin/login/status').then((r) => r.data),

  check: () => http.post<{ ok: boolean; message: string }>('/douyin/check').then((r) => r.data),

  logout: () => http.post<{ message: string }>('/douyin/logout').then((r) => r.data),

  qrcodeUrl: () => `/api/douyin/login/qrcode?t=${Date.now()}`,
}

// YouTube 登录：匿名下载会撞「确认你不是机器人」风控，需要 cookies。
// 这一步开浏览器登录一次，登录态会被导出成 yt-dlp 能用的 cookies.txt 并自动验证。
export const youtubeApi = {
  status: () => http.get<Record<string, any>>('/youtube/status').then((r) => r.data),

  login: () => http.post<{ message: string }>('/youtube/login').then((r) => r.data),

  loginStatus: () => http.get<Record<string, any>>('/youtube/login/status').then((r) => r.data),

  check: () => http.post<{ ok: boolean; message: string }>('/youtube/check').then((r) => r.data),

  logout: () => http.post<{ message: string }>('/youtube/logout').then((r) => r.data),
}

// ---------------------------------------------------------------- 统计

export const statsApi = {
  overview: () => http.get<StatsOverview>('/stats/overview').then((r) => r.data),
  runtime: () => http.get<{ running_task_ids: number[] }>('/stats/runtime').then((r) => r.data),
}
