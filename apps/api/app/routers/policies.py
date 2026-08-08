"""策略生命周期接口 —— SPEC-006 第五节的接口表, 逐行对应。

路由层只做三件事, 业务与数据都在下面的层里 (CLAUDE.md 不变量 4/5):

1. **最外层快速失败的权限门**, 与 service 层复用同一个 has_permission 判定
   (经 require_permission 包装), 档位逐字对齐 SPEC-004 决策 6:
   decide / publish / revoke 是 manager+, 写草稿/校验/模拟/提交审批是 operator+,
   读是 viewer+。manager 档在 service 层还有第二道闸 (Agent tools 不经过 HTTP),
   数据库还有审批外键兜底 (ADR-007) —— 这一道不是唯一防线, 与 SPEC-005
   "前端隐藏按钮不是安全措施"是同一条道理, 只是往下挪了一层。
2. **service 异常 -> HTTP 状态码** (集中在 _service_errors, 每类异常一个明确的
   状态码与人话): 找不到 404, 越权/自批 403, 流转冲突/已裁决/重复发布 409,
   Schema 不合法/跨策略冲突 422。兜底的 IntegrityError 统一 409 ——
   ADR-007: 数据库保证它不可能发生, 应用层保证它发生时人话说得清楚。
3. **请求体与响应体都是显式 pydantic 模型** (/docs 可交互调用是 W1 立的验收项)。
   请求体里的 DSL body 直接用 packages/policy_engine 的 Policy 模型 ——
   /docs 与 OpenAPI 暴露的 Schema 因此与 policy_json_schema() 同一个来源;
   W4 把这些接口包成 Agent 工具时 (get_available_actions 之类) 也取同一份,
   不各生成各的。
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from policy_engine import Policy

from ..db import get_session
from ..services import policy_run_service, policy_service
from ..services.auth_service import (
    PERM_APPROVE_POLICY,
    PERM_POLICY_DRAFT,
    PERM_READ,
    AuthUser,
)
from .auth import require_permission

router = APIRouter(tags=["policies"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
# 档位 (SPEC-004 决策 6): 读 viewer+, 草稿类 operator+, 裁决/发布/撤销 manager+
_read = Depends(require_permission(PERM_READ))
_draft = Depends(require_permission(PERM_POLICY_DRAFT))
OperatorDep = Annotated[AuthUser, Depends(require_permission(PERM_POLICY_DRAFT))]
ManagerDep = Annotated[AuthUser, Depends(require_permission(PERM_APPROVE_POLICY))]


# ===== 请求体 =====


class CreatePolicyPayload(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    # body 用引擎的 Policy 模型而不是裸 dict: /docs 里能看到并试出完整 DSL Schema,
    # 且与 policy_json_schema() 同源 (它就是 Policy.model_json_schema())
    body: Policy


class AddVersionPayload(BaseModel):
    body: Policy


class SimulatePayload(BaseModel):
    source: str = Field(
        min_length=1,
        description="场景名 (scenarios/*.yaml) 或仓库内相对路径 (.yaml/.yml/.csv)",
    )


class DecidePayload(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str | None = None


# ===== 响应体 =====


class PolicySummary(BaseModel):
    id: int
    name: str
    created_by: int | None
    created_at: datetime
    publication_id: int | None
    active_version_id: int | None
    active_version: int | None
    latest_version: int | None


class PolicyListResponse(BaseModel):
    ok: bool = True
    policies: list[PolicySummary]


class PolicyInfo(BaseModel):
    id: int
    name: str
    created_by: int | None
    created_at: datetime


class VersionSummary(BaseModel):
    id: int
    version: int
    status: str
    created_at: datetime


class PublicationInfo(BaseModel):
    id: int
    policy_version_id: int
    version: int
    approval_id: int
    published_by: int
    published_at: datetime


class PolicyDetailResponse(BaseModel):
    ok: bool = True
    policy: PolicyInfo
    versions: list[VersionSummary]
    publication: PublicationInfo | None


class VersionDetail(BaseModel):
    ok: bool = True
    id: int
    policy_id: int
    version: int
    name: str
    status: str
    created_at: datetime
    # 读侧回原文 dict: 写入侧已由 Policy 模型强类型把守, 读侧不再重复校验 ——
    # 历史版本永不原地修改, 万一 Schema 演进也不能让旧版本变得读不出来
    body: dict[str, Any]


class DraftCreated(BaseModel):
    ok: bool = True
    policy_id: int
    version_id: int
    version: int


class ValidationIssueOut(BaseModel):
    code: str
    path: str
    message: str
    hint: str | None = None


class ValidateResult(BaseModel):
    ok: bool  # 这里的 ok 是校验结论, 不是 HTTP 惯例字段: 有 issue 时 False 且不转移
    status: str
    issues: list[ValidationIssueOut]


class EffectSubjectOut(BaseModel):
    sensor_id: int | None = None
    zone_id: int | None = None
    device_id: str | None = None
    incident_id: int | None = None


class EffectOut(BaseModel):
    ts_ms: int
    policy_id: int
    policy_version: int
    action_type: str
    subject: EffectSubjectOut
    detail: dict[str, Any]


class SkippedActionOut(BaseModel):
    ts_ms: int
    policy_id: int
    policy_version: int
    action_type: str
    missing: list[str]
    reason: str


class ReplayWarningOut(BaseModel):
    code: str
    message: str


class SimulateReport(BaseModel):
    ok: bool = True
    source: str
    events_count: int
    span_s: float
    tick_seconds: int
    tail_s: int
    effects: list[EffectOut]
    skipped: list[SkippedActionOut]
    by_action_type: dict[str, int]
    by_zone: dict[int, int]
    by_sensor: dict[int, int]
    warnings: list[ReplayWarningOut]
    data_note: str
    side_effects_note: str


class RequestApprovalResult(BaseModel):
    ok: bool = True
    approval_id: int
    policy_version_id: int


class DecideResult(BaseModel):
    ok: bool = True
    approval_id: int
    decision: str
    policy_version_id: int
    version_status: str


class PublishResult(BaseModel):
    ok: bool = True
    publication_id: int
    policy_id: int
    policy_version_id: int
    version: int
    approval_id: int


class RevokeResult(BaseModel):
    ok: bool = True
    policy_id: int
    policy_version_id: int


class PolicyRunOut(BaseModel):
    id: int
    policy_id: int
    policy_version_id: int
    version: int
    fired_at: datetime
    effects: list[dict[str, Any]]


class PolicyRunsResponse(BaseModel):
    ok: bool = True
    runs: list[PolicyRunOut]


# ===== 异常 -> HTTP 状态码 (每类一个明确的映射, 集中一处不散在各路由里) =====


@contextmanager
def _service_errors() -> Iterator[None]:
    try:
        yield
    except policy_service.PolicyNotFound:
        raise HTTPException(status_code=404, detail="策略不存在") from None
    except policy_service.PolicyVersionNotFound:
        raise HTTPException(status_code=404, detail="策略版本不存在") from None
    except policy_service.ApprovalNotFound:
        raise HTTPException(status_code=404, detail="审批记录不存在") from None
    except policy_service.SourceNotFound as e:
        raise HTTPException(
            status_code=404,
            detail=f"模拟数据源不存在或不合法: {e} (场景名或仓库内 .yaml/.yml/.csv 相对路径)",
        ) from None
    except policy_service.InvalidPolicyBody as e:
        raise HTTPException(
            status_code=422,
            detail={"message": "策略 body 未通过 Schema 校验", "issues": e.issues},
        ) from None
    except policy_service.TransitionConflict as e:
        raise HTTPException(
            status_code=409, detail=f"当前状态 {e.current_status} 不允许该流转"
        ) from None
    except policy_service.PermissionDenied:
        # service 层的第二道闸 (正常请求在路由层的 require_permission 就被拦住,
        # 话术刻意与它不同 —— 测试靠话术区分拦截层, 见 test_policies_http)
        raise HTTPException(
            status_code=403, detail="该操作需要 manager 及以上角色"
        ) from None
    except policy_service.SelfApprovalDenied:
        raise HTTPException(
            status_code=403,
            detail="不能审批自己提交的申请, 请交由另一位 manager 裁决 (本次尝试已记入审计)",
        ) from None
    except policy_service.ApprovalAlreadyDecided:
        raise HTTPException(status_code=409, detail="该审批已有结论, 不能重复裁决") from None
    except policy_service.NotApproved as e:
        raise HTTPException(
            status_code=409,
            detail=f"版本尚无通过的审批 (当前状态 {e.current_status}), 不能发布",
        ) from None
    except policy_service.AlreadyPublished:
        raise HTTPException(
            status_code=409, detail="该策略已有生效版本, 先撤销当前发布再发布新版本"
        ) from None
    except policy_service.NothingToRevoke:
        raise HTTPException(status_code=409, detail="没有生效中的发布可撤销") from None
    except policy_service.PublishRejected as e:
        raise HTTPException(
            status_code=422,
            detail={"message": "发布前跨策略检查不通过", "conflicts": e.conflicts},
        ) from None
    except IntegrityError:
        # 兜底: 数据库约束保证不变量不可能被破坏 (ADR-007), 应用层预检没赶上的
        # 并发窗口在这里翻译成人话, 不把 IntegrityError 裸漏给用户
        raise HTTPException(
            status_code=409, detail="操作与数据库约束冲突 (通常是并发请求撞上), 请刷新后重试"
        ) from None


# ===== 路由 (SPEC-006 第五节的表, 自上而下同序) =====


@router.get("/policies", dependencies=[_read])
async def list_policies(session: SessionDep) -> PolicyListResponse:
    rows = await policy_service.list_policies(session)
    return PolicyListResponse(policies=[PolicySummary.model_validate(r) for r in rows])


@router.post("/policies", status_code=201)
async def create_policy(
    payload: CreatePolicyPayload, session: SessionDep, user: OperatorDep
) -> DraftCreated:
    with _service_errors():
        created = await policy_service.create_policy(
            session, payload.name, payload.body.model_dump(mode="json"), user.id
        )
    return DraftCreated(**created)


@router.get("/policies/{policy_id}", dependencies=[_read])
async def get_policy(policy_id: int, session: SessionDep) -> PolicyDetailResponse:
    with _service_errors():
        data = await policy_service.get_policy(session, policy_id)
    return PolicyDetailResponse(
        policy=PolicyInfo.model_validate(data["policy"]),
        versions=[VersionSummary.model_validate(v) for v in data["versions"]],
        publication=(
            PublicationInfo.model_validate(data["publication"])
            if data["publication"] is not None
            else None
        ),
    )


@router.post("/policies/{policy_id}/versions", status_code=201)
async def add_version(
    policy_id: int, payload: AddVersionPayload, session: SessionDep, user: OperatorDep
) -> DraftCreated:
    with _service_errors():
        created = await policy_service.add_version(
            session, policy_id, payload.body.model_dump(mode="json"), user.id
        )
    return DraftCreated(**created)


@router.get("/policy-versions/{version_id}", dependencies=[_read])
async def get_version(version_id: int, session: SessionDep) -> VersionDetail:
    with _service_errors():
        row = await policy_service.get_version(session, version_id)
    return VersionDetail.model_validate(row)


@router.post("/policy-versions/{version_id}/validate", dependencies=[_draft])
async def validate_version(version_id: int, session: SessionDep) -> ValidateResult:
    with _service_errors():
        result = await policy_service.validate_version(session, version_id)
    return ValidateResult.model_validate(result)


@router.post("/policy-versions/{version_id}/simulate", dependencies=[_draft])
async def simulate_version(
    version_id: int, payload: SimulatePayload, session: SessionDep
) -> SimulateReport:
    with _service_errors():
        report = await policy_service.simulate_version(session, version_id, payload.source)
    return SimulateReport.model_validate(report)


@router.post("/policy-versions/{version_id}/request-approval")
async def request_approval(
    version_id: int, session: SessionDep, user: OperatorDep
) -> RequestApprovalResult:
    with _service_errors():
        req = await policy_service.request_approval(session, version_id, user.id)
    return RequestApprovalResult(**req)


@router.post("/approvals/{approval_id}/decide")
async def decide_approval(
    approval_id: int, payload: DecidePayload, session: SessionDep, user: ManagerDep
) -> DecideResult:
    with _service_errors():
        decided = await policy_service.decide_approval(
            session, approval_id, user, payload.decision, payload.note
        )
    return DecideResult(**decided)


@router.post("/policy-versions/{version_id}/publish")
async def publish_version(
    version_id: int, session: SessionDep, user: ManagerDep
) -> PublishResult:
    with _service_errors():
        pub = await policy_service.publish_version(session, version_id, user)
    return PublishResult(**pub)


@router.post("/policies/{policy_id}/revoke")
async def revoke_publication(
    policy_id: int, session: SessionDep, user: ManagerDep
) -> RevokeResult:
    with _service_errors():
        revoked = await policy_service.revoke_publication(session, policy_id, user)
    return RevokeResult(**revoked)


@router.get("/policy-runs", dependencies=[_read])
async def list_policy_runs(
    session: SessionDep,
    policy_id: Annotated[int | None, Query(description="留空表示全部策略")] = None,
    since: Annotated[datetime | None, Query(description="fired_at >= since")] = None,
    until: Annotated[datetime | None, Query(description="fired_at < until")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> PolicyRunsResponse:
    rows = await policy_run_service.list_runs(session, policy_id, since, until, limit)
    return PolicyRunsResponse(runs=[PolicyRunOut.model_validate(r) for r in rows])
