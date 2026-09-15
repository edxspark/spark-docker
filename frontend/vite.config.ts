import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 端口从环境变量读取，与 scripts/dev.sh 保持一致：
// 否则改了 SPARK_PORT 后代理仍指向默认端口，前端会拿不到数据。
const backendPort = process.env.SPARK_PORT || '8720'
const frontendPort = Number(process.env.VITE_PORT || 5173)

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: frontendPort,
    strictPort: true,
    proxy: {
      // 开发模式下把 API 与 WebSocket 代理到 FastAPI 后端
      '/api': {
        target: `http://127.0.0.1:${backendPort}`,
        changeOrigin: true,
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1500,
  },
})
