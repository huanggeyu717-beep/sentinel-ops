// 传感器抽屉: 最近读数 + 关联事故 (SPEC-005 页面结构·上栏)。
// 关联事故直接从事故列表查询里过滤, 不另开接口。
import { useIncidents, useRecentReadings } from '../../api/queries'
import type { Sensor } from '../../api/types'
import { fmtAge, fmtTime } from '../../lib/format'
import { SENSOR_VISUAL_LABEL, sensorVisual } from './FloorPlan'

interface Props {
  sensor: Sensor
  onClose: () => void
}

export default function SensorDrawer({ sensor, onClose }: Props) {
  const readings = useRecentReadings(sensor.sensor_id)
  const incidents = useIncidents()
  const related = (incidents.data ?? []).filter((i) => i.sensor_id === sensor.sensor_id)
  const openOnes = related.filter((i) => i.status !== 'resolved')

  return (
    <aside className="drawer" aria-label={`传感器 ${sensor.sensor_id} 详情`}>
      <div className="drawer-head">
        <h3>传感器 S{sensor.sensor_id}</h3>
        <button className="btn-ghost btn-sm" onClick={onClose}>
          关闭
        </button>
      </div>

      <dl className="kv">
        <dt>状态</dt>
        <dd>
          <strong>{SENSOR_VISUAL_LABEL[sensorVisual(sensor)]}</strong>
          {/* state 是设备上报的原文, 从没上报过的探头没有这个字段 */}
          {sensor.state !== null && <span className="muted"> ({sensor.state})</span>}
        </dd>
        <dt>区域</dt>
        <dd>{sensor.zone_name ?? '—'}</dd>
        <dt>最近读数</dt>
        <dd>
          {sensor.last_value ?? '—'}
          {sensor.threshold_value !== null && (
            <span className="muted"> / 阈值 {sensor.threshold_value}</span>
          )}
        </dd>
        <dt>更新于</dt>
        <dd>
          {fmtTime(sensor.updated_at)}
          <span className="muted"> ({fmtAge(sensor.age_seconds)})</span>
        </dd>
      </dl>

      <div>
        <strong className="small">关联事故</strong>
        {related.length === 0 ? (
          <p className="muted small">无</p>
        ) : (
          <ul className="mini-list small">
            {related.slice(0, 5).map((i) => (
              <li key={i.id}>
                <span>
                  #{i.id} <span className={`badge ${i.status}`}>{i.status}</span>
                </span>
                <span className="muted">{fmtTime(i.opened_at)}</span>
              </li>
            ))}
          </ul>
        )}
        {openOnes.length > 0 && (
          <p className="muted small">未解决 {openOnes.length} 起, 处理入口在下方事故列表。</p>
        )}
      </div>

      <div>
        <strong className="small">最近 10 条读数</strong>
        {readings.isPending ? (
          <p className="muted small">加载中…</p>
        ) : (
          <ul className="mini-list small">
            {(readings.data ?? []).map((r) => (
              <li key={r.id}>
                <span>
                  {r.value ?? '—'} {r.wet ? <strong>湿</strong> : '干'}
                </span>
                <span className="muted">{fmtTime(r.received_at)}</span>
              </li>
            ))}
            {(readings.data ?? []).length === 0 && <li className="muted">暂无读数</li>}
          </ul>
        )}
      </div>
    </aside>
  )
}
