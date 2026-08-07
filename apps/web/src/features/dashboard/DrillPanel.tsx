// 演练面板 (SPEC-005 页面结构·下栏右): 场景下拉 + 启动 + 进度。
// 前端一律不自己造事件 —— 触发只走 /drills 接口, 事件由服务端复用模拟器投递。
// 进度 2 秒轮询 (决策 1), 跑完自动停; 状态在服务端内存, API 重启即丢 (决策 4)。
import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { ApiError, api, post } from '../../api/client'
import { POLL_DRILL_MS, useScenarios } from '../../api/queries'
import type { Drill } from '../../api/types'

const startErrorMessage = (err: unknown): string => {
  if (err instanceof ApiError) {
    if (err.status === 409) return '该场景正在演练中, 等它跑完再启动'
    if (err.status === 403) return '启动演练需要 operator 及以上角色'
    return err.detail
  }
  return '网络错误, 请重试'
}

export default function DrillPanel({ canTrigger }: { canTrigger: boolean }) {
  const scenarios = useScenarios()
  const [selected, setSelected] = useState('')
  const [activeDrillId, setActiveDrillId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const start = useMutation({
    mutationFn: (scenario: string) => post<{ ok: boolean } & Drill>(`/drills/${scenario}`),
    onSuccess: (drill) => {
      setError(null)
      setActiveDrillId(drill.drill_id)
    },
    onError: (err) => setError(startErrorMessage(err)),
  })

  const drill = useQuery({
    queryKey: ['drill', activeDrillId],
    queryFn: () => api<{ ok: boolean } & Drill>(`/drills/${activeDrillId}`),
    enabled: activeDrillId !== null,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? POLL_DRILL_MS : false),
  })

  const list = scenarios.data ?? []
  const current = selected || (list[0]?.scenario ?? '')

  return (
    <>
      <div className="drill-form">
        <select
          value={current}
          onChange={(e) => setSelected(e.target.value)}
          disabled={list.length === 0}
          aria-label="演练场景"
        >
          {list.map((s) => (
            <option key={s.scenario} value={s.scenario}>
              {s.name} ({s.events_total} 事件 / {s.duration_s}s)
            </option>
          ))}
          {list.length === 0 && <option>无可用场景</option>}
        </select>
        {canTrigger ? (
          <button
            className="btn"
            disabled={!current || start.isPending || drill.data?.status === 'running'}
            onClick={() => start.mutate(current)}
          >
            启动演练
          </button>
        ) : (
          <span className="muted small">触发演练需要 operator 及以上角色, 当前账号只能查看</span>
        )}
      </div>

      {scenarios.isError && (
        <p className="error-text small">场景列表加载失败: {scenarios.error.message}</p>
      )}
      {error && <p className="error-text small">{error}</p>}

      {drill.data && <DrillProgress drill={drill.data} />}
    </>
  )
}

function DrillProgress({ drill }: { drill: Drill }) {
  const pct = drill.events_total > 0 ? Math.round((drill.events_sent / drill.events_total) * 100) : 0
  const statusText =
    drill.status === 'running' ? '进行中' : drill.status === 'completed' ? '已完成' : '失败'

  return (
    <div style={{ marginTop: 12 }}>
      <div className="small">
        <strong>{drill.scenario}</strong>
        <span className="muted"> · {statusText} · {drill.speed}x 加速</span>
      </div>
      <div
        className="progress-track"
        role="progressbar"
        aria-valuenow={drill.events_sent}
        aria-valuemin={0}
        aria-valuemax={drill.events_total}
      >
        <div className="progress-fill" style={{ width: `${pct}%` }} />
      </div>
      <div className="small muted">
        事件 {drill.events_sent} / {drill.events_total}
        {drill.status === 'failed' && drill.error && (
          <span className="error-text"> {drill.error}</span>
        )}
      </div>
      <p className="small muted">{drill.note}</p>
    </div>
  )
}
