"""事故报告接口 —— SPEC-008 第八节的五个端点。

分工照 agent_tasks.py 的先例:
1. 权限门: 生成与弃稿 `reports:draft`、定稿 `reports:finalize` (都是 operator+,
   不做行级归属判断, SPEC 第八节写了理由); 两个 GET 是 viewer+。
2. service 异常 -> 状态码集中翻译; 三种 429 与 agent_tasks 同一套
   (X-Error-Code 头机器读, detail 人话)。
3. **去重命中返回 200 + 既有 task_id, 不报 4xx** (SPEC 第八节): 判据是
   "该事故有没有未走完的报告任务" (跨用户, service 自查, 不能只靠索引)。
4. 生成走后台协程 + 既有 SSE (/agent-tasks/{id}/events), 本文件不再开一条流。

POST 不用请求级事务而是自开事务, 理由同 agent_tasks: 任务行必须先提交再 spawn。
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from ..config import settings
from ..db import session_factory
from ..services import agent_prompts, agent_runtime, budget_service, report_task_service
from ..services.auth_service import (
    PERM_READ,
    PERM_REPORT_DRAFT,
    PERM_REPORT_FINALIZE,
    AuthUser,
)
from ..services.incident_service import IncidentNotFound
from ..services.llm_client import LLMClient, build_llm_client
from .auth import require_permission

router = APIRouter(tags=["reports"])

DrafterDep = Annotated[AuthUser, Depends(require_permission(PERM_REPORT_DRAFT))]
FinalizerDep = Annotated[AuthUser, Depends(require_permission(PERM_REPORT_FINALIZE))]
_read = Depends(require_permission(PERM_READ))


def get_llm_client() -> LLMClient:
    """报告任务的模型客户端 (独立成依赖是为了测试注入, 同 agent_tasks)。
    prompt 版本用报告自己的 r 号 —— 录制回放的键不与策略 v 号串味。"""
    return build_llm_client(prompt_version=agent_prompts.REPORT_PROMPT_VERSION)


LLMDep = Annotated[LLMClient, Depends(get_llm_client)]


# ===== 响应体 =====


class ReportTaskCreated(BaseModel):
    ok: bool = True
    task_id: int
    created: bool  # False = 已有未走完的报告任务 (任何人开的), 带回那一条
    status: str
    stage: str | None = None
    suspected_interrupted: bool = False


class ReportBody(BaseModel):
    summary: str
    handling: str
    impact: str
    notable: str
    suggestion: str


class ReportInfo(BaseModel):
    id: int
    incident_id: int
    task_id: int
    status: str  # draft / final / discarded
    body: ReportBody                      # 占位符原文
    rendered: ReportBody | None           # 渲染后的正文; 草稿还没过校验时为 None
    fact_pack: list[dict[str, Any]]       # 生成那一刻的事实包快照
    # 两个倾向计数, 分开报、不加总 (SPEC-008 第三节: 不报"幻觉率")
    bare_fact_attempts: int
    dangling_ref_attempts: int
    created_by: int
    created_at: datetime
    updated_at: datetime
    finalized_by: int | None
    finalized_at: datetime | None


class ReportSnapshot(BaseModel):
    ok: bool = True
    report: ReportInfo


class ReportActionResult(BaseModel):
    ok: bool = True
    report_id: int
    status: str


# ===== service 异常 -> 状态码 =====


@contextmanager
def _service_errors() -> Iterator[None]:
    try:
        yield
    except IncidentNotFound:
        raise HTTPException(status_code=404, detail="事故不存在") from None
    except report_task_service.ReportNotFound:
        raise HTTPException(status_code=404, detail="报告不存在") from None
    except report_task_service.IncidentNotResolved as e:
        raise HTTPException(
            status_code=422,
            detail=f"事故当前状态 {e.status}, 只有已解决 (resolved) 的事故能生成报告 "
                   f"—— 报告是那一刻的定影, 进行中的事故请看 Dashboard",
        ) from None
    except report_task_service.ReportStateConflict as e:
        raise HTTPException(status_code=409, detail=e.detail) from None
    except agent_runtime.CapacityExceeded:
        raise HTTPException(
            status_code=429,
            detail=f"同时在跑的 Agent 任务已达上界 "
                   f"({settings().agent_max_concurrent_tasks} 条), 请等一条跑完再提交",
            headers={"X-Error-Code": "capacity_exceeded"},
        ) from None
    except budget_service.UserQuotaExhausted:
        raise HTTPException(
            status_code=429,
            detail=f"这个账号今天的任务数已用完 "
                   f"(每个账号每天 {settings().agent_user_daily_tasks} 条), 明天再来",
            headers={"X-Error-Code": "user_quota_exhausted"},
        ) from None
    except budget_service.DailyBudgetExhausted:
        raise HTTPException(
            status_code=429,
            detail="今天的模型体验额度已用完 (全站共享的日预算), 明天再来试",
            headers={"X-Error-Code": "daily_budget_exhausted"},
        ) from None


def _as_json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _report_info(row: dict[str, Any]) -> ReportInfo:
    return ReportInfo(
        id=row["id"], incident_id=row["incident_id"], task_id=row["task_id"],
        status=row["status"],
        body=ReportBody(**row["body"]),
        rendered=ReportBody(**row["rendered"]) if row["rendered"] else None,
        fact_pack=row["fact_pack"],
        bare_fact_attempts=row["bare_fact_attempts"],
        dangling_ref_attempts=row["dangling_ref_attempts"],
        created_by=row["created_by"], created_at=row["created_at"],
        updated_at=row["updated_at"],
        finalized_by=row["finalized_by"], finalized_at=row["finalized_at"],
    )


# ===== 路由 =====


@router.post("/incidents/{incident_id}/report")
async def create_report(
    incident_id: int, user: DrafterDep, llm: LLMDep, response: Response
) -> ReportTaskCreated:
    """点一下生成报告: 建任务 202 + task_id, 状态机在后台跑, 进度走既有的
    /agent-tasks/{id}/events。事故非 resolved 422; 已有未走完的报告任务 200。"""
    with _service_errors():
        agent_runtime.reserve_task_slot()
    factory = session_factory()
    try:
        async with factory() as session, session.begin():
            with _service_errors():
                created = await report_task_service.create_report_task(
                    session,
                    user_id=user.id,
                    incident_id=incident_id,
                    lease_timeout_seconds=settings().agent_lease_timeout_seconds,
                )
            if created["created"]:
                # 预扣与任务行同一个事务 (SPEC-009); 去重命中不扣
                with _service_errors():
                    await budget_service.reserve_task_budget(session, user_id=user.id)
    except BaseException:
        agent_runtime.release_task_slot()
        raise
    if not created["created"]:
        agent_runtime.release_task_slot()
        response.status_code = 200
        return ReportTaskCreated(
            task_id=created["task_id"], created=False,
            status=created["status"], stage=created["stage"],
            suspected_interrupted=created["suspected_interrupted"],
        )
    round_task = agent_runtime.spawn_task(created["task_id"], llm, factory)
    budget_service.refund_when_done(round_task, created["task_id"], factory)
    response.status_code = 202
    return ReportTaskCreated(task_id=created["task_id"], created=True,
                             status="running", stage="collecting")


@router.get("/reports/{report_id}", dependencies=[_read])
async def get_report(report_id: int) -> ReportSnapshot:
    """报告 + 渲染后的正文 + 事实包快照 (渲染只读快照, 不重算)。"""
    factory = session_factory()
    async with factory() as session:
        with _service_errors():
            row = await report_task_service.get_report(session, report_id)
    return ReportSnapshot(report=_report_info(row))


@router.get("/incidents/{incident_id}/report", dependencies=[_read])
async def get_incident_report(incident_id: int) -> ReportSnapshot:
    """取该事故当前那一份 (非 discarded 的最多一份); 没有则 404。"""
    factory = session_factory()
    async with factory() as session:
        with _service_errors():
            row = await report_task_service.get_incident_report(session, incident_id)
    return ReportSnapshot(report=_report_info(row))


@router.post("/reports/{report_id}/finalize")
async def finalize_report(report_id: int, user: FinalizerDep) -> ReportActionResult:
    """人定稿: 报告 draft -> final, 任务 completed (SPEC-008 第四节)。"""
    factory = session_factory()
    async with factory() as session, session.begin():
        with _service_errors():
            result = await report_task_service.finalize_report(
                session, report_id=report_id, user_id=user.id
            )
    return ReportActionResult(**result)


@router.post("/reports/{report_id}/discard")
async def discard_report(report_id: int, user: DrafterDep) -> ReportActionResult:
    """人弃稿: draft 与 final 都允许 (弃掉才能重开第二份), 一律进 audit_log;
    弃的是等人过目的草稿时任务落 completed —— **人退回不是 failed**。"""
    factory = session_factory()
    async with factory() as session, session.begin():
        with _service_errors():
            result = await report_task_service.discard_report(
                session, report_id=report_id, user_id=user.id
            )
    return ReportActionResult(**result)
