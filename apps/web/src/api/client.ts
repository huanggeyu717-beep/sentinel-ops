// 统一 API 客户端 (SPEC-005 决策 3): 会话在 httpOnly cookie 里, 前端不碰 token,
// 所有请求带 credentials; 收到 401 统一跳登录页 (登录接口本身除外, 它的 401 是"密码不对")。
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(`${status} ${detail}`)
  }
}

// 会话过期时由 App 注册的回调负责跳转, client 不直接依赖 router
let onUnauthorized: (() => void) | null = null
export const setOnUnauthorized = (fn: (() => void) | null): void => {
  onUnauthorized = fn
}

export const api = async <T>(path: string, init?: RequestInit): Promise<T> => {
  const res = await fetch(`/api${path}`, { credentials: 'include', ...init })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body: unknown = await res.json()
      if (body && typeof body === 'object' && 'detail' in body) {
        detail = String((body as { detail: unknown }).detail)
      }
    } catch {
      // 非 JSON 响应体, 保留 statusText
    }
    if (res.status === 401 && !path.startsWith('/auth/login')) onUnauthorized?.()
    throw new ApiError(res.status, detail)
  }
  return res.json() as Promise<T>
}

export const post = <T>(path: string, body?: unknown): Promise<T> =>
  api<T>(path, {
    method: 'POST',
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
