import { onBeforeUnmount, onMounted, ref, type Ref } from 'vue'
import { taskApi } from '@/api'
import type { TaskDetail, TaskLog, WsEvent } from '@/types'

interface Options {
  /** 新日志回调 */
  onLog?: (log: Partial<TaskLog> & { message: string }) => void
  /** 任何更新回调（用于刷新列表等） */
  onUpdate?: (event: WsEvent) => void
}

/**
 * 订阅任务进度 WebSocket。
 * 收到 snapshot 事件时直接替换整个任务详情，其余事件增量合并，
 * 断线后 3 秒自动重连。
 */
export function useTaskSocket(taskId: Ref<number | null>, options: Options = {}) {
  const detail = ref<TaskDetail | null>(null)
  const connected = ref(false)
  const logs = ref<TaskLog[]>([])
  let socket: WebSocket | null = null
  let reconnectTimer: number | null = null
  let closedByUser = false
  let localLogId = -1

  function mergeItem(payload: Record<string, any>) {
    if (!detail.value) return
    const items = detail.value.items || []
    const index = items.findIndex((item) => item.id === payload.id)
    if (index >= 0) {
      items[index] = { ...items[index], ...payload }
    }
    options.onUpdate?.({ type: 'item.updated', task_id: taskId.value ?? 0, payload })
  }

  function handle(event: WsEvent) {
    switch (event.type) {
      case 'snapshot':
        detail.value = event.payload as unknown as TaskDetail
        break
      case 'task.updated':
        if (detail.value) detail.value = { ...detail.value, ...(event.payload as any) }
        break
      case 'item.updated':
        mergeItem(event.payload)
        break
      case 'log': {
        const entry = {
          id: localLogId--,
          task_id: taskId.value ?? 0,
          item_id: (event.payload.item_id as number) ?? null,
          level: (event.payload.level as string) || 'info',
          stage: (event.payload.stage as string) || '',
          message: String(event.payload.message || ''),
          created_at: event.ts || new Date().toISOString(),
        } as TaskLog
        logs.value.push(entry)
        if (logs.value.length > 800) logs.value.splice(0, logs.value.length - 800)
        options.onLog?.(entry)
        break
      }
      default:
        break
    }
    options.onUpdate?.(event)
  }

  function connect() {
    if (!taskId.value) return
    closedByUser = false
    const ws = new WebSocket(taskApi.wsUrl(taskId.value))
    socket = ws

    ws.onopen = () => {
      connected.value = true
    }
    ws.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data) as WsEvent
        if (event.type === 'ping') return
        handle(event)
      } catch {
        /* 忽略非法消息 */
      }
    }
    ws.onclose = () => {
      connected.value = false
      if (!closedByUser) {
        reconnectTimer = window.setTimeout(connect, 3000)
      }
    }
    ws.onerror = () => {
      ws.close()
    }
  }

  function close() {
    closedByUser = true
    if (reconnectTimer) window.clearTimeout(reconnectTimer)
    reconnectTimer = null
    socket?.close()
    socket = null
  }

  async function loadHistory() {
    if (!taskId.value) return
    const [detailData, logData] = await Promise.all([
      taskApi.detail(taskId.value),
      taskApi.logs(taskId.value, { limit: 300 }).catch(() => []),
    ])
    detail.value = detailData
    logs.value = logData
  }

  onMounted(() => {
    loadHistory().catch(() => undefined)
    connect()
  })

  onBeforeUnmount(close)

  return { detail, logs, connected, reload: loadHistory, close }
}
