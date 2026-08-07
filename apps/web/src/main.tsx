import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'
import ReactDOM from 'react-dom/client'

import { ApiError } from './api/client'
import App from './App'
import './styles.css'

const qc = new QueryClient({
  defaultOptions: {
    queries: {
      // 4xx 是明确的业务答复 (未登录/无权限/不存在), 重试只会拖慢反馈; 5xx/网络错误才值得重试
      retry: (failureCount, error) =>
        !(error instanceof ApiError && error.status < 500) && failureCount < 2,
    },
  },
})

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={qc}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
)
