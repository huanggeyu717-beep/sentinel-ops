// 审批区: 回放报告 + 批准/否决 + 发布。
//
// 按角色置灰按钮只是体验优化, **前端隐藏不是安全措施** (SPEC-005 决策 2 的先例,
// SPEC-002 第五节那张四层表): 真正的拦截在服务端 —— 路由门 403、service 层
// 第二道闸、数据库的审批外键 (ADR-007)。绕过这里直接 curl 发布接口照样 403,
// test_agent_http 的验收 16 钉着这件事。
import { useState } from 'react'

import { ApiError, post } from '../../api/client'
import { usePolicyVersion } from '../../api/queries'
import type { Me, TimelineItem } from '../../api/types'

const APPROVER_ROLES = new Set(['manager', 'admin'])

interface DecideResult {
  ok: boolean
  decision: string
  version_status: string
}

interface PublishResult {
  ok: boolean
  publication_id: number
  version: number
}

export default function ApprovalPanel({
  me,
  items,
  taskStatus,
}: {
  me: Me
  items: TimelineItem[]
  taskStatus: string | null
}) {
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [decision, setDecision] = useState<string | null>(null)
  const [published, setPublished] = useState(false)
  const [busy, setBusy] = useState(false)

  const draft = items.find(
    (i) => i.label === 'create_policy' || i.label === 'add_policy_version',
  )?.detail
  const report = items.find((i) => i.label === 'simulate_policy')?.detail
  const approval = items.find((i) => i.label === 'request_approval')?.detail
  const versionId = typeof draft?.version_id === 'number' ? draft.version_id : null
  const approvalId =
    typeof approval?.approval_id === 'number' ? approval.approval_id : null
  const version = usePolicyVersion(versionId)

  if (!draft || !report || !approval) return null

  const canApprove = me.roles.some((r) => APPROVER_ROLES.has(r))
  const decidable = taskStatus === 'awaiting_approval' && decision === null

  const act = async <T,>(fn: () => Promise<T>, done: (r: T) => void) => {
    setBusy(true)
    setError(null)
    setMessage(null)
    try {
      done(await fn())
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e))
    } finally {
      setBusy(false)
    }
  }

  const decide = (d: 'approved' | 'rejected') =>
    void act(
      () => post<DecideResult>(`/approvals/${approvalId}/decide`, { decision: d }),
      (r) => {
        setDecision(r.decision)
        setMessage(r.decision === 'approved' ? '已批准, 现在可以发布。' : '已否决。')
      },
    )

  const publish = () =>
    void act(
      () => post<PublishResult>(`/policy-versions/${versionId}/publish`),
      (r) => {
        setPublished(true)
        setMessage(`已发布: v${r.version} (publication ${r.publication_id}) 正在生效。`)
      },
    )

  const warnings = Array.isArray(report.warnings)
    ? (report.warnings as { code: string; message: string }[])
    : []
  const byAction = (report.by_action_type ?? {}) as Record<string, number>
  const body = version.data?.body

  return (
    <section className="card" aria-label="审批区">
      <h2>回放报告与审批</h2>

      {/* 报数字必须带上产生它的配置 (SPEC-002 第八节): scope 与 cooldown_s
          和触发数摆在一起, 不然 65/35 那个坑会在这块界面上重演 */}
      <dl className="kv">
        <dt>草稿</dt>
        <dd>
          {version.data ? `${version.data.name} · v${version.data.version}` : '加载中…'}
          {typeof draft.version_id === 'number' && ` (version_id ${draft.version_id})`}
        </dd>
        <dt>作用范围</dt>
        <dd>{body ? JSON.stringify(body.scope) : '…'}</dd>
        <dt>冷却 (秒)</dt>
        <dd>{body ? String(body.cooldown_s) : '…'}</dd>
        <dt>回放数据</dt>
        <dd>
          {String(report.source)} · {String(report.events_count)} 个事件
        </dd>
        <dt>触发统计</dt>
        <dd>
          {Object.keys(byAction).length === 0
            ? '0 次'
            : Object.entries(byAction)
                .map(([k, v]) => `${k}: ${v} 次`)
                .join(' · ')}
        </dd>
      </dl>

      {/* 界面说事实, 不讲设计主张 —— "人工审批的意义"那类话属于 README
          与视频旁白, 不属于盯着警告框做决定的店长 (W4 收尾第一条)。 */}
      {warnings.map((w) =>
        w.code === 'W_NEVER_TRIGGERED' ? (
          <div key={w.code} className="warn-banner" role="alert">
            <strong>这条规则在这段历史数据里一次都没命中。</strong>
            <p>可能是条件写紧了, 也可能是这种情况本来就没发生过。</p>
            <p className="small muted">{w.message}</p>
          </div>
        ) : (
          <div key={w.code} className="warn-banner soft">
            <strong>{w.code}</strong>
            <p className="small">{w.message}</p>
          </div>
        ),
      )}

      <div className="row-actions">
        <button
          className="btn"
          disabled={!canApprove || !decidable || busy}
          onClick={() => decide('approved')}
        >
          批准
        </button>
        <button
          className="btn-ghost"
          disabled={!canApprove || !decidable || busy}
          onClick={() => decide('rejected')}
        >
          否决
        </button>
        <button
          className="btn"
          disabled={!canApprove || decision !== 'approved' || published || busy}
          onClick={publish}
        >
          发布上线
        </button>
        {!canApprove && (
          <span className="small muted">
            批准与发布需要 manager 及以上角色 (服务端强制, 置灰只是少一个注定 403 的按钮)
          </span>
        )}
      </div>

      {message && <p className="muted">{message}</p>}
      {error && <p className="error-text">{error}</p>}
    </section>
  )
}
