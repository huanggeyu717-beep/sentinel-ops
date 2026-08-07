import { useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { ApiError, post } from '../../api/client'

export default function LoginPage() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const navigate = useNavigate()
  const qc = useQueryClient()

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setPending(true)
    setError(null)
    try {
      // token 在 httpOnly cookie 里, 响应体不含 token, 前端无须(也无法)保存 (SPEC-004 决策 3)
      await post('/auth/login', { email, password })
      qc.clear() // 丢掉未登录时缓存的 401 状态, 全部重新拉
      navigate('/', { replace: true })
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : '网络错误, 请重试')
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="login-wrap">
      <form className="card login-card" onSubmit={(e) => void submit(e)}>
        <h1>Sentinel 登录</h1>
        <label>
          邮箱
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="username"
            required
          />
        </label>
        <label>
          密码
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        {error && <p className="error-text">{error}</p>}
        <button className="btn" type="submit" disabled={pending}>
          {pending ? '登录中…' : '登录'}
        </button>
        <p className="muted small">
          演示账号 (密码见 .env.example): admin@example.com / chris@example.com /
          alex@example.com
        </p>
      </form>
    </div>
  )
}
