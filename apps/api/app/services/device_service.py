"""设备与传感器状态查询 —— 迁移自 legacy status-api Lambda。

保留原 Lambda 的在线判定语义 (HEARTBEAT_TIMEOUT_SECONDS=60), 但做了三点工程化:
- 判定逻辑从 Lambda 内联代码下沉到 service, 可被 Agent tool / MCP server 复用;
- 超时阈值改为配置项而非硬编码常量;
- 返回 age_seconds, 前端不必再自己算时间差(原前端 app.js 里算过一遍, 与后端口径不一致)。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings

_SENSOR_STATUS = text("""
    SELECT s.sensor_id,
           s.state,
           s.wet,
           s.last_value,
           s.updated_at,
           EXTRACT(EPOCH FROM (now() - s.updated_at))::int AS age_seconds,
           cfg.zone_id,
           z.name AS zone_name,
           cfg.threshold_value,
           cfg.active
    FROM sensorstate s
    LEFT JOIN sensors cfg ON cfg.id = s.sensor_id
    LEFT JOIN zones z ON z.id = cfg.zone_id
    ORDER BY s.sensor_id
""")

_DEVICE_STATUS = text("""
    SELECT h.device_id,
           h.last_seen_at,
           h.uptime_ms,
           EXTRACT(EPOCH FROM (now() - h.last_seen_at))::int AS age_seconds
    FROM device_heartbeats h
    ORDER BY h.device_id
""")

_RECENT_READINGS = text("""
    SELECT id, received_at, device_id, sensor_id, value, wet
    FROM waterlevel_readings
    WHERE (CAST(:sensor_id AS int) IS NULL OR sensor_id = CAST(:sensor_id AS int))
    ORDER BY received_ts DESC
    LIMIT :limit
""")


async def sensor_status(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(_SENSOR_STATUS)).mappings().all()
    return [dict(r) for r in rows]


async def device_status(session: AsyncSession) -> list[dict]:
    timeout = settings().heartbeat_timeout_seconds
    rows = (await session.execute(_DEVICE_STATUS)).mappings().all()
    return [{**dict(r), "online": r["age_seconds"] <= timeout} for r in rows]


async def recent_readings(
    session: AsyncSession, sensor_id: int | None = None, limit: int = 100
) -> list[dict]:
    rows = (
        await session.execute(
            _RECENT_READINGS, {"sensor_id": sensor_id, "limit": min(limit, 1000)}
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def counts(session: AsyncSession) -> dict:
    """演示与冒烟测试用的一行式统计。"""
    row = (
        await session.execute(
            text("""
                SELECT (SELECT count(*) FROM waterlevel_readings) AS readings,
                       (SELECT count(*) FROM rfid_scans)          AS rfid_scans,
                       (SELECT count(*) FROM device_heartbeats)   AS devices,
                       (SELECT count(*) FROM sensorstate)         AS sensors
            """)
        )
    ).mappings().one()
    return dict(row)
