// 路由: /login、Dashboard、/studio (W4 Automation Studio)。四页信息架构
// (Dashboard / Automation Studio / Tasks / Evals) 的其余两页在 W5 加入。
import { useEffect, type ReactElement } from 'react'
import { BrowserRouter, Navigate, Route, Routes, useNavigate } from 'react-router-dom'

import { ApiError, setOnUnauthorized } from './api/client'
import { useMe } from './api/queries'
import type { Me } from './api/types'
import LoginPage from './features/auth/LoginPage'
import DashboardPage from './features/dashboard/DashboardPage'
import StudioPage from './features/studio/StudioPage'
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

function RequireAuth({ render }: { render: (me: Me) => ReactElement }) {
  const me = useMe()
  if (me.isPending) return <p style={{ padding: 24 }}>加载中…</p>
  if (me.isError) {
    if (me.error instanceof ApiError && me.error.status === 401) {
      return <Navigate to="/login" replace />
    }
    return <p className="error-text" style={{ margin: 24 }}>无法连接服务端: {me.error.message}</p>
  }
  return render(me.data)
}

export default function App() {
  useApplyTheme()
  return (
    <BrowserRouter>
      <UnauthorizedRedirect />
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/" element={<RequireAuth render={(me) => <DashboardPage me={me} />} />} />
        <Route path="/studio" element={<RequireAuth render={(me) => <StudioPage me={me} />} />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
