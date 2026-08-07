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
