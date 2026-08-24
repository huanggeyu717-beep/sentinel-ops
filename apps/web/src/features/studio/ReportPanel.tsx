// 事故报告页 (SPEC-008 第二段): 挑一条已解决事故 -> 点生成 -> 生成过程走
// Studio 既有的任务时间线 (onTaskStarted 把 task_id 交给父组件) -> 停在等人
// 过目后, 这里展示渲染后的五个字段与两个倾向计数, 人点定稿或退回。
import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'

import { ApiError, post } from '../../api/client'
import { useIncidentReport, useIncidents } from '../../api/queries'
import type { IncidentReport, ReportTaskCreated } from '../../api/types'
import { fmtTime } from '../../lib/format'

export default function ReportPanel({
  onTaskStarted,
}: {
  onTaskStarted: (taskId: number) => void
}) {
  const incidents = useIncidents()
  const [incidentId, setIncidentId] = useState<number | ''>('')
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const queryClient = useQueryClient()
  const report = useIncidentReport(incidentId === '' ? null : incidentId)

  // 报告只对已解决事故生成 (事故还在跑时事实包会变, 报告是定影不是直播)
  const resolved = (incidents.data ?? []).filter((i) => i.status === 'resolved')

  const generate = async () => {
    if (incidentId === '') return
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const created = await post<ReportTaskCreated>(`/incidents/${incidentId}/report`)
      onTaskStarted(created.task_id)
      if (!created.created) {
        setNotice(
          created.status === 'awaiting_review'
            ? '这条事故已有一份报告在等人过目, 已带你回到它的任务。'
            : '这条事故已有人在生成报告, 已带你回到那条任务 —— 没有重复提交。',
        )
      } else {
        setNotice('报告任务已开始, 生成过程见上方执行时间线; 完成后草稿会出现在下面。')
      }
      await report.refetch()
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e))
    } finally {
      setBusy(false)
    }
  }

  const act = async (path: string) => {
    setBusy(true)
    setError(null)
    try {
      await post(path)
      await queryClient.invalidateQueries({ queryKey: ['incident-report'] })
      await queryClient.invalidateQueries({ queryKey: ['agent-tasks'] })
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="card" aria-label="事故报告">
      <h2>事故报告</h2>
      <div className="studio-submit">
        <select
          value={incidentId}
          onChange={(e) =>
            setIncidentId(e.target.value === '' ? '' : Number(e.target.value))
          }
          aria-label="选择已解决的事故"
        >
          <option value="">选择已解决的事故…</option>
          {resolved.map((i) => (
            <option key={i.id} value={i.id}>
              #{i.id} · {i.zone_name ?? '无区域'} · {fmtTime(i.opened_at)}
            </option>
          ))}
        </select>
        <button
          className="btn"
          disabled={incidentId === '' || busy}
          onClick={() => void generate()}
        >
          {busy ? '处理中…' : '生成报告'}
        </button>
      </div>
      {resolved.length === 0 && !incidents.isPending && (
        <p className="muted">还没有已解决的事故 —— 报告只对已解决的事故生成。</p>
      )}
      {notice && <p className="muted">{notice}</p>}
      {error && <p className="error-text">{error}</p>}

      {incidentId !== '' && report.data && (
        <ReportView
          report={report.data}
          busy={busy}
          onFinalize={() => void act(`/reports/${report.data!.id}/finalize`)}
          onDiscard={() => void act(`/reports/${report.data!.id}/discard`)}
        />
      )}
      {incidentId !== '' && report.data === null && !report.isPending && (
        <p className="muted">这条事故当前没有报告。</p>
      )}
    </section>
  )
}

const FIELD_LABELS: Array<[keyof IncidentReport['body'], string]> = [
  ['summary', '概要'],
  ['handling', '处理过程'],
  ['impact', '影响与耗时'],
  ['notable', '值得注意'],
  ['suggestion', '建议'],
]

function ReportView({
  report,
  busy,
  onFinalize,
  onDiscard,
}: {
  report: IncidentReport
  busy: boolean
  onFinalize: () => void
  onDiscard: () => void
}) {
  // 渲染后的正文优先; 修复中途 (还没过校验) 退回占位符原文, 并说明这是中间态
  const text = report.rendered ?? report.body
  return (
    <div className="report-view">
      <p>
        <span className={`badge ${report.status === 'final' ? 'resolved' : 'assigned'}`}>
          {report.status === 'final' ? '已定稿' : report.status === 'draft' ? '草稿' : '已弃'}
        </span>{' '}
        {report.rendered === null && (
          <span className="muted small">(草稿还在修复中, 下面是占位符原文)</span>
        )}
        {report.finalized_at && (
          <span className="muted small">定稿于 {fmtTime(report.finalized_at)}</span>
        )}
      </p>
      <dl>
        {FIELD_LABELS.map(([key, label]) => (
          <div key={key}>
            <dt>{label}</dt>
            <dd>{text[key] || <span className="muted">(空)</span>}</dd>
          </div>
        ))}
      </dl>
      <p className="muted small">
        {/* 两个数分开报, 不加总; 为 0 不说明模型老实, 只说明这一跑里它没试 */}
        裸写事实被拦 {report.bare_fact_attempts} 项 · 悬空引用被拦{' '}
        {report.dangling_ref_attempts} 项 · 每个数字与专名都可溯源到事实包快照 (
        {report.fact_pack.length} 条事实)
      </p>
      {report.status === 'draft' && (
        <div className="studio-submit">
          <button className="btn" disabled={busy} onClick={onFinalize}>
            定稿
          </button>
          <button className="btn-ghost" disabled={busy} onClick={onDiscard}>
            退回 (弃稿)
          </button>
        </div>
      )}
      {report.status === 'final' && (
        <div className="studio-submit">
          <button className="btn-ghost" disabled={busy} onClick={onDiscard}>
            弃稿 (弃掉后可重新生成)
          </button>
        </div>
      )}
    </div>
  )
}
