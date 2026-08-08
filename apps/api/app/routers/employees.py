"""员工名录接口 —— W2 遗留项, SPEC-006 第五节顺手补上 (派单下拉框要用)。

权限与 /incidents 同档 (viewer+); 业务在 services/employee_service。
响应刻意不含 rfid_uid: 那是刷卡凭据, 名录用不到的字段不进响应面。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..services import employee_service
from ..services.auth_service import PERM_READ
from .auth import require_permission

router = APIRouter(prefix="/employees", tags=["employees"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class Employee(BaseModel):
    id: int
    name: str
    role: str | None
    email: str | None
    zone_id: int | None
    zone_name: str | None


class EmployeeListResponse(BaseModel):
    ok: bool = True
    employees: list[Employee]


@router.get("", dependencies=[Depends(require_permission(PERM_READ))])
async def list_employees(session: SessionDep) -> EmployeeListResponse:
    rows = await employee_service.list_employees(session)
    return EmployeeListResponse(employees=[Employee.model_validate(r) for r in rows])
