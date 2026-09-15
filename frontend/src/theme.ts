// 主题色单一来源：修改 primary 即可整体换色。
// 需要与 styles/theme.css 里的 --el-color-primary 保持一致。
const primary = '#325AB4'

/** #RRGGBB → [r, g, b] */
function toRgb(hex: string): [number, number, number] {
  const v = hex.replace('#', '')
  return [parseInt(v.slice(0, 2), 16), parseInt(v.slice(2, 4), 16), parseInt(v.slice(4, 6), 16)]
}

const hexToRgbString = (hex: string) => toRgb(hex).join(', ')

/** 与白色按比例混合，用于生成浅色阶（Element Plus 的 light-3/5/7/8/9） */
function mixWithWhite(hex: string, weight: number): string {
  const [r, g, b] = toRgb(hex)
  const mix = (c: number) => Math.round(c * weight + 255 * (1 - weight))
  return `#${[mix(r), mix(g), mix(b)].map((c) => c.toString(16).padStart(2, '0')).join('')}`
}

/** 与黑色混合，用于 dark-2 与按下态 */
function mixWithBlack(hex: string, weight: number): string {
  const [r, g, b] = toRgb(hex)
  const mix = (c: number) => Math.round(c * weight)
  return `#${[mix(r), mix(g), mix(b)].map((c) => c.toString(16).padStart(2, '0')).join('')}`
}

export const theme = {
  primary,
  primaryRgb: hexToRgbString(primary),
  light: {
    3: mixWithWhite(primary, 0.7),
    5: mixWithWhite(primary, 0.5),
    7: mixWithWhite(primary, 0.3),
    8: mixWithWhite(primary, 0.2),
    9: mixWithWhite(primary, 0.1),
  },
  dark2: mixWithBlack(primary, 0.8),
}

/**
 * 生成全局 CSS 变量（运行时注入，避免依赖 sass 主题编译）。
 * Element Plus 组件默认读取 --el-color-primary 系列变量，覆盖后
 * 按钮、链接、开关、单选、进度条、标签等会一起变色。
 */
export const themeVars: Record<string, string> = {
  '--spark-primary': theme.primary,
  '--spark-primary-rgb': theme.primaryRgb,
  '--spark-primary-8': theme.light[8],
  '--spark-primary-9': theme.light[9],
  '--el-color-primary': theme.primary,
  '--el-color-primary-rgb': theme.primaryRgb,
  '--el-color-primary-light-3': theme.light[3],
  '--el-color-primary-light-5': theme.light[5],
  '--el-color-primary-light-7': theme.light[7],
  '--el-color-primary-light-8': theme.light[8],
  '--el-color-primary-light-9': theme.light[9],
  '--el-color-primary-dark-2': theme.dark2,
  '--el-color-primary-dark': theme.dark2,
}

export default theme
