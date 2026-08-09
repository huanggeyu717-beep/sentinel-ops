"""库存只读查询 —— Agent 的 discovering 阶段要用的三样 (SPEC-002 第九节)。

新建本模块的原因 (先例: employee_service / policy_run_service): W3 只有验证器
内部用的 _ZONES/_SENSORS 查询, 缺三样对外的只读 service ——
区列表**带区名** (模型要把"生鲜区"映射成 zone_id)、传感器列表**带 zone 归属**、
在册角色列表。工具层一律调这里, 不自己拼 SQL (CLAUDE.md 不变量 4)。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_ZONES = text("SELECT id, name FROM zones ORDER BY id")

# never_reported 与 /status 的口径一致 (device_service._SENSOR_STATUS): "装了却
# 从没上报过"的探头必须让模型看得见 —— dev seed 里的占位 sensor 0 (挂在
# UNKNOWN_DEVICE 上, db.py 的种子注释写明是刻意放的) 若以正常姿态进库存清单,
# 模型完全可能给一个从来不说话的设备写监控策略, 而静态验证器会放行 (它确实在
# sensor_ids 里)。
# 与 /status 不同, 这里**只查登记过的传感器 (LEFT JOIN), 不做 FULL JOIN**:
# 这份清单是模型引用 id 的取值域, 必须与静态验证器同源 (sensors 表) ——
# "上报了却没登记"的探头验证器不认, 摆进清单只会引导模型引用一个必被打回的 id,
# 白烧修复配额 (与 list_roles 用 user_roles 是同一个道理)。那个方向由 /status
# 给人看, 不给模型。
_SENSORS = text("""
    SELECT s.id, s.zone_id, z.name AS zone_name, s.active, d.name AS device_name,
           (st.sensor_id IS NULL) AS never_reported
    FROM sensors s
    LEFT JOIN zones z ON z.id = s.zone_id
    LEFT JOIN devices d ON d.id = s.device_id
    LEFT JOIN sensorstate st ON st.sensor_id = s.id
    ORDER BY s.id
""")

# 取值域是 user_roles ("当前有哪些角色下挂着账号"), **与静态验证器的
# roles_present 同一个事实源** (SPEC-001 第五节定死)。不能用 roles 表全集:
# 模型挑了一个没有账号的角色, 草案必然被 E_ROLE_NOT_STAFFED 打回,
# 白白烧掉一次修复配额 —— 而修复次数只有两次 (SPEC-002 第五节)。
_ROLES_PRESENT = text("""
    SELECT DISTINCT r.name FROM roles r JOIN user_roles ur ON ur.role_id = r.id
    ORDER BY r.name
""")


async def list_zones(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(_ZONES)).mappings().all()
    return [dict(r) for r in rows]


async def list_sensors(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(_SENSORS)).mappings().all()
    return [dict(r) for r in rows]


async def list_roles_present(session: AsyncSession) -> list[str]:
    rows = (await session.execute(_ROLES_PRESENT)).scalars().all()
    return [str(r) for r in rows]
