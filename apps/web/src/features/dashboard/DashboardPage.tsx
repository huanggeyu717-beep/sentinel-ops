// Dashboard 第一页 (SPEC-005): 顶栏 + 平面图(带抽屉) + 事故列表 + 演练面板。
// 传感器/设备/事故 5 秒轮询, 演练进度 2 秒 (决策 1, 轮询定义在 api/queries.ts)。
import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { post } from '../../api/client'
import { useDevices, useSensors } from '../../api/queries'
import type { Me } from '../../api/types'
import { THEME_LABEL, THEME_ORDER, useThemeStore } from '../../theme'
import { canCrossZoneAssign, canTransitionIncidents, canTriggerDrill } from '../auth/roles'
import DrillPanel from './DrillPanel'
import FloorPlan from './FloorPlan'
import IncidentList from './IncidentList'
import SensorDrawer from './SensorDrawer'

export default function DashboardPage({ me }: { me: Me }) {
  const sensors = useSensors()
  const devices = useDevices()
  const [selectedSensorId, setSelectedSensorId] = useState<number | null>(null)

  const roles = me.roles
  const selectedSensor =
    selectedSensorId !== null
      ? (sensors.data ?? []).find((s) => s.sensor_id === selectedSensorId) ?? null
      : null

  return (
    <>
      <TopBar me={me} />
      <main className="dashboard">
        <section className="card plan-card" aria-label="平面图">
          <h2>门店平面图</h2>
          {sensors.isError || devices.isError ? (
            <p className="error-text">状态加载失败, 将自动重试…</p>
          ) : sensors.isPending || devices.isPending ? (
            <p className="muted">加载中…</p>
          ) : (
            <div className="plan-body">
              <FloorPlan
                sensors={sensors.data}
                devices={devices.data}
                selectedSensorId={selectedSensorId}
                onSelectSensor={setSelectedSensorId}
              />
              {selectedSensor && (
                <SensorDrawer sensor={selectedSensor} onClose={() => setSelectedSensorId(null)} />
              )}
            </div>
          )}
        </section>

        <section className="card" aria-label="事故列表">
          <h2>事故</h2>
          <IncidentList
            canTransition={canTransitionIncidents(roles)}
            canCrossZone={canCrossZoneAssign(roles)}
          />
        </section>

        <section className="card" aria-label="演练面板">
          <h2>演练</h2>
          <DrillPanel canTrigger={canTriggerDrill(roles)} />
        </section>
      </main>
    </>
  )
}

function TopBar({ me }: { me: Me }) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { theme, setTheme } = useThemeStore()

  const cycleTheme = () => {
    const next = THEME_ORDER[(THEME_ORDER.indexOf(theme) + 1) % THEME_ORDER.length]
    setTheme(next)
  }

  const logout = async () => {
    // cookie 由服务端置空+过期; 本地只需清掉缓存的查询结果
    await post('/auth/logout').catch(() => undefined)
    qc.clear()
    navigate('/login', { replace: true })
  }

  return (
    <header className="topbar">
      <h1>Sentinel</h1>
      <div className="topbar-actions">
        <span className="who small">
          <strong>{me.user.display_name ?? me.user.email}</strong> · {me.roles.join(', ') || '无角色'}
        </span>
        <button className="btn-ghost btn-sm" onClick={cycleTheme}>
          {THEME_LABEL[theme]}
        </button>
        <button className="btn-ghost btn-sm" onClick={() => void logout()}>
          退出登录
        </button>
      </div>
    </header>
  )
}
