"""Sentinel API 入口。

W1 目标: /health + /ingest 落库 + /status 查询。
路由按域拆分, 业务逻辑一律下沉 services/, 引擎逻辑在 packages/policy_engine。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import apply_dev_seed, run_migrations
from .routers import agent_tasks, auth, drills, employees, incidents, ingest, policies, status
from .services import agent_runtime, auth_service, policy_runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("sentinel")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时把数据库带到最新版本, 让 `docker compose up` 真正是一条命令。"""
    # 非开发环境带着默认 JWT 密钥, 宁可起不来 (SPEC-004 决策 2)
    auth_service.validate_startup_security(settings())
    revision = await run_migrations()
    log.info("database at alembic revision: %s", revision)
    if settings().apply_dev_seed:
        await apply_dev_seed()
        log.info("dev seed applied")
    # W3: 策略引擎 tick 后台任务 (SPEC-006 第四节)。多实例会重复 tick 的边界
    # 与关停语义见 policy_runtime.tick_loop 的 docstring。
    tick_task = asyncio.create_task(policy_runtime.tick_loop(), name="engine-tick")
    # W4: Agent 打卡与清扫 (SPEC-002 第二节)。**一个任务两件事**, 不另开;
    # 边界与关停语义见 agent_runtime.maintenance_loop 的 docstring。
    agent_task = asyncio.create_task(
        agent_runtime.maintenance_loop(), name="agent-maintenance"
    )
    try:
        yield
    finally:
        # 干净取消: 不 cancel 会让测试与 uvicorn 关停时挂在这些任务上。
        # W4 第三段: 连同 HTTP 层 spawn 的 Agent 后台任务一起取消 —— 被取消的
        # 任务轮事务回滚、行停在 running, 下次启动后由租约清扫收成 dead_letter
        # (SPEC-002 第一节的重启边界, 不在关停时抢救)。
        for task in (tick_task, agent_task, *agent_runtime.background_tasks()):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Sentinel API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,  # 会话走 httpOnly cookie, 跨端口请求需要显式允许带凭据 (SPEC-004)
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth.router)
app.include_router(ingest.router)
app.include_router(status.router)
app.include_router(incidents.router)
app.include_router(drills.router)
app.include_router(policies.router)
app.include_router(employees.router)
app.include_router(agent_tasks.router)  # W4: Agent 任务 + SSE (SPEC-002 第一节)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "sentinel-api", "version": app.version}
