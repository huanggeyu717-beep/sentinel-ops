"""事故生命周期接口 —— SPEC-003。

- 列表/详情 + assign / acknowledge / resolve 三个手工流转;
- RFID 刷卡接单不在这里: 复用 POST /ingest (kind=rfid_scan), 真机就是把刷卡当遥测发的;
- 非法流转返回 409 (service 层条件更新保证并发下只有一个成功);
- actor 暂取 X-Actor 请求头, 缺省 "system" —— SPEC-004 JWT 落地后改从 token 取, 签名不变。
"""
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..services import incident_service
from ..services.incident_service import (
    CrossZoneAssignDenied,
    IncidentNotFound,
    TransitionConflict,
    UnknownEmployee,
)

router = APIRouter(prefix="/incidents", tags=["incidents"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ActorDep = Annotated[str, Header(alias="X-Actor", description="操作者标识, 如 employee:3")]

IncidentStatus = Literal["open", "assigned", "acknowledged", "resolved"]


class AssignPayload(BaseModel):
    employee_id: int
    # 决策 7: 跨区派单默认拒绝, 显式放行才通过, 且审计记 cross_zone
    allow_cross_zone: bool = False


class ResolvePayload(BaseModel):
    note: str | None = None


@router.get("")
async def list_incidents(
    session: SessionDep,
    status: Annotated[IncidentStatus | None, Query(description="留空表示全部状态")] = None,
    zone_id: Annotated[int | None, Query(description="留空表示全部区域")] = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "incidents": await incident_service.list_incidents(session, status, zone_id),
    }


@router.get("/{incident_id}")
async def get_incident(incident_id: int, session: SessionDep) -> dict[str, Any]:
    try:
        incident = await incident_service.get_incident(session, incident_id)
    except IncidentNotFound:
        raise HTTPException(status_code=404, detail="事故不存在") from None
    events = await incident_service.get_timeline(session, incident_id)
    return {"ok": True, "incident": incident, "events": events}


@router.post("/{incident_id}/assign")
async def assign(
    incident_id: int, payload: AssignPayload, session: SessionDep, actor: ActorDep = "system"
) -> dict[str, Any]:
    try:
        incident = await incident_service.assign(
            session, incident_id, payload.employee_id, actor,
            allow_cross_zone=payload.allow_cross_zone,
        )
    except IncidentNotFound:
        raise HTTPException(status_code=404, detail="事故不存在") from None
    except UnknownEmployee:
        raise HTTPException(status_code=422, detail="员工不存在") from None
    except CrossZoneAssignDenied as e:
        raise HTTPException(
            status_code=422,
            detail=(
                f"员工不在事故所在区域 (员工 zone={e.employee_zone_id}, "
                f"事故 zone={e.incident_zone_id}), 跨区派单需显式 allow_cross_zone=true"
            ),
        ) from None
    except TransitionConflict as e:
        raise HTTPException(
            status_code=409, detail=f"当前状态 {e.current_status} 不允许分配"
        ) from None
    return {"ok": True, "incident": incident}


@router.post("/{incident_id}/acknowledge")
async def acknowledge(
    incident_id: int, session: SessionDep, actor: ActorDep = "system"
) -> dict[str, Any]:
    try:
        incident = await incident_service.acknowledge(session, incident_id, actor)
    except IncidentNotFound:
        raise HTTPException(status_code=404, detail="事故不存在") from None
    except TransitionConflict as e:
        raise HTTPException(
            status_code=409, detail=f"当前状态 {e.current_status} 不允许接单"
        ) from None
    return {"ok": True, "incident": incident}


@router.post("/{incident_id}/resolve")
async def resolve(
    incident_id: int,
    session: SessionDep,
    payload: ResolvePayload | None = None,
    actor: ActorDep = "system",
) -> dict[str, Any]:
    try:
        incident = await incident_service.resolve(
            session, incident_id, actor, payload.note if payload else None
        )
    except IncidentNotFound:
        raise HTTPException(status_code=404, detail="事故不存在") from None
    except TransitionConflict as e:
        raise HTTPException(
            status_code=409, detail=f"当前状态 {e.current_status} 不允许解决"
        ) from None
    return {"ok": True, "incident": incident}
