// Agent 任务的 SSE 订阅: timeline 事件按 seq 去重排序, status 事件更新任务状态。
//
// 两条铁律 (SPEC-002 第三段易错点五):
// 1. 地址必须是同源的 /api/...(走 vite 代理 / 生产 nginx), **不能直连
//    http://localhost:8000** —— 浏览器原生 EventSource 不支持自定义请求头,
//    Authorization: Bearer 这条路走不通, 只能靠 cookie; 而会话 cookie 是
//    SameSite=Lax, 跨源的子资源请求不会带它。直连的表现是 401, 看起来像
//    登录状态丢了, 非常难查。
// 2. withCredentials: true —— 同源代理下也要显式声明带凭据。
//
// 断线重连不用自己写: EventSource 原生自动重连, 且自动带 Last-Event-ID 请求头,
// 服务端从那个 seq 之后接着推 (SPEC-002 验收 18)。这里只需按 seq 去重兜底。
// 任务到终态后服务端推完即关流; 客户端必须主动 close, 否则 EventSource
// 会把"服务端正常关闭"当成断线, 无限重连打转。
import { useEffect, useRef, useState } from 'react'

import type { TaskStatusEvent, TimelineItem } from '../../api/types'

const TERMINAL = new Set(['completed', 'failed', 'dead_letter'])

export interface TaskEvents {
  items: TimelineItem[]
  status: TaskStatusEvent | null
  /** SSE 连接出错且已被浏览器放弃 (readyState CLOSED 而任务未到终态) */
  broken: boolean
}

export function useTaskEvents(taskId: number | null): TaskEvents {
  const [items, setItems] = useState<TimelineItem[]>([])
  const [status, setStatus] = useState<TaskStatusEvent | null>(null)
  const [broken, setBroken] = useState(false)
  const statusRef = useRef<TaskStatusEvent | null>(null)

  useEffect(() => {
    setItems([])
    setStatus(null)
    setBroken(false)
    statusRef.current = null
    if (taskId === null) return

    const es = new EventSource(`/api/agent-tasks/${taskId}/events`, {
      withCredentials: true,
    })
    const seen = new Set<number>()

    es.addEventListener('timeline', (ev) => {
      const item = JSON.parse((ev as MessageEvent<string>).data) as TimelineItem
      if (seen.has(item.seq)) return
      seen.add(item.seq)
      setItems((prev) => [...prev, item].sort((a, b) => a.seq - b.seq))
    })

    es.addEventListener('status', (ev) => {
      const s = JSON.parse((ev as MessageEvent<string>).data) as TaskStatusEvent
      statusRef.current = s
      setStatus(s)
      if (TERMINAL.has(s.status)) es.close()
    })

    es.onerror = () => {
      // 终态后的关闭是正常收尾 (上面已 close); 其余情况浏览器会自动重连,
      // 只有它彻底放弃 (CLOSED) 才提示用户刷新
      if (
        es.readyState === EventSource.CLOSED &&
        !(statusRef.current && TERMINAL.has(statusRef.current.status))
      ) {
        setBroken(true)
      }
    }

    return () => es.close()
  }, [taskId])

  return { items, status, broken }
}
