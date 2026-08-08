"""员工名录 —— GET /employees 的数据源 (W2 遗留项, SPEC-006 第五节顺手补上)。

Dashboard 的派单现在是手填员工 ID 数字框; 前端硬编码员工名单等于重蹈
sensorZoneMap 的覆辙, 所以名录必须从库里出。

刻意不返回 rfid_uid: 那是刷卡凭据, 派单下拉框用不到 —— 不需要的字段不进响应面。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_EMPLOYEES = text("""
    SELECT e.id, e.name, e.role, e.email, e.zone_id, z.name AS zone_name
    FROM employees e
    LEFT JOIN zones z ON z.id = e.zone_id
    ORDER BY e.id
""")


async def list_employees(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(_EMPLOYEES)).mappings().all()
    return [dict(r) for r in rows]
