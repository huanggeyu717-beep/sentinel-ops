// Automation Studio (SPEC-002 第三段): 一句人话 -> Trace 逐步冒出来 ->
// 回放报告 -> 人审批。四块: 提交框 / Trace 时间线 / 澄清回答框 / 审批区。
import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { ApiError, post } from '../../api/client'
import { useAgentTasks, usePolicies } from '../../api/queries'
import type { AgentTaskCreated, AgentTaskListItem, Me, TimelineItem } from '../../api/types'
import { fmtTime } from '../../lib/format'
import ApprovalPanel from './ApprovalPanel'
import ReportPanel from './ReportPanel'
import TraceTimeline from './TraceTimeline'
import { useTaskEvents } from './useTaskEvents'

export default function StudioPage({ me }: { me: Me }) {
  // 任务 id 进 URL (/studio?task=N), 直达链接仍是分享机制; 但审批人打开系统
  // 应该自己看见"有几条等我批", 所以还有下面的任务列表 (W4 收尾第三条),
  // 点一条 URL 跟着变 —— 两个入口落到同一个状态上。
  const [params, setParams] = useSearchParams()
  const urlTask = Number(params.get('task'))
  const [taskId, setTaskIdState] = useState<number | null>(
    Number.isInteger(urlTask) && urlTask > 0 ? urlTask : null,
  )
  const [notice, setNotice] = useState<string | null>(null)
  const { items, status, broken } = useTaskEvents(taskId)

  const setTaskId = (id: number) => {
    setTaskIdState(id)
    setParams({ task: String(id) }, { replace: true })
  }

  return (
    <>
      <header className="topbar">
        <h1>Sentinel · Automation Studio</h1>
        <div className="topbar-actions">
          <span className="who small">
            <strong>{me.user.display_name ?? me.user.email}</strong> ·{' '}
            {me.roles.join(', ') || '无角色'}
          </span>
          <Link className="btn-ghost btn-sm" to="/">
            返回控制台
          </Link>
        </div>
      </header>
      <main className="studio">
        <SubmitBox
          onSubmitted={(created, message) => {
            setNotice(message)
            setTaskId(created.task_id)
          }}
        />

        <TaskListPanel selectedId={taskId} onSelect={setTaskId} />

        {notice && <p className="muted">{notice}</p>}
        {broken && (
          <p className="error-text">实时推送断了且自动重连失败 —— 刷新页面可恢复。</p>
        )}

        {taskId !== null && (
          <section className="card" aria-label="执行过程">
            <h2>
              执行过程 <span className="small muted">任务 #{taskId}</span>
              {status && (
                <span style={{ marginLeft: 8 }}>
                  <StatusBadge status={status.status} stage={status.stage} />
                </span>
              )}
            </h2>
            <TraceTimeline items={items} />
            {status?.error_detail && (
              <p className="error-text">{status.error_detail}</p>
            )}
          </section>
        )}

        {taskId !== null && status?.status === 'clarifying' && (
          <ClarifyBox taskId={taskId} items={items} />
        )}

        {taskId !== null && (
          <ApprovalPanel me={me} items={items} taskStatus={status?.status ?? null} />
        )}

        {/* W6 事故报告 (SPEC-008): 生成任务的执行过程复用上面的时间线 */}
        <ReportPanel onTaskStarted={setTaskId} />
      </main>
    </>
  )
}

const STATUS_LABELS: Record<string, string> = {
  running: '执行中',
  clarifying: '等你回答',
  awaiting_approval: '等待审批',
  awaiting_review: '等人过目', // W6 报告任务: 过目不是审批, 词也分开 (SPEC-008 第五节)
  completed: '已完成',
  failed: '失败',
  dead_letter: '异常中止',
}

function StatusBadge({ status, stage }: { status: string; stage: string }) {
  const cls =
    status === 'failed' || status === 'dead_letter'
      ? 'open'
      : status === 'completed'
        ? 'resolved'
        : status === 'running'
          ? 'acknowledged'
          : 'assigned'
  return (
    <span className={`badge ${cls}`}>
      {STATUS_LABELS[status] ?? status}
      {status === 'running' && ` · ${stage}`}
    </span>
  )
}

/** 任务列表: 5 秒轮询 (useAgentTasks 的注释里有"为什么不是 SSE")。
    服务端已排好序 —— 未走完的在前, 等审批的就在最上面几行。 */
function TaskListPanel({
  selectedId,
  onSelect,
}: {
  selectedId: number | null
  onSelect: (id: number) => void
}) {
  const tasks = useAgentTasks()
  if (tasks.isError) {
    return (
      <section className="card" aria-label="任务列表">
        <h2>任务</h2>
        <p className="error-text">任务列表加载失败, 将自动重试…</p>
      </section>
    )
  }
  const rows = tasks.data ?? []
  const awaiting = rows.filter((t) => t.status === 'awaiting_approval').length

  return (
    <section className="card" aria-label="任务列表">
      <h2>
        任务
        {awaiting > 0 && (
          <span className="badge assigned" style={{ marginLeft: 8 }}>
            {awaiting} 条等待审批
          </span>
        )}
      </h2>
      {tasks.isPending ? (
        <p className="muted">加载中…</p>
      ) : rows.length === 0 ? (
        <p className="muted">还没有任务 —— 在下方输入一句话发起第一条。</p>
      ) : (
        <ul className="task-list">
          {rows.map((t) => (
            <li key={t.id}>
              <TaskRow task={t} selected={t.id === selectedId} onSelect={onSelect} />
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function TaskRow({
  task: t,
  selected,
  onSelect,
}: {
  task: AgentTaskListItem
  selected: boolean
  onSelect: (id: number) => void
}) {
  const cls = [
    'task-item',
    selected ? 'selected' : '',
    // 审批人打开页面第一眼要找的东西, 加左侧色条, 不只靠徽标文字
    t.status === 'awaiting_approval' ? 'awaiting' : '',
  ]
    .filter(Boolean)
    .join(' ')
  return (
    <button type="button" className={cls} onClick={() => onSelect(t.id)}>
      <StatusBadge status={t.status} stage={t.stage} />
      <span className="task-text">
        {t.input_preview}
        {t.input_truncated && '…'}
      </span>
      {t.policy_name && <span className="muted small">{t.policy_name}</span>}
      <span className="muted small">{t.requested_by}</span>
      <span className="muted small">{fmtTime(t.created_at)}</span>
    </button>
  )
}

function SubmitBox({
  onSubmitted,
}: {
  onSubmitted: (created: AgentTaskCreated, message: string | null) => void
}) {
  const policies = usePolicies()
  const [text, setText] = useState('')
  const [targetId, setTargetId] = useState<number | ''>('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      const created = await post<AgentTaskCreated>('/agent-tasks', {
        text: text.trim(),
        target_policy_id: targetId === '' ? null : targetId,
      })
      onSubmitted(created, dedupeMessage(created))
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="card" aria-label="提交">
      <h2>用一句话描述你要的规则</h2>
      <div className="studio-submit">
        <input
          type="text"
          value={text}
          placeholder="例: 生鲜区两个探头三分钟内都湿了就通知这个区的主管"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && text.trim() && !busy) void submit()
          }}
        />
        <select
          value={targetId}
          onChange={(e) =>
            setTargetId(e.target.value === '' ? '' : Number(e.target.value))
          }
          aria-label="改哪条已有策略"
        >
          <option value="">新建策略</option>
          {(policies.data ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              改: {p.name}
            </option>
          ))}
        </select>
        <button className="btn" disabled={!text.trim() || busy} onClick={() => void submit()}>
          {busy ? '提交中…' : '编译'}
        </button>
      </div>
      {error && <p className="error-text">{error}</p>}
    </section>
  )
}

/** 去重命中不是错误 (SPEC-002 第二节): 说清带回的是哪条, 尤其"疑似中断"。 */
function dedupeMessage(created: AgentTaskCreated): string | null {
  if (created.created) return null
  if (created.suspected_interrupted) {
    return '这句话有一条疑似中断的任务 (服务可能刚重启过), 已带你回到它 —— 稍后可重试。'
  }
  if (created.status === 'clarifying') {
    return '这句话已有一条在等你回答的任务, 先回答它, 不再新开。'
  }
  return '同样的话已有一条任务在跑, 已带你回到它 —— 没有重复提交。'
}

function ClarifyBox({ taskId, items }: { taskId: number; items: TimelineItem[] }) {
  const [answer, setAnswer] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // 挂着的问题 = 问题数多于回答数时的最后一问 (服务端保证同时最多一个未回答)
  const questions = items.filter((i) => i.kind === 'clarification_question')
  const answers = items.filter((i) => i.kind === 'clarification_answer')
  const pending = questions.length > answers.length ? questions[questions.length - 1] : null
  if (!pending) return null

  const reply = async () => {
    setBusy(true)
    setError(null)
    try {
      await post(`/agent-tasks/${taskId}/reply`, { answer: answer.trim() })
      setAnswer('') // 回答后同一条任务接着跑, 时间线经 SSE 继续往下长
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="card clarify-card" aria-label="回答澄清">
      <h2>Agent 在等你回答</h2>
      <p>{pending.label}</p>
      <div className="studio-submit">
        <input
          type="text"
          value={answer}
          placeholder="你的回答 (只有发起人本人能答)"
          onChange={(e) => setAnswer(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && answer.trim() && !busy) void reply()
          }}
        />
        <button className="btn" disabled={!answer.trim() || busy} onClick={() => void reply()}>
          回答并继续
        </button>
      </div>
      {error && <p className="error-text">{error}</p>}
    </section>
  )
}
