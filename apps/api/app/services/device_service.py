"""设备与传感器状态查询 —— 迁移自 legacy status-api Lambda。

保留原 Lambda 的在线判定语义 (HEARTBEAT_TIMEOUT_SECONDS=60), 但做了三点工程化:
- 判定逻辑从 Lambda 内联代码下沉到 service, 可被 Agent tool / MCP server 复用;
- 超时阈值改为配置项而非硬编码常量;
- 返回 age_seconds, 前端不必再自己算时间差(原前端 app.js 里算过一遍, 与后端口径不一致)。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings

# FULL JOIN 而不是从任一侧单向 LEFT JOIN: 两个方向的"只有一边有"都必须看得见。
#
# - **配置里有、从没上报过** (装了却一次没说过话): 单从 sensorstate 出发查, 这种探头
#   在接口里根本不存在, 平面图上凭空少一个点 —— 而"装了不出声"本身就是要派人去看的
#   状态, 恰恰不能让它消失。全新库 + 只跑演练时尤其明显: 剧本只用到 1/2/4 号,
#   3/5 号就永远画不出来, SPEC-005"全新库启动后带得出坐标"那条验收也就是假的。
# - **上报了、配置里没有** (现场插了块没登记的板子): 单从 sensors 出发查又会漏掉它。
#   这种更要看得见, 不然就是一个谁都不知道的盲点。
#
# never_reported 让前端把这两类分开画: "从没上报"与"上报过但超时失联"是两种故障,
# 对应两种处理动作 (去装 / 去修), 不能用同一个灰点糊过去。
_SENSOR_STATUS = text("""
    SELECT COALESCE(cfg.id, s.sensor_id) AS sensor_id,
           s.state,
           s.wet,
           s.last_value,
           s.updated_at,
           EXTRACT(EPOCH FROM (now() - s.updated_at))::int AS age_seconds,
           (s.sensor_id IS NULL) AS never_reported,
           cfg.zone_id,
           z.name AS zone_name,
           cfg.threshold_value,
           cfg.active,
           cfg.pos_x::float8 AS pos_x,
           cfg.pos_y::float8 AS pos_y
    FROM sensors cfg
    FULL JOIN sensorstate s ON s.sensor_id = cfg.id
    LEFT JOIN zones z ON z.id = cfg.zone_id
    ORDER BY 1
""")

# 心跳表的主键是设备上报的名字字符串, 坐标在 devices 配置表里, 按 name 关联。
# pos 列是 numeric, 直接返回会被序列化成字符串, 显式转 float8 让 JSON 里是数字。
# FULL JOIN 的理由同上 —— 种子里的 UNKNOWN_DEVICE 从不发心跳, 单向 join 会让
# "未定位"那条前端分支在演示数据里根本走不到。
_DEVICE_STATUS = text("""
    SELECT COALESCE(h.device_id, d.name) AS device_id,
           h.last_seen_at,
           h.uptime_ms,
           EXTRACT(EPOCH FROM (now() - h.last_seen_at))::int AS age_seconds,
           (h.device_id IS NULL) AS never_reported,
           d.pos_x::float8 AS pos_x,
           d.pos_y::float8 AS pos_y
    FROM devices d
    FULL JOIN device_heartbeats h ON h.device_id = d.name
    ORDER BY 1
""")

_RECENT_READINGS = text("""
    SELECT id, received_at, device_id, sensor_id, value, wet
    FROM waterlevel_readings
    WHERE (CAST(:sensor_id AS int) IS NULL OR sensor_id = CAST(:sensor_id AS int))
    ORDER BY received_ts DESC
    LIMIT :limit
""")


async def sensor_status(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(_SENSOR_STATUS)).mappings().all()
    return [dict(r) for r in rows]


async def device_status(session: AsyncSession) -> list[dict[str, Any]]:
    timeout = settings().heartbeat_timeout_seconds
    rows = (await session.execute(_DEVICE_STATUS)).mappings().all()
    # 从没上报过的一律 online=False。不能写成 age_seconds <= timeout: age 是 NULL,
    # 比较结果是 None 而不是 False, 会把一个三态的东西悄悄塞进一个布尔字段。
    return [
        {**dict(r), "online": r["age_seconds"] is not None and r["age_seconds"] <= timeout}
        for r in rows
    ]


async def recent_readings(
    session: AsyncSession, sensor_id: int | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            _RECENT_READINGS, {"sensor_id": sensor_id, "limit": min(limit, 1000)}
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def counts(session: AsyncSession) -> dict[str, Any]:
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
