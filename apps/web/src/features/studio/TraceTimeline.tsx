// Trace 时间线 —— 这个功能的招牌 (SPEC-002 第十节)。
// 四种 kind 在视觉上分开: 状态迁移是居中的小胶囊, 工具步骤是带耗时/token 的行,
// 澄清问答是左右对话气泡。步骤详情 (含修复前的草稿 body) 折叠在 details 里。
import type { TimelineItem } from '../../api/types'

const STAGE_LABELS: Record<string, string> = {
  parsing: '理解输入',
  discovering: '发现实体',
  compiling: '编译草案',
  validating: '静态校验',
  repairing: '修复',
  simulating: '历史回放',
  clarifying: '等待澄清',
  awaiting_approval: '等待审批',
  completed: '完成',
  failed: '失败',
  dead_letter: '死信',
}

const TOOL_LABELS: Record<string, string> = {
  parse_input: '理解输入',
  list_zones: '查区列表',
  list_sensors: '查传感器',
  list_roles: '查在册角色',
  list_employees: '查员工名录',
  get_policy: '读已有策略',
  get_available_actions: '取 DSL Schema',
  create_policy: '新建策略草稿',
  add_policy_version: '新增版本草稿',
  update_policy_draft: '修改草稿',
  validate_policy: '静态校验',
  simulate_policy: '历史回放',
  request_approval: '提交审批',
}

export default function TraceTimeline({ items }: { items: TimelineItem[] }) {
  if (items.length === 0) {
    return <p className="muted">还没有步骤 —— 提交一句话, 这里会一步步长出来。</p>
  }
  return (
    <ol className="trace" aria-label="执行时间线">
      {items.map((item) => (
        <li key={item.seq}>
          <TraceItem item={item} />
        </li>
      ))}
    </ol>
  )
}

function TraceItem({ item }: { item: TimelineItem }) {
  switch (item.kind) {
    case 'transition': {
      const to = String(item.arguments?.to ?? '')
      const failed = to === 'failed' || to === 'dead_letter'
      return (
        <div className={`trace-transition${failed ? ' bad' : ''}`}>
          <span className="seq">#{item.seq}</span>
          进入 {STAGE_LABELS[to] ?? to}
          {typeof item.arguments?.error_code === 'string' && item.arguments.error_code && (
            <> · {String(item.arguments.error_code)}</>
          )}
        </div>
      )
    }
    case 'clarification_question':
      return (
        <div className="trace-bubble question">
          <span className="seq">#{item.seq}</span>
          <strong>Agent 问</strong>
          <p>{item.label}</p>
        </div>
      )
    case 'clarification_answer':
      return (
        <div className="trace-bubble answer">
          <span className="seq">#{item.seq}</span>
          <strong>发起人答</strong>
          <p>{item.label}</p>
        </div>
      )
    default:
      return <StepRow item={item} />
  }
}

function StepRow({ item }: { item: TimelineItem }) {
  const ok = statusOf(item)
  return (
    <div className={`trace-step${ok ? '' : ' bad'}`}>
      <span className="seq">#{item.seq}</span>
      <span className="tool">{TOOL_LABELS[item.label] ?? item.label}</span>
      <span className="metrics small">
        {item.latency_ms !== null && <span>{item.latency_ms}ms</span>}
        {item.retry_count !== null && item.retry_count > 0 && (
          <span>重试 {item.retry_count}</span>
        )}
        {item.input_tokens !== null && (
          <span>
            tokens {item.input_tokens}↓ {item.output_tokens ?? 0}↑
          </span>
        )}
      </span>
      {item.detail && Object.keys(item.detail).length > 0 && (
        <details className="trace-detail">
          <summary className="small">详情</summary>
          <pre className="trace-json">{JSON.stringify(item.detail, null, 1)}</pre>
        </details>
      )}
    </div>
  )
}

/** validate_policy 的失败要在时间线上一眼看出来 (detail.ok === false)。 */
function statusOf(item: TimelineItem): boolean {
  if (item.label === 'validate_policy' && item.detail && 'ok' in item.detail) {
    return item.detail.ok === true
  }
  return true
}
