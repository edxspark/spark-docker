<script setup lang="ts">
/**
 * 合集/频道条目选择弹窗。
 *
 * 目的：链接是合集（playlist）或频道时，先让用户看清里面有哪些视频、勾选要搬运的条目，
 * 确认之后才创建任务 —— 避免一次解析就把整个合集几百个视频全部建进队列。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import type { TableInstance } from 'element-plus'
import type { ProbeEntry } from '@/types'
import { formatDuration, formatNumber } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    title?: string
    author?: string
    sourceType?: string
    entries: ProbeEntry[]
    confirming?: boolean
  }>(),
  { title: '', author: '', sourceType: 'playlist', confirming: false },
)

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  /** 确认勾选：["all"] 表示全选，否则是勾选条目的 video_id 列表 */
  (e: 'confirm', videoIds: string[]): void
}>()

const PAGE_SIZE = 50
/** 超过这个数量在提交前再提醒一次，避免手滑全选 */
const BULK_WARN_THRESHOLD = 20

const tableRef = ref<TableInstance>()
const page = ref(1)
/** 每次翻页自增，作为表格的 key 强制重建，避免复用旧行导致勾选状态错乱 */
const pageId = ref(0)
const selectedIds = ref<string[]>([])
const syncing = ref(false)

const total = computed(() => props.entries.length)
const pagedEntries = computed(() =>
  props.entries.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE),
)
const allSelected = computed(() => total.value > 0 && selectedIds.value.length === total.value)
const selectionSet = computed(() => new Set(selectedIds.value))
const selectedEntries = computed(() => props.entries.filter((e) => selectionSet.value.has(e.video_id)))
const selectedDuration = computed(() => selectedEntries.value.reduce((sum, e) => sum + (e.duration || 0), 0))
const isBulk = computed(() => selectedIds.value.length > BULK_WARN_THRESHOLD)

function isPicked(row: ProbeEntry): boolean {
  return selectionSet.value.has(row.video_id)
}

/**
 * 把表格里的勾选状态对齐到 selectedIds。
 *
 * 逐行调用 toggleRowSelection 并显式给出目标状态：el-table 只在「状态真的变了」
 * （见 element-plus util.toggleRowStatus）时才更新内部 selection 并刷新对应行，
 * 因此这个操作是幂等的，不会重置整页勾选框，也不会与用户点选互相覆盖。
 */
function syncSelection() {
  const table = tableRef.value
  if (!table || syncing.value) return
  const wanted = new Set(selectedIds.value)
  syncing.value = true
  try {
    props.entries.forEach((entry) => table.toggleRowSelection(entry, wanted.has(entry.video_id)))
  } finally {
    syncing.value = false
  }
}

function reset() {
  page.value = 1
  selectedIds.value = props.entries.map((e) => e.video_id)
  syncing.value = false
  // 打开弹窗时表格行是新建的，等 DOM 更新后再对齐一次
  void nextTick(() => syncSelection())
}

onMounted(() => {
  // 翻页后表格按 :key 重建，挂载完成后把勾选状态打上去
  if (props.modelValue) syncSelection()
})

function selectCurrentPage() {
  const ids = new Set(selectedIds.value)
  pagedEntries.value.forEach((e) => ids.add(e.video_id))
  selectedIds.value = props.entries.map((e) => e.video_id).filter((id) => ids.has(id))
}

function clearSelection() {
  selectedIds.value = []
}

function selectFirst(count: number) {
  selectedIds.value = props.entries.slice(0, count).map((e) => e.video_id)
}

function toggleAll() {
  if (allSelected.value) clearSelection()
  else selectedIds.value = props.entries.map((e) => e.video_id)
}

/** 行级勾选：把本页变化合并进完整勾选集合，保留其他页已勾选的条目 */
function handleSelect(rows: ProbeEntry[]) {
  if (syncing.value) return
  mergeIntoSelection(rows)
}

/** 表头勾选：rows 是当前页的勾选结果，未出现在其中的本页条目视为取消 */
function handleSelectAll(rows: ProbeEntry[]) {
  if (syncing.value) return
  mergeIntoSelection(rows)
}

function mergeIntoSelection(rows: ProbeEntry[]) {
  const incoming = new Set(rows.map((r) => r.video_id))
  const pageIds = new Set(pagedEntries.value.map((e) => e.video_id))
  const next = selectedIds.value.filter((id) => !pageIds.has(id))
  props.entries
    .map((e) => e.video_id)
    .filter((id) => incoming.has(id) && !next.includes(id))
    .forEach((id) => next.push(id))
  selectedIds.value = props.entries.map((e) => e.video_id).filter((id) => next.includes(id))
}

function confirm() {
  if (!selectedIds.value.length) return
  const ids = allSelected.value ? ['all'] : [...selectedIds.value]
  emit('confirm', ids)
}

function close() {
  emit('update:modelValue', false)
}

/** 等 DOM 更新（打开弹窗 / 翻页）之后再对齐勾选状态 */
function syncSoon() {
  void nextTick(() => {
    syncSelection()
    // nextTick 之后 el-table 还会因数据变化做一次内部勾选清理，
    // 因此额外排一个宏任务：清理结束后再按 selectedIds 打一遍勾选状态
    window.setTimeout(syncSelection, 0)
  })
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) reset()
  },
)

watch(selectedIds, () => {
  syncSelection()
})

watch(
  () => props.entries,
  () => {
    if (props.modelValue) reset()
  },
)

watch(page, () => {
  // 翻页会让表格重新挂载（见模板上的 :key），等新表格就绪后再对齐勾选状态
  pageId.value += 1
  syncSoon()
})

onMounted(() => {
  if (props.modelValue) syncSelection()
})
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    width="1040px"
    top="6vh"
    append-to-body
    destroy-on-close
    class="collection-picker-dialog"
    @close="close"
  >
    <template #header>
      <div class="picker-header">
        <div class="picker-heading">
          <span class="picker-title">这个链接是{{ sourceType === 'channel' ? '频道' : '合集' }}，先选要搬运的视频</span>
          <span class="muted picker-sub">
            {{ title || '未命名' }}
            <template v-if="author"> · {{ author }}</template>
            · 共 {{ total }} 个视频
          </span>
        </div>
      </div>
    </template>

    <div class="picker-toolbar">
      <div class="toolbar-left">
        <el-checkbox :model-value="allSelected" :indeterminate="!allSelected && selectedIds.length > 0" @change="toggleAll">
          全选（{{ total }}）
        </el-checkbox>
        <el-button link type="primary" @click="selectCurrentPage">选中本页</el-button>
        <el-button link type="primary" @click="selectFirst(10)">前 10 个</el-button>
        <el-button link type="primary" @click="selectFirst(20)">前 20 个</el-button>
        <el-button link type="primary" @click="clearSelection">清空</el-button>
      </div>
      <div class="muted toolbar-right">默认已全选，可逐个取消；确认后只创建勾选条目的任务</div>
    </div>

    <el-table
      ref="tableRef"
      :key="`page-${pageId}-${page}`"
      :data="pagedEntries"
      row-key="video_id"
      max-height="420"
      style="width: 100%"
      @select="handleSelect"
      @select-all="handleSelectAll"
    >
      <el-table-column type="selection" width="46" />
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
          <div :class="{ 'row-off': !isPicked(row) }">{{ row.title || row.video_id }}</div>
          <div class="muted mono" style="font-size: 11.5px">{{ row.video_id }}</div>
        </template>
      </el-table-column>
      <el-table-column label="时长" width="96">
        <template #default="{ row }">{{ formatDuration(row.duration) }}</template>
      </el-table-column>
      <el-table-column label="发布日期" width="112">
        <template #default="{ row }">
          <span class="muted mono">{{ row.upload_date || '-' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="播放量" width="96" align="right">
        <template #default="{ row }">
          <span class="muted">{{ row.view_count ? formatNumber(row.view_count) : '-' }}</span>
        </template>
      </el-table-column>
    </el-table>

    <div class="picker-footer">
      <el-pagination
        v-model:current-page="page"
        :page-size="PAGE_SIZE"
        :total="total"
        layout="prev, pager, next, jumper, total"
        background
        small
        :hide-on-single-page="true"
      />
    </div>

    <template #footer>
      <div class="dialog-footer">
        <div class="footer-summary">
          <div class="summary-line">
            已勾选 <strong>{{ selectedIds.length }}</strong> / {{ total }} 个视频
            <span class="muted">· 预计原片总时长 {{ formatDuration(selectedDuration) }}</span>
          </div>
          <div v-if="isBulk" class="summary-warn">
            <el-icon><WarningFilled /></el-icon>
            勾选数量较多，每个视频都会完整跑一遍下载 → 翻译 → 配音 → 发布，请确认后再创建
          </div>
        </div>
        <div class="footer-actions">
          <el-button @click="close">取消</el-button>
          <el-button type="primary" :disabled="!selectedIds.length" :loading="confirming" @click="confirm">
            确定并继续
          </el-button>
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.picker-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.picker-heading {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-right: 24px;
}

.picker-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--spark-text);
}

.picker-sub {
  font-size: 12.5px;
}

.picker-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.toolbar-right {
  font-size: 12px;
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

.row-off {
  color: var(--spark-text-2);
}

.picker-footer {
  display: flex;
  justify-content: flex-end;
  margin-top: 10px;
}

.dialog-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.footer-summary {
  text-align: left;
}

.summary-line {
  font-size: 13px;
}

.summary-warn {
  margin-top: 4px;
  font-size: 12.5px;
  color: #d97706;
  display: flex;
  align-items: center;
  gap: 6px;
}

.footer-actions {
  display: flex;
  gap: 10px;
}
</style>
