"""事件规范化与落库 —— 取代原系统的 IoT Rules -> database-ingest Lambda。

设计要点(面试可讲):
1. **幂等**: 三类事件都有 (device_id, 业务标识, ts) 唯一键, 重放不会产生重复行;
2. **乱序容忍**: sensorstate / device_heartbeats 的 UPSERT 带 `WHERE 旧.ts < 新.ts`,
   迟到的旧事件不会覆盖较新的状态 —— 原系统的 Lambda 是无条件覆盖的, 这是一处实质修复;
3. **原始报文留存**: raw 列保留设备原始 JSON, 便于回溯与评测复现。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings


@dataclass(slots=True)
class IngestResult:
    kind: str
    stored: bool          # 是否新写入(False = 幂等命中或被乱序保护拒绝)
    state_updated: bool    # 是否推进了当前状态


def derive_wet(state: str | None, value: int | None) -> bool:
    """湿判定: 优先用设备上报的 state, 缺失时用阈值兜底。"""
    if state:
        return state.upper() == "WET"
    if value is not None:
        return value >= settings().default_wet_threshold
    return False


_INSERT_READING = text("""
    INSERT INTO waterlevel_readings
        (received_at, received_ts, device_id, sensor_id, value, wet, raw)
    VALUES (
        to_timestamp(CAST(:ts AS bigint) / 1000.0), CAST(:ts AS bigint),
        :device_id, :sensor_id, :value, :wet, CAST(:raw AS jsonb)
    )
    ON CONFLICT (device_id, sensor_id, received_ts) DO NOTHING
    RETURNING id
""")

_UPSERT_SENSORSTATE = text("""
    INSERT INTO sensorstate (sensor_id, wet, state, updated_ts, updated_at, last_value)
    VALUES (
        :sensor_id, :wet, :state, CAST(:ts AS bigint),
        to_timestamp(CAST(:ts AS bigint) / 1000.0), :value
    )
    ON CONFLICT (sensor_id) DO UPDATE SET
        wet        = EXCLUDED.wet,
        state      = EXCLUDED.state,
        updated_ts = EXCLUDED.updated_ts,
        updated_at = EXCLUDED.updated_at,
        last_value = EXCLUDED.last_value
    WHERE sensorstate.updated_ts < EXCLUDED.updated_ts
    RETURNING sensor_id
""")

_UPSERT_HEARTBEAT = text("""
    INSERT INTO device_heartbeats (device_id, last_seen_at, last_seen_ts, uptime_ms, raw)
    VALUES (
        :device_id, to_timestamp(CAST(:ts AS bigint) / 1000.0), CAST(:ts AS bigint),
        :uptime_ms, CAST(:raw AS jsonb)
    )
    ON CONFLICT (device_id) DO UPDATE SET
        last_seen_at = EXCLUDED.last_seen_at,
        last_seen_ts = EXCLUDED.last_seen_ts,
        uptime_ms    = EXCLUDED.uptime_ms,
        raw          = EXCLUDED.raw
    WHERE device_heartbeats.last_seen_ts < EXCLUDED.last_seen_ts
    RETURNING device_id
""")

_INSERT_RFID = text("""
    INSERT INTO rfid_scans (device_id, rfid_id, rfid_uid, scan_ts, scanned_at, raw)
    VALUES (
        :device_id, :rfid_id, :rfid_uid, CAST(:ts AS bigint),
        to_timestamp(CAST(:ts AS bigint) / 1000.0), CAST(:raw AS jsonb)
    )
    ON CONFLICT (device_id, rfid_uid, scan_ts) DO NOTHING
    RETURNING id
""")


async def handle_sensor_state(session: AsyncSession, ev: dict) -> IngestResult:
    wet = derive_wet(ev.get("state"), ev.get("value"))
    state = (ev.get("state") or ("WET" if wet else "DRY")).upper()
    params = {
        "ts": ev["ts"],
        "device_id": ev["device_id"],
        "sensor_id": ev["sensor_id"],
        "value": ev.get("value"),
        "wet": wet,
        "state": state,
        "raw": json.dumps(ev, ensure_ascii=False),
    }
    stored = (await session.execute(_INSERT_READING, params)).scalar_one_or_none() is not None
    updated = (await session.execute(_UPSERT_SENSORSTATE, params)).scalar_one_or_none() is not None
    return IngestResult("sensor_state", stored, updated)


async def handle_heartbeat(session: AsyncSession, ev: dict) -> IngestResult:
    params = {
        "ts": ev["ts"],
        "device_id": ev["device_id"],
        "uptime_ms": ev.get("uptime_ms"),
        "raw": json.dumps(ev, ensure_ascii=False),
    }
    updated = (await session.execute(_UPSERT_HEARTBEAT, params)).scalar_one_or_none() is not None
    return IngestResult("heartbeat", updated, updated)


async def handle_rfid_scan(session: AsyncSession, ev: dict) -> IngestResult:
    params = {
        "ts": ev["ts"],
        "device_id": ev["device_id"],
        "rfid_uid": ev["rfid_uid"],
        "rfid_id": ev.get("rfid_id") or ev["rfid_uid"],
        "raw": json.dumps(ev, ensure_ascii=False),
    }
    stored = (await session.execute(_INSERT_RFID, params)).scalar_one_or_none() is not None
    return IngestResult("rfid_scan", stored, stored)


HANDLERS = {
    "sensor_state": handle_sensor_state,
    "heartbeat": handle_heartbeat,
    "rfid_scan": handle_rfid_scan,
}


async def ingest_event(session: AsyncSession, ev: dict) -> IngestResult:
    """W1: 落库即结束。W3 起在这里把事件投递给 Policy 引擎消费队列。"""
    return await HANDLERS[ev["kind"]](session, ev)
