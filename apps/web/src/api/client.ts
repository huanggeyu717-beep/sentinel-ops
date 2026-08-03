// 统一 API 客户端: fetch 封装 + JWT header + SSE 订阅 (W4: /agent-tasks/{id}/events)
export const api = async <T>(path: string, init?: RequestInit): Promise<T> => {
  const res = await fetch(`/api${path}`, init)
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.json() as Promise<T>
}
