<script setup lang="ts">
import { computed } from 'vue'
import { STAGE_LABELS } from '@/utils/format'

const props = defineProps<{
  stage: string
  status?: string
  /** 显示为紧凑模式（只显示当前阶段名） */
  compact?: boolean
}>()

const ORDER = ['probe', 'download', 'subtitle', 'translate', 'tts', 'align', 'metadata', 'publish']

const currentIndex = computed(() => {
  const index = ORDER.indexOf(props.stage)
  return index < 0 ? (props.status === 'succeeded' ? ORDER.length : 0) : index
})

const done = computed(() => props.status === 'succeeded' || props.status === 'partial')
</script>

<template>
  <div v-if="compact" class="stage-compact">
    <el-icon v-if="status === 'running'" class="is-loading"><Loading /></el-icon>
    {{ STAGE_LABELS[stage] || (status === 'succeeded' ? '已完成' : '待执行') }}
  </div>

  <div v-else class="stage-track">
    <div
      v-for="(key, index) in ORDER"
      :key="key"
      class="stage-node"
      :class="{
        done: done || index < currentIndex,
        active: !done && index === currentIndex && status === 'running',
      }"
    >
      <div class="dot">
        <el-icon v-if="done || index < currentIndex"><Check /></el-icon>
        <el-icon v-else-if="index === currentIndex && status === 'running'" class="is-loading">
          <Loading />
        </el-icon>
        <span v-else>{{ index + 1 }}</span>
      </div>
      <div class="label">{{ STAGE_LABELS[key] }}</div>
    </div>
  </div>
</template>

<style scoped>
.stage-compact {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--spark-text-2);
  font-size: 13px;
}

.stage-track {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.stage-node {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 5px 11px 5px 6px;
  border-radius: 999px;
  background: #f2f4f8;
  color: var(--spark-text-2);
  font-size: 12.5px;
  white-space: nowrap;
  transition: all 0.2s ease;
}

.stage-node.done {
  background: #e8f5ec;
  color: #1f8a4c;
}

.stage-node.active {
  background: var(--spark-primary-9);
  color: var(--spark-primary);
  box-shadow: 0 0 0 1px rgba(var(--spark-primary-rgb), 0.35);
}

.dot {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: #fff;
  display: grid;
  place-items: center;
  font-size: 11px;
  font-weight: 600;
  flex: none;
}
</style>
