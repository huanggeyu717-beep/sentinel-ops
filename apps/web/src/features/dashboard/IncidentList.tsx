// 事故列表与时间线 (SPEC-005 页面结构·下栏左)。
// "派给谁"与"谁接的单"分列两栏 —— SPEC-003 修订 1 特意把这两个字段拆开:
// 派单对象与实际到场接单的人经常不是同一个 (派 Alex, Bo 刷卡接了), 合并显示会说谎。
// 操作按钮按角色隐藏, 但那只是体验优化, 拦截在服务端 (SPEC-004 决策 6)。
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useState } from 'react'

import { ApiError, post } from '../../api/client'
import { useIncidentDetail, useIncidents } from '../../api/queries'
import type { Incident } from '../../api/types'
import { fmtTime } from '../../lib/format'

const STATUS_LABEL: Record<Incident['status'], string> = {
  open: '待处理',
  assigned: '已派单',
  acknowledged: '已接单',
  resolved: '已解决',
}

interface Props {
  canTransition: boolean
  canCrossZone: boolean
}

export default function IncidentList({ canTransition, canCrossZone }: Props) {
  const incidents = useIncidents()
  const [expandedId, setExpandedId] = useState<number | null>(null)

  if (incidents.isPending) return <p className="muted">加载中…</p>
  if (incidents.isError) return <p className="error-text">事故列表加载失败: {incidents.error.message}</p>

  const rows = incidents.data
  if (rows.length === 0) return <p className="muted">暂无事故。可在右侧演练面板触发一个场景。</p>

  return (
    <table className="incident-table">
      <thead>
        <tr>
          <th>#</th>
          <th>状态</th>
          <th>区域</th>
          <th>传感器</th>
          <th>派给谁</th>
          <th>谁接的单</th>
          <th>开始时间</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((i) => (
          <IncidentRow
            key={i.id}
            incident={i}
            expanded={expandedId === i.id}
            onToggle={() => setExpandedId(expandedId === i.id ? null : i.id)}
            canTransition={canTransition}
            canCrossZone={canCrossZone}
          />
        ))}
      </tbody>
    </table>
  )
}

function IncidentRow({
  incident: i,
  expanded,
  onToggle,
  canTransition,
  canCrossZone,
}: {
  incident: Incident
  expanded: boolean
  onToggle: () => void
  canTransition: boolean
  canCrossZone: boolean
}) {
  return (
    <>
      <tr className="expandable" onClick={onToggle}>
        <td>{i.id}</td>
        <td>
          <span className={`badge ${i.status}`}>{STATUS_LABEL[i.status]}</span>
        </td>
        <td>{i.zone_name ?? '—'}</td>
        <td>{i.sensor_id !== null ? `S${i.sensor_id}` : '—'}</td>
        <td>{i.assigned_employee_name ?? '—'}</td>
        <td>{i.acknowledged_by_employee_name ?? '—'}</td>
        <td className="muted">{fmtTime(i.opened_at)}</td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={7}>
            <IncidentDetail incident={i} canTransition={canTransition} canCrossZone={canCrossZone} />
          </td>
        </tr>
      )}
    </>
  )
}

function IncidentDetail({
  incident: i,
  canTransition,
  canCrossZone,
}: {
  incident: Incident
  canTransition: boolean
  canCrossZone: boolean
}) {
  const detail = useIncidentDetail(i.id)
  const qc = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  const [assignOpen, setAssignOpen] = useState(false)
  const [employeeId, setEmployeeId] = useState('')
  const [allowCrossZone, setAllowCrossZone] = useState(false)

  const mutation = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: unknown }) =>
      post<{ ok: boolean }>(path, body),
    onSuccess: () => {
      setError(null)
      setAssignOpen(false)
      setAllowCrossZone(false)
      // 每步流转之后立即刷新列表与时间线, 不等下一个轮询周期
      void qc.invalidateQueries({ queryKey: ['incidents'] })
      void qc.invalidateQueries({ queryKey: ['incident', i.id] })
    },
    onError: (err) => setError(err instanceof ApiError ? err.detail : err.message),
  })

  const submitAssign = (e: FormEvent) => {
    e.preventDefault()
    const id = Number(employeeId)
    if (!Number.isInteger(id) || id <= 0) {
      setError('请输入员工 ID (正整数)')
      return
    }
    mutation.mutate({
      path: `/incidents/${i.id}/assign`,
      body: { employee_id: id, allow_cross_zone: allowCrossZone },
    })
  }

  const canAssign = i.status === 'open' || i.status === 'assigned'
  const canAck = i.status === 'open' || i.status === 'assigned'
  const canResolve = i.status !== 'resolved'

  return (
    <div className="incident-detail">
      {canTransition && (
        <div className="row-actions">
          {canAssign && (
            <button className="btn-ghost btn-sm" onClick={() => setAssignOpen(!assignOpen)}>
              {i.status === 'assigned' ? '改派…' : '派单…'}
            </button>
          )}
          {canAck && (
            <button
              className="btn-ghost btn-sm"
              disabled={mutation.isPending}
              onClick={() => mutation.mutate({ path: `/incidents/${i.id}/acknowledge` })}
            >
              接单
            </button>
          )}
          {canResolve && (
            <button
              className="btn-ghost btn-sm"
              disabled={mutation.isPending}
              onClick={() => mutation.mutate({ path: `/incidents/${i.id}/resolve` })}
            >
              解决
            </button>
          )}
          {!canAssign && !canAck && !canResolve && <span className="muted small">已解决, 无可用操作</span>}
        </div>
      )}

      {assignOpen && canTransition && (
        <form className="assign-form" onSubmit={submitAssign}>
          <input
            type="number"
            min={1}
            placeholder="员工 ID"
            value={employeeId}
            onChange={(e) => setEmployeeId(e.target.value)}
            style={{ width: 110 }}
          />
          {/* 跨区放行是 manager/admin 的能力, operator 连选项都不给 —— 给了也只会 403 */}
          {canCrossZone && (
            <label className="small">
              <input
                type="checkbox"
                checked={allowCrossZone}
                onChange={(e) => setAllowCrossZone(e.target.checked)}
              />{' '}
              允许跨区派单
            </label>
          )}
          <button className="btn btn-sm" type="submit" disabled={mutation.isPending}>
            确认
          </button>
          <span className="muted small">默认只能派给事故所在区域的员工</span>
        </form>
      )}

      {error && <p className="error-text small">{error}</p>}

      <div className="small">
        <span className="muted">派给谁: </span>
        {i.assigned_employee_name ?? '未派单'}
        {i.assigned_at && <span className="muted"> ({fmtTime(i.assigned_at)})</span>}
        <span className="muted"> · 谁接的单: </span>
        {i.acknowledged_by_employee_name ?? '未接单'}
        {i.acknowledged_at && <span className="muted"> ({fmtTime(i.acknowledged_at)})</span>}
        {i.resolved_by && (
          <>
            <span className="muted"> · 解决方式: </span>
            {i.resolved_by}
          </>
        )}
      </div>

      <strong className="small">时间线</strong>
      {detail.isPending ? (
        <p className="muted small">加载中…</p>
      ) : detail.isError ? (
        <p className="error-text small">时间线加载失败: {detail.error.message}</p>
      ) : (
        <ul className="timeline small">
          {detail.data.events.map((e) => (
            <li key={e.id}>
              <span className="t">{fmtTime(e.at)}</span>
              <span>
                <strong>{e.kind}</strong>
                <span className="muted"> by {e.actor}</span>
                {e.detail && <span className="muted"> {JSON.stringify(e.detail)}</span>}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
