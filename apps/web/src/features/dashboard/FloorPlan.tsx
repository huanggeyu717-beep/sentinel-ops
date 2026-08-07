// 平面图: 底图 (PlanBase) 与数据点画在同一个 <svg> 里 —— 当 <img> 背景会让两者
// 分属两套坐标系, 容器一缩放就错位。底图只有 PlanBase.tsx 一份, 不再另存 .svg:
// 两份一定会走散, 而走散的那份看不出来。
//
// 状态的冗余编码 (照底图注释的约定): 红绿在色盲模拟下 ΔE=4.1, 低于可辨认下限 8,
// 形状必须独立可辨 —— 漏水=实心圆+水滴+外圈脉冲, 正常=空心圆, 失联=虚线圆+斜杠;
// 设备用方形与传感器区分, 离线同样虚线+斜杠。颜色只是强化, 图例带文字标签。
import type { KeyboardEvent } from 'react'

import type { Device, Sensor } from '../../api/types'
import PlanBase, { PLAN_ARIA_LABEL } from './PlanBase'

// 数据库存百分比 (0-100), 底图 viewBox 是 1000x600: x% × 10, y% × 6
const SX = (x: number) => x * 10
const SY = (y: number) => y * 6

// 显示层口径: 超过 15 分钟没有任何读数就画成"失联"。后端没有传感器级的在线判定
// (心跳是设备级的), 这里的阈值只影响画法, 不影响任何业务判断。
const SENSOR_STALE_SECONDS = 15 * 60

// never = 配置里有这个探头, 但它一次都没上报过 (装了没接上, 或压根没装)。
// 与 lost (上报过, 但超过 15 分钟没动静) 是两回事: 一个要去装, 一个要去修。
// 用同一个灰点糊过去, 现场就会跑错人。
export type SensorVisual = 'leak' | 'ok' | 'lost' | 'never'

export const sensorVisual = (s: Sensor): SensorVisual => {
  if (s.never_reported) return 'never'
  // age_seconds 只有 never_reported 时才是 null, 上面已经拦掉; 这里的 ?? 是给
  // 类型收窄用的兜底, 真走到说明后端字段口径变了, 当失联处理比当正常安全。
  if ((s.age_seconds ?? Number.POSITIVE_INFINITY) > SENSOR_STALE_SECONDS) return 'lost'
  return s.wet ? 'leak' : 'ok'
}

export const SENSOR_VISUAL_LABEL: Record<SensorVisual, string> = {
  leak: '漏水',
  ok: '正常',
  lost: '失联',
  never: '从未上报',
}

const DROPLET = 'M0 -6 C 3.2 -1.8 4.6 0.6 4.6 2.4 A 4.6 4.6 0 1 1 -4.6 2.4 C -4.6 0.6 -3.2 -1.8 0 -6 Z'

function SensorGlyph({ visual }: { visual: SensorVisual }) {
  switch (visual) {
    case 'leak':
      return (
        <>
          <circle className="pulse-ring" r={11} fill="none" stroke="var(--status-leak)" strokeWidth={2} />
          <circle r={11} fill="var(--status-leak)" />
          <path d={DROPLET} fill="var(--plan-surface)" />
        </>
      )
    case 'ok':
      return <circle r={9} fill="var(--plan-surface)" stroke="var(--status-ok)" strokeWidth={3} />
    case 'lost':
      return (
        <>
          <circle r={9} fill="var(--plan-surface)" stroke="var(--status-lost)" strokeWidth={2.5} strokeDasharray="4 3" />
          <line x1={-6} y1={6} x2={6} y2={-6} stroke="var(--status-lost)" strokeWidth={2.5} />
        </>
      )
    case 'never':
      // 与 lost 同为虚线圈, 但里面是问号而不是斜杠 —— 斜杠读作"断了",
      // 问号读作"没消息", 两个形状本身就能分开, 不靠颜色也不靠位置。
      return (
        <>
          <circle r={9} fill="var(--plan-surface)" stroke="var(--status-lost)" strokeWidth={2.5} strokeDasharray="4 3" />
          <text
            y={4.5}
            textAnchor="middle"
            fontSize={12}
            fontWeight={700}
            fontFamily="system-ui, -apple-system, sans-serif"
            fill="var(--status-lost)"
          >
            ?
          </text>
        </>
      )
  }
}

export type DeviceVisual = 'online' | 'offline' | 'never'

export const deviceVisual = (d: Device): DeviceVisual =>
  d.never_reported ? 'never' : d.online ? 'online' : 'offline'

export const DEVICE_VISUAL_LABEL: Record<DeviceVisual, string> = {
  online: '在线',
  offline: '离线',
  never: '从未上报',
}

function DeviceGlyph({ visual }: { visual: DeviceVisual }) {
  const box = (stroke: string, dashed: boolean) => (
    <rect
      x={-8}
      y={-8}
      width={16}
      height={16}
      rx={2}
      fill="var(--plan-surface)"
      stroke={stroke}
      strokeWidth={dashed ? 2.5 : 3}
      strokeDasharray={dashed ? '4 3' : undefined}
    />
  )
  switch (visual) {
    case 'online':
      return box('var(--status-ok)', false)
    case 'offline':
      return (
        <>
          {box('var(--status-lost)', true)}
          <line x1={-6} y1={6} x2={6} y2={-6} stroke="var(--status-lost)" strokeWidth={2.5} />
        </>
      )
    case 'never':
      return (
        <>
          {box('var(--status-lost)', true)}
          <text
            y={4.5}
            textAnchor="middle"
            fontSize={12}
            fontWeight={700}
            fontFamily="system-ui, -apple-system, sans-serif"
            fill="var(--status-lost)"
          >
            ?
          </text>
        </>
      )
  }
}

interface Props {
  sensors: Sensor[]
  devices: Device[]
  selectedSensorId: number | null
  onSelectSensor: (id: number | null) => void
}

export default function FloorPlan({ sensors, devices, selectedSensorId, onSelectSensor }: Props) {
  const placedSensors = sensors.filter((s) => s.pos_x !== null && s.pos_y !== null)
  const placedDevices = devices.filter((d) => d.pos_x !== null && d.pos_y !== null)
  // 无坐标的不能悄悄消失 (SPEC-005 前置 A): 列在图外的"未定位"区
  const unplacedSensors = sensors.filter((s) => s.pos_x === null || s.pos_y === null)
  const unplacedDevices = devices.filter((d) => d.pos_x === null || d.pos_y === null)

  const keySelect = (e: KeyboardEvent, id: number) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      onSelectSensor(id === selectedSensorId ? null : id)
    }
  }

  return (
    <div className="plan-svg-wrap">
      <svg
        xmlns="http://www.w3.org/2000/svg"
        viewBox="0 0 1000 600"
        role="img"
        aria-label={PLAN_ARIA_LABEL}
      >
        <PlanBase />

        {/* ===== 设备点 ===== */}
        {placedDevices.map((d) => (
          <g key={d.device_id} transform={`translate(${SX(d.pos_x!)} ${SY(d.pos_y!)})`}>
            <title>{`${d.device_id} · ${DEVICE_VISUAL_LABEL[deviceVisual(d)]}`}</title>
            <DeviceGlyph visual={deviceVisual(d)} />
            <text y={26} textAnchor="middle" fontSize={12} fill="var(--plan-label-muted)">
              {d.device_id}
            </text>
          </g>
        ))}

        {/* ===== 传感器点 (可点击出抽屉) ===== */}
        {placedSensors.map((s) => {
          const visual = sensorVisual(s)
          const selected = s.sensor_id === selectedSensorId
          return (
            <g
              key={s.sensor_id}
              className="marker"
              transform={`translate(${SX(s.pos_x!)} ${SY(s.pos_y!)})`}
              role="button"
              tabIndex={0}
              aria-label={`传感器 ${s.sensor_id}: ${SENSOR_VISUAL_LABEL[visual]}`}
              onClick={() => onSelectSensor(selected ? null : s.sensor_id)}
              onKeyDown={(e) => keySelect(e, s.sensor_id)}
            >
              <circle
                className="focus-ring"
                r={16}
                fill="none"
                stroke={selected ? 'var(--accent)' : 'none'}
                strokeWidth={2}
              />
              <SensorGlyph visual={visual} />
              <text y={32} textAnchor="middle" fontSize={13} fill="var(--plan-label)">
                S{s.sensor_id}
              </text>
            </g>
          )
        })}
      </svg>

      {/* 图例: 形状 + 文字, 不依赖颜色辨认 */}
      <div className="plan-legend small">
        <span className="item">
          <LegendGlyph visual="ok" /> 传感器·正常
        </span>
        <span className="item">
          <LegendGlyph visual="leak" /> 传感器·漏水
        </span>
        <span className="item">
          <LegendGlyph visual="lost" /> 传感器·失联 (15 分钟无数据)
        </span>
        <span className="item">
          <LegendGlyph visual="never" /> 传感器·从未上报
        </span>
        <span className="item">
          <LegendDevice visual="online" /> 设备·在线
        </span>
        <span className="item">
          <LegendDevice visual="offline" /> 设备·离线
        </span>
        <span className="item">
          <LegendDevice visual="never" /> 设备·从未上报
        </span>
      </div>

      {(unplacedSensors.length > 0 || unplacedDevices.length > 0) && (
        <div className="unpositioned small">
          <strong>未定位</strong>
          <span className="muted"> (已入库但尚未标注坐标): </span>
          {unplacedSensors.map((s) => {
            const visual = sensorVisual(s)
            return (
              <span className="item" key={`s-${s.sensor_id}`}>
                <LegendGlyph visual={visual} /> 传感器 S{s.sensor_id} · {SENSOR_VISUAL_LABEL[visual]}
              </span>
            )
          })}
          {unplacedDevices.map((d) => (
            <span className="item" key={`d-${d.device_id}`}>
              <LegendDevice visual={deviceVisual(d)} /> {d.device_id} ·{' '}
              {DEVICE_VISUAL_LABEL[deviceVisual(d)]}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function LegendGlyph({ visual }: { visual: SensorVisual }) {
  return (
    <svg width={22} height={22} viewBox="-13 -13 26 26" aria-hidden>
      <SensorGlyph visual={visual} />
    </svg>
  )
}

function LegendDevice({ visual }: { visual: DeviceVisual }) {
  return (
    <svg width={22} height={22} viewBox="-13 -13 26 26" aria-hidden>
      <DeviceGlyph visual={visual} />
    </svg>
  )
}
