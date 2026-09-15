<script setup lang="ts">
import { computed } from 'vue'
import { PUBLISH_META, STATUS_META } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    status: string
    kind?: 'task' | 'publish'
    size?: 'small' | 'default' | 'large'
    effect?: 'dark' | 'light' | 'plain'
  }>(),
  { kind: 'task', size: 'small', effect: 'light' },
)

const meta = computed(() => {
  if (props.kind === 'publish') {
    return PUBLISH_META[props.status] || { label: props.status || '未发布', type: 'info' }
  }
  return STATUS_META[props.status] || { label: props.status || '-', type: 'info' }
})

const tagType = computed(() => (meta.value.type === 'gray' ? 'info' : (meta.value.type as any)))

const spinning = computed(
  () => props.status === 'running' || (props.kind === 'publish' && props.status === 'publishing'),
)
</script>

<template>
  <el-tag :type="tagType" :size="size" :effect="effect" round>
    <el-icon v-if="spinning" class="is-loading" style="margin-right: 4px"><Loading /></el-icon>
    {{ meta.label }}
  </el-tag>
</template>
