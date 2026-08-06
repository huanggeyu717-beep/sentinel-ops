"""事故生命周期接口 —— SPEC-003, 鉴权与 RBAC 由 SPEC-004 接入。

- 列表/详情 + assign / acknowledge / resolve 三个手工流转;
- RFID 刷卡接单不在这里: 复用 POST /ingest (kind=rfid_scan), 真机就是把刷卡当遥测发的;
- 非法流转返回 409 (service 层条件更新保证并发下只有一个成功);
- actor 由路由层从当前登录用户拼成 `user:{id}` (SPEC-004 决策 8), service 签名不变;
- 权限分层 (决策 6): 读需 read, 流转需 incidents:transition;
  `allow_cross_zone=true` 是 manager/admin 的能力, 无权限传它是 **403**,
  与"没带 flag 的跨区派单"这种业务不允许的 **422** 是两回事, 不混用状态码。
"""
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..services import auth_service, incident_service
from ..services.auth_service import (
    PERM_CROSS_ZONE_ASSIGN,
    PERM_INCIDENT_TRANSITION,
    PERM_READ,
    AuthUser,
)
from ..services.incident_service import (
    CrossZoneAssignDenied,
    CrossZonePermissionRequired,
    IncidentNotFound,
    TransitionConflict,
    UnknownEmployee,
)
from .auth import require_permission

router = APIRouter(prefix="/incidents", tags=["incidents"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
OperatorDep = Annotated[AuthUser, Depends(require_permission(PERM_INCIDENT_TRANSITION))]

IncidentStatus = Literal["open", "assigned", "acknowledged", "resolved"]


class AssignPayload(BaseModel):
    employee_id: int
    # 决策 7: 跨区派单默认拒绝, 显式放行才通过, 且审计记 cross_zone
    allow_cross_zone: bool = False


class ResolvePayload(BaseModel):
    note: str | None = None


@router.get("", dependencies=[Depends(require_permission(PERM_READ))])
async def list_incidents(
    session: SessionDep,
    status: Annotated[IncidentStatus | None, Query(description="留空表示全部状态")] = None,
    zone_id: Annotated[int | None, Query(description="留空表示全部区域")] = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "incidents": await incident_service.list_incidents(session, status, zone_id),
    }


@router.get("/{incident_id}", dependencies=[Depends(require_permission(PERM_READ))])
async def get_incident(incident_id: int, session: SessionDep) -> dict[str, Any]:
    try:
        incident = await incident_service.get_incident(session, incident_id)
    except IncidentNotFound:
        raise HTTPException(status_code=404, detail="事故不存在") from None
    events = await incident_service.get_timeline(session, incident_id)
    return {"ok": True, "incident": incident, "events": events}


@router.post("/{incident_id}/assign")
async def assign(
    incident_id: int, payload: AssignPayload, session: SessionDep, user: OperatorDep
) -> dict[str, Any]:
    # 跨区的两层判定都在 service 里按次序做: 先"有没有资格"(403), 再"确认了没有"(422)。
    # 这里只负责把操作者的资格传下去, 不自己抢跑 —— 因为"这次到底跨不跨区"要查了
    # 员工与事故的 zone 才知道, 那是 service 的活。
    try:
        incident = await incident_service.assign(
            session, incident_id, payload.employee_id, f"user:{user.id}",
            allow_cross_zone=payload.allow_cross_zone,
            caller_may_cross_zone=auth_service.has_permission(user, PERM_CROSS_ZONE_ASSIGN),
        )
    except IncidentNotFound:
        raise HTTPException(status_code=404, detail="事故不存在") from None
    except UnknownEmployee:
        raise HTTPException(status_code=422, detail="员工不存在") from None
    except CrossZonePermissionRequired as e:
        raise HTTPException(
            status_code=403,
            detail=(
                f"跨区派单需要 manager 或 admin 角色 (员工 zone={e.employee_zone_id}, "
                f"事故 zone={e.incident_zone_id})"
            ),
        ) from None
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
    incident_id: int, session: SessionDep, user: OperatorDep
) -> dict[str, Any]:
    try:
        incident = await incident_service.acknowledge(
            session, incident_id, f"user:{user.id}", employee_id=user.employee_id
        )
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
    user: OperatorDep,
    payload: ResolvePayload | None = None,
) -> dict[str, Any]:
    try:
        incident = await incident_service.resolve(
            session, incident_id, f"user:{user.id}", payload.note if payload else None
        )
    except IncidentNotFound:
        raise HTTPException(status_code=404, detail="事故不存在") from None
    except TransitionConflict as e:
        raise HTTPException(
            status_code=409, detail=f"当前状态 {e.current_status} 不允许解决"
        ) from None
    return {"ok": True, "incident": incident}
