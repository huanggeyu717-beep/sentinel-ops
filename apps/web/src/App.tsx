// 两条路由: /login 与 Dashboard。四页信息架构 (Dashboard / Automation Studio /
// Tasks / Evals) 的其余三页在 W4/W5 加入。
import { useEffect } from 'react'
import { BrowserRouter, Navigate, Route, Routes, useNavigate } from 'react-router-dom'

import { ApiError, setOnUnauthorized } from './api/client'
import { useMe } from './api/queries'
import LoginPage from './features/auth/LoginPage'
import DashboardPage from './features/dashboard/DashboardPage'
import { useApplyTheme } from './theme'

/** 会话过期 (任意请求 401) 时跳登录页 —— 注册成全局回调, client 不依赖 router。 */
function UnauthorizedRedirect() {
  const navigate = useNavigate()
  useEffect(() => {
    setOnUnauthorized(() => navigate('/login', { replace: true }))
    return () => setOnUnauthorized(null)
  }, [navigate])
  return null
}

function RequireAuth() {
  const me = useMe()
  if (me.isPending) return <p style={{ padding: 24 }}>加载中…</p>
  if (me.isError) {
    if (me.error instanceof ApiError && me.error.status === 401) {
      return <Navigate to="/login" replace />
    }
    return <p className="error-text" style={{ margin: 24 }}>无法连接服务端: {me.error.message}</p>
  }
  return <DashboardPage me={me.data} />
}

export default function App() {
  useApplyTheme()
  return (
    <BrowserRouter>
      <UnauthorizedRedirect />
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/" element={<RequireAuth />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
