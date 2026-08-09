"""Agent 任务接口 —— SPEC-002 第一节的四个端点 + 第三段落地的 SSE。

路由层三件事的分工照 policies.py 的先例:
1. 权限门: 提交与回答用**已有的 policies:draft 档** (operator+, SPEC-002 第五节:
   不新增权限点); 读任务与 Trace 是 viewer+ —— 审批人要在 Studio 里看回放报告,
   与策略读接口同档。
2. service 异常 -> 状态码, 集中在 _service_errors: 任务不存在 404、非发起人
   回答 403、流转冲突 (并发回答撞车) 409、后台并发到上界 429。
   **去重撞索引不是 4xx**: 用户明明什么都没等到, 报"重复提交"会让人一头雾水
   (SPEC-002 第二节) —— 返回 200 与正在跑的那条任务, running 但心跳已停的
   标 suspected_interrupted, 界面上写"疑似中断, 稍后可重试"。
3. 显式 pydantic 响应模型 (SSE 除外, 它是事件流)。

SSE 的两条铁律 (本段易错点):
- **数据从数据库尾随读, 不建内存事件总线**: Trace 的事实源只有
  agent_steps + agent_clarifications 两张表 (SPEC-002 第十节), 内存总线会让
  "事实源只有一个"作废, 且任务与 SSE 连接不在同一进程时 (W6 多实例) 直接失效。
  SPEC-005 "Agent 用 SSE"讲的是前端怎么拿数据, 不约束服务端内部实现。
- **`seq` 是每个任务自己的编号**, id: 字段发它; 浏览器重连自动带 Last-Event-ID,
  从那个 seq **之后**接着推 (验收 18: 不重复不遗漏)。

POST 与 reply 不用 get_session 依赖而是自开事务: 任务行必须**先提交**再 spawn,
后台协程的 claim 在新会话里, 看不见未提交的行 —— 用请求级事务会让 spawn 出去的
协程认领扑空, 任务挂到租约清扫才收尸。
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from ..config import settings
from ..db import session_factory
from ..services import agent_prompts, agent_runtime, agent_service
from ..services.auth_service import PERM_POLICY_DRAFT, PERM_READ, AuthUser
from ..services.llm_client import LLMClient, build_llm_client
from .auth import require_permission

router = APIRouter(tags=["agent"])

OperatorDep = Annotated[AuthUser, Depends(require_permission(PERM_POLICY_DRAFT))]
_read = Depends(require_permission(PERM_READ))

# SSE 尾随查询的间隔。不进 config: 它只影响推送延迟的手感, 不影响正确性 ——
# 事实源在数据库里, 慢一拍也不丢 (断线重连按 seq 续传同理)。
_SSE_POLL_SECONDS = 0.3

_TERMINAL_STATUSES = frozenset({"completed", "failed", "dead_letter"})


def get_llm_client() -> LLMClient:
    """每个请求组装一次模型客户端 (record/replay/off 由 config 决定)。

    独立成依赖是为了测试注入: HTTP 层的测试用打桩客户端 (可精确控制耗时),
    不走 cassette —— 路由层要验的是立刻返回/权限/状态码/SSE 续传,
    与模型输出无关 (本段易错点七)。
    """
    return build_llm_client(prompt_version=agent_prompts.PROMPT_VERSION)


LLMDep = Annotated[LLMClient, Depends(get_llm_client)]


# ===== 请求体与响应体 =====


class CreateTaskPayload(BaseModel):
    text: str = Field(min_length=1, max_length=2000, description="一句人话")
    target_policy_id: int | None = Field(
        default=None, description="改哪条已有策略; 新建时留空"
    )


class TaskCreated(BaseModel):
    ok: bool = True
    task_id: int
    # created=False: 撞上了还没走完的同一句话, 返回那一条 (不是错误)
    created: bool
    status: str
    stage: str | None = None
    # running 但打卡已停、还没到判死线: 服务可能刚崩溃过 (SPEC-002 第二节的代价栏),
    # 界面标"疑似中断, 稍后可重试", 不报"重复提交"
    suspected_interrupted: bool = False


class ReplyPayload(BaseModel):
    answer: str = Field(min_length=1, max_length=2000)


class ReplyResult(BaseModel):
    ok: bool = True
    task_id: int
    answered_seq: int
    status: str = "running"


class TimelineItem(BaseModel):
    seq: int
    kind: str  # transition / step / clarification_question / clarification_answer
    label: str
    detail: dict[str, Any] | None = None
    arguments: dict[str, Any] | None = None
    latency_ms: int | None = None
    retry_count: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class TaskInfo(BaseModel):
    id: int
    user_id: int
    status: str
    stage: str
    error_code: str | None
    error_detail: str | None
    input_text: str
    target_policy_id: int | None
    created_at: datetime
    completed_at: datetime | None


class TaskSnapshot(BaseModel):
    ok: bool = True
    task: TaskInfo
    timeline: list[TimelineItem]


class TaskListItem(BaseModel):
    id: int
    status: str
    stage: str
    error_code: str | None
    input_preview: str  # 截断到 80 字, 整段原文在单条接口里
    input_truncated: bool
    requested_by: str  # 发起人显示名 (无显示名退回 email)
    policy_name: str | None  # 编译失败没建出草稿、又不是改已有策略时为 NULL
    created_at: datetime
    completed_at: datetime | None


class TaskList(BaseModel):
    ok: bool = True
    tasks: list[TaskListItem]


# status 过滤的合法取值 = agent_tasks.status 的 CHECK 集合去掉 'rejected'
# (0001 留的死值, 本 SPEC 不用, 见 agent_service 模块注释) —— 用 Literal 让
# 写错的过滤值得到 422, 而不是静默返回空列表
TaskStatusFilter = Literal[
    "running", "clarifying", "awaiting_approval", "completed", "failed", "dead_letter"
]


# ===== service 异常 -> 状态码 =====


@contextmanager
def _service_errors() -> Iterator[None]:
    try:
        yield
    except agent_service.TaskNotFound:
        raise HTTPException(status_code=404, detail="任务不存在") from None
    except agent_service.NotTaskOwner:
        raise HTTPException(
            status_code=403,
            detail="只有发起这条任务的人能回答澄清问题 (草案记的是发起人的意思)",
        ) from None
    except agent_service.TransitionConflict as e:
        raise HTTPException(
            status_code=409,
            detail=f"任务当前状态 {e.current_status} 不能回答 (可能已被回答或已结束)",
        ) from None
    except agent_runtime.CapacityExceeded:
        raise HTTPException(
            status_code=429,
            detail=(
                f"同时在跑的 Agent 任务已达上界 "
                f"({settings().agent_max_concurrent_tasks} 条), 请等一条跑完再提交"
            ),
        ) from None


def _as_json(value: Any) -> Any:
    """jsonb 列经不同驱动可能回 str 或 dict, 统一成 Python 对象。"""
    return json.loads(value) if isinstance(value, str) else value


def _task_info(task: dict[str, Any]) -> TaskInfo:
    task_input = _as_json(task["input"]) or {}
    return TaskInfo(
        id=task["id"],
        user_id=task["user_id"],
        status=task["status"],
        stage=task["stage"],
        error_code=task["error_code"],
        error_detail=task["error_detail"],
        input_text=str(task_input.get("text", "")),
        target_policy_id=task_input.get("target_policy_id"),
        created_at=task["created_at"],
        completed_at=task["completed_at"],
    )


def _timeline_item(row: dict[str, Any]) -> TimelineItem:
    return TimelineItem(
        seq=row["seq"], kind=row["kind"], label=row["label"],
        detail=_as_json(row["detail"]), arguments=_as_json(row["arguments"]),
        latency_ms=row["latency_ms"], retry_count=row["retry_count"],
        input_tokens=row["input_tokens"], output_tokens=row["output_tokens"],
    )


# ===== 路由 =====


@router.post("/agent-tasks")
async def create_task(
    payload: CreateTaskPayload, user: OperatorDep, llm: LLMDep, response: Response
) -> TaskCreated:
    """开一条任务并立刻返回 (验收 2), 状态机在后台协程里跑, 进度走 SSE。"""
    with _service_errors():
        # 预留槽位是同步操作, 先于插行 —— 429 时不留任何痕迹 (行都没插)
        agent_runtime.reserve_task_slot()
    factory = session_factory()
    try:
        async with factory() as session, session.begin():
            created = await agent_service.create_task(
                session,
                user_id=user.id,
                input_text=payload.text,
                target_policy_id=payload.target_policy_id,
                lease_timeout_seconds=settings().agent_lease_timeout_seconds,
            )
    except BaseException:
        agent_runtime.release_task_slot()
        raise
    if not created["created"]:
        # 去重命中: 不开协程, 退回槽位, 返回正在跑的那条 (200, 不是错误)
        agent_runtime.release_task_slot()
        response.status_code = 200
        return TaskCreated(
            task_id=created["task_id"], created=False,
            status=created["status"], stage=created["stage"],
            suspected_interrupted=created["suspected_interrupted"],
        )
    # 事务已提交, 行可见, 才能 spawn (spawn 内部把预留转正)
    agent_runtime.spawn_task(created["task_id"], llm, factory)
    response.status_code = 201
    return TaskCreated(task_id=created["task_id"], created=True, status="running",
                       stage="parsing")


@router.get("/agent-tasks", dependencies=[_read])
async def list_tasks(
    status: TaskStatusFilter | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> TaskList:
    """任务列表 (W4 收尾): 审批人打开 Studio 要看见"有几条等我批", 不能只靠
    别人把 /studio?task=N 链接发过来。权限与单条读取同档 (viewer+)。

    响应有上界: limit 默认 20、硬上限 100 (SPEC-005 决策 4 "必须有上界"这条
    规矩用在响应体上); 输入文本截断, 列表不背整段输入。
    """
    factory = session_factory()
    async with factory() as session:
        rows = await agent_service.list_tasks(session, status=status, limit=limit)
    return TaskList(tasks=[TaskListItem(**row) for row in rows])


@router.get("/agent-tasks/{task_id}", dependencies=[_read])
async def get_task(task_id: int) -> TaskSnapshot:
    """当前快照 + 完整时间线 (SSE 用不了时的兜底, SPEC-002 第一节)。"""
    factory = session_factory()
    async with factory() as session:
        with _service_errors():
            task = await agent_service.get_task(session, task_id)
        timeline = await agent_service.get_timeline(session, task_id)
    return TaskSnapshot(
        task=_task_info(task), timeline=[_timeline_item(r) for r in timeline]
    )


@router.post("/agent-tasks/{task_id}/reply")
async def reply(
    task_id: int, payload: ReplyPayload, user: OperatorDep, llm: LLMDep
) -> ReplyResult:
    """发起人回答澄清问题, 同一条任务从 discovering 继续 (SPEC-002 第三节)。

    恢复的一轮同样是后台协程、同样占并发上界的一个槽位 —— 它接下来要发的
    真实模型调用与新任务没有区别。
    """
    with _service_errors():
        agent_runtime.reserve_task_slot()
    factory = session_factory()
    try:
        async with factory() as session, session.begin():
            with _service_errors():
                answered = await agent_service.answer_clarification(
                    session, task_id, user.id, payload.answer
                )
    except BaseException:
        agent_runtime.release_task_slot()
        raise
    agent_runtime.spawn_task(task_id, llm, factory)
    return ReplyResult(task_id=task_id, answered_seq=answered["answered_seq"])


@router.get("/agent-tasks/{task_id}/events", dependencies=[_read])
async def task_events(task_id: int, request: Request) -> EventSourceResponse:
    """SSE: 按 seq 尾随 Trace 时间线。

    - `id:` 发任务自己的 seq; 浏览器重连自动带 Last-Event-ID, 从那之后接着推;
    - `timeline` 事件是时间线记录 (事实源: 两张表按同一条 seq 排序);
    - `status` 事件在任务状态/阶段变化时发 (不带 id —— 它是快照不是时间线记录,
      占用 seq 会让续传漏掉真正的记录);
    - 任务到终态推完即关; clarifying / awaiting_approval 不关 —— 人回答或
      审批后时间线还会接着长。
    """
    factory = session_factory()
    async with factory() as session:
        with _service_errors():
            await agent_service.get_task(session, task_id)  # 404 在建流之前报

    raw_last_id = request.headers.get("last-event-id", "")
    after = int(raw_last_id) if raw_last_id.isdigit() else 0

    async def stream() -> AsyncIterator[dict[str, str]]:
        cursor = after
        last_status: tuple[str, str] | None = None
        while True:
            async with factory() as session:
                task = await agent_service.get_task(session, task_id)
                rows = await agent_service.get_timeline(session, task_id)
            for row in rows:
                if row["seq"] <= cursor:
                    continue
                cursor = int(row["seq"])
                yield {
                    "id": str(cursor),
                    "event": "timeline",
                    "data": _timeline_item(row).model_dump_json(),
                }
            status = (str(task["status"]), str(task["stage"]))
            if status != last_status:
                last_status = status
                yield {
                    "event": "status",
                    "data": json.dumps({
                        "status": task["status"], "stage": task["stage"],
                        "error_code": task["error_code"],
                        "error_detail": task["error_detail"],
                    }, ensure_ascii=False),
                }
            if task["status"] in _TERMINAL_STATUSES:
                return
            # 客户端断开时必须退出, 否则连接泄漏、测试挂住 (本段易错点四)
            if await request.is_disconnected():
                return
            await asyncio.sleep(_SSE_POLL_SECONDS)

    return EventSourceResponse(stream())
