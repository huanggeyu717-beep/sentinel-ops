"""状态查询 —— 迁移自 legacy status-api Lambda。

在线判定语义与原 Lambda 一致 (age <= HEARTBEAT_TIMEOUT_SECONDS), 实现下沉到 DeviceService 并补测试。
读接口对四种角色全部开放, 但必须登录 (SPEC-004 决策 6)。
"""
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..services import device_service
from ..services.auth_service import PERM_READ
from .auth import require_permission

router = APIRouter(
    prefix="/status", tags=["status"], dependencies=[Depends(require_permission(PERM_READ))]
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/sensors")
async def sensors(session: SessionDep) -> dict[str, Any]:
    return {"ok": True, "sensors": await device_service.sensor_status(session)}


@router.get("/devices")
async def devices(session: SessionDep) -> dict[str, Any]:
    """online = 距最后一次心跳 <= SENTINEL_HEARTBEAT_TIMEOUT_SECONDS(默认 60s)。"""
    return {"ok": True, "devices": await device_service.device_status(session)}


@router.get("/readings")
async def readings(
    session: SessionDep,
    sensor_id: Annotated[int | None, Query(description="留空表示全部传感器")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> dict[str, Any]:
    return {
        "ok": True,
        "readings": await device_service.recent_readings(session, sensor_id, limit),
    }


@router.get("/summary")
async def summary(session: SessionDep) -> dict[str, Any]:
    return {"ok": True, **await device_service.counts(session)}
