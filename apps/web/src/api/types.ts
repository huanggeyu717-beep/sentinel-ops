// 与后端 service 层返回的字段一一对应 (device_service / incident_service /
// drill_service / auth_service), 改这里之前先改后端。

export interface Sensor {
  sensor_id: number
  // 下面四个在 never_reported 时全是 null: 配置表里有这个探头, 但它一次都没上报过。
  // 类型上就必须可空 —— 写成非空等于假装"装了就一定有数据"。
  state: string | null // 'WET' | 'DRY' (设备上报原文, 大写)
  wet: boolean | null
  last_value: number | null
  updated_at: string | null
  age_seconds: number | null
  // true = 配置里有但从没上报过。与"上报过但超时失联"是两种故障 (去装 / 去修),
  // 前端必须分开画。
  never_reported: boolean
  zone_id: number | null
  zone_name: string | null
  threshold_value: number | null
  active: boolean | null
  pos_x: number | null // 相对底图的百分比 0-100 (SPEC-005 前置 A), 未标定为 null
  pos_y: number | null
}

export interface Device {
  device_id: string
  last_seen_at: string | null
  uptime_ms: number | null
  age_seconds: number | null
  never_reported: boolean
  pos_x: number | null
  pos_y: number | null
  online: boolean // 从没上报过时恒为 false, 靠 never_reported 区分两者
}

export type IncidentStatus = 'open' | 'assigned' | 'acknowledged' | 'resolved'

export interface Incident {
  id: number
  zone_id: number | null
  zone_name: string | null
  sensor_id: number | null
  severity: string
  status: IncidentStatus
  // "派给谁"与"谁接的单"是两个字段 (SPEC-003 修订 1), 展示时必须分列
  assigned_employee_id: number | null
  assigned_employee_name: string | null
  acknowledged_by_employee_id: number | null
  acknowledged_by_employee_name: string | null
  opened_at: string
  assigned_at: string | null
  acknowledged_at: string | null
  resolved_at: string | null
  resolved_by: string | null
}

export interface IncidentEvent {
  id: number
  kind: string
  actor: string
  detail: Record<string, unknown> | null
  at: string
}

export interface Reading {
  id: number
  received_at: string
  device_id: string
  sensor_id: number
  value: number | null
  wet: boolean
}

export interface Scenario {
  scenario: string // POST /drills/{scenario} 用这个
  name: string
  events_total: number
  duration_s: number
}

export interface Drill {
  drill_id: string
  scenario: string
  speed: number
  status: 'running' | 'completed' | 'failed'
  events_total: number
  events_sent: number
  started_at: string
  finished_at: string | null
  error: string | null
  note: string
}

export interface User {
  id: number
  email: string
  display_name: string | null
  employee_id: number | null
}

export interface Me {
  user: User
  roles: string[]
  employee: { id: number; name: string; zone_id: number | null } | null
}

// ===== W4 Automation Studio (SPEC-002 第三段) =====
// 与 apps/api/app/routers/agent_tasks.py 的响应模型一一对应, 改这里之前先改后端。

export interface AgentTaskCreated {
  ok: boolean
  task_id: number
  created: boolean // false = 撞上还没走完的同一句话, 拿回那一条 (不是错误)
  status: string
  stage: string | null
  // running 但打卡已停 (服务可能刚崩溃过): 界面标"疑似中断", 不说"重复提交"
  suspected_interrupted: boolean
}

export type TimelineKind =
  | 'transition'
  | 'step'
  | 'clarification_question'
  | 'clarification_answer'

export interface TimelineItem {
  seq: number
  kind: TimelineKind
  label: string // step: 工具名; transition: 'stage_transition'; 澄清: 问题/回答原文
  detail: Record<string, unknown> | null
  arguments: Record<string, unknown> | null // transition 的去向在 arguments.to
  latency_ms: number | null
  retry_count: number | null
  input_tokens: number | null
  output_tokens: number | null
}

export interface AgentTaskInfo {
  id: number
  user_id: number
  status: string
  stage: string
  error_code: string | null
  error_detail: string | null
  input_text: string
  target_policy_id: number | null
  created_at: string
  completed_at: string | null
}

export interface AgentTaskListItem {
  id: number
  status: string
  stage: string
  error_code: string | null
  input_preview: string // 服务端截断到 80 字, 整段原文在单条接口里
  input_truncated: boolean
  requested_by: string
  policy_name: string | null
  created_at: string
  completed_at: string | null
}

export interface AgentTaskSnapshot {
  ok: boolean
  task: AgentTaskInfo
  timeline: TimelineItem[]
}

export interface TaskStatusEvent {
  status: string
  stage: string
  error_code: string | null
  error_detail: string | null
}

export interface PolicyListItem {
  id: number
  name: string
  created_by: number | null
  created_at: string
  publication_id: number | null
  active_version_id: number | null
  active_version: number | null
  latest_version: number | null
}

export interface PolicyVersionDetail {
  ok: boolean
  id: number
  policy_id: number
  version: number
  name: string
  status: string
  created_at: string
  body: Record<string, unknown>
}

// ===== W6 事故报告 (SPEC-008) =====
// 与 apps/api/app/routers/reports.py 的响应模型一一对应, 改这里之前先改后端。

export interface ReportBody {
  summary: string
  handling: string
  impact: string
  notable: string
  suggestion: string
}

export interface ReportFact {
  id: string
  label: string
  value: unknown
  text: string
}

export interface IncidentReport {
  id: number
  incident_id: number
  task_id: number
  status: 'draft' | 'final' | 'discarded'
  body: ReportBody // 占位符原文
  rendered: ReportBody | null // 渲染后的正文; 草稿还没过校验时为 null
  fact_pack: ReportFact[] // 生成那一刻的事实包快照
  // 两个倾向计数, 分开展示、不加总 (SPEC-008 第三节: 不报"幻觉率")
  bare_fact_attempts: number
  dangling_ref_attempts: number
  created_by: number
  created_at: string
  updated_at: string
  finalized_by: number | null
  finalized_at: string | null
}

export interface ReportSnapshot {
  ok: boolean
  report: IncidentReport
}

export interface ReportTaskCreated {
  ok: boolean
  task_id: number
  created: boolean // false = 已有未走完的报告任务 (任何人开的), 带回那一条
  status: string
  stage: string | null
  suspected_interrupted: boolean
}
