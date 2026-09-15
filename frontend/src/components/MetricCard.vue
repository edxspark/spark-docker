<script setup lang="ts">
/**
 * 指标卡：工作台与任务历史共用的统一样式。
 *
 * 结构固定为「图标 + 标签 + 数值 + 说明」四层：图标提供色块锚点，
 * 标签弱化、数值强调，说明补一行上下文。各页只负责传入数据与颜色，
 * 不再各写一套 .stat-card / .metric-card 样式（历史上两处风格就不一致）。
 *
 * 高度交给外层网格行（同一行等高），文案过长只会省略号，不会把卡片撑高。
 */
withDefaults(
  defineProps<{
    /** 标签，如「任务总数」 */
    label: string
    /** 数值，已格式化好的字符串 */
    value: string | number
    /** 说明行：失败数、占比、数据来源等 */
    hint?: string
    /** Element Plus 图标组件名 */
    icon?: string
    /** 强调色，用于图标底色与图标本身 */
    color?: string
    /** 数值是否用强调色（用于失败数这类需要示警的指标） */
    valueColored?: boolean
  }>(),
  { hint: '', icon: '', color: 'var(--spark-primary)', valueColored: false },
)
</script>

<template>
  <div class="panel metric-card">
    <!-- 底色用 color-mix 计算：主题色是 CSS 变量时无法用字符串拼 alpha 后缀 -->
    <div v-if="icon" class="metric-icon" :style="{ background: `color-mix(in srgb, ${color} 12%, transparent)`, color }">
      <el-icon :size="18"><component :is="icon" /></el-icon>
    </div>
    <div class="metric-body">
      <div class="metric-label">{{ label }}</div>
      <div class="metric-value" :style="valueColored ? { color } : undefined">{{ value }}</div>
      <div v-if="hint" class="metric-hint muted">{{ hint }}</div>
    </div>
  </div>
</template>

<style scoped>
.metric-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
}

.metric-icon {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  flex: none;
}

.metric-body {
  /* 窄列里允许收缩，配合下面的 nowrap + ellipsis 不换行 */
  min-width: 0;
  flex: 1;
}

.metric-label {
  font-size: 12.5px;
  line-height: 1.35;
  color: var(--spark-text-2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.metric-value {
  font-size: 21px;
  font-weight: 650;
  line-height: 1.3;
  letter-spacing: -0.5px;
  white-space: nowrap;
}

.metric-hint {
  font-size: 11.5px;
  line-height: 1.35;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
