// 数据获取全走 react-query: 轮询用 refetchInterval (SPEC-005 决策 1),
// 传感器/设备/事故 5 秒, 演练进度 2 秒 (在 DrillPanel 里单独定义)。
import { useQuery } from '@tanstack/react-query'

import { api } from './client'
import type {
  AgentTaskListItem,
  Device,
  Incident,
  IncidentEvent,
  Me,
  PolicyListItem,
  PolicyVersionDetail,
  Reading,
  Scenario,
  Sensor,
} from './types'

export const POLL_STATUS_MS = 5000
export const POLL_DRILL_MS = 2000

export const useMe = () =>
  useQuery({
    queryKey: ['me'],
    queryFn: () => api<{ ok: boolean } & Me>('/auth/me'),
    retry: false,
    staleTime: 60_000,
  })

export const useSensors = () =>
  useQuery({
    queryKey: ['sensors'],
    queryFn: () => api<{ ok: boolean; sensors: Sensor[] }>('/status/sensors'),
    refetchInterval: POLL_STATUS_MS,
    select: (d) => d.sensors,
  })

export const useDevices = () =>
  useQuery({
    queryKey: ['devices'],
    queryFn: () => api<{ ok: boolean; devices: Device[] }>('/status/devices'),
    refetchInterval: POLL_STATUS_MS,
    select: (d) => d.devices,
  })

export const useIncidents = () =>
  useQuery({
    queryKey: ['incidents'],
    queryFn: () => api<{ ok: boolean; incidents: Incident[] }>('/incidents'),
    refetchInterval: POLL_STATUS_MS,
    select: (d) => d.incidents,
  })

export const useIncidentDetail = (incidentId: number | null) =>
  useQuery({
    queryKey: ['incident', incidentId],
    queryFn: () =>
      api<{ ok: boolean; incident: Incident; events: IncidentEvent[] }>(
        `/incidents/${incidentId}`,
      ),
    enabled: incidentId !== null,
    refetchInterval: POLL_STATUS_MS,
  })

export const useRecentReadings = (sensorId: number | null) =>
  useQuery({
    queryKey: ['readings', sensorId],
    queryFn: () =>
      api<{ ok: boolean; readings: Reading[] }>(`/status/readings?sensor_id=${sensorId}&limit=10`),
    enabled: sensorId !== null,
    refetchInterval: POLL_STATUS_MS,
    select: (d) => d.readings,
  })

export const useScenarios = () =>
  useQuery({
    queryKey: ['scenarios'],
    queryFn: () => api<{ ok: boolean; scenarios: Scenario[] }>('/drills/scenarios'),
    staleTime: 60_000,
    select: (d) => d.scenarios,
  })

// ===== W4 Automation Studio (SPEC-002 第三段) =====
// Agent 执行进度走 SSE (features/studio/useTaskEvents), 不在这里轮询 ——
// SPEC-005 "状态与事故用轮询、Agent 用 SSE"。下面两条是 Studio 的静态数据。

// 任务列表是"看当前值", 按 SPEC-005 的分界用轮询, 不开 SSE ——
// SSE 只给单个任务的执行过程 ("一步步冒出来"的那种)。
export const useAgentTasks = () =>
  useQuery({
    queryKey: ['agent-tasks'],
    queryFn: () => api<{ ok: boolean; tasks: AgentTaskListItem[] }>('/agent-tasks'),
    refetchInterval: POLL_STATUS_MS,
    select: (d) => d.tasks,
  })

export const usePolicies = () =>
  useQuery({
    queryKey: ['policies'],
    queryFn: () => api<{ ok: boolean; policies: PolicyListItem[] }>('/policies'),
    staleTime: 30_000,
    select: (d) => d.policies,
  })

export const usePolicyVersion = (versionId: number | null) =>
  useQuery({
    queryKey: ['policy-version', versionId],
    queryFn: () => api<PolicyVersionDetail>(`/policy-versions/${versionId}`),
    enabled: versionId !== null,
    staleTime: 10_000,
  })
