import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // 去掉 /api 前缀再转发, 与生产 nginx 的 `proxy_pass http://api:8000/` 行为对齐
      // (字符串简写不去前缀, 会打到后端不存在的 /api/xxx 上)
      '/api': {
        target: 'http://localhost:8000',
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
})
